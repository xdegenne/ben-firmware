#!/usr/bin/env python3
"""ben-radio — daemon SEUL propriétaire du RFM95 (façade radio du Pi).

Contrainte structurante : une seule puce RFM95 → un seul process peut piloter le SPI + l'IRQ.
`ben-radio` est ce process unique. Il fait UNIQUEMENT du transport (aucun décode, aucune clé) :

  RX  : trame reçue → publie {from, rssi, snr, hex} sur MQTT `ben/lora/rx`.
  TX  : souscrit `ben/lora/tx` {to, hex} → émet (send_to_wait), SÉRIALISÉ avec le RX (verrou radio).
  LED : possède la LED RGB du Pi. Heartbeat santé WiFi/LoRa + flash RF à la réception + souscrit
        `ben/led` {r,g,b,duration,bypass} (intentions colorées de la télémétrie).

Le décode / vérif MAC / déchiffrement / stockage vit dans `ben-telemetry` (consommateur de
`ben/lora/rx`). → plus jamais de stop/start du récepteur pour émettre une commande volet
(c'est ce qui figeait le SPI / oops noyau). Cf. docs/canal-commande-lora-descendant.md.

Watchdogs répliqués de l'ancien monolithe (self-test REG_VERSION, taint noyau, silence RF à
backoff, heartbeat systemd Type=notify/WatchdogSec).
"""
import os
import sys
import json
import time
import socket
import logging
import threading
from time import sleep
from threading import Thread

import RPi.GPIO as GPIO
from raspi_lora import LoRa, ModemConfig
import paho.mqtt.client as mqtt

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "store"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lora-receiver"))
import settings     # noqa: E402  (led_factor pour la luminosité LED)
import frame_codec  # noqa: E402  (open_frame : verify+decrypt MONTANT)
import secure_link  # noqa: E402  (seal_command : chiffrement DESCENDANT)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ben-radio")

# --------------------------------------------------------------------------- #
# Config radio (ISO ancien monolithe) + MQTT                                   #
# --------------------------------------------------------------------------- #
RF95_FREQ          = 868.0
RF95_POW           = 5
SERVER_ADDRESS     = 32          # adresse LoRa de la centrale (0x20)
LORA_INTERRUPT_PIN = 22
RFM95_RST_PIN      = 17

MQTT_HOST = "127.0.0.1"
MQTT_PORT = 1883
TOPIC_RX  = "ben/lora/rx"        # publié : {from, rssi, snr, hex}  hex = trame EN CLAIR (déchiffrée)
TOPIC_TX  = "ben/lora/tx"        # souscrit : {to, body}  body = corps commande EN CLAIR (hex), scellé ici
TOPIC_LED = "ben/led"            # souscrit : {r, g, b, duration, bypass} — topic TRANSVERSE (pas sous lora)

# --------------------------------------------------------------------------- #
# Clés — la façade est le SEUL détenteur (bus en clair, secure-by-default)      #
# --------------------------------------------------------------------------- #
HMAC_KEY_PATH = "/etc/ben-firmware/hmac.key"
with open(HMAC_KEY_PATH) as _f:
    K_RACINE = bytes.fromhex(_f.read().strip())      # RACINE de dérivation (clé du central ben-0001)

# Devices du domaine : adresse LoRa → nom local (deviceId). La clé de chaque device est DÉRIVÉE :
#   K_device = HMAC(K_racine, nom)  → puis K_up/K_dn par label (secure_link).
# → isolation par device (un EEPROM dumpé ne donne que SA clé), rotation possible par nom/epoch.
# Le nom est STABLE (découplé de l'adressage radio). Le central dérive tout seul (racine locale).
DEVICES_PATH = "/etc/ben-firmware/devices.yaml"   # registre adresse LoRa → nom (donnée de PROVISIONING)


