"""
ben-telemetry — décode + stocke la télémétrie LoRa (consommateur MQTT).

Refactor de l'ancien monolithe `lora-receiver/main.py` MOINS la radio : ce service ne possède
PLUS le RFM95. Il s'abonne à `ben/lora/rx` (publié par `ben-radio`, la façade radio unique),
vérifie le HMAC, route par pdl_index selon sources.json, décode (frame_codec) et stocke en
SQLite — exactement comme avant. La LED n'est plus pilotée en direct : les intentions colorées
sont publiées sur `ben/led` (ben-radio possède le GPIO). Aucun watchdog radio ici (c'est
ben-radio qui porte le contrat systemd Type=notify) : simple service Restart=always.

Cf. docs/canal-commande-lora-descendant.md (split monolithe → ben-radio + ben-telemetry).

Message MQTT `ben/lora/rx` : {"from": int, "rssi": float, "snr": float, "hex": str}.
Clé HMAC : /etc/ben-firmware/hmac.key. Sources : /etc/ben-firmware/sources.json.
État     : /var/lib/ben-firmware/lora-state.json.
"""

import json
import logging
import os
import time
import traceback
from time import sleep

import paho.mqtt.client as mqtt

# Module store partagé (src/pi/store/db.py)
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "store"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lora-receiver"))  # frame_codec, curve_codec
import db  # noqa: E402

# Décodeur courbe v0x05 (module pur, même dossier — testable hors device).
import curve_codec  # noqa: E402,F401
import frame_codec  # noqa: E402

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
SOURCES_PATH   = "/etc/ben-firmware/sources.json"
STATE_PATH     = "/var/lib/ben-firmware/lora-state.json"

# ---------------------------------------------------------------------------
# MQTT
# ---------------------------------------------------------------------------
MQTT_HOST = "127.0.0.1"
MQTT_PORT = 1883
TOPIC_RX  = "ben/lora/rx"        # souscrit : {from, rssi, snr, hex} (publié par ben-radio)
TOPIC_LED = "ben/led"            # publié : {r, g, b, duration, bypass} (rendu par ben-radio) — topic TRANSVERSE

# ---------------------------------------------------------------------------
# Protocole
# ---------------------------------------------------------------------------
PROTOCOL_VERSION = 0x02
PAYLOAD_LEN      = 20
BOOT_MAX_LEN     = 32   # v0x01 étendu : version + ADCO(12) + ISOUSC + PREF + NGTF([len]+≤16)
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
# LED : plus de GPIO ici — on publie l'INTENTION sur ben/led (ben-radio rend).
# Mêmes sites d'appel/couleurs qu'avant (jaune/orange/rouge/blanc/vert).
# ---------------------------------------------------------------------------
_mqtt = None   # client MQTT (défini dans main)

def blink_rgb(r, g, b, duration=0.05, bypass=False):
    if _mqtt is None:
        return
    try:
        _mqtt.publish(TOPIC_LED, json.dumps(
            {"r": r, "g": g, "b": b, "duration": duration, "bypass": bypass}), qos=0)
    except Exception:
        pass

# Pas de clé HMAC ici : la façade `ben-radio` a déjà vérifié le MAC + déchiffré (bus en clair,
# secure-by-default). ben-telemetry ne fait que parser du clair (frame_codec.parse).

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
        "silence_restart_count": int(raw.get("silence_restart_count", 0)),
    }

def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f)

