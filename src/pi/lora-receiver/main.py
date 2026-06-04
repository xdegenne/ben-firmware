"""
lora-receiver — BEN Pi LoRa receiver agent (v1: log-only)

Réceptionne les trames binaires v0x02 émises par les Arduino tic-reader,
vérifie le HMAC, route par pdl_index selon sources.json, **logue** les trames.

v1 : aucun sink (pas d'InfluxDB, pas de publisher cloud).
v2 (OTA upgrade) : InfluxDB local + Grafana.

Protocole binaire v0x02 — 20 octets :
  0       version       uint8  = 0x02
  1       flags         bit0-1 DEMAIN, bit2 ADPS, bit3 PEJP
  2-3     boot_seq      uint16 LE
  4       index_id      uint8  BASE=0x00 HCHC=0x01 ... BBRHPJR=0x0A
  5-8     index_value   uint32 LE — Wh
  9       IINST         uint8  — A
  10-11   PAPP          uint16 LE — VA
  12-19   HMAC          HMAC-SHA256(key, octets 0..11) tronqué 8

Clé HMAC : /etc/ben-firmware/hmac.key (32 octets hex)
Sources  : /etc/ben-firmware/sources.json — mapping lora_address → pdl_index
État     : /var/lib/ben-firmware/lora-state.json
           {"adco": str, "indexes": {name: wh}, "last_active_id": int, "last_boot_seq": int}
           Reset automatique (indexes + boot_seq) à la réception d'un nouveau PDL (trame v0x01).
"""

import hashlib
import hmac as hmaclib
import json
import logging
import os
import struct
import sys
import time
import traceback
from threading import Thread
from time import sleep

import RPi.GPIO as GPIO
from raspi_lora import LoRa, ModemConfig

# Module store partagé (src/pi/store/db.py)
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "store"))
import db  # noqa: E402
import settings  # noqa: E402

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
HMAC_KEY_PATH  = "/etc/ben-firmware/hmac.key"
SOURCES_PATH   = "/etc/ben-firmware/sources.json"
STATE_PATH     = "/var/lib/ben-firmware/lora-state.json"

# ---------------------------------------------------------------------------
# LoRa
# ---------------------------------------------------------------------------
RF95_FREQ          = 868.0
RF95_POW           = 5
SERVER_ADDRESS     = 32
LORA_INTERRUPT_PIN = 22
RFM95_RST_PIN      = 17

# ---------------------------------------------------------------------------
# Protocole
# ---------------------------------------------------------------------------
PROTOCOL_VERSION = 0x02
PAYLOAD_LEN      = 20
HMAC_OFFSET      = 12
HMAC_LEN         = 8

INDEX_NAMES = {
    0x00: "BASE",
    0x01: "HCHC",    0x02: "HCHP",
    0x03: "EJPHN",   0x04: "EJPHPM",
    0x05: "BBRHCJB", 0x06: "BBRHPJB",
    0x07: "BBRHCJW", 0x08: "BBRHPJW",
    0x09: "BBRHCJR", 0x0A: "BBRHPJR",
}
INDEX_UNKNOWN = 0xFF

DEMAIN_NAMES = {0: "BLEU", 1: "BLAN", 2: "ROUG", 3: None}
FLAG_ADPS = 0x04
FLAG_PEJP = 0x08

BOOT_SEQ_MOD        = 65536
DUPLICATE_MAX_GAP_S = 10
WATCHDOG_THRESHOLD  = 180

RSSI_MIN_PLAUSIBLE = -130
RSSI_MAX_PLAUSIBLE = 0
SNR_MIN_PLAUSIBLE  = -25
SNR_MAX_PLAUSIBLE  = 20   # bumpé depuis 12 : SNR > 12 dB est valide en close-range (proxy émetteur)

# ---------------------------------------------------------------------------
# LED RGB (GPIO BCM) — cathode commune
# R=GPIO12 (HW PWM0, pin 32), G=GPIO13 (HW PWM1, pin 33), B=GPIO16 (SW PWM, pin 36)
# ---------------------------------------------------------------------------
RGB_R = 12
RGB_G = 13
RGB_B = 16

