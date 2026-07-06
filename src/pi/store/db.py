"""
db.py — Store local SQLite des mesures BEN (Phase 1).

Rôle triple :
  1. Store local court (rétention 3 mois glissants) des trames, à la cadence
     native (~30 s).
  2. Source de l'API locale lue par l'app Flutter (visu conso sur le LAN).
  3. Outbox store-and-forward : la colonne `sent` marque les points déjà
     poussés au cloud (futur ben-publisher).

Deux tables :
  measurements(ts, pdl_index, base, hchc, hchp, papp, iinst, sent)
    - données de consommation (tous modèles)
  lora_link(ts, pdl_index, rssi, snr, sent)
    - qualité de réception LoRa (modèles pi0-lora uniquement)

Conventions :
  - ts        : epoch secondes UTC. ⚠ Pi Zero W sans RTC → dépend du NTP ;
                les points écrits avant la sync NTP auront un ts faux (~1970).
  - pdl_index : index opaque de la source (0 = câblé ; LoRa via sources.json).
  - base/hchc/hchp : index d'énergie (Wh), nullables selon le tarif.
  - papp      : puissance apparente (VA) ; iinst : courant (A).
  - rssi      : dBm (int) ; snr : dB (float).
  - sent      : 0 = pas encore poussé au cloud, 1 = poussé.

Mode WAL : le reader écrit, l'API lit en concurrence sans verrou bloquant.
"""

import sqlite3
import time
from pathlib import Path

DB_PATH = "/var/lib/ben-firmware/measurements.db"
RETENTION_DAYS = 90  # 3 mois glissants

_SCHEMA = """
CREATE TABLE IF NOT EXISTS measurements (
    ts            INTEGER NOT NULL,
    pdl_index     INTEGER NOT NULL,
    base          INTEGER,
    hchc          INTEGER,
    hchp          INTEGER,
    papp          INTEGER,
    iinst         INTEGER,
    tariff        INTEGER,
    src_standard  INTEGER NOT NULL DEFAULT 0,
    index_id      INTEGER,
    index_value   INTEGER,
    inject_total  INTEGER,
    meter_ts      INTEGER,
    sent          INTEGER NOT NULL DEFAULT 0
);
-- Index COUVRANT (pdl_index, ts, papp) : la CTE d'agrégation de curve_buckets
-- (AVG/MIN/MAX(papp) par bucket sur une plage) se résout alors ENTIÈREMENT depuis
-- l'index, sans lookup table ligne par ligne — c'est le point chaud du /curve 7j
-- (~640k lignes en standard → ~13 s). Supersede idx_meas_pdl_ts (dont (pdl_index,
-- ts) est le préfixe), qu'on retire pour ne pas doubler le coût d'écriture.
CREATE INDEX IF NOT EXISTS idx_meas_pdl_ts_papp ON measurements(pdl_index, ts, papp);
DROP INDEX IF EXISTS idx_meas_pdl_ts;
CREATE INDEX IF NOT EXISTS idx_meas_pdl_papp ON measurements(pdl_index, papp);
CREATE INDEX IF NOT EXISTS idx_meas_sent     ON measurements(sent);

CREATE TABLE IF NOT EXISTS lora_link (
    ts          INTEGER NOT NULL,
    pdl_index   INTEGER NOT NULL,
    rssi        INTEGER,
    snr         REAL,
    sent        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_lora_pdl_ts ON lora_link(pdl_index, ts);
CREATE INDEX IF NOT EXISTS idx_lora_sent   ON lora_link(sent);

CREATE TABLE IF NOT EXISTS tariff_labels (
    pdl_index    INTEGER NOT NULL,
    src_standard INTEGER NOT NULL,
    index_id     INTEGER NOT NULL,
    ngtf         TEXT    NOT NULL DEFAULT '',   -- CONTRAT (calendrier fournisseur) sous lequel ce label vaut
    label        TEXT    NOT NULL,
    updated_ts   INTEGER NOT NULL,
    PRIMARY KEY (pdl_index, src_standard, index_id, ngtf)
);

CREATE TABLE IF NOT EXISTS level_profile (
    pdl_index        INTEGER PRIMARY KEY,
    computed_ts      INTEGER NOT NULL,
    p_low            INTEGER,
    p_mid            INTEGER,
    p_high           INTEGER,
    talon            INTEGER,
    papp_max_alltime INTEGER,
    isousc           INTEGER,
    pref             INTEGER,
    n_samples        INTEGER NOT NULL DEFAULT 0,
    span_sec         INTEGER NOT NULL DEFAULT 0
);

-- Résumé pré-agrégé de la courbe, UNE ligne par (tranche de temps, tarif). Le tarif
-- (index_id) est DANS la clé → une bascule HC↔HP coupe la tranche automatiquement (2 lignes).
-- Alimenté au fil de l'eau (§3) + backfill progressif (§3bis). Sert /curve large + bandes HP/HC
-- + index par tarif SANS rescanner measurements. Cf. docs/rollup-par-index.md.
CREATE TABLE IF NOT EXISTS curve_rollup (
    pdl_index    INTEGER NOT NULL,
    bucket_ts    INTEGER NOT NULL,   -- début aligné de la tranche (ts // ROLLUP_BUCKET_SEC)
    src_standard INTEGER NOT NULL,
    index_id     INTEGER NOT NULL,   -- LE TARIF, dans la clé
    ts_start     INTEGER NOT NULL,   -- ts RÉEL du 1er point (bornes exactes des bandes)
    ts_end       INTEGER NOT NULL,   -- ts RÉEL du dernier point
    papp_min     INTEGER,
    papp_max     INTEGER,            -- pics préservés (démarrages)
    papp_sum     INTEGER,            -- + count → moyenne à la lecture (incrémental)
    papp_count   INTEGER,
    index_last   INTEGER,            -- dernier index cumulé (Wh) → coût + index par tarif
    PRIMARY KEY (pdl_index, bucket_ts, src_standard, index_id)
);

-- État du backfill du rollup (singleton). `watermark` = borne basse couverte : le rollup est
-- complet pour [watermark, now] ; le backfill fait RECULER watermark (newest-first). Persistant
-- → reprise exacte après brownout. `done`=1 quand watermark a atteint la plus vieille mesure.
CREATE TABLE IF NOT EXISTS rollup_state (
    id         INTEGER PRIMARY KEY CHECK (id = 0),
    watermark  INTEGER,
    done       INTEGER NOT NULL DEFAULT 0
);
"""