state = load_state()
# last_frame_time reste utile ICI pour `time_since_prev` (détection duplicata/reboot émetteur).
# Le watchdog de SILENCE, lui, a migré dans ben-radio (état RADIO). silence_restart_count est
# conservé dans le state file (inoffensif) mais n'est plus utilisé côté télémétrie.
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
# Réception (via MQTT ben/lora/rx → ben-radio a déjà lu les octets sur l'air)
# ---------------------------------------------------------------------------
def on_recv(raw: bytes, rssi, snr, sender_addr: int) -> None:
    global last_frame_time, last_prune
    try:
        now = time.time()
        time_since_prev = now - last_frame_time
        last_frame_time = now
        blink_rgb(5, 5, 0, 0.05)  # jaune court & faible — trame reçue (RF only, avant validation)
        sleep(0.4)                # délai pour distinguer du flash suivant

        if not (RSSI_MIN_PLAUSIBLE <= rssi <= RSSI_MAX_PLAUSIBLE
                and SNR_MIN_PLAUSIBLE <= snr <= SNR_MAX_PLAUSIBLE):
            log.info(f"RSSI/SNR aberrants ({rssi}/{snr}) — lib raspi_lora non patchée ?")

        log.info(f"From=0x{sender_addr:02x} RSSI={rssi} SNR={snr} len={len(raw)}")

        # Résolution pdl_index
        pdl_index = get_pdl_index(sender_addr)
        if pdl_index is None:
            log.warning(f"Adresse émetteur inconnue 0x{sender_addr:02x} — ignorée (non dans sources.json)")
            blink_rgb(30, 15, 0, 0.3, bypass=True)  # orange — source inconnue
            return

        try:
            decoded = frame_codec.parse(raw)   # trame DÉJÀ en clair (MAC vérifié + déchiffré par ben-radio)
        except frame_codec.FrameError as e:
            log.error(f"parse échoué : {e}")
            blink_rgb(30, 0, 0, 0.3, bypass=True)  # rouge — format
            return

        ftype = decoded["type"]
        if ftype == frame_codec.TYPE_CURVE:
            on_recv_curve(decoded, rssi, snr, pdl_index, now, time_since_prev)
        elif ftype == frame_codec.TYPE_BOOT:
            on_recv_boot(decoded, rssi, snr, pdl_index)
        else:
            log.error(f"type de trame inconnu : 0x{ftype:02x}")
            blink_rgb(30, 0, 0, 0.3, bypass=True)  # rouge — proto
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


# Champs collectés mais NON câblés au stockage (DEMAIN/ADPS/PEJP, NJOURF/NJOURF+1 — tous en TLV) :
# logués À MINIMA, ON-CHANGE en INFO — MÊME format/manière que le lecteur wired (main_uart.py).
# MSG1/MSG2 = non émis (RAM ATmega328). NB : DEMAIN_NAMES/FLAG_ADPS/FLAG_PEJP en tête de fichier
# sont MORTS (reliquat des flags v0x02) — DEMAIN passe désormais par TLV, pas par l'octet flags.
_last_uncabled: dict = {}


def log_uncabled(pdl_index, tlvs) -> None:
    """Logge en INFO les TLV connus-mais-non-stockés quand leur valeur change (aligné wired)."""
    for _tag, name, val, known, stored in frame_codec.interpret_tlvs(tlvs):
        if known and not stored:
            key = (pdl_index, name)
            if _last_uncabled.get(key) != val:
                log.info(f"non câblé : {name}={val!r} (collecté, pas stocké) pdl_index={pdl_index}")
                _last_uncabled[key] = val


def on_recv_boot(decoded, rssi, snr, pdl_index) -> None:
    """Trame BOOT/IDENTITÉ (format cible) : TLV identité (ADCO/ISOUSC/PREF/CONTRAT), MAC déjà
    vérifié. Reset state si nouveau PDL (ADCO). TLV non câblés → logués."""
    tlvs = decoded["tlvs"]
    adco = (frame_codec.interpret_tlv(frame_codec.T_ADCO, tlvs[frame_codec.T_ADCO])
            if frame_codec.T_ADCO in tlvs else "")
    if adco and adco != state.get("adco", ""):
        log.info(f"BOOT pdl_index={pdl_index} ADCO={adco} — NOUVEAU PDL, reset state")
        state["indexes"] = {}
        state["last_boot_seq"] = None
        state["last_active_id"] = None
        state["adco"] = adco
        save_state(state)
    else:
        log.info(f"BOOT pdl_index={pdl_index} ADCO={adco} (PDL connu)")
    if measurements_db is not None:
        isousc = tlvs.get(frame_codec.T_ISOUSC)
        if isousc:
            try:
                if db.record_isousc(measurements_db, pdl_index, isousc[0]):
                    log.info(f"ISOUSC={isousc[0]} A (maxVa≈{isousc[0]*230}) pdl_index={pdl_index}")
            except Exception as e:
                log.warning(f"store: record_isousc échoué: {e}")
        pref = tlvs.get(frame_codec.T_PREF)
        if pref:
            try:
                if db.record_pref(measurements_db, pdl_index, pref[0]):
                    log.info(f"PREF={pref[0]} kVA (maxVa≈{pref[0]*1000}) pdl_index={pdl_index}")
            except Exception as e:
                log.warning(f"store: record_pref échoué: {e}")
        contrat = tlvs.get(frame_codec.T_CONTRAT)
        if contrat:
            ngtf = frame_codec.interpret_tlv(frame_codec.T_CONTRAT, contrat)
            try:
                if db.record_ngtf(measurements_db, pdl_index, ngtf):
                    log.info(f"CONTRAT={ngtf!r} pdl_index={pdl_index}")
            except Exception as e:
                log.warning(f"store: record_ngtf échoué: {e}")
    log_uncabled(pdl_index, tlvs)                    # TLV connus non stockés → INFO on-change
    blink_rgb(30, 30, 30, 2.0)   # blanc long = discovery


