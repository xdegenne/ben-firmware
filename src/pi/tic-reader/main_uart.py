"""
tic-reader — BEN Pi wired TIC reader

Lit la TIC Linky via /dev/ttyS0 (mini-UART, 1200 baud 8N1, masque 0x7F),
décode multi-tarif (BASE/HC/HP/EJP/BBR), écrit dans InfluxDB.

Aligné sur src/arduino/tic-reader/tic-reader.ino :
  - même mapping PTEC → index (selectActiveIndex)
  - même détection DEMAIN / ADPS / PEJP (buildFlags)
  - même schéma InfluxDB que lora-receiver/main.py
    (tags: pdl_index, active_index, demain, adps, pejp
     champs: <active_name>, IINST, PAPP)

Note mini-UART : ttyS0 ne supporte pas la parité. On lit en 8N1
et on masque le bit de parité (& 0x7F) — même technique que l'original.

pdl_index : 0  (source câblée — toujours index 0 dans sources.json)
"""

import json
import logging
import os
import socket
import sys
import time
import traceback
from threading import Thread
from time import sleep

import serial
from influxdb import InfluxDBClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
UART_DEV          = "/dev/ttyAMA0"
UART_BAUD         = 1200
TIC_TIMEOUT_S     = 12      # max pour lire une trame complète (~4s à 1200 baud)

PDL_INDEX         = 0       # source câblée — toujours index 0 par convention

INFLUX_HOST        = "127.0.0.1"
INFLUX_PORT        = 8086
INFLUX_DB          = "linky"
INFLUX_MEASUREMENT = "linkyEvents"

PERIOD_S           = 60     # intervalle entre deux cycles
WATCHDOG_THRESHOLD = 300    # secondes sans succès → relance

STATE_PATH         = "/var/lib/ben-firmware/tic-state.json"

# ---------------------------------------------------------------------------
# Protocole TIC (historique Linky)
# ---------------------------------------------------------------------------
STX = 0x02
ETX = 0x03
LF  = 0x0A
CR  = 0x0D

# Miroir de selectActiveIndex() dans tic-reader.ino
# (prefix, longueur_comparaison, index_id, index_name)
PTEC_MAP = [
    ("TH",   2, 0x00, "BASE"),
    ("HC..", 4, 0x01, "HCHC"),
    ("HP..", 4, 0x02, "HCHP"),
    ("HN",   2, 0x03, "EJPHN"),
    ("PM",   2, 0x04, "EJPHPM"),
    ("HCJB", 4, 0x05, "BBRHCJB"),
    ("HPJB", 4, 0x06, "BBRHPJB"),
    ("HCJW", 4, 0x07, "BBRHCJW"),
    ("HPJW", 4, 0x08, "BBRHPJW"),
    ("HCJR", 4, 0x09, "BBRHCJR"),
    ("HPJR", 4, 0x0A, "BBRHPJR"),
]

INDEX_LABELS = {
    "BASE", "HCHC", "HCHP", "EJPHN", "EJPHPM",
    "BBRHCJB", "BBRHPJB", "BBRHCJW", "BBRHPJW", "BBRHCJR", "BBRHPJR",
}

DEMAIN_NAMES = {"BLEU": "BLEU", "BLAN": "BLAN", "ROUG": "ROUG"}

# ---------------------------------------------------------------------------
# TIC — lecture trame
# ---------------------------------------------------------------------------
def tic_checksum_ok(line: str) -> bool:
    """Vérifie le checksum TIC. Format : 'LABEL VALEUR <checksum>'."""
    if len(line) < 3 or line[-2] != ' ':
        return False
    total = sum(ord(c) for c in line[:-2])
    return chr((total & 0x3F) + 0x20) == line[-1]


