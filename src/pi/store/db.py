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
    return (
        int(labels.get("_src_standard", 0) or 0),
        labels.get("_index_id"),
        labels.get("_index_value"),
        labels.get("_inject_total"),
        labels.get("_meter_ts"),
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
            *_generic_cols(labels),
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
    for pdl_index, labels, ts in rows:
        papp = labels.get("PAPP")
        params.append((
            int(ts),
            pdl_index,
            labels.get("BASE"),
            labels.get("HCHC"),
            labels.get("HCHP"),
            papp,
            labels.get("IINST"),
            _tariff_from_labels(labels),
            *_generic_cols(labels),
        ))
        if papp is not None and papp > maxima.get(pdl_index, -1):
            maxima[pdl_index] = papp
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


def prune(conn: sqlite3.Connection, retention_days: int = RETENTION_DAYS) -> dict:
    """Supprime les points plus vieux que la rétention dans les deux tables.
    Retourne le nombre de lignes supprimées par table."""
    cutoff = int(time.time()) - retention_days * 86400
    m = conn.execute("DELETE FROM measurements WHERE ts < ?", (cutoff,)).rowcount
    l = conn.execute("DELETE FROM lora_link WHERE ts < ?", (cutoff,)).rowcount
    conn.commit()
    return {"measurements": m, "lora_link": l}