def on_recv_curve(decoded, rssi, snr, pdl_index, now, time_since_prev) -> None:
    """Trame COURBE (format cible) déjà décodée + MAC vérifié par frame_codec. Reconstruit la
    courbe PAPP, l'horodate, l'insère EN BATCH dans `measurements` (comme le wired). Décode
    tous les TLV ; les non câblés (DEMAIN/ADPS/PEJP/NJOURF/MSG) sont LOGUÉS."""
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
    iinst = decoded.get("iinst")          # 2e courbe IINST (histo) : liste par point, ou None (standard)
    tlvs = decoded["tlvs"]
    ts_list = frame_codec.anchor_timestamps(decoded, now)         # ts SYSTÈME (Pi/NTP, approx LoRa)
    meter_ts_list = frame_codec.meter_timestamps(decoded)         # meter_ts COMPTEUR (standard) ou None
    batch_seq = decoded["batch_seq"]
    index_value = decoded["index_value"]
    # EAIT/LTARF câblés (stockés) ; DEMAIN/ADPS/PEJP/NJOURF/MSG connus mais PAS encore câblés
    # → décodés et LOGUÉS (visibilité avant câblage), tags inconnus déjà sautés par frame_codec.
    inject_total = (frame_codec.interpret_tlv(frame_codec.T_EAIT, tlvs[frame_codec.T_EAIT])
                    if frame_codec.T_EAIT in tlvs else None)
    ltarf = (frame_codec.interpret_tlv(frame_codec.T_LTARF, tlvs[frame_codec.T_LTARF])
             if frame_codec.T_LTARF in tlvs else None)
    log_uncabled(pdl_index, tlvs)                    # DEMAIN/ADPS/PEJP/NJOURF/NJOURF+1 → INFO on-change

    mode = "std" if src_standard else "histo"
    idlbl = f"NTARF={index_id}" if src_standard else active_name
    extra = f" inject_total={inject_total}" if inject_total is not None else ""
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

    # LTARF (label tarif standard, ext v2) : cache NTARF→LTARF (write-on-change) → alimente
    # la résolution de label serveur (/registers, /live.tariff_label). Chantier labels+index.
    if ltarf and measurements_db is not None:
        try:
            ngtf = db.get_ngtf(measurements_db, pdl_index) or ""   # contrat courant → scope du label
            if db.record_tariff_label(measurements_db, pdl_index,
                                      1 if src_standard else 0, index_id, ltarf, ngtf):
                log.info(f"LTARF NTARF={index_id} → {ltarf!r} pdl_index={pdl_index}")
        except Exception as e:
            log.warning(f"store: record_tariff_label échoué: {e}")

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
                if iinst is not None and i < len(iinst):   # 2e courbe IINST (histo) — décodée mais
                    labels["IINST"] = iinst[i]              #   PAS stockée avant ce fix → colonne iinst NULL
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
# MQTT
# ---------------------------------------------------------------------------
def on_mqtt_message(client, userdata, msg) -> None:
    """ben/lora/rx : {from, rssi, snr, hex} → décode + stocke."""
    try:
        d = json.loads(msg.payload.decode())
        raw = bytes.fromhex(d["hex"])
        on_recv(raw, d.get("rssi", 0), d.get("snr", 0), int(d["from"]))
    except Exception:
        log.error(f"MQTT rx illisible :\n{traceback.format_exc()}")


def on_mqtt_connect(client, userdata, flags, *args) -> None:
    client.subscribe(TOPIC_RX, qos=0)
    log.info(f"MQTT connecté ({MQTT_HOST}:{MQTT_PORT}), abonné {TOPIC_RX}")


def main():
    global _mqtt
    try:
        _mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="ben-telemetry")
    except (AttributeError, TypeError):
        _mqtt = mqtt.Client(client_id="ben-telemetry")     # paho 1.x
    _mqtt.on_connect = on_mqtt_connect
    _mqtt.on_message = on_mqtt_message
    log.info(f"PDL connu : {state.get('adco') or '(aucun)'}")
    log.info(f"Index connus : {state['indexes']}")
    _mqtt.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
    _mqtt.loop_forever()


if __name__ == "__main__":
    main()