def load_devices(path=DEVICES_PATH):
    """Registre `adresse LoRa → nom device`, lu depuis un fichier de provisioning (plus codé en dur).

    Source de vérité côté opérateur : `ben-ops/artifacts/<central>/devices.yaml`, poussé au
    provisioning/OTA (même modèle que la CA). Format minimal (sous-ensemble YAML, sans dépendance) :
    lignes `0x1f: tic` (# commentaires ignorés). Le nom sert la dérivation `K = HMAC(K_racine, nom)` ;
    l'adresse sert le routage radio (découplés). Ajouter un device = éditer ce fichier + flasher le
    device (adresse+clé en EEPROM), AUCUN edit de code. Fallback intégré = filet si le fichier manque
    (le central live ne doit pas démarrer aveugle), pas la source de vérité."""
    devices = {}
    try:
        with open(path) as f:
            for raw in f:
                line = raw.split("#", 1)[0].strip()
                if not line or ":" not in line:
                    continue
                addr_s, name = line.split(":", 1)
                try:
                    devices[int(addr_s.strip(), 0)] = name.strip()
                except ValueError:
                    log.warning(f"devices.yaml : ligne ignorée {raw!r}")
    except FileNotFoundError:
        log.error(f"{path} ABSENT → registre de secours (à provisionner !)")
    if not devices:
        devices = {0x1f: "tic", 0x2a: "actuator-01"}      # filet de secours
    log.info(f"registre devices : {{{', '.join(f'0x{a:02x}:{n}' for a, n in devices.items())}}}")
    return devices


DEVICES = load_devices()
# TRANSITION (VIDE depuis 2026-07-22) : plus aucun device sur la clé plate. L'émetteur 0x1f a été
# re-keyé en EEPROM avec K_tic = HMAC(K_racine,"tic") (avrdude), le satellite 0x2a avec
# K=HMAC(K_racine,"actuator-01"). Tout le parc est en dérivé par-device. Garder ce set (vide) pour
# rouvrir une transition si on ajoute un device de terrain non-encore-re-keyé.
FLAT_DEVICES = set()
DOWNLINK_STATE_PATH = "/var/lib/ben-firmware/radio-tx.json"   # compteurs anti-rejeu descendants persistés
_dev_key_cache = {}


def device_key(addr: int):
    """Clé du device. DÉRIVÉE (HMAC(K_racine, nom)) sauf si encore en transition à plat (K_racine)."""
    if addr in _dev_key_cache:
        return _dev_key_cache[addr]
    if addr in FLAT_DEVICES:
        return K_RACINE                                 # clé plate (transition, avant re-key)
    name = DEVICES.get(addr)
    if name is None:
        return None
    k = secure_link.derive_device_key(K_RACINE, name)   # HMAC(K_racine, nom)
    _dev_key_cache[addr] = k
    return k

# LED RGB (BCM, cathode commune) : R=GPIO12 (HW PWM0), G=GPIO13 (HW PWM1), B=GPIO16 (SW PWM)
RGB_R, RGB_G, RGB_B = 12, 13, 16
RECEPTION_TIMEOUT_S  = 60
TIC_HEALTH_TIMEOUT_S = 90

# Plausibilité RSSI/SNR (log qualité signal, radio-level)
RSSI_MIN_PLAUSIBLE, RSSI_MAX_PLAUSIBLE = -140, -20
SNR_MIN_PLAUSIBLE,  SNR_MAX_PLAUSIBLE  = -20, 20

# --------------------------------------------------------------------------- #
# Watchdogs (répliqués ISO main.py)                                            #
# --------------------------------------------------------------------------- #
SX127X_REG_VERSION = 0x42
SX127X_VERSION     = 0x12
WATCHDOG_PING_S    = 30
RADIO_FAIL_MAX     = 2
TAINT_DIE          = 0x80

SILENCE_RESTART_BASE_S  = 300
SILENCE_RESTART_MAX_S   = 3600
SILENCE_BACKOFF_EXP_MAX = 4
# état silence persisté (indépendant de la télémétrie : c'est de l'état RADIO)
SILENCE_STATE_PATH = "/var/lib/ben-firmware/radio-state.json"

# --------------------------------------------------------------------------- #
# Globals                                                                      #
# --------------------------------------------------------------------------- #
GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)
_pwm_r = _pwm_g = _pwm_b = None

lora = None
lora_ok = False
radio_lock = threading.Lock()    # sérialise TX et self-test SPI vs l'IRQ RX
mqttc = None