def connect(path: str = DB_PATH, *, read_only: bool = False) -> sqlite3.Connection:
    """Ouvre la base. En écriture : crée le schéma + active WAL.
    En lecture seule (`read_only=True`, pour l'API) : ouvre en mode `ro`.

    `check_same_thread=False` : le récepteur LoRa ouvre la connexion au démarrage
    (thread principal) mais écrit depuis le thread RX de la radio (`on_recv`).
    SQLite interdit sinon le partage entre threads. Un seul thread écrivain par
    process → sûr. (Sans ce flag, le sink LoRa échouait silencieusement à chaque
    trame : « SQLite objects created in a thread can only be used in that same
    thread ».)"""
    if read_only:
        conn = sqlite3.connect(
            f"file:{path}?mode=ro", uri=True, timeout=5.0, check_same_thread=False)
    else:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=5.0, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        # Migration idempotente : colonne `tariff` (index tarifaire actif),
        # ajoutée en 0.0.26. CREATE IF NOT EXISTS ne l'ajoute pas à une table
        # existante → ALTER conditionnel.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(measurements)")]
        if "tariff" not in cols:
            conn.execute("ALTER TABLE measurements ADD COLUMN tariff INTEGER")
        # Chantier index bi-mode : stockage GÉNÉRIQUE des index d'énergie (histo+standard)
        # + horodate compteur. Migration idempotente ; double-écriture transitoire avec
        # base/hchc/hchp (cf. docs/chantier-index-energie-bimode.md).
        #   src_standard : 0=histo (index_id=rang PTEC 0..0x0A), 1=standard (index_id=NTARF 1..10)
        #   index_value  : Wh du registre actif (carry-forward) ; inject_total : EAIT (Wh, producteur)
        #   meter_ts     : horodate compteur (epoch UTC), NULL si indispo (provenance du temps)
        if "src_standard" not in cols:
            conn.execute("ALTER TABLE measurements ADD COLUMN src_standard INTEGER NOT NULL DEFAULT 0")
        if "index_id" not in cols:
            conn.execute("ALTER TABLE measurements ADD COLUMN index_id INTEGER")
        if "index_value" not in cols:
            conn.execute("ALTER TABLE measurements ADD COLUMN index_value INTEGER")
        if "inject_total" not in cols:
            conn.execute("ALTER TABLE measurements ADD COLUMN inject_total INTEGER")
        if "meter_ts" not in cols:
            conn.execute("ALTER TABLE measurements ADD COLUMN meter_ts INTEGER")
        # Migration 0.0.34 : modèle de niveau « course [talon, plafond] ».
        # talon = ancrage bas (percentile bas) ; papp_max_alltime = high-water
        # mark monotone (jamais décrémenté, survit au prune → vrai plafond foyer).
        lvl_cols = [r[1] for r in conn.execute("PRAGMA table_info(level_profile)")]
        if "talon" not in lvl_cols:
            conn.execute("ALTER TABLE level_profile ADD COLUMN talon INTEGER")
        if "papp_max_alltime" not in lvl_cols:
            conn.execute(
                "ALTER TABLE level_profile ADD COLUMN papp_max_alltime INTEGER")
        # Chantier ISOUSC : intensité souscrite (A) par PDL, sert à l'étalonnage
        # de la jauge (maxVa = isousc×230) + borne du plafond. Statique → écrite
        # sur changement uniquement (cf. record_isousc).
        if "isousc" not in lvl_cols:
            conn.execute("ALTER TABLE level_profile ADD COLUMN isousc INTEGER")
        # Chantier ISOUSC standard : PREF (puiss. de réf., kVA) par PDL → maxVa = pref×1000.
        # Le standard ne fournit pas ISOUSC (A) mais PREF (kVA) ; on stocke brut, /live arbitre.
        if "pref" not in lvl_cols:
            conn.execute("ALTER TABLE level_profile ADD COLUMN pref INTEGER")
        # Chantier labels+index : NGTF (nom du calendrier tarifaire fournisseur, standard)
        # par PDL, write-on-change → sert à keyer/segmenter les registres au changement de
        # fournisseur (cf. CHANTIERS.md « Unification labels + index »).
        if "ngtf" not in lvl_cols:
            conn.execute("ALTER TABLE level_profile ADD COLUMN ngtf TEXT")
        # Segmenter les labels par CONTRAT → `ngtf` dans la PK de tariff_labels (un changement
        # de fournisseur crée de nouvelles lignes sans écraser l'historique). SQLite ne peut pas
        # ajouter une colonne à une PK existante → drop + recreate (labels re-captés en direct
        # depuis LTARF, donnée non critique). No-op si déjà à la bonne def.
        tl_cols = [r[1] for r in conn.execute("PRAGMA table_info(tariff_labels)")]
        if tl_cols and "ngtf" not in tl_cols:
            conn.execute("DROP TABLE tariff_labels")
            conn.executescript(_SCHEMA)   # recrée tariff_labels (nouvelle PK) ; autres tables = no-op
        # Backfill index GÉNÉRIQUE de la donnée LEGACY histo (index_value NULL, d'avant
        # que le reader ne peuple la générique en pi-0.0.43) : dérivé de `tariff` +
        # base/hchc/hchp. Sans lui, /consumption fait COALESCE(index_value,base,hchc,hchp)
        # qui écrase HC et HP dans une seule colonne (hchc) → registres mal calculés
        # (sur-comptage ~+30 % mesuré sur legacy HC/HP, ben-0003). ONE-SHOT via
        # user_version : les écritures récentes peuplent déjà index_value → jamais
        # de re-run. UPDATE en masse une fois (~qq s sur grosse base, au 1er boot post-MAJ).
        if conn.execute("PRAGMA user_version").fetchone()[0] < 1:
            conn.execute(
                "UPDATE measurements SET index_id = tariff, "
                "index_value = CASE tariff WHEN 0 THEN base WHEN 1 THEN hchc "
                "                          WHEN 2 THEN hchp END "
                "WHERE src_standard=0 AND index_value IS NULL AND tariff IS NOT NULL")
            conn.execute("PRAGMA user_version = 1")
        conn.commit()
    conn.row_factory = sqlite3.Row
    return conn


# Index tarifaire actif (mêmes ids que l'Arduino LoRa : 0=BASE, 1=HC, 2=HP, …).
def tariff_from_ptec(ptec: str | None) -> int | None:
    """PTEC (TIC wired) → index tarifaire actif. None si inconnu/absent."""
    if not ptec:
        return None
    p = ptec.strip()
    if p.startswith("TH"):
        return 0  # BASE
    if p.startswith("HC"):
        return 1  # Heures Creuses
    if p.startswith("HP"):
        return 2  # Heures Pleines
    if p.startswith("HN"):
        return 3  # EJP Heures Normales
    if p.startswith("PM"):
        return 4  # EJP Pointe Mobile
    return None


# Libellés HISTORIQUE par rang PTEC (cf. tariff_from_ptec) — convention (l'histo ne porte
# pas de LTARF). Le STANDARD, lui, a le LTARF autoritatif capté dans tariff_labels.
HISTO_LABELS = {
    0: "Base",
    1: "Heures Creuses",
    2: "Heures Pleines",
    3: "EJP Heures Normales",
    4: "EJP Pointe Mobile",
}


def _tariff_from_labels(labels: dict) -> int | None:
    """Index tarifaire actif depuis les labels : PTEC (wired, tous les index
    présents) sinon l'unique étiquette d'index présente (LoRa = 1 index actif
    par trame)."""
    t = tariff_from_ptec(labels.get("PTEC"))
    if t is not None:
        return t
    for name, tid in (("BASE", 0), ("HCHC", 1), ("HCHP", 2)):
        if labels.get(name) is not None:
            return tid
    return None


