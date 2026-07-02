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

# Décodeur courbe v0x05 (module pur, même dossier — testable hors device).
import curve_codec  # noqa: E402

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
    # Blink de boot : 3 flashs bleus brefs (remplace l'ancienne séquence arc-en-ciel
    # ~5 s "disco" ; aligné avec le wired).
    import time as _t
    for _ in range(3):
        _pwm_b.ChangeDutyCycle(20); _t.sleep(0.12)
        _pwm_b.ChangeDutyCycle(0);  _t.sleep(0.12)

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
        "last_boot_seq":   raw.get("last_boot_seq"),   # v0x02
        "last_batch_seq":  raw.get("last_batch_seq"),  # v0x05
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


def detect_batch_seq_event(current: int, time_since_prev: float):
    """Idem detect_boot_seq_event mais pour batch_seq (v0x05). Chaque batch v0x05
    s'auto-ancre (keyframe par trame, §17.3) → un trou se LOGUE seulement, aucune
    correction n'est nécessaire. batch_seq est RAM côté Arduino → reset au boot."""
    last = state.get("last_batch_seq")
    if last is None:
        log.info(f"Premier batch, batch_seq={current}")
        return ("first_seen", {"seq": current})
    if current == last + 1:
        return (None, None)
    is_fresh_cycle = time_since_prev >= DUPLICATE_MAX_GAP_S
    if current == last:
        if not is_fresh_cycle:
            log.warning(f"batch_seq dupliqué (retransmission, gap={time_since_prev:.1f}s): {current}")
            return ("duplicate", {"seq": current})
        log.warning(f"REBOOT émetteur (batch_seq stagne à {current}, gap={time_since_prev:.1f}s)")
        return ("emitter_reboot", {"prev_seq": last, "new_seq": current})
    if current < last:
        log.warning(f"REBOOT émetteur : batch_seq {last} → {current}")
        return ("emitter_reboot", {"prev_seq": last, "new_seq": current})
    gap = current - last - 1
    log.warning(f"{gap} batch(s) perdu(s) (batch_seq {last} → {current})")
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

        version = raw[0]

        # v0x05 = trame courbe batchée (longueur VARIABLE) → branche dédiée, AVANT
        # le contrôle de longueur fixe (sinon toute trame v0x05 serait rejetée ici).
        if version == curve_codec.PROTOCOL_VERSION_CURVE:
            on_recv_curve(raw, rssi, snr, pdl_index, now, time_since_prev)
            return

        # v0x01 (boot) et v0x02 (mesure) sont à longueur fixe (20 o).
        if len(raw) != PAYLOAD_LEN:
            log.error(f"Longueur incorrecte : {len(raw)} octets, {PAYLOAD_LEN} attendus")
            blink_rgb(30, 0, 0, 0.3, bypass=True)  # rouge — erreur proto
            return

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
            # ISOUSC (abonnement) : octet 13 de la trame d'identité = intensité
            # souscrite en A (0 = absent, ex. émetteur < 0.0.2 → rétro-compat).
            # Chantier ISOUSC. record_isousc fait sa garde (écrit seulement sur
            # changement) ; le boot frame est rare → pas de garde RAM nécessaire.
            isousc = raw[13]
            if isousc and measurements_db is not None:
                try:
                    if db.record_isousc(measurements_db, pdl_index, isousc):
                        log.info(f"ISOUSC={isousc} A enregistré "
                                 f"(maxVa≈{isousc * 230} VA) pdl_index={pdl_index}")
                except Exception as e:
                    log.warning(f"store: record_isousc échoué: {e}")
            # PREF (abonnement en mode STANDARD, kVA) : octet 14 (0 = absent, ex. émetteur
            # < 0.0.7 → rétro-compat). Le standard ne porte pas ISOUSC ; PREF×1000 calibre la jauge.
            pref = raw[14] if len(raw) > 14 else 0
            if pref and measurements_db is not None:
                try:
                    if db.record_pref(measurements_db, pdl_index, pref):
                        log.info(f"PREF={pref} kVA enregistré "
                                 f"(maxVa≈{pref * 1000} VA) pdl_index={pdl_index}")
                except Exception as e:
                    log.warning(f"store: record_pref échoué: {e}")
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