RECEIVER_BOOT_TIME = time.time()
last_frame_time = 0.0
silence_restarts = 0
tx_counters = {}     # {peer_addr: dernier compteur} anti-rejeu DESCENDANT, persisté
_tx_hid = 0          # header_id RadioHead incrémental (évite le dédup côté satellite en fire-and-forget)


def load_tx_counters():
    global tx_counters
    try:
        with open(DOWNLINK_STATE_PATH) as f:
            tx_counters = {int(k): int(v) for k, v in json.load(f).items()}
    except Exception:
        tx_counters = {}


def next_tx_counter(peer: int) -> int:
    """Compteur descendant STRICTEMENT croissant par pair (persisté) → anti-rejeu satellite."""
    c = tx_counters.get(peer, 0) + 1
    tx_counters[peer] = c
    try:
        os.makedirs(os.path.dirname(DOWNLINK_STATE_PATH), exist_ok=True)
        tmp = DOWNLINK_STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump({str(k): v for k, v in tx_counters.items()}, f)
        os.replace(tmp, DOWNLINK_STATE_PATH)
    except Exception:
        pass
    return c


def send_acked(to: int, frame: bytes, hid: int, timeout: float = 0.5, retries: int = 2):
    """Émet une commande et attend l'ACK RadioHead du satellite. Retourne le RTT (s) ou None.

    Corrige le `send_to_wait` natif de raspi_lora sur le lien DESCENDANT : il faisait
    `send()` puis `set_mode_rx()` SANS `wait_packet_sent()`, donc sa fenêtre d'attente
    (0,2–0,4 s) démarrait pendant l'émission de la commande → l'ACK réel (RTT mesuré ~130 ms
    APRÈS la fin d'émission) tombait souvent juste hors fenêtre → faux « pas ACK » → retries
    ~2 s qui empêchaient le stop-en-course.

    Ici : on ATTEND la fin d'émission avant d'écouter, et la fenêtre 0,5 s couvre large les
    130 ms. Un retry ne re-scelle pas (même frame, même hid) → dédup RadioHead côté satellite
    → pas de double actionnement, juste un nouvel ACK. Idempotent."""
    for _ in range(retries + 1):
        lora._last_payload = None
        lora.send(frame, to, header_id=hid)
        lora.wait_packet_sent()              # ← garantit la commande émise EN ENTIER avant d'écouter
        lora.set_mode_rx()
        t0 = time.time()
        while time.time() - t0 < timeout:
            p = lora._last_payload
            if p and (p.header_flags & 0x80) and p.header_to == lora._this_address \
                    and p.header_id == hid:
                return time.time() - t0      # ACK reçu
            time.sleep(0.005)
    return None                              # vraie perte RF après retries


def send_app_ack(to: int, clear: bytes, key: bytes) -> None:
    """ACK APPLICATIF crypto-vérifié d'une trame BOOT : renvoie HMAC(K_mac, nonce)[:8] à l'émetteur.

    Anti cross-talk MULTI-LOGEMENT : plusieurs centrales partagent SERVER_ADDRESS=0x20, et RadioHead
    ACK au niveau LIAISON toute trame adressée à 0x20 AVANT toute vérif de clé → une centrale VOISINE
    « vole » le link-ACK du boot et l'émetteur croit être enregistré chez elle (il émettrait ADCO,
    OPTARIF, abonnement… à côté). Parade : la centrale ne renvoie cet ACK QUE si le MAC montant est
    valide (elle détient donc la clé du device) ET l'ACK est lui-même un HMAC(K_mac, nonce) que SEUL
    le détenteur de la clé peut produire. L'émetteur (recvfromAckTimeout) ne se déclare `bootAcked`
    que sur un ACK applicatif valide → il ignore le voisin. Le nonce = header clair de SA trame boot
    (boot_count‖msg_count), unique → pas rejouable. Émis depuis le callback RX : `radio_lock` sérialise
    contre un TX concurrent ; `header_id` frais → pas de dédup RadioHead côté émetteur."""
    _, k_mac = frame_codec.derive_keys(key)
    ack = frame_codec._mac(k_mac, clear[1:7])            # HMAC(K_mac, boot_count(3)‖msg_count(3))[:8]
    global _tx_hid
    _tx_hid = (_tx_hid + 1) & 0xFF
    with radio_lock:
        lora.send(ack, to, header_id=_tx_hid)
        lora.wait_packet_sent()                          # émission complète avant de rendre la main
        lora.set_mode_rx()                               # retour écoute (ne PAS rester en TX)
    log.info(f"boot 0x{to:02x} MAC OK → ACK applicatif émis (hid={_tx_hid})")