def _generic_cols(labels: dict) -> tuple:
    """Colonnes GÉNÉRIQUES d'index (chantier bi-mode) depuis les clés réservées du dict
    de labels, posées par le reader wired ou le récepteur LoRa. Absentes → NULL/0.
      _src_standard (0/1) · _index_id · _index_value · _inject_total · _meter_ts
    Cohabite avec base/hchc/hchp (double-écriture transitoire)."""
    # Garde `index_value == 0 → NULL` : un index d'énergie cumulatif compteur n'est
    # JAMAIS 0. Des `index_value=0` parasites (carry-forward EASF empoisonné côté
    # émetteur — cf. chantier-index-energie-bimode / fix Arduino) polluaient la
    # donnée BRUTE (courbe, /measurements) au-delà de /consumption. On les normalise
    # en NULL à l'écriture → base propre pour tous les lecteurs (+ futur cloud).
    index_value = labels.get("_index_value")
    if index_value == 0:
        index_value = None
    return (
        int(labels.get("_src_standard", 0) or 0),
        labels.get("_index_id"),
        index_value,
        labels.get("_inject_total"),
        labels.get("_meter_ts"),
    )


ROLLUP_BUCKET_SEC = 120   # tranche de résumé = 2 min (compromis finesse bandes / volume rollup)


def _rollup_ingest(conn: sqlite3.Connection, pdl_index: int, points: list) -> None:
    """Agrège des points `(ts, src_standard, index_id, papp, index_value)` dans `curve_rollup`,
    groupés par (tranche `ROLLUP_BUCKET_SEC`, tarif `index_id`). UPSERT incrémental
    (min/max/sum/count/index_last) au fil de l'eau — cf. docs/rollup-par-index.md §3.
    Points sans `papp` OU sans `index_id` (pas de tarif) ignorés. Un paquet LoRa (~58 pts)
    tombe dans 1-2 tranches → 1-2 upserts (bien moins que 58 écritures)."""
    # a = [ts_min, ts_max, papp_min, papp_max, papp_sum, papp_count, index_last, index_last_ts]
    agg: dict = {}
    for ts, src_standard, index_id, papp, index_value in points:
        if papp is None or index_id is None:
            continue
        bucket = (ts // ROLLUP_BUCKET_SEC) * ROLLUP_BUCKET_SEC
        key = (bucket, int(src_standard or 0), index_id)
        a = agg.get(key)
        if a is None:
            agg[key] = [ts, ts, papp, papp, papp, 1, index_value, ts]
        else:
            if ts < a[0]: a[0] = ts
            if ts > a[1]: a[1] = ts
            if papp < a[2]: a[2] = papp
            if papp > a[3]: a[3] = papp
            a[4] += papp
            a[5] += 1
            if ts >= a[7] and index_value is not None:   # index_last = index du point le + récent
                a[6] = index_value
                a[7] = ts
    for (bucket, src, idx), a in agg.items():
        conn.execute(
            "INSERT INTO curve_rollup "
            "(pdl_index, bucket_ts, src_standard, index_id, ts_start, ts_end, "
            " papp_min, papp_max, papp_sum, papp_count, index_last) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(pdl_index, bucket_ts, src_standard, index_id) DO UPDATE SET "
            "  ts_start   = MIN(ts_start, excluded.ts_start), "
            "  ts_end     = MAX(ts_end,   excluded.ts_end), "
            "  papp_min   = MIN(papp_min, excluded.papp_min), "
            "  papp_max   = MAX(papp_max, excluded.papp_max), "
            "  papp_sum   = papp_sum   + excluded.papp_sum, "
            "  papp_count = papp_count + excluded.papp_count, "
            "  index_last = CASE WHEN excluded.ts_end >= ts_end AND excluded.index_last IS NOT NULL "
            "                    THEN excluded.index_last ELSE index_last END",
            (pdl_index, bucket, src, idx, a[0], a[1], a[2], a[3], a[4], a[5], a[6]),
        )


def record_measurement(
    conn: sqlite3.Connection,
    pdl_index: int,
    labels: dict,
    *,
    ts: int | None = None,
) -> None:
    """Insère une mesure de conso depuis le dict de labels TIC parsés
    (BASE/HCHC/HCHP/PAPP/IINST + clés génériques _index_*). Les labels absents → NULL.
    `tariff` = index tarifaire actif (PTEC wired / index LoRa)."""
    ts = int(ts if ts is not None else time.time())
    papp = labels.get("PAPP")
    gcols = _generic_cols(labels)   # (src_standard, index_id, index_value, inject_total, meter_ts)
    conn.execute(
        "INSERT INTO measurements "
        "(ts, pdl_index, base, hchc, hchp, papp, iinst, tariff, "
        " src_standard, index_id, index_value, inject_total, meter_ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ts,
            pdl_index,
            labels.get("BASE"),
            labels.get("HCHC"),
            labels.get("HCHP"),
            papp,
            labels.get("IINST"),
            _tariff_from_labels(labels),
            *gcols,
        ),
    )
    # High-water mark monotone de la PAPP (plafond du modèle de niveau). Mis à
    # jour ICI (au fil de l'eau) et JAMAIS via un SELECT MAX sur measurements,
    # qui sont prunées à 3 mois → on perdrait les vieux pics. SoC : le reader ne
    # touche QUE cette colonne ; le profiler (refresh) possède talon/percentiles.
    if papp is not None:
        conn.execute(
            "INSERT INTO level_profile (pdl_index, computed_ts, papp_max_alltime) "
            "VALUES (?, 0, ?) "
            "ON CONFLICT(pdl_index) DO UPDATE SET "
            "papp_max_alltime = MAX(COALESCE(papp_max_alltime, 0), excluded.papp_max_alltime)",
            (pdl_index, papp),
        )
    # Rollup incrémental (fil de l'eau) — cette mesure unique.
    _rollup_ingest(conn, pdl_index, [(ts, gcols[0], gcols[1], papp, gcols[2])])
    conn.commit()


def record_measurements_batch(
    conn: sqlite3.Connection,
    rows: list[tuple[int, dict, int]],
) -> int:
    """Insère un LOT de mesures en UNE seule transaction (executemany) + met à
    jour le high-water mark `papp_max_alltime` par PDL — un seul `commit()` pour
    tout le lot, au lieu d'un par mesure.

    `rows` = liste de `(pdl_index, labels, ts)`. Sert la lecture UART au fil de
    l'eau (cadence ~2 s) sans matraquer la SD d'un fsync par trame
    (cf. chantier-courbe-temps-reel.md, volet B : « granularité de perte = le
    batch »). Le LoRa garde `record_measurement` (cadence ~1/32 s, batch inutile).
    Renvoie le nombre de lignes insérées. Lot vide → no-op."""
    if not rows:
        return 0
    params = []
    maxima: dict[int, int] = {}  # pdl_index -> max PAPP du lot
    rollup_pts: dict = {}        # pdl_index -> [(ts, src, idx, papp, index_value)] pour le rollup
    for pdl_index, labels, ts in rows:
        papp = labels.get("PAPP")
        gcols = _generic_cols(labels)
        ts = int(ts)
        params.append((
            ts,
            pdl_index,
            labels.get("BASE"),
            labels.get("HCHC"),
            labels.get("HCHP"),
            papp,
            labels.get("IINST"),
            _tariff_from_labels(labels),
            *gcols,
        ))
        if papp is not None and papp > maxima.get(pdl_index, -1):
            maxima[pdl_index] = papp
        rollup_pts.setdefault(pdl_index, []).append((ts, gcols[0], gcols[1], papp, gcols[2]))
    conn.executemany(
        "INSERT INTO measurements "
        "(ts, pdl_index, base, hchc, hchp, papp, iinst, tariff, "
        " src_standard, index_id, index_value, inject_total, meter_ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        params,
    )
    # High-water mark par PDL : même logique monotone que record_measurement,
    # mais une fois par PDL pour tout le lot (le max du lot suffit).
    for pdl_index, papp in maxima.items():
        conn.execute(
            "INSERT INTO level_profile (pdl_index, computed_ts, papp_max_alltime) "
            "VALUES (?, 0, ?) "
            "ON CONFLICT(pdl_index) DO UPDATE SET "
            "papp_max_alltime = MAX(COALESCE(papp_max_alltime, 0), excluded.papp_max_alltime)",
            (pdl_index, papp),
        )
    # Rollup incrémental : chaque paquet résumé en 1-2 tranches (bien moins d'écritures que N).
    for pdl_index, pts in rollup_pts.items():
        _rollup_ingest(conn, pdl_index, pts)
    conn.commit()
    return len(params)


