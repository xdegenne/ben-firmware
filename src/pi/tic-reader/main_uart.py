"""
tic-reader — BEN Pi wired TIC reader (v1: log-only)

Lit la TIC Linky via /dev/ttyS0 (mini-UART, 1200 baud 8N1, masque 0x7F),
décode multi-tarif (BASE/HC/HP/EJP/BBR), **logue** les trames parsées.

v1 : aucun sink (pas d'InfluxDB, pas de publisher cloud).
v2 (OTA upgrade) : InfluxDB local + Grafana.

Aligné sur src/arduino/tic-reader/tic-reader.ino :
  - même mapping PTEC → index (selectActiveIndex)
  - même détection DEMAIN / ADPS / PEJP (buildFlags)

Note mini-UART : ttyS0 ne supporte pas la parité. On lit en 8N1
et on masque le bit de parité (& 0x7F) — même technique que l'original.

pdl_index : 0  (source câblée — toujours index 0 dans sources.json)
"""

import json
import logging
import os
import signal
import sys
import time
import traceback
from threading import Thread
from time import sleep

import RPi.GPIO as GPIO
import serial

# Module store partagé (src/pi/store/db.py)
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "store"))
import db  # noqa: E402
import settings  # noqa: E402

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

# Lecture au fil de l'eau (volet A) : plus de PERIOD_S — on suit la cadence des
# trames du compteur (~1,7 s historique, ~1 s standard).
WATCHDOG_THRESHOLD = 600    # 10 min sans trame valide → relance process

# Écritures BDD batchées (volet B) : un commit par LOT, pas par trame.
BATCH_MAX_AGE_S    = 15     # flush au plus tard toutes les 15 s (= cadence d'hier)
BATCH_MAX_SIZE     = 60     # plafond de sécurité (anti-emballement RAM)
HEARTBEAT_S        = 20     # cadence max du flash LED vert « vivant » (≠ par trame)

STATE_PATH         = "/var/lib/ben-firmware/tic-state.json"

# ---------------------------------------------------------------------------
# LED RGB (cathode commune sur PCB rev01)
# R=GPIO12 (HW PWM0), G=GPIO13 (HW PWM1, boot indicator via gpio=13=op,dh),
# B=GPIO16. Piloté en PWM software (~500 Hz) pour pouvoir varier l'intensité.
# ---------------------------------------------------------------------------
RGB_R = 12
RGB_G = 13
RGB_B = 16

_pwm_r = _pwm_g = _pwm_b = None

def setup_led() -> None:
    """Init pins LED + PWM (~500 Hz) + éteint le boot indicator vert."""
    global _pwm_r, _pwm_g, _pwm_b
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    for pin in (RGB_R, RGB_G, RGB_B):
        GPIO.setup(pin, GPIO.OUT)
        GPIO.output(pin, GPIO.LOW)
    _pwm_r = GPIO.PWM(RGB_R, 500); _pwm_r.start(0)
    _pwm_g = GPIO.PWM(RGB_G, 500); _pwm_g.start(0)
    _pwm_b = GPIO.PWM(RGB_B, 500); _pwm_b.start(0)

def blink_rgb(r: int, g: int, b: int, duration: float = 0.05,
              bypass: bool = False) -> None:
    """Pulse RGB en PWM. r/g/b = duty cycle 0..100.

    La luminosité réglée par l'utilisateur (led_level) est appliquée ; l'appelant
    passe bypass=True pour les états critiques (erreur) → visibles même LED
    baissée/éteinte."""
    try:
        f = settings.led_factor(bypass)
        _pwm_r.ChangeDutyCycle(max(0, min(100, round(r * f))))
        _pwm_g.ChangeDutyCycle(max(0, min(100, round(g * f))))
        _pwm_b.ChangeDutyCycle(max(0, min(100, round(b * f))))
        sleep(duration)
        _pwm_r.ChangeDutyCycle(0)
        _pwm_g.ChangeDutyCycle(0)
        _pwm_b.ChangeDutyCycle(0)
    except Exception:
        pass

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
setup_led()
log.info("LED RGB initialisée (boot indicator vert éteint)")

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

# Store SQLite local (conso + outbox cloud). Non bloquant : si la base est
# indisponible, le reader continue (LED/logs), juste sans stockage.
try:
    measurements_db = db.connect()
    log.info(f"Store SQLite ouvert : {db.DB_PATH}")
except Exception as e:
    measurements_db = None
    log.error(f"Store SQLite indisponible ({e}) — on continue sans stockage")
last_prune = time.time()