# --------------------------------------------------------------------------- #
# LED                                                                          #
# --------------------------------------------------------------------------- #
def setup_led():
    global _pwm_r, _pwm_g, _pwm_b
    for pin in (RGB_R, RGB_G, RGB_B):
        GPIO.setup(pin, GPIO.OUT)
        GPIO.output(pin, GPIO.LOW)
    _pwm_r = GPIO.PWM(RGB_R, 500); _pwm_g = GPIO.PWM(RGB_G, 500); _pwm_b = GPIO.PWM(RGB_B, 500)
    _pwm_r.start(0); _pwm_g.start(0); _pwm_b.start(0)
    for _ in range(3):
        _pwm_b.ChangeDutyCycle(20); sleep(0.12)
        _pwm_b.ChangeDutyCycle(0);  sleep(0.12)


_led_lock = threading.Lock()

def blink_rgb(r, g, b, duration=0.05, bypass=False):
    with _led_lock:
        try:
            f = settings.led_factor(bypass)
            _pwm_r.ChangeDutyCycle(max(0, min(100, round(r * f))))
            _pwm_g.ChangeDutyCycle(max(0, min(100, round(g * f))))
            _pwm_b.ChangeDutyCycle(max(0, min(100, round(b * f))))
            sleep(duration)
            _pwm_r.ChangeDutyCycle(0); _pwm_g.ChangeDutyCycle(0); _pwm_b.ChangeDutyCycle(0)
        except Exception:
            pass


def is_wifi_up() -> bool:
    try:
        with open("/sys/class/net/wlan0/operstate") as f:
            return f.read().strip() == "up"
    except Exception:
        return False


def heartbeat_loop() -> None:
    sleep(1)
    while True:
        blink_rgb(0, 5, 0, 0.05) if is_wifi_up() else blink_rgb(5, 0, 8, 0.3, bypass=True)
        sleep(0.5)
        if time.time() - last_frame_time <= TIC_HEALTH_TIMEOUT_S:
            blink_rgb(0, 5, 0, 0.05)
        else:
            blink_rgb(8, 2, 0, 0.3, bypass=True)
        sleep(RECEPTION_TIMEOUT_S)


# --------------------------------------------------------------------------- #
# État silence (persisté)                                                      #
# --------------------------------------------------------------------------- #
def load_silence_state():
    global last_frame_time, silence_restarts
    try:
        with open(SILENCE_STATE_PATH) as f:
            d = json.load(f)
        last_frame_time = float(d.get("last_frame_time", 0) or 0)
        silence_restarts = int(d.get("silence_restart_count", 0) or 0)
    except Exception:
        pass


def save_silence_state():
    try:
        os.makedirs(os.path.dirname(SILENCE_STATE_PATH), exist_ok=True)
        tmp = SILENCE_STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"last_frame_time": last_frame_time,
                       "silence_restart_count": silence_restarts}, f)
        os.replace(tmp, SILENCE_STATE_PATH)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# systemd + self-test radio                                                    #
# --------------------------------------------------------------------------- #
def sd_notify(msg: str) -> None:
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr[0] == "@":
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr); s.sendall(msg.encode())
    except Exception:
        pass


def kernel_died() -> bool:
    try:
        return bool(int(open("/proc/sys/kernel/tainted").read()) & TAINT_DIE)
    except Exception:
        return False


