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
    ts          INTEGER NOT NULL,
    pdl_index   INTEGER NOT NULL,
    base        INTEGER,
    hchc        INTEGER,
    hchp        INTEGER,
    papp        INTEGER,
    iinst       INTEGER,
    tariff      INTEGER,
    sent        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_meas_pdl_ts ON measurements(pdl_index, ts);
CREATE INDEX IF NOT EXISTS idx_meas_sent   ON measurements(sent);

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
    pdl_index   INTEGER PRIMARY KEY,
    computed_ts INTEGER NOT NULL,
    p_low       INTEGER,
    p_mid       INTEGER,
    p_high      INTEGER,
    n_samples   INTEGER NOT NULL DEFAULT 0,
    span_sec    INTEGER NOT NULL DEFAULT 0
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


def record_measurement(
    conn: sqlite3.Connection,
    pdl_index: int,
    labels: dict,
    *,
    ts: int | None = None,
) -> None:
    """Insère une mesure de conso depuis le dict de labels TIC parsés
    (BASE/HCHC/HCHP/PAPP/IINST). Les labels absents → NULL. `tariff` = index
    tarifaire actif (PTEC wired / index LoRa)."""
    ts = int(ts if ts is not None else time.time())
    conn.execute(
        "INSERT INTO measurements "
        "(ts, pdl_index, base, hchc, hchp, papp, iinst, tariff) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ts,
            pdl_index,
            labels.get("BASE"),
            labels.get("HCHC"),
            labels.get("HCHP"),
            labels.get("PAPP"),
            labels.get("IINST"),
            _tariff_from_labels(labels),
        ),
    )
    conn.commit()


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


def prune(conn: sqlite3.Connection, retention_days: int = RETENTION_DAYS) -> dict:
    """Supprime les points plus vieux que la rétention dans les deux tables.
    Retourne le nombre de lignes supprimées par table."""
    cutoff = int(time.time()) - retention_days * 86400
    m = conn.execute("DELETE FROM measurements WHERE ts < ?", (cutoff,)).rowcount
    l = conn.execute("DELETE FROM lora_link WHERE ts < ?", (cutoff,)).rowcount
    conn.commit()
    return {"measurements": m, "lora_link": l}