def record_lora_link(
    conn: sqlite3.Connection,
    pdl_index: int,
    rssi: int | None,
    snr: float | None,
    *,
    ts: int | None = None,
) -> None:
    """Insère un point de qualité de réception LoRa."""
    ts = int(ts if ts is not None else time.time())
    conn.execute(
        "INSERT INTO lora_link (ts, pdl_index, rssi, snr) VALUES (?, ?, ?, ?)",
        (ts, pdl_index, rssi, snr),
    )
    conn.commit()


def record_isousc(
    conn: sqlite3.Connection,
    pdl_index: int,
    isousc: int | None,
) -> bool:
    """Enregistre l'intensité souscrite (ISOUSC, en ampères) d'un PDL dans
    `level_profile`. Donnée STATIQUE (l'abonnement ne change quasiment jamais) →
    **écriture SUR CHANGEMENT uniquement** : si la valeur stockée est déjà
    identique, no-op (on ne matraque pas la SD à chaque trame). `isousc` None ou 0
    = absent → ignoré. Renvoie True si une écriture a eu lieu.

    Wired : appelé à chaque trame (ISOUSC présent partout) mais n'écrit qu'au
    1er passage / sur changement réel. LoRa : appelé à réception de la trame
    d'identité v0x01. La valeur PERSISTE → survit au redémarrage du Pi (résout la
    robustesse LoRa où ISOUSC n'arrive qu'au boot de l'Arduino)."""
    if not isousc:  # None ou 0 → absent
        return False
    row = conn.execute(
        "SELECT isousc FROM level_profile WHERE pdl_index=?", (pdl_index,)
    ).fetchone()
    if row is not None and row["isousc"] == isousc:
        return False  # inchangé → aucune écriture
    conn.execute(
        "INSERT INTO level_profile (pdl_index, computed_ts, isousc) "
        "VALUES (?, 0, ?) "
        "ON CONFLICT(pdl_index) DO UPDATE SET isousc = excluded.isousc",
        (pdl_index, isousc),
    )
    conn.commit()
    return True


def record_pref(
    conn: sqlite3.Connection,
    pdl_index: int,
    pref: int | None,
) -> bool:
    """Enregistre la puissance de référence (PREF, en kVA) d'un PDL dans
    `level_profile` — l'abonnement en mode STANDARD (le standard ne fournit pas
    ISOUSC mais PREF). Même logique que `record_isousc` : STATIQUE, écriture SUR
    CHANGEMENT uniquement, None/0 = absent → ignoré. Renvoie True si écrit.

    Wired : PREF dans chaque trame standard. LoRa : octet 14 de la trame d'identité
    v0x01. La conversion en jauge (maxVa = pref×1000) se fait à la lecture (/live)."""
    if not pref:  # None ou 0 → absent
        return False
    row = conn.execute(
        "SELECT pref FROM level_profile WHERE pdl_index=?", (pdl_index,)
    ).fetchone()
    if row is not None and row["pref"] == pref:
        return False  # inchangé → aucune écriture
    conn.execute(
        "INSERT INTO level_profile (pdl_index, computed_ts, pref) "
        "VALUES (?, 0, ?) "
        "ON CONFLICT(pdl_index) DO UPDATE SET pref = excluded.pref",
        (pdl_index, pref),
    )
    conn.commit()
    return True