def radio_alive() -> bool:
    if kernel_died() or not lora_ok or lora is None:
        return False
    try:
        with radio_lock:
            return lora._spi_read(SX127X_REG_VERSION) == SX127X_VERSION
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# RX : callback raspi_lora → publie sur MQTT                                   #
# --------------------------------------------------------------------------- #
def on_recv(payload) -> None:
    global last_frame_time, silence_restarts
    try:
        raw = bytes(payload.message)
        rssi = getattr(payload, "rssi", 0)
        snr = getattr(payload, "snr", 0)
        sender = getattr(payload, "header_from", 0)
    except Exception:
        log.exception("on_recv : payload illisible")
        return

    last_frame_time = time.time()
    silence_restarts = 0
    save_silence_state()
    blink_rgb(8, 8, 0, 0.03)   # flash RF (radio-level) : "trame reçue"

    if not (RSSI_MIN_PLAUSIBLE <= rssi <= RSSI_MAX_PLAUSIBLE and SNR_MIN_PLAUSIBLE <= snr <= SNR_MAX_PLAUSIBLE):
        log.warning(f"signal invraisemblable rssi={rssi} snr={snr} (bruit ?)")

    # secure-link MONTANT : clé DÉRIVÉE du device émetteur → vérifie MAC + déchiffre → bus en CLAIR.
    key = device_key(int(sender))
    if key is None:
        log.warning(f"device émetteur inconnu 0x{int(sender):02x} — trame ignorée")
        return
    try:
        clear = frame_codec.open_frame(raw, key)
    except frame_codec.FrameError as e:
        log.warning(f"trame rejetée à la façade (MAC/format) de 0x{int(sender):02x} : {e}")
        blink_rgb(30, 0, 0, 0.2, bypass=True)   # rouge = rejet MAC
        return

    # MAC montant OK → si c'est un BOOT, prouver à l'émetteur qu'on détient SA clé (anti cross-talk
    # multi-logement). Émis AVANT le publish (fenêtre d'écoute émetteur ~800 ms). Seul le BOOT attend
    # cet ACK ; les trames courbe (0x05) non → on ne répond qu'au BOOT.
    if clear and (clear[0] & 0x7F) == frame_codec.TYPE_BOOT:
        try:
            send_app_ack(int(sender), clear, key)
        except Exception as e:
            log.error(f"ACK applicatif boot 0x{int(sender):02x} échec : {e}")

    if mqttc is not None:
        mqttc.publish(TOPIC_RX, json.dumps({
            "from": int(sender), "rssi": float(rssi), "snr": float(snr), "hex": clear.hex(),
        }), qos=0)


# --------------------------------------------------------------------------- #
# MQTT : TX (ben/lora/tx) + LED (ben/led)                                 #
# --------------------------------------------------------------------------- #
def on_mqtt_message(client, userdata, msg) -> None:
    try:
        d = json.loads(msg.payload.decode())
    except Exception:
        log.warning(f"MQTT {msg.topic} : payload JSON invalide")
        return

    if msg.topic == TOPIC_TX:
        try:
            to = int(d["to"]); body = bytes.fromhex(d["body"])   # corps commande EN CLAIR
        except Exception:
            log.warning("ben/lora/tx : {to, body(hex clair)} attendu")
            return
        key = device_key(to)                                 # clé DÉRIVÉE du device destinataire
        if key is None:
            log.error(f"TX ignoré : device inconnu 0x{to:02x}")
            return
        if not lora_ok:
            log.error("TX ignoré : radio KO")
            return
        counter = next_tx_counter(to)
        frame = secure_link.seal_command(key, counter, body)     # chiffre + MAC ICI (façade)
        global _tx_hid
        _tx_hid = (_tx_hid + 1) & 0xFF                            # header_id distinct → pas de dédup RadioHead
        rtt = None
        with radio_lock:
            try:
                rtt = send_acked(to, frame, _tx_hid)   # fiable ET rapide (~130 ms), retry si perte RF
            except Exception as e:
                log.error(f"TX échec : {e}")
        ack_s = f"ACK @ {rtt*1000:.0f} ms" if rtt is not None else "PAS d'ACK (3 essais → perte RF)"
        log.info(f"TX → 0x{to:02x} cmd={body.hex()} cnt={counter} hid={_tx_hid} : {ack_s}")

    elif msg.topic == TOPIC_LED:
        blink_rgb(int(d.get("r", 0)), int(d.get("g", 0)), int(d.get("b", 0)),
                  float(d.get("duration", 0.05)), bool(d.get("bypass", False)))