RECEPTION_TIMEOUT_S  = 60   # intervalle heartbeat (passé de 15s à 60s pour moins de scintillement)
TIC_HEALTH_TIMEOUT_S = 90  # seuil sans trame TIC pour le 2e flash du heartbeat

GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)

_pwm_r = _pwm_g = _pwm_b = None

# last_frame_time est initialisé depuis le state file plus bas (après load_state).
# Cette pré-déclaration sert juste à exister comme variable module-level
# (pour le `global` dans on_recv) — la vraie valeur vient de state.
last_frame_time = 0

def setup_led(*args):
    global _pwm_r, _pwm_g, _pwm_b
    for pin in (RGB_R, RGB_G, RGB_B):
        GPIO.setup(pin, GPIO.OUT)
        GPIO.output(pin, GPIO.LOW)
    _pwm_r = GPIO.PWM(RGB_R, 500)
    _pwm_g = GPIO.PWM(RGB_G, 500)
    _pwm_b = GPIO.PWM(RGB_B, 500)
    _pwm_r.start(0); _pwm_g.start(0); _pwm_b.start(0)
    import time as _t
    colors = [
        (28, 0,  0),   # rouge
        (28, 14, 0),   # orange
        (28, 28, 0),   # jaune
        (0,  28, 0),   # vert
        (0,  28, 28),  # cyan
        (0,  0,  28),  # bleu
        (20, 0,  28),  # violet
        (28, 0,  10),  # rose
        (28, 28, 28),  # blanc
    ]
    for r, g, b in colors:
        _pwm_r.ChangeDutyCycle(r)
        _pwm_g.ChangeDutyCycle(g)
        _pwm_b.ChangeDutyCycle(b)
        _t.sleep(0.5)
        _pwm_r.ChangeDutyCycle(0)
        _pwm_g.ChangeDutyCycle(0)
        _pwm_b.ChangeDutyCycle(0)
        _t.sleep(0.06)
    _pwm_r.ChangeDutyCycle(0)
    _pwm_g.ChangeDutyCycle(0)
    _pwm_b.ChangeDutyCycle(0)

def blink_rgb(r, g, b, duration=0.05, bypass=False):
    # La luminosité réglée (led_level) est appliquée ; l'appelant passe
    # bypass=True pour les états critiques (erreur/alerte), visibles même LED
    # baissée/éteinte.
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
# HMAC key
# ---------------------------------------------------------------------------
try:
    with open(HMAC_KEY_PATH) as f:
        HMAC_KEY = bytes.fromhex(f.read().strip())
except FileNotFoundError:
    log.error(f"Clé HMAC introuvable : {HMAC_KEY_PATH}")
    raise
if len(HMAC_KEY) != 32:
    raise ValueError(f"Clé HMAC invalide : {len(HMAC_KEY)} octets, 32 attendus")

# ---------------------------------------------------------------------------
# sources.json — mapping lora_address (str "0xNN") → pdl_index (int)
# ---------------------------------------------------------------------------
def load_sources(path: str) -> dict:
    """Retourne {lora_address_str: pdl_index} pour les sources de type lora."""
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        log.warning(f"sources.json introuvable : {path} — routing par défaut désactivé")
        return {}
    mapping = {}
    for src in data.get("sources", []):
        if src.get("type") == "lora" and "lora_address" in src:
            mapping[src["lora_address"].lower()] = int(src["index"])
    return mapping

sources_map = load_sources(SOURCES_PATH)
log.info(f"Sources LoRa : {sources_map}")

def get_pdl_index(lora_address_int: int) -> int | None:
    """Résout le pdl_index depuis l'adresse RadioHead source (int)."""
    key = f"0x{lora_address_int:02x}"
    return sources_map.get(key)

# ---------------------------------------------------------------------------
# État persistant
# ---------------------------------------------------------------------------
def load_state() -> dict:
    try:
        with open(STATE_PATH) as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        raw = {}
    indexes = raw.get("indexes", {})
    # Migration état lora_v5/v6 (last_base)
    if not indexes and "last_base" in raw:
        last_base = raw.get("last_base", 0)
        if last_base > 0:
            indexes["BASE"] = int(last_base)
            log.info(f"Migration état legacy : BASE={last_base}")
    return {
        "indexes":         indexes,
        "last_active_id":  raw.get("last_active_id"),
        "last_boot_seq":   raw.get("last_boot_seq"),
        "adco":            raw.get("adco", ""),
        "last_frame_time": raw.get("last_frame_time", 0),
    }