def _parse_label(line: str, out: dict) -> None:
    """Extrait label et valeur d'une ligne TIC validée."""
    parts = line.split(' ')
    if len(parts) < 3:
        return
    name  = parts[0]
    value = parts[1]

    if name in INDEX_LABELS:
        try:
            out[name] = int(value)
        except ValueError:
            pass
    elif name == "PTEC":
        out["PTEC"] = value
    elif name == "DEMAIN":
        out["DEMAIN"] = value
    elif name == "IINST":
        try:
            out["IINST"] = int(value)
        except ValueError:
            pass
    elif name == "PAPP":
        try:
            out["PAPP"] = int(value)
        except ValueError:
            pass
    elif name == "ADCO":
        out["ADCO"] = value.strip()
    elif name in ("ADPS", "PEJP"):
        out[name] = True


def read_frame(ser: serial.Serial) -> dict | None:
    """
    Lit une trame TIC complète (STX..ETX).
    Retourne un dict des labels parsés, ou None si timeout / trame vide.
    """
    deadline = time.time() + TIC_TIMEOUT_S

    # Synchronisation sur STX
    while time.time() < deadline:
        raw = ser.read(1)
        if raw and (raw[0] & 0x7F) == STX:
            break
    else:
        log.warning("TIC timeout en attente STX")
        return None

    labels: dict = {}
    current = bytearray()
    in_line = False
    kept = dropped = 0

    while time.time() < deadline:
        raw = ser.read(1)
        if not raw:
            continue
        b = raw[0] & 0x7F

        if b == ETX:
            log.debug(f"Trame TIC complète : {kept} lignes gardées, {dropped} rejetées")
            return labels if labels else None
        elif b == LF:
            current = bytearray()
            in_line = True
        elif b == CR:
            if in_line and current:
                line = current.decode("ascii", errors="replace")
                if tic_checksum_ok(line):
                    _parse_label(line, labels)
                    kept += 1
                else:
                    log.debug(f"Checksum invalide : <{line}>")
                    dropped += 1
            in_line = False
        elif in_line:
            current.append(b)

    log.warning("TIC timeout en lecture trame")
    return None

# ---------------------------------------------------------------------------
# TIC — décodage (miroir Arduino)
# ---------------------------------------------------------------------------
def select_active_index(ptec: str, labels: dict) -> tuple[str | None, int | None]:
    """Miroir de selectActiveIndex() tic-reader.ino — sélectionne l'index selon PTEC.

    Retourne (None, None) si PTEC inconnu.
    Retourne (name, None) si la ligne index est absente du dict (checksum KO ou timeout).
    """
    p = ptec.strip()
    for prefix, n, _id, name in PTEC_MAP:
        if p[:n] == prefix:
            return name, labels.get(name)  # None si etiquette non vue
    return None, None


def build_flags(labels: dict) -> tuple[str | None, bool, bool]:
    """Miroir de buildFlags() tic-reader.ino — retourne (demain, adps, pejp)."""
    demain = DEMAIN_NAMES.get(labels.get("DEMAIN", ""))
    adps   = bool(labels.get("ADPS", False))
    pejp   = bool(labels.get("PEJP", False))
    return demain, adps, pejp

# ---------------------------------------------------------------------------
# État persistant
# ---------------------------------------------------------------------------
def load_state() -> dict:
    try:
        with open(STATE_PATH) as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        raw = {}
    return {"adco": raw.get("adco", "")}


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f)


state = load_state()

# ---------------------------------------------------------------------------
# InfluxDB
# ---------------------------------------------------------------------------
influx_client = None


def test_influx_connection(host: str, port: int) -> None:
    while True:
        s = None
        try:
            log.info(f"Test InfluxDB {host}:{port}...")
            s = socket.socket()
            s.connect((host, port))
            log.info("InfluxDB OK")
            return
        except Exception as e:
            log.warning(f"InfluxDB indisponible ({e}), retry 10s...")
            time.sleep(10)
        finally:
            if s:
                try: s.close()
                except Exception: pass