# --- Batch d'écriture (volet B) ---------------------------------------------
# La lecture au fil de l'eau densifie les trames (~7×). Pour ne pas faire un fsync
# par trame, on accumule et on flush en UN commit (executemany) toutes les
# BATCH_MAX_AGE_S (ou BATCH_MAX_SIZE). Granularité de perte = le batch (crash → au
# pire les ~15 dernières s ; courbe append-only, non critique à la seconde).
batch: list = []          # (pdl_index, labels, ts)
last_flush = time.time()
last_heartbeat = 0.0


def flush_batch() -> None:
    global batch, last_flush
    last_flush = time.time()
    if not batch or measurements_db is None:
        batch = []
        return
    try:
        n = db.record_measurements_batch(measurements_db, batch)
        log.debug(f"store: batch flush {n} mesures")
    except Exception as e:
        log.warning(f"store: flush batch échoué ({len(batch)} pts perdus): {e}")
    finally:
        batch = []


def _on_sigterm(signum, frame):
    # systemd stop → flush le batch courant avant de mourir (pas de perte évitable).
    log.info("SIGTERM — flush batch puis arrêt")
    flush_batch()
    raise SystemExit(0)


signal.signal(signal.SIGTERM, _on_sigterm)

try:
    while True:
        frame_ok = False
        try:
            # Au fil de l'eau : read_frame se cale sur la cadence du compteur.
            # PAS de reset_input_buffer (on lit le flux en continu) ni de sleep
            # (la trame suivante nous attend déjà dans le port).
            labels = read_frame(ser)

            if labels is None:
                log.warning("Trame TIC invalide ou timeout")
            else:
                adco = labels.get("ADCO", "")
                if adco:
                    prev_adco = state.get("adco", "")
                    if adco != prev_adco:
                        log.info(f"NOUVEAU PDL détecté : ADCO={adco} (précédent={prev_adco or 'aucun'})")
                        state["adco"] = adco
                        save_state(state)

                ptec   = labels.get("PTEC")
                iinst  = labels.get("IINST")
                papp   = labels.get("PAPP")
                active_name, active_value = (None, None)
                if ptec:
                    active_name, active_value = select_active_index(ptec, labels)

                if not ptec:
                    log.error("PTEC absent de la trame TIC")
                elif active_name is None:
                    log.warning(f"PTEC inconnu : '{ptec}' — trame ignorée")
                elif active_value is None:
                    log.warning(f"{active_name} absent de la trame TIC (checksum KO?) — trame ignorée")
                elif iinst is None:
                    log.warning("IINST absent de la trame TIC (checksum KO?) — trame ignorée")
                elif papp is None:
                    log.warning("PAPP absent de la trame TIC (checksum KO?) — trame ignorée")
                else:
                    demain, adps, pejp = build_flags(labels)
                    log.debug(f"OK pdl_index={PDL_INDEX} PTEC={ptec} {active_name}={active_value} "
                              f"IINST={iinst} PAPP={papp} demain={demain} adps={adps} pejp={pejp}")
                    # On EMPILE dans le batch ; l'écriture BDD se fait par lot
                    # (flush plus bas, volet B). Log en DEBUG : à ~1,7 s/trame, un
                    # INFO par trame noierait journald (cf. preshipping).
                    if measurements_db is not None:
                        batch.append((PDL_INDEX, labels, int(time.time())))
                    frame_ok = True

        except Exception:
            log.error(f"Exception dans la boucle principale:\n{traceback.format_exc()}")

        now = time.time()
        if frame_ok:
            last_success_time = now
            # Heartbeat vert DISCRET, throttlé (≠ un flash par trame ~1,7 s) — la
            # LED en bonne santé reste calme (cf. note d'origine).
            if now - last_heartbeat >= HEARTBEAT_S:
                blink_rgb(0, 5, 0, 0.1)
                last_heartbeat = now
        else:
            blink_rgb(5, 0, 0, 0.1, bypass=True)  # rouge immédiat (erreur, visible)

        # Flush du batch si âge ou taille atteinte (volet B).
        if batch and (len(batch) >= BATCH_MAX_SIZE
                      or now - last_flush >= BATCH_MAX_AGE_S):
            flush_batch()

        if measurements_db is not None and now - last_prune > 3600:
            try:
                deleted = db.prune(measurements_db)
                log.info(f"store purge (>{db.RETENTION_DAYS}j): {deleted}")
            except Exception as e:
                log.warning(f"store: purge échouée: {e}")
            last_prune = time.time()

except (KeyboardInterrupt, SystemExit):
    log.info("Arrêt.")
finally:
    flush_batch()   # ne pas perdre le batch courant à l'arrêt
    try: ser.close()
    except Exception: pass
    try: GPIO.cleanup()
    except Exception: pass