def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f)

state = load_state()
# Restauré depuis le state file → survit aux watchdog-restarts (process execv).
# 0 = aucune trame jamais reçue → heartbeat YELLOW honnête + watchdog désactivé.
last_frame_time = state.get("last_frame_time", 0)

# Store SQLite local (conso + qualité LoRa + outbox cloud). Non bloquant.
try:
    measurements_db = db.connect()
    log.info(f"Store SQLite ouvert : {db.DB_PATH}")
except Exception as e:
    measurements_db = None
    log.error(f"Store SQLite indisponible ({e}) — on continue sans stockage")
last_prune = time.time()

# ---------------------------------------------------------------------------
# Détection boot_seq
# ---------------------------------------------------------------------------
def detect_boot_seq_event(current: int, time_since_prev: float):
    last = state.get("last_boot_seq")
    if last is None:
        log.info(f"Première trame, boot_seq={current}")
        return ("first_seen", {"seq": current})
    if current == last + 1:
        return (None, None)
    is_fresh_cycle = time_since_prev >= DUPLICATE_MAX_GAP_S
    if current == last:
        if not is_fresh_cycle:
            log.warning(f"boot_seq dupliqué (retransmission, gap={time_since_prev:.1f}s): {current}")
            return ("duplicate", {"seq": current})
        log.warning(f"REBOOT émetteur (seq stagne à {current}, gap={time_since_prev:.1f}s)")
        return ("emitter_reboot", {"prev_seq": last, "new_seq": current})
    if current < last:
        log.warning(f"REBOOT émetteur : boot_seq {last} → {current}")
        return ("emitter_reboot", {"prev_seq": last, "new_seq": current})
    gap = current - last - 1
    log.warning(f"{gap} trame(s) perdue(s) (boot_seq {last} → {current})")
    return ("frame_loss", {"missing": gap, "prev_seq": last, "new_seq": current})