def tic_mode(conn: sqlite3.Connection, pdl_index: int) -> str | None:
    """Mode TIC courant d'un PDL — 'standard' / 'historique' — depuis le `src_standard`
    de la dernière mesure, ou None si inconnu. Sert l'API (/live → affichage app).

    Défensif (comme get_isousc) : si la colonne `src_standard` n'existe pas encore
    (API read-only interrogée juste après l'upgrade, avant que le reader ait exécuté la
    migration ALTER) → None au lieu de planter le /live."""
    try:
        row = conn.execute(
            "SELECT src_standard FROM measurements WHERE pdl_index=? "
            "ORDER BY ts DESC LIMIT 1", (pdl_index,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None or row["src_standard"] is None:
        return None
    return "standard" if row["src_standard"] else "historique"


def get_isousc(conn: sqlite3.Connection, pdl_index: int) -> int | None:
    """Intensité souscrite (A) d'un PDL depuis level_profile, ou None si absente.
    Sert l'API (/live → maxVa=isousc×230 + réglages app).

    Défensif : juste après l'upgrade, l'API (read-only) peut interroger avant que
    le 1er connect en écriture ait exécuté la migration ALTER → la colonne
    `isousc` n'existe pas encore. On traite ce cas comme « absente » (None) au
    lieu de planter le /live."""
    try:
        row = conn.execute(
            "SELECT isousc FROM level_profile WHERE pdl_index=?", (pdl_index,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return row["isousc"] if row and row["isousc"] else None


def get_pref(conn: sqlite3.Connection, pdl_index: int) -> int | None:
    """Puissance de référence (PREF, kVA) d'un PDL depuis level_profile, ou None.
    Sert l'API (/live → maxVa=pref×1000 en mode standard). Même garde défensive
    que get_isousc (colonne absente juste après upgrade → None, pas de crash)."""
    try:
        row = conn.execute(
            "SELECT pref FROM level_profile WHERE pdl_index=?", (pdl_index,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return row["pref"] if row and row["pref"] else None


# Échelle de bucket QUANTIFIÉE : on snappe `bucket_sec` sur des paliers fixes pour
# que la résolution soit stable d'un fetch à l'autre. Couplé au bucketing ABSOLU
# (`CAST(ts/bucket_sec)`), une même zone donne TOUJOURS les mêmes buckets → en panant,
# le re-fetch ne fait pas « sauter » la courbe (les zones recouvrantes sont identiques).
_BUCKET_LADDER = (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200,
                  10800, 21600, 43200, 86400)


def _snap_bucket_sec(sec: int) -> int:
    """Arrondit `sec` au palier ≥ le plus proche (résolution stable entre fetches)."""
    sec = max(1, int(sec))
    for b in _BUCKET_LADDER:
        if b >= sec:
            return b
    return 86400 * ((sec + 86399) // 86400)  # au-delà d'un jour : multiples de 86400


def producer(conn: sqlite3.Connection, pdl_index: int,
             latest_inject_total: int | None = None) -> bool:
    """True si le PDL a une injection CONSTATÉE — pilote la jauge bidir
    soutirage/injection de l'app (Lot C). Deux signaux :
      - EAIT cumulé > 0 : `inject_total` est un index monotone (croissant), donc
        la DERNIÈRE valeur suffit (== MAX) → passée par `latest_inject_total`, zéro
        requête ;
      - un `papp` NET négatif déjà vu : en standard `papp` est signé (<0 = surplus
        injecté). `MIN(papp)` est instantané via l'index (pdl_index, papp).
    Volontairement PAS « EAIT présent » : un foyer anti-injection (cas A) déclare un
    EAIT figé à 0 → jauge bidir inutile (cf. chantier-index-energie-bimode.md §inj)."""
    if latest_inject_total and latest_inject_total > 0:
        return True
    row = conn.execute(
        "SELECT MIN(papp) AS mp FROM measurements WHERE pdl_index=?", (pdl_index,)
    ).fetchone()
    return row is not None and row["mp"] is not None and row["mp"] < 0


def curve_buckets(
    conn: sqlite3.Connection,
    pdl_index: int,
    since: int,
    until: int,
    bucket_sec: int,
    *,
    with_minmax: bool = True,
) -> list[dict]:
    """Courbe de PAPP **agrégée par bucket temporel** de `bucket_sec` secondes sur
    `[since, until]`. Sert l'app (`/curve`, `/measurements` degrade-safe) : on
    stocke fin, on sert grossier (cf. chantier-courbe-temps-reel.md, volet C).

    Chaque point : `{ts, papp_avg, n[, papp_min, papp_max]}`, ordonné par ts.
    L'agrégation **préserve les pics** via min/max — JAMAIS de décimation naïve
    (« 1 point sur N ») qui tuerait les transitoires (signatures NILM). `tariff`
    = celui du dernier échantillon du bucket (pour les marqueurs de zones tarifaires
    de l'app). `with_minmax=False` → moyenne seule.

    Le bucketing par `CAST((ts - since) / bucket_sec AS INT)` s'appuie sur l'index
    `(pdl_index, ts)` → pas de transfert des lignes fines hors SQL."""
    bucket_sec = _snap_bucket_sec(bucket_sec)
    # CTE `agg` : bucket sur GRILLE ABSOLUE (`ts / bucket_sec`, pas `(ts - since)`) →
    # frontières fixes, stables au pan. tariff/index = ceux de l'échantillon au ts_max
    # du bucket (via JOIN sur valeur concrète ; SQLite refuse MAX() dans une corrélée).
    rows = conn.execute(
        "WITH agg AS ("
        "  SELECT CAST(ts / ? AS INT) AS bucket, "
        # ts du point = CENTROÏDE temporel du bucket (AVG), pas le bord gauche
        # (MIN) : sinon un bucket large ancre sa moyenne à gauche → la courbe
        # grossière paraît décalée, et passe à droite quand le fin arrive (« saut
        # dans le temps »). Le centroïde aligne grossier et fin.
        "         CAST(AVG(ts) AS INT) AS ts, MAX(ts) AS ts_max, "
        "         AVG(papp) AS papp_avg, MIN(papp) AS papp_min, "
        "         MAX(papp) AS papp_max, COUNT(*) AS n "
        "  FROM measurements "
        "  WHERE pdl_index=? AND ts>=? AND ts<=? AND papp IS NOT NULL "
        "  GROUP BY bucket"
        ") "
        # tariff ET index d'énergie (base/hchc/hchp) = ceux du dernier échantillon
        # du bucket (à ts_max). L'index est INDISPENSABLE : l'app calcule la conso/
        # le coût d'une période par DIFFÉRENCE d'index (cf. _indexAt) → sans lui,
        # pas de coût sur les vues agrégées. GROUP BY a.bucket dédoublonne un ts_max
        # éventuellement partagé.
        "SELECT a.ts AS ts, a.papp_avg, a.papp_min, a.papp_max, a.n, "
        "       l.tariff AS tariff, l.base AS base, l.hchc AS hchc, l.hchp AS hchp, "
        "       l.index_id AS index_id, l.index_value AS index_value, "
        "       l.src_standard AS src_standard, l.inject_total AS inject_total "
        "FROM agg a "
        "LEFT JOIN measurements l ON l.pdl_index=? AND l.ts=a.ts_max "
        "GROUP BY a.bucket ORDER BY a.bucket",
        (bucket_sec, pdl_index, since, until, pdl_index),
    ).fetchall()
    out = []
    for r in rows:
        pt = {"ts": r["ts"], "papp": int(round(r["papp_avg"])),
              "tariff": r["tariff"], "base": r["base"], "hchc": r["hchc"],
              "hchp": r["hchp"], "n": r["n"],
              # Index GÉNÉRIQUE bi-mode (échantillon à ts_max, comme base/hchc/hchp).
              # index_id = QUEL registre (histo: rang PTEC 0..0x0A ; standard: NTARF
              # 1..10), index_value = sa valeur Wh. En STANDARD base/hchc/hchp sont
              # NULL → seul (index_id, index_value) porte la conso. Le couple permet
              # à l'app de reconstruire chaque registre (HC vs HP…) par carry-forward
              # et de sommer (cf. chantier-index-energie-bimode.md §6, Lot C).
              "index_id": r["index_id"], "index_value": r["index_value"],
              "src_standard": r["src_standard"], "inject_total": r["inject_total"]}
        if with_minmax:
            pt["papp_min"] = r["papp_min"]
            pt["papp_max"] = r["papp_max"]
        out.append(pt)
    return out


def rollup_watermark(conn: sqlite3.Connection) -> int | None:
    """Borne basse couverte par le rollup : complet pour [watermark, now]. None = pas encore
    initialisé (le reader retombe alors sur le brut). Cf. docs/rollup-par-index.md §3bis/§8."""
    row = conn.execute("SELECT watermark FROM rollup_state WHERE id = 0").fetchone()
    return row[0] if row else None


def curve_from_rollup(conn: sqlite3.Connection, pdl_index: int, since: int, until: int,
                      bucket_sec: int, *, with_minmax: bool = True) -> list[dict]:
    """Courbe agrégée DEPUIS LE ROLLUP (résumé 2 min) au lieu du brut — MÊME forme de retour que
    curve_buckets (l'app ne voit pas la différence). Ré-agrège les tranches de 2 min en buckets
    d'affichage de `bucket_sec`. `tariff` = index_id en histo (mêmes ids), None en standard (label
    via resolve_label). `index_value` = index_last de la dernière tranche du bucket. Ne SERT QUE
    quand la fenêtre est couverte (since ≥ watermark) ET la tranche demandée ≥ 2 min (arbitré par
    l'appelant). Cf. docs/rollup-par-index.md §2/§6."""
    bucket_sec = _snap_bucket_sec(bucket_sec)
    # Le rollup est PETIT (~720 lignes/jour ⇒ ~15 k sur la rétention 3 mois) → on lit les tranches
    # de la plage et on ré-agrège EN PYTHON. Volontairement PAS de self-JOIN SQL (un JOIN sur
    # ts_end sans index scannait la table → plus lent que le brut, l'inverse du but ; mesuré sur
    # ben-0003). Ordonné par ts_start (temps RÉEL) → départage correct des 2 lignes d'une tranche
    # à bascule tarif (même bucket_ts). Cf. docs/rollup-par-index.md §2.
    rows = conn.execute(
        "SELECT bucket_ts, src_standard, index_id, ts_start, ts_end, "
        "       papp_min, papp_max, papp_sum, papp_count, index_last "
        "FROM curve_rollup WHERE pdl_index=? AND bucket_ts>=? AND bucket_ts<? "
        "ORDER BY ts_start",
        (pdl_index, since, until),
    ).fetchall()
    # db_bucket -> agrégat. `last` = (ts_end, src, index_id, index_last) de la tranche la + récente.
    agg: dict = {}
    for r in rows:
        b = (r["bucket_ts"] // bucket_sec) * bucket_sec
        a = agg.get(b)
        if a is None:
            agg[b] = {"ts_sum": r["bucket_ts"], "nb": 1,
                      "psum": r["papp_sum"] or 0, "pcount": r["papp_count"] or 0,
                      "pmin": r["papp_min"], "pmax": r["papp_max"],
                      "last": (r["ts_end"], r["src_standard"], r["index_id"], r["index_last"])}
        else:
            a["ts_sum"] += r["bucket_ts"]; a["nb"] += 1
            a["psum"] += r["papp_sum"] or 0; a["pcount"] += r["papp_count"] or 0
            if r["papp_min"] is not None and (a["pmin"] is None or r["papp_min"] < a["pmin"]):
                a["pmin"] = r["papp_min"]
            if r["papp_max"] is not None and (a["pmax"] is None or r["papp_max"] > a["pmax"]):
                a["pmax"] = r["papp_max"]
            if r["ts_end"] >= a["last"][0]:      # tranche la + récente → porte le tarif/index
                a["last"] = (r["ts_end"], r["src_standard"], r["index_id"], r["index_last"])
    out = []
    for b in sorted(agg):
        a = agg[b]
        _, std, index_id, index_last = a["last"]
        pt = {"ts": a["ts_sum"] // a["nb"],
              "papp": int(round(a["psum"] / a["pcount"])) if a["pcount"] else 0,
              "tariff": (index_id if not std else None),   # histo: index_id == tariff
              "base": None, "hchc": None, "hchp": None,     # rollup = générique (comme standard)
              "n": a["pcount"], "index_id": index_id, "index_value": index_last,
              "src_standard": std, "inject_total": None}
        if with_minmax:
            pt["papp_min"] = a["pmin"]
            pt["papp_max"] = a["pmax"]
        out.append(pt)
    return out


def _band_kind(label: str | None) -> str:
    """Classe un libellé tarifaire en hc / hp / base (pour la couleur des bandes)."""
    if not label:
        return "base"
    l = label.lower()
    if "creus" in l:
        return "hc"
    if "plein" in l:
        return "hp"
    return "base"


def tariff_bands(conn: sqlite3.Connection, pdl_index: int, since: int, until: int) -> list[dict]:
    """Bandes tarifaires `[{from, to, kind}]` DEPUIS LE ROLLUP (les zones HP/HC de la courbe) —
    JAMAIS un parcours de points (§5). Le rollup est déjà découpé par tarif : les tranches
    CONSÉCUTIVES de même (src_standard, index_id) fusionnent en une bande, bornes `ts_start`/
    `ts_end` EXACTES. `kind` classé 1 fois par bande via resolve_label (creuse→hc / pleine→hp /
    base). Quelques lignes pour toute la fenêtre. Ne couvre que la zone rollup-couverte (pendant
    le backfill, la partie ancienne n'a pas encore de bande — comblé quand le backfill remonte)."""
    rows = conn.execute(
        "SELECT ts_start, ts_end, src_standard, index_id FROM curve_rollup "
        "WHERE pdl_index=? AND ts_end>=? AND ts_start<=? ORDER BY ts_start",
        (pdl_index, since, until)).fetchall()
    # Chaque bande est AUTO-DESCRIPTIVE : (src_standard, index_id, label, kind). L'app colore par
    # `kind` (grossier : hc/hp/base) OU par `label`/`index_id` pour distinguer TOUS les tarifs
    # (Tempo bleu/blanc/rouge, EJP pointe…) — aucun tarif écrasé, en HISTO comme en STANDARD.
    # `resolve_label` est mode-agnostique (histo=convention HISTO_LABELS / standard=LTARF capté).
    meta_cache: dict = {}   # (src, index_id) -> (label, kind) — résolu 1 fois par registre (§5)
    def meta_of(std, idx):
        key = (std, idx)
        if key not in meta_cache:
            lbl = resolve_label(conn, pdl_index, std, idx)
            meta_cache[key] = (lbl, _band_kind(lbl))
        return meta_cache[key]
    # Fusionne les tranches CONSÉCUTIVES de même REGISTRE (src_standard, index_id) — PAS juste même
    # kind : deux registres distincts de même couleur (ex. 2 HP Tempo) restent 2 bandes. Ordonné
    # par ts_start → pas de chevauchement.
    runs = []   # [(src, idx), from, to]
    for r in rows:
        reg = (r["src_standard"], r["index_id"])
        if runs and runs[-1][0] == reg:
            runs[-1][2] = max(runs[-1][2], r["ts_end"])
        else:
            runs.append([reg, r["ts_start"], r["ts_end"]])
    # Absorbe les MICRO-bandes de transition (≤ 1 tranche) : à une bascule tarif le compteur peut
    # osciller 1-2 min → tranches parasites. On les fond dans la bande précédente pour une
    # coloration propre (~2 bandes/jour, pas un stroboscope).
    merged = []
    for run in runs:
        dur = run[2] - run[1]
        if merged and (dur <= ROLLUP_BUCKET_SEC or merged[-1][0] == run[0]):
            merged[-1][2] = max(merged[-1][2], run[2])
        else:
            merged.append(run)
    out = []
    for (std, idx), t0, t1 in merged:
        label, kind = meta_of(std, idx)
        out.append({"from": max(t0, since), "to": min(t1, until),
                    "src_standard": std, "index_id": idx, "label": label, "kind": kind})
    return out


def consumption(
    conn: sqlite3.Connection, pdl_index: int, since: int, until: int,
) -> dict:
    """Consommation PAR REGISTRE (Wh) sur `[since, until]`, calculée server-side.

    **Contrat commun Pi-local / cloud** (l'app est agnostique du backend, cf.
    §12.2 souveraineté : les données seront aussi déversées dans le cloud qui
    servira le MÊME `/consumption`). Le carry-forward vit ICI, pas dans l'app —
    une seule implémentation, réutilisée par app + cloud + Home Assistant.

    Par registre : `MAX(index) − MIN(index)`. L'index d'un registre est **monotone**
    → exact et **immunisé au saut de registre** (pas besoin de détecter les
    bascules). `reg` = `index_id` générique (v0x05) sinon `tariff` (legacy histo) ;
    la valeur = `index_value` sinon `base/hchc/hchp` (registre actif legacy) — via
    `COALESCE`, donc bi-mode ET rétro-compatible avec la donnée pré-v0x05.

    Retourne `{by_register: [{src_standard, index_id, wh}], total_wh}`. L'app
    applique le prix : `Σ wh_r × prix_r` — prix moyen unique aujourd'hui ; **par
    registre à terme** (coût à l'euro près, rétroactif) sans changer ce contrat.
    Cf. `docs/chantier-index-energie-bimode.md` §6-7."""
    # `> 0` (et pas `IS NOT NULL`) : un index d'énergie cumulatif compteur n'est
    # JAMAIS 0 (le Linky tourne depuis des années). Des `index_value=0` parasites
    # (trame LoRa dont l'EASF a sauté / keyframe avant 1re lecture → carry-forward
    # non encore amorcé) polluaient sinon le MIN → `MAX-MIN` = l'index ABSOLU
    # (~15 MWh → coût délirant). Les exclure rend le calcul robuste.
    # PERF : si la fenêtre est ENTIÈREMENT couverte par le rollup (since ≥ watermark), on calcule
    # MAX-MIN(index_last) par registre sur curve_rollup (~qq k lignes) au lieu de rescanner
    # measurements (millions). MÊME résultat (index monotone → MAX-MIN identique ; imprécision de
    # bord ≤ 1 tranche de 2 min, négligeable vs coût). Sinon (zone non backfillée) → brut, exact.
    wm = rollup_watermark(conn)
    if wm is not None and since >= wm:
        rows = conn.execute(
            "SELECT src_standard, index_id AS reg, "
            "       MAX(index_last) - MIN(index_last) AS wh "
            "FROM curve_rollup "
            "WHERE pdl_index=? AND bucket_ts>=? AND bucket_ts<=? AND index_last > 0 "
            "GROUP BY src_standard, index_id",
            (pdl_index, since, until),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT src_standard, COALESCE(index_id, tariff) AS reg, "
            "       MAX(COALESCE(index_value, base, hchc, hchp)) - "
            "       MIN(COALESCE(index_value, base, hchc, hchp)) AS wh "
            "FROM measurements "
            "WHERE pdl_index=? AND ts>=? AND ts<=? "
            "      AND COALESCE(index_value, base, hchc, hchp) > 0 "
            "GROUP BY src_standard, reg",
            (pdl_index, since, until),
        ).fetchall()
    by_register = [
        {"src_standard": r["src_standard"], "index_id": r["reg"], "wh": r["wh"]}
        for r in rows if r["wh"] is not None
    ]
    total = sum(x["wh"] for x in by_register)
    return {"by_register": by_register, "total_wh": total}


def record_tariff_label(conn: sqlite3.Connection, pdl_index: int,
                        src_standard: int, index_id: int, label: str,
                        ngtf: str = "") -> bool:
    """Cache le libellé tarifaire d'un registre (LTARF standard capté par le reader), SCOPÉ au
    CONTRAT (`ngtf`) → un changement de fournisseur crée de nouvelles lignes sans écraser
    l'historique. **Write-on-change** : n'écrit que si absent ou différent. Retourne True si
    écrit. Alimente la résolution de label serveur (`/registers`, `/live.tariff_label`)."""
    label = (label or "").strip()
    if not label:
        return False
    ngtf = (ngtf or "").strip()
    row = conn.execute(
        "SELECT label FROM tariff_labels "
        "WHERE pdl_index=? AND src_standard=? AND index_id=? AND ngtf=?",
        (pdl_index, src_standard, index_id, ngtf)).fetchone()
    if row is not None and row[0] == label:
        return False
    conn.execute(
        "INSERT INTO tariff_labels(pdl_index, src_standard, index_id, ngtf, label, updated_ts) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(pdl_index, src_standard, index_id, ngtf) "
        "DO UPDATE SET label=excluded.label, updated_ts=excluded.updated_ts",
        (pdl_index, src_standard, index_id, ngtf, label, int(time.time())))
    conn.commit()
    return True


def record_ngtf(conn: sqlite3.Connection, pdl_index: int, ngtf: str) -> bool:
    """NGTF (nom du calendrier tarifaire fournisseur) par PDL — **write-on-change**.
    Un changement de `ngtf` = changement de fournisseur/offre → époque tarifaire."""
    ngtf = (ngtf or "").strip()
    if not ngtf:
        return False
    row = conn.execute(
        "SELECT ngtf FROM level_profile WHERE pdl_index=?", (pdl_index,)).fetchone()
    if row is not None and row[0] == ngtf:
        return False
    conn.execute(
        "INSERT INTO level_profile(pdl_index, computed_ts, ngtf) VALUES(?,0,?) "
        "ON CONFLICT(pdl_index) DO UPDATE SET ngtf=excluded.ngtf",
        (pdl_index, ngtf))
    conn.commit()
    return True


def resolve_label(conn: sqlite3.Connection, pdl_index: int,
                  src_standard: int, index_id) -> str | None:
    """Libellé tarifaire d'un registre — résolution **UNIFIÉE côté serveur** (cœur du
    chantier labels). Standard : LTARF autoritatif capté (`tariff_labels`) ; historique :
    convention `HISTO_LABELS` (rang PTEC). None si inconnu → l'app peut retomber sur sa
    propre convention (rétro-compat). Mode-agnostique pour l'appelant."""
    if index_id is None:
        return None
    if src_standard:
        # Label du registre SOUS LE CONTRAT COURANT (level_profile.ngtf).
        ngtf = get_ngtf(conn, pdl_index) or ""
        row = conn.execute(
            "SELECT label FROM tariff_labels "
            "WHERE pdl_index=? AND src_standard=1 AND index_id=? AND ngtf=?",
            (pdl_index, index_id, ngtf)).fetchone()
        if row and row[0]:
            return row[0]
        # Repli : label le plus récent pour ce registre, tous contrats (ex. NGTF pas encore
        # capté au moment où le LTARF est arrivé) → évite un None inutile.
        row = conn.execute(
            "SELECT label FROM tariff_labels WHERE pdl_index=? AND src_standard=1 AND index_id=? "
            "ORDER BY updated_ts DESC LIMIT 1", (pdl_index, index_id)).fetchone()
        return row[0] if row and row[0] else None
    return HISTO_LABELS.get(index_id)


def get_ngtf(conn: sqlite3.Connection, pdl_index: int) -> str | None:
    """NGTF = le CONTRAT (nom du calendrier tarifaire fournisseur). None si inconnu.
    À ne pas confondre avec LTARF (tarif EN COURS, dans tariff_labels)."""
    row = conn.execute(
        "SELECT ngtf FROM level_profile WHERE pdl_index=?", (pdl_index,)).fetchone()
    return row[0] if row and row[0] else None


def registers(conn: sqlite3.Connection, pdl_index: int) -> list:
    """Registres tarifaires vus pour un PDL : libellé résolu + dernier index (monotone → MAX).
    Sert la carte réglages de l'app (un registre par tarif : Base / HC / HP…)."""
    # PERF : le rollup (~qq k lignes) porte le dernier index par registre (index_last) → MAX par
    # (src_standard, index_id) = le plus récent, en ms au lieu d'un GROUP BY sur measurements
    # (millions de lignes → ~28 s mesuré sur ben-0003). Fallback brut si le rollup est vide (pas
    # encore initialisé). Registres actifs (HC/HP/BASE, vus quotidiennement) toujours dans le
    # rollup [watermark, now] ; MÊME résultat que le brut (index monotone → MAX identique).
    rows = conn.execute(
        "SELECT src_standard, index_id, MAX(index_last) AS index_value, MAX(ts_end) AS last_ts "
        "FROM curve_rollup WHERE pdl_index=? AND index_last IS NOT NULL "
        "GROUP BY src_standard, index_id ORDER BY src_standard, index_id",
        (pdl_index,)).fetchall()
    if not rows:
        rows = conn.execute(
            "SELECT src_standard, index_id, MAX(index_value) AS index_value, MAX(ts) AS last_ts "
            "FROM measurements WHERE pdl_index=? AND index_id IS NOT NULL AND index_value IS NOT NULL "
            "GROUP BY src_standard, index_id ORDER BY src_standard, index_id",
            (pdl_index,)).fetchall()
    return [{
        "src_standard": r["src_standard"],
        "index_id": r["index_id"],
        "label": resolve_label(conn, pdl_index, r["src_standard"], r["index_id"]),
        "index_value": r["index_value"],
        "last_ts": r["last_ts"],
    } for r in rows]


ROLLUP_BACKFILL_BATCH_SEC = 86400   # 1 jour par pas de backfill (le GROUP BY d'UN jour est tractable)


def rollup_backfill_step(conn: sqlite3.Connection) -> dict:
    """UN pas de backfill du rollup, de MAINTENANT vers le passé (newest-first) : recompute une
    tranche de 1 jour depuis le brut via INSERT OR REPLACE → IDEMPOTENT + reprenable (curseur
    `watermark` persistant, survit au brownout). Disjoint de l'incrémental (qui n'écrit que le
    présent). Greffé sur `prune()`. Cf. docs/rollup-par-index.md §3bis.
    Retourne `{advanced, watermark, done}`."""
    oldest = conn.execute("SELECT MIN(ts) FROM measurements").fetchone()[0]
    if oldest is None:
        return {"advanced": False, "watermark": None, "done": True}
    oldest_bucket = (oldest // ROLLUP_BUCKET_SEC) * ROLLUP_BUCKET_SEC
    row = conn.execute("SELECT watermark, done FROM rollup_state WHERE id = 0").fetchone()
    if row is not None and row[1]:
        return {"advanced": False, "watermark": row[0], "done": True}
    watermark = row[0] if row is not None else None
    if watermark is None:
        # Init de la borne basse couverte. Le backfill remplit STRICTEMENT en dessous.
        #  - rollup NON vide (incrémental ≈ déploiement en cours) → plus vieux bucket déjà couvert.
        #  - rollup VIDE (aucun incrémental encore) → rien n'est couvert : borne = juste au-dessus
        #    du bucket le PLUS RÉCENT (newest+1 tranche) → le backfill remonte depuis MAINTENANT
        #    (newest-first). NE PAS init à oldest_bucket : la garde `<= oldest` conclurait done sans
        #    rien backfiller.
        r = conn.execute("SELECT MIN(bucket_ts) FROM curve_rollup").fetchone()
        if r and r[0] is not None:
            watermark = r[0]
        else:
            newest = conn.execute("SELECT MAX(ts) FROM measurements").fetchone()[0]
            watermark = ((newest // ROLLUP_BUCKET_SEC) * ROLLUP_BUCKET_SEC) + ROLLUP_BUCKET_SEC
    if watermark <= oldest_bucket:
        conn.execute(
            "INSERT INTO rollup_state (id, watermark, done) VALUES (0, ?, 1) "
            "ON CONFLICT(id) DO UPDATE SET watermark = excluded.watermark, done = 1",
            (oldest_bucket,))
        conn.commit()
        return {"advanced": False, "watermark": oldest_bucket, "done": True}
    start = max(oldest_bucket, watermark - ROLLUP_BACKFILL_BATCH_SEC)
    # Recompute [start, watermark) depuis le brut. index_last = MAX(index_value) (index cumulé
    # monotone → le max = le plus récent). INSERT OR REPLACE → ré-exécuter un batch = même résultat.
    conn.execute(
        "INSERT OR REPLACE INTO curve_rollup "
        "(pdl_index, bucket_ts, src_standard, index_id, ts_start, ts_end, "
        " papp_min, papp_max, papp_sum, papp_count, index_last) "
        "SELECT pdl_index, CAST(ts / ? AS INT) * ?, COALESCE(src_standard, 0), index_id, "
        "       MIN(ts), MAX(ts), MIN(papp), MAX(papp), SUM(papp), COUNT(papp), MAX(index_value) "
        "FROM measurements "
        "WHERE ts >= ? AND ts < ? AND papp IS NOT NULL AND index_id IS NOT NULL "
        "GROUP BY pdl_index, CAST(ts / ? AS INT) * ?, COALESCE(src_standard, 0), index_id",
        (ROLLUP_BUCKET_SEC, ROLLUP_BUCKET_SEC, start, watermark,
         ROLLUP_BUCKET_SEC, ROLLUP_BUCKET_SEC),
    )
    conn.execute(
        "INSERT INTO rollup_state (id, watermark, done) VALUES (0, ?, 0) "
        "ON CONFLICT(id) DO UPDATE SET watermark = excluded.watermark",
        (start,))
    conn.commit()
    return {"advanced": True, "watermark": start, "done": False}


def prune(conn: sqlite3.Connection, retention_days: int = RETENTION_DAYS) -> dict:
    """Supprime les points plus vieux que la rétention dans les deux tables.
    Retourne le nombre de lignes supprimées par table."""
    cutoff = int(time.time()) - retention_days * 86400
    m = conn.execute("DELETE FROM measurements WHERE ts < ?", (cutoff,)).rowcount
    l = conn.execute("DELETE FROM lora_link WHERE ts < ?", (cutoff,)).rowcount
    r = conn.execute("DELETE FROM curve_rollup WHERE bucket_ts < ?", (cutoff,)).rowcount
    conn.commit()
    # Checkpoint WAL TRUNCATE (maintenance ~horaire via _maybe_prune) : le fichier
    # `-wal` ne se tronque JAMAIS seul (grossit à son high-water mark → observé
    # 392 Mo sur ben-0001, ce qui ralentit toutes les lectures). On rapatrie les
    # frames dans la base et on remet le fichier à 0. Sur la connexion WRITER
    # (prune) → aucune contention. Aucune donnée modifiée (pure maintenance).
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.OperationalError:
        pass  # un lecteur tient un vieux snapshot → retenté au prochain prune
    # Backfill progressif du rollup : quelques pas par maintenance, BORNÉ EN TEMPS (~2 s) → pas de
    # monopolisation du mono-cœur (réception LoRa + API restent prioritaires). Newest-first,
    # reprenable. Isolé en try : une erreur de backfill ne casse jamais la maintenance. Cf. §3bis.
    try:
        budget = time.time() + 2.0
        while time.time() < budget and rollup_backfill_step(conn)["advanced"]:
            pass
    except Exception:
        pass
    return {"measurements": m, "lora_link": l, "curve_rollup": r}