def _maybe_prune() -> None:
    """Purge la rétention au plus une fois/heure (commun v0x02/v0x05)."""
    global last_prune
    if measurements_db is not None and time.time() - last_prune > 3600:
        try:
            deleted = db.prune(measurements_db)
            log.info(f"store purge (>{db.RETENTION_DAYS}j): {deleted}")
        except Exception as e:
            log.warning(f"store: purge échouée: {e}")
        last_prune = time.time()


def on_recv_curve(raw, rssi, snr, pdl_index, now, time_since_prev) -> None:
    """Trame courbe v0x05 : HMAC vérifié + courbe PAPP reconstruite par curve_codec,
    horodatée (ancrage réception en historique), puis insérée EN BATCH dans la même
    table `measurements` que le wired → /curve et /measurements à l'identique."""
    try:
        decoded = curve_codec.decode_v04(raw, HMAC_KEY)
    except curve_codec.CurveDecodeError as e:
        log.error(f"v0x05 rejetée : {e}")
        blink_rgb(30, 0, 0, 0.3, bypass=True)  # rouge — HMAC / proto
        return

    index_id = decoded["index_id"]
    src_standard = decoded["src_standard"]

    # Index actif : histo → nom canonique (INDEX_NAMES) écrit dans base/hchc/hchp ;
    # standard → index_id = NTARF (1..10), OPAQUE (nom = LTARF fournisseur, pas de table
    # en dur) → pas de mapping base/hchc/hchp, seules les colonnes génériques le portent.
    if src_standard:
        if not (1 <= index_id <= 10):
            log.warning(f"v0x05 standard : NTARF hors plage ({index_id}), batch ignoré")
            blink_rgb(30, 0, 0, 0.3, bypass=True)
            return
        active_name = None
    else:
        active_name = INDEX_NAMES.get(index_id)
        if active_name is None:
            if index_id == INDEX_UNKNOWN:
                log.warning("v0x05 index_id=0xFF (PTEC inconnu côté Arduino), batch ignoré")
            else:
                log.error(f"v0x05 index_id inconnu : 0x{index_id:02x}")
            blink_rgb(30, 0, 0, 0.3, bypass=True)
            return

    papp = decoded["papp"]
    ts_list = curve_codec.anchor_timestamps(decoded, now)         # ts SYSTÈME (Pi/NTP, approx LoRa)
    meter_ts_list = curve_codec.meter_timestamps(decoded)         # meter_ts COMPTEUR (standard) ou None
    flags = decoded["flags"]
    demain = DEMAIN_NAMES[flags & 0x03]
    adps = bool(flags & FLAG_ADPS)
    pejp = bool(flags & FLAG_PEJP)
    batch_seq = decoded["batch_seq"]
    index_value = decoded["index_value"]
    inject_total = decoded["inject_total"]

    mode = "std" if src_standard else "histo"
    idlbl = f"NTARF={index_id}" if src_standard else active_name
    extra = (f" inject_total={inject_total}" if src_standard
             else f" demain={demain} adps={adps} pejp={pejp}")
    log.info(f"v0x05 OK [{mode}] pdl_index={pdl_index} batch_seq={batch_seq} {idlbl} "
             f"index={index_value} n={decoded['n']} period_ds={decoded['period_ds']} "
             f"papp[0]={papp[0]} papp[-1]={papp[-1]}" + extra)

    # Chaque batch s'auto-ancre (§17.3) → on logue les trous, sans correction.
    event, details = detect_batch_seq_event(batch_seq, time_since_prev)
    if event:
        log.info(f"EVENT {event} {details}")

    # INDEX0 — instrumentation cause racine : l'émetteur a envoyé un keyframe
    # index_value=0 (carry-forward EASF empoisonné côté Arduino, cf. chantier
    # reflash Arduino). Le récepteur ne voit PAS la ligne EASF brute (elle est sur
    # l'Arduino) ; on logue le CONTEXTE (greppable "INDEX0") pour corréler l'épisode :
    # gap radio ? bascule NTARF ? lien marginal (rssi/snr) ? Rare (~0-2 batchs/j).
    if index_value == 0:
        log.warning(
            f"INDEX0 keyframe index_value=0 batch_seq={batch_seq} NTARF={index_id} "
            f"n={decoded['n']} papp[0]={papp[0]} papp[-1]={papp[-1]} "
            f"rssi={rssi} snr={snr} dt_prev={time_since_prev} "
            f"event={event or '-'} {details or ''}")

    # Stockage : un row par échantillon. L'index actif est CONSTANT sur le batch (l'Arduino
    # coupe au changement d'index) → estampillé sur CHAQUE row (curve_buckets JOIN ts_max →
    # jamais d'index NULL). Colonnes GÉNÉRIQUES (_src_standard/_index_id/_index_value) +
    # meter_ts (horodate compteur, standard) ; base/hchc/hchp en double-écriture (histo).
    if measurements_db is not None:
        try:
            rows = []
            for i in range(len(papp)):
                labels = {
                    "PAPP": papp[i],                       # net signé en standard (− = injection)
                    "_src_standard": 1 if src_standard else 0,
                    "_index_id": index_id,
                    "_index_value": index_value,
                }
                if active_name is not None:                # double-écriture histo
                    labels[active_name] = index_value
                if inject_total is not None:
                    labels["_inject_total"] = inject_total
                if meter_ts_list is not None:
                    labels["_meter_ts"] = meter_ts_list[i]
                rows.append((pdl_index, labels, ts_list[i]))
            n_ins = db.record_measurements_batch(measurements_db, rows)
            db.record_lora_link(measurements_db, pdl_index, rssi, snr)
            log.debug(f"store: v0x05 [{mode}] batch {n_ins} mesures + 1 lora_link")
        except Exception as e:
            log.warning(f"store: écriture v0x05 échouée: {e}")
        _maybe_prune()

    # Anti-rollback de l'index (sur le keyframe = absolu du 1er échantillon).
    prev_value = state["indexes"].get(active_name, 0)
    if index_value < prev_value:
        log.warning(f"{active_name} en décroissance : {index_value} < {prev_value}")
        blink_rgb(30, 15, 0, 0.3, bypass=True)  # orange — décroissance
    else:
        blink_rgb(0, 5, 0, 0.05)               # vert court — batch valide
        state["indexes"][active_name] = int(index_value)

    state["last_active_id"] = int(index_id)
    state["last_batch_seq"] = int(batch_seq)
    state["last_frame_time"] = now
    save_state(state)

# ---------------------------------------------------------------------------
# Watchdog + Heartbeat
# ---------------------------------------------------------------------------
# Pas de watchdog « relance si pas de trame » côté récepteur LoRa : contrairement au
# wired (où l'absence de TIC peut signaler un port série bloqué qu'un restart resynchronise),
# ici « pas de trame » est NORMAL (émetteur éteint, hors portée, supercap en recharge). Un
# restart ne reçoit pas plus et perd l'état (indexes/seq). La fraîcheur LoRa reste signalée
# par le heartbeat (flash orange si rien depuis >90 s) — informatif, sans relance.
# (Si un jour la radio se fige vraiment, ajouter un vrai self-test radio plutôt qu'un
#  timeout sur l'absence de trafic, qui confond « radio HS » et « rien à recevoir ».)

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

Thread(target=heartbeat_loop, daemon=True, name="heartbeat").start()
log.info(f"Heartbeat démarré ({RECEPTION_TIMEOUT_S}s) — pas de watchdog restart-sur-silence")

try:
    while True:
        sleep(0.1)
except KeyboardInterrupt:
    log.info("Arrêt.")
finally:
    try:
        if lora: lora.close()
    except Exception: pass