# ---------------------------------------------------------------------------
# Réception
# ---------------------------------------------------------------------------
def on_recv(payload) -> None:
    global last_frame_time, last_prune
    try:
        now = time.time()
        time_since_prev = now - last_frame_time
        last_frame_time = now
        blink_rgb(5, 5, 0, 0.05)  # jaune court & faible — trame reçue (RF only, avant validation)
        sleep(0.4)                # délai pour distinguer du flash suivant

        raw = bytes(payload.message)
        rssi = payload.rssi
        snr  = payload.snr

        if not (RSSI_MIN_PLAUSIBLE <= rssi <= RSSI_MAX_PLAUSIBLE
                and SNR_MIN_PLAUSIBLE <= snr <= SNR_MAX_PLAUSIBLE):
            log.info(f"RSSI/SNR aberrants ({rssi}/{snr}) — lib raspi_lora non patchée ?")

        sender_addr = payload.header_from
        log.info(f"From=0x{sender_addr:02x} RSSI={rssi} SNR={snr} len={len(raw)}")

        # Résolution pdl_index
        pdl_index = get_pdl_index(sender_addr)
        if pdl_index is None:
            log.warning(f"Adresse émetteur inconnue 0x{sender_addr:02x} — ignorée (non dans sources.json)")
            blink_rgb(30, 15, 0, 0.3, bypass=True)  # orange — source inconnue
            return

        if len(raw) != PAYLOAD_LEN:
            log.error(f"Longueur incorrecte : {len(raw)} octets, {PAYLOAD_LEN} attendus")
            blink_rgb(30, 0, 0, 0.3, bypass=True)  # rouge — erreur proto
            return

        version = raw[0]

        if version == 0x01:
            adco = raw[1:13].decode("ascii", errors="replace").rstrip("\x00")
            prev_adco = state.get("adco", "")
            if adco != prev_adco:
                log.info(f"BOOT FRAME addr=0x{sender_addr:02x} pdl_index={pdl_index} ADCO={adco} — NOUVEAU PDL, reset state")
                state["indexes"] = {}
                state["last_boot_seq"] = None
                state["last_active_id"] = None
                state["adco"] = adco
                save_state(state)
            else:
                log.info(f"BOOT FRAME addr=0x{sender_addr:02x} pdl_index={pdl_index} ADCO={adco} (PDL connu)")
            blink_rgb(30, 30, 30, 2.0)   # blanc long = discovery
            return

        if version != PROTOCOL_VERSION:
            log.error(f"Version inconnue : 0x{version:02x}")
            blink_rgb(30, 0, 0, 0.3, bypass=True)  # rouge — erreur proto
            return

        signed       = raw[:HMAC_OFFSET]
        mac_received = raw[HMAC_OFFSET:HMAC_OFFSET + HMAC_LEN]
        mac_expected = hmaclib.new(HMAC_KEY, signed, hashlib.sha256).digest()[:HMAC_LEN]
        if not hmaclib.compare_digest(mac_received, mac_expected):
            log.error(f"HMAC invalide — reçu={mac_received.hex()} attendu={mac_expected.hex()}")
            blink_rgb(30, 0, 0, 0.3, bypass=True)  # rouge — HMAC
            return

        (_ver, flags, boot_seq, index_id,
         index_value, iinst, papp) = struct.unpack("<BBHBIBH", signed)

        active_name = INDEX_NAMES.get(index_id)
        if active_name is None:
            if index_id == INDEX_UNKNOWN:
                log.warning("index_id=0xFF (PTEC inconnu côté Arduino), trame ignorée")
            else:
                log.error(f"index_id inconnu : 0x{index_id:02x}")
            blink_rgb(30, 0, 0, 0.3, bypass=True)
            return

        demain = DEMAIN_NAMES[flags & 0x03]
        adps   = bool(flags & FLAG_ADPS)
        pejp   = bool(flags & FLAG_PEJP)

        log.info(f"OK pdl_index={pdl_index} seq={boot_seq} idx={active_name} "
                 f"val={index_value} IINST={iinst} PAPP={papp} "
                 f"demain={demain} adps={adps} pejp={pejp}")

        # Store local : conso (1 index actif par trame LoRa) + qualité radio.
        if measurements_db is not None:
            try:
                db.record_measurement(
                    measurements_db, pdl_index,
                    {active_name: index_value, "IINST": iinst, "PAPP": papp},
                )
                db.record_lora_link(measurements_db, pdl_index, rssi, snr)
            except Exception as e:
                log.warning(f"store: écriture échouée: {e}")
            if time.time() - last_prune > 3600:
                try:
                    deleted = db.prune(measurements_db)
                    log.info(f"store purge (>{db.RETENTION_DAYS}j): {deleted}")
                except Exception as e:
                    log.warning(f"store: purge échouée: {e}")
                last_prune = time.time()

        event, details = detect_boot_seq_event(boot_seq, time_since_prev)
        if event:
            log.info(f"EVENT {event} {details}")

        prev_value = state["indexes"].get(active_name, 0)
        if index_value < prev_value:
            log.warning(f"{active_name} en décroissance : {index_value} < {prev_value}")
            blink_rgb(30, 15, 0, 0.3, bypass=True)  # orange — décroissance
        else:
            blink_rgb(0, 5, 0, 0.05)   # vert court & faible — données valides (HMAC OK)

        if index_value >= prev_value:
            state["indexes"][active_name] = int(index_value)
        state["last_active_id"]  = int(index_id)
        state["last_boot_seq"]   = int(boot_seq)
        state["last_frame_time"] = now
        save_state(state)

    except Exception:
        log.error(f"Exception dans on_recv :\n{traceback.format_exc()}")
        blink_rgb(30, 0, 0, 0.5, bypass=True)  # rouge — exception