def on_mqtt_connect(client, userdata, flags, *args) -> None:
    client.subscribe([(TOPIC_TX, 1), (TOPIC_LED, 0)])
    log.info(f"MQTT connecté ({MQTT_HOST}:{MQTT_PORT}), abonné {TOPIC_TX} + {TOPIC_LED}")


def mqtt_start():
    global mqttc
    try:
        mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="ben-radio")
    except (AttributeError, TypeError):
        mqttc = mqtt.Client(client_id="ben-radio")     # paho 1.x
    mqttc.on_connect = on_mqtt_connect
    mqttc.on_message = on_mqtt_message
    # connect_async + loop_start : retente tout seul si mosquitto n'est pas encore up au démarrage
    # (la radio ne doit pas dépendre de l'ordre de boot du broker). Reconnexion auto ensuite.
    mqttc.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
    mqttc.loop_start()


# --------------------------------------------------------------------------- #
# Main                                                                         #
# --------------------------------------------------------------------------- #
def main():
    global lora, lora_ok, silence_restarts
    setup_led()
    load_silence_state()
    load_tx_counters()
    mqtt_start()

    GPIO.setup(RFM95_RST_PIN, GPIO.OUT)
    GPIO.output(RFM95_RST_PIN, GPIO.HIGH); sleep(0.01)
    GPIO.output(RFM95_RST_PIN, GPIO.LOW);  sleep(0.01)
    GPIO.output(RFM95_RST_PIN, GPIO.HIGH); sleep(0.05)

    try:
        lora = LoRa(0, LORA_INTERRUPT_PIN, SERVER_ADDRESS,
                    modem_config=ModemConfig.Bw125Cr45Sf128,
                    tx_power=RF95_POW, acks=True, freq=RF95_FREQ)
        lora._spi_write(0x1D, 0x72); lora._spi_write(0x1E, 0x94); lora._spi_write(0x26, 0x00)
        lora.on_recv = on_recv
        lora.set_mode_rx()
        lora_ok = True
        log.info(f"LoRa addr={SERVER_ADDRESS} freq={RF95_FREQ}MHz — façade radio prête")
    except Exception as e:
        log.error(f"LoRa init FAILED : {e}")
        for _ in range(3):
            blink_rgb(30, 0, 0, 0.5, bypass=True); sleep(0.3)

    Thread(target=heartbeat_loop, daemon=True, name="heartbeat").start()
    sd_notify("READY=1")

    radio_fails = 0
    try:
        while True:
            now = time.time()
            if last_frame_time > 0:
                threshold = min(SILENCE_RESTART_BASE_S * (2 ** min(silence_restarts, SILENCE_BACKOFF_EXP_MAX)),
                                SILENCE_RESTART_MAX_S)
                if now - last_frame_time > threshold and now - RECEIVER_BOOT_TIME > threshold:
                    silence_restarts += 1
                    save_silence_state()
                    log.error(f"SILENCE LoRa {int(now-last_frame_time)}s > {int(threshold)}s → restart "
                              f"(backoff niveau {silence_restarts})")
                    blink_rgb(30, 8, 0, 0.3, bypass=True)
                    sd_notify("STOPPING=1")
                    sys.exit(1)
            if radio_alive():
                radio_fails = 0
                sd_notify("WATCHDOG=1")
            else:
                radio_fails += 1
                if radio_fails < RADIO_FAIL_MAX:
                    sd_notify("WATCHDOG=1")
                    log.warning(f"self-test radio KO ({radio_fails}/{RADIO_FAIL_MAX})")
                else:
                    log.error("radio figée → arrêt du ping watchdog → systemd restart")
                    blink_rgb(30, 0, 0, 0.3, bypass=True)
            sleep(WATCHDOG_PING_S)
    except KeyboardInterrupt:
        log.info("Arrêt.")
    finally:
        try:
            if lora:
                lora.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