def write_to_influx(active_name: str, active_value: int,
                    iinst: int, papp: int,
                    demain, adps: bool, pejp: bool) -> None:
    if influx_client is None:
        return
    fields = {
        active_name: int(active_value),
        "IINST":     int(iinst),
        "PAPP":      int(papp),
    }
    tags = {
        "pdl_index":    str(PDL_INDEX),
        "active_index": active_name,
        "demain":       demain if demain is not None else "n/a",
        "adps":         "1" if adps else "0",
        "pejp":         "1" if pejp else "0",
    }
    try:
        influx_client.write_points([{
            "measurement": INFLUX_MEASUREMENT,
            "tags":        tags,
            "fields":      fields,
        }])
    except Exception:
        log.error(f"Influx write failed:\n{traceback.format_exc()}")

# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------
last_success_time = time.time()


def watchdog_loop() -> None:
    while True:
        sleep(30)
        elapsed = time.time() - last_success_time
        if elapsed > WATCHDOG_THRESHOLD:
            log.critical(f"WATCHDOG : {int(elapsed)}s sans succès — relance")
            sleep(1)
            os.execv(sys.executable, [sys.executable] + sys.argv)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
test_influx_connection(INFLUX_HOST, INFLUX_PORT)
influx_client = InfluxDBClient(host=INFLUX_HOST, port=INFLUX_PORT)
influx_client.switch_database(INFLUX_DB)
log.info(f"InfluxDB connecté — db={INFLUX_DB}")

ser = serial.Serial(
    port=UART_DEV,
    baudrate=UART_BAUD,
    bytesize=serial.EIGHTBITS,
    parity=serial.PARITY_NONE,
    stopbits=serial.STOPBITS_ONE,
    timeout=1,
)
log.info(f"Série ouvert : {UART_DEV} {UART_BAUD} 8N1 (masque 0x7F) proto TIC historique")
log.info(f"PDL connu : {state.get('adco') or '(aucun)'}")

Thread(target=watchdog_loop, daemon=True, name="watchdog").start()
log.info(f"Watchdog démarré (seuil={WATCHDOG_THRESHOLD}s)")

try:
    while True:
        try:
            ser.reset_input_buffer()
            labels = read_frame(ser)

            if labels is None:
                log.error("Trame TIC invalide ou timeout")
                sleep(PERIOD_S)
                continue

            adco = labels.get("ADCO", "")
            if adco:
                prev_adco = state.get("adco", "")
                if adco != prev_adco:
                    log.info(f"NOUVEAU PDL détecté : ADCO={adco} (précédent={prev_adco or 'aucun'})")
                    state["adco"] = adco
                    save_state(state)

            ptec = labels.get("PTEC")
            if not ptec:
                log.error("PTEC absent de la trame TIC")
                sleep(PERIOD_S)
                continue

            active_name, active_value = select_active_index(ptec, labels)
            if active_name is None:
                log.warning(f"PTEC inconnu : '{ptec}' — trame ignorée")
                sleep(PERIOD_S)
                continue

            # Guard : index actif, IINST et PAPP doivent être présents dans la trame.
            # Si une ligne a un checksum KO elle est absente du dict → valeur None/manquante.
            if active_value is None:
                log.warning(f"{active_name} absent de la trame TIC (checksum KO?) — trame ignorée")
                sleep(PERIOD_S)
                continue
            iinst = labels.get("IINST")
            if iinst is None:
                log.warning("IINST absent de la trame TIC (checksum KO?) — trame ignorée")
                sleep(PERIOD_S)
                continue
            papp = labels.get("PAPP")
            if papp is None:
                log.warning("PAPP absent de la trame TIC (checksum KO?) — trame ignorée")
                sleep(PERIOD_S)
                continue

            demain, adps, pejp = build_flags(labels)

            log.info(f"OK pdl_index={PDL_INDEX} PTEC={ptec} {active_name}={active_value} "
                     f"IINST={iinst} PAPP={papp} demain={demain} adps={adps} pejp={pejp}")

            write_to_influx(active_name, active_value, iinst, papp, demain, adps, pejp)
            last_success_time = time.time()

        except Exception:
            log.error(f"Exception dans la boucle principale:\n{traceback.format_exc()}")

        sleep(PERIOD_S)

except KeyboardInterrupt:
    log.info("Arrêt.")
finally:
    try: ser.close()
    except Exception: pass
    try: influx_client.close()
    except Exception: pass