# ---------------------------------------------------------------------------
# Watchdog + Heartbeat
# ---------------------------------------------------------------------------
def watchdog_loop() -> None:
    while True:
        sleep(30)
        if not lora_ok or last_frame_time == 0:
            continue
        elapsed = time.time() - last_frame_time
        if elapsed > WATCHDOG_THRESHOLD:
            log.critical(f"WATCHDOG : {int(elapsed)}s sans trame — relance")
            sleep(1)
            os.execv(sys.executable, [sys.executable] + sys.argv)

def is_wifi_up() -> bool:
    try:
        with open("/sys/class/net/wlan0/operstate") as f:
            return f.read().strip() == "up"
    except Exception:
        return False

def heartbeat_loop() -> None:
    """Toutes les RECEPTION_TIMEOUT_S : flash réseau (vert=WiFi OK, violet=down) puis flash LoRa (vert=récent <90s, orange=timeout).
    UX :
    - États OK (vert) = flashs très courts (50 ms × 5/255) — quasi-imperceptibles, "I'm alive" en background.
    - États erreur (violet/orange) = flashs plus longs (300 ms × 8/255) — l'œil les remarque immédiatement."""
    sleep(1)  # laisse le fondu se terminer
    while True:
        if is_wifi_up():
            blink_rgb(0, 5, 0, 0.05)   # vert court — WiFi up (RAS)
        else:
            blink_rgb(5, 0, 8, 0.3, bypass=True)  # violet long — WiFi down (alerte)
        sleep(0.5)
        elapsed = time.time() - last_frame_time
        if elapsed <= TIC_HEALTH_TIMEOUT_S:
            blink_rgb(0, 5, 0, 0.05)   # vert court — trame récente (RAS)
        else:
            blink_rgb(8, 2, 0, 0.3, bypass=True)  # orange long — timeout LoRa (alerte)
        sleep(RECEPTION_TIMEOUT_S)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
setup_led()

GPIO.setup(RFM95_RST_PIN, GPIO.OUT)
GPIO.output(RFM95_RST_PIN, GPIO.HIGH); sleep(0.01)
GPIO.output(RFM95_RST_PIN, GPIO.LOW);  sleep(0.01)
GPIO.output(RFM95_RST_PIN, GPIO.HIGH); sleep(0.05)

lora = None
lora_ok = False
try:
    lora = LoRa(
        0, LORA_INTERRUPT_PIN, SERVER_ADDRESS,
        modem_config=ModemConfig.Bw125Cr45Sf128,
        tx_power=RF95_POW,
        acks=True,
        freq=RF95_FREQ,
    )
    # SF9 BW125 — pas de constante raspi_lora prédéfinie, override registres directs.
    # {REG_1D, REG_1E, REG_26} = {BW125+CR4/5, SF9+CRC, LDROptimize off}.
    # Validé terrain ben01 (RSSI ≈ -97 à -103 dBm, SNR +7/+10 dB).
    # Doit rester ISO avec src/arduino/tic-reader/tic-reader.ino côté émetteur.
    lora._spi_write(0x1D, 0x72)
    lora._spi_write(0x1E, 0x94)
    lora._spi_write(0x26, 0x00)
    lora.on_recv = on_recv
    lora.set_mode_rx()
    lora_ok = True
    log.info(f"LoRa addr={SERVER_ADDRESS} freq={RF95_FREQ}MHz proto=v0x{PROTOCOL_VERSION:02x}")
except Exception as e:
    log.error(f"LoRa init FAILED : {e} — mode sans radio")
    for _ in range(3):
        blink_rgb(30, 0, 0, 0.5, bypass=True)
        sleep(0.3)
    sleep(0.5)
log.info(f"PDL connu : {state.get('adco') or '(aucun)'}")
log.info(f"Index connus : {state['indexes']}")
log.info(f"Last boot_seq : {state.get('last_boot_seq')}")

Thread(target=watchdog_loop, daemon=True, name="watchdog").start()
Thread(target=heartbeat_loop, daemon=True, name="heartbeat").start()
log.info(f"Watchdog démarré (seuil={WATCHDOG_THRESHOLD}s), heartbeat={RECEPTION_TIMEOUT_S}s")

try:
    while True:
        sleep(0.1)
except KeyboardInterrupt:
    log.info("Arrêt.")
finally:
    try:
        if lora: lora.close()
    except Exception: pass
