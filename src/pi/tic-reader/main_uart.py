"""
tic-reader — BEN Pi wired TIC reader (bi-mode : historique + standard)

Lit la TIC Linky sur l'UART et décode les deux modes Enedis (cf.
Enedis-NOI-CPT_54E v3, §5.2/5.3 et §6) :
  - HISTORIQUE : 1200 baud, séparateur SP (0x20), étiquettes courtes
    (ADCO/BASE/PAPP/IINST/PTEC…), checksum SP de queue EXCLU.
  - STANDARD   : 9600 baud, séparateur HT (0x09), étiquettes longues
    (ADSC/SINSTS/IRMS1/EAST/EASFxx/NTARF…), groupes parfois horodatés,
    checksum HT de queue INCLUS.

Le mode est **auto-détecté au boot** (on sonde chaque débit et on compte les
groupes au checksum valide ; le mode persisté est testé en premier → reboot
rapide). Le compteur sort d'usine en historique ; Enedis le reprogramme en
standard. Si Enedis rebascule le mode, le watchdog relance le process et la
détection re-sonde les deux débits.

v1 : aucun sink (pas d'InfluxDB). Le store SQLite local alimente l'API locale.

Aligné sur src/arduino/tic-reader/tic-reader.ino :
  - même mapping PTEC → index (selectActiveIndex), mode historique
  - même détection DEMAIN / ADPS / PEJP (buildFlags)

Note parité : on lit en 8N1 et on masque le bit de parité (& 0x7F) dans les deux
modes — la TIC est 7E1, le masque suffit (le checksum TIC couvre l'intégrité).
Marche sur mini-UART (ttyS0, sans parité matérielle) comme sur PL011.

Stockage (chantier index bi-mode, docs/chantier-index-energie-bimode.md) : on
remplit les colonnes GÉNÉRIQUES de measurements — (src_standard, index_id,
index_value) + inject_total (EAIT) + meter_ts (horodate compteur). En historique
index_id = rang PTEC ; en standard index_id = NTARF, index_value = EASF[NTARF].
papp (←SINSTS, net signé en standard) + iinst (←IRMS1) restent stockés pour la
courbe + la jauge ; base/hchc/hchp en double-écriture (compat app pas-à-jour).
PREF (kVA) ≠ ISOUSC (A) → pas mappé dans la jauge (chantier ISOUSC standard).

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
UART_BAUD_HISTO   = 1200    # mode historique
UART_BAUD_STD     = 9600    # mode standard
TIC_TIMEOUT_S     = 12      # max pour lire une trame complète (~4s à 1200 baud)

# Auto-détection du mode au boot : on sonde un débit pendant DETECT_WINDOW_S et
# on compte les groupes au checksum valide. Au mauvais débit on lit du bruit →
# ~0 groupe valide (un faux positif checksum est à ~1/64 par ligne) ; le seuil
# rend la confusion négligeable. La fenêtre couvre plusieurs trames aux deux
# débits (~1 s/trame en standard, ~1,7 s en historique).
DETECT_WINDOW_S    = 6
DETECT_MIN_GROUPS  = 5

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
    """Init pins LED + PWM (~500 Hz) + 3 flashs bleus de boot (signe « démarré »)."""
    global _pwm_r, _pwm_g, _pwm_b
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    for pin in (RGB_R, RGB_G, RGB_B):
        GPIO.setup(pin, GPIO.OUT)
        GPIO.output(pin, GPIO.LOW)
    _pwm_r = GPIO.PWM(RGB_R, 500); _pwm_r.start(0)
    _pwm_g = GPIO.PWM(RGB_G, 500); _pwm_g.start(0)
    _pwm_b = GPIO.PWM(RGB_B, 500); _pwm_b.start(0)
    # Blink de boot : 3 flashs bleus brefs (idem récepteur LoRa, modèles alignés).
    for _ in range(3):
        _pwm_b.ChangeDutyCycle(20); time.sleep(0.12)
        _pwm_b.ChangeDutyCycle(0);  time.sleep(0.12)

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
# Protocole TIC (commun aux deux modes — cf. Enedis-NOI-CPT_54E §5.3.6)
# ---------------------------------------------------------------------------
STX = 0x02
ETX = 0x03
LF  = 0x0A
CR  = 0x0D
HT  = 0x09  # séparateur de champ en mode STANDARD (SP 0x20 en historique)

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

# Étiquettes du mode STANDARD (Enedis-NOI-CPT_54E §6.2.2). On ne garde que
# celles utiles à BEN ; on mappe vers les mêmes clés génériques que l'historique
# (PAPP/IINST/ADCO) pour que le stockage et l'API existants marchent tels quels.
STD_EASF_LABELS = {f"EASF{i:02d}" for i in range(1, 11)}  # EASF01..EASF10

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
    elif name == "ISOUSC":
        # Intensité souscrite (abonnement, A) — statique. Chantier ISOUSC :
        # sert à l'étalonnage de la jauge (maxVa = ISOUSC×230).
        try:
            out["ISOUSC"] = int(value)
        except ValueError:
            pass
    elif name == "ADCO":
        out["ADCO"] = value.strip()
    elif name in ("ADPS", "PEJP"):
        out[name] = True


def tic_checksum_std_ok(line: str) -> bool:
    """Vérifie le checksum TIC en mode STANDARD (Enedis-NOI-CPT_54E §5.3.6).

    `line` = contenu entre LF et CR : "ETIQ <HT> [HORODATE <HT>] DONNEE <HT> CK".
    Le checksum couvre tous les caractères de l'étiquette jusqu'au HT séparateur
    AVANT le checksum, **HT de queue inclus** (≠ historique où le SP est exclu).
    """
    if len(line) < 3 or line[-2] != "\t":
        return False
    total = sum(ord(c) for c in line[:-1])  # tout sauf le checksum → HT de queue inclus
    return chr((total & 0x3F) + 0x20) == line[-1]


def _std_int(value: str) -> int | None:
    """Convertit une donnée standard (zéros d'en-tête possibles) en int, ou None."""
    try:
        return int(value)
    except ValueError:
        return None


def _parse_label_std(line: str, out: dict) -> None:
    """Extrait label/donnée d'une ligne TIC STANDARD validée et la mappe vers les
    clés génériques (PAPP/IINST/ADCO…) + garde les champs standard spécifiques.

    Groupe à 7 ou 9 parties (Enedis §5.3.6) : après split sur HT on a
    [étiquette, donnée, checksum] (3) ou [étiquette, horodate, donnée, checksum]
    (4). La donnée est donc toujours l'avant-dernier champ ; l'horodate (si
    présente) le 2e. On déduit l'horodatage du nombre de champs — rien à coder en
    dur sur quelles étiquettes sont horodatées.
    """
    parts = line.split("\t")
    if len(parts) < 3:
        return
    name = parts[0]
    data = parts[-2]            # checksum = parts[-1] (déjà validé), donnée = parts[-2]

    if name == "ADSC":                       # adresse compteur ≈ ADCO historique
        out["ADCO"] = data.strip()
    elif name == "SINSTS":                    # puiss. app. instantanée soutirée (VA) ≈ PAPP
        v = _std_int(data)
        if v is not None:
            out["PAPP"] = v
    elif name == "IRMS1":                     # courant efficace phase 1 (A) ≈ IINST
        v = _std_int(data)
        if v is not None:
            out["IINST"] = v
    # --- Champs standard spécifiques : parsés + loggés, stockage différé ------
    # (chantier index bi-mode : docs/chantier-index-energie-bimode.md)
    elif name == "EAST":                      # énergie active soutirée totale (Wh)
        out["EAST"] = _std_int(data)
    elif name in STD_EASF_LABELS:             # index fournisseur EASF01..10 (Wh)
        out.setdefault("EASF", {})[name] = _std_int(data)
    elif name == "NTARF":                     # n° index tarifaire en cours (1..10)
        out["NTARF"] = _std_int(data)
    elif name == "SINSTI":                    # puiss. app. instantanée injectée (VA)
        out["SINSTI"] = _std_int(data)
    elif name == "EAIT":                      # énergie active injectée totale (Wh)
        out["EAIT"] = _std_int(data)
    elif name == "PREF":                      # puiss. app. de référence (kVA) ≈ ISOUSC (autre unité)
        out["PREF"] = _std_int(data)
    elif name == "LTARF":                     # libellé tarif fournisseur en cours
        out["LTARF"] = data.strip()
    elif name == "VTIC":                      # version de la TIC (« 02 »)
        out["VTIC"] = data.strip()
    elif name == "DATE":                      # horodatée, donnée vide → horodate = 2e champ
        if len(parts) >= 4:                   # "SAAMMJJhhmmss" (saison + 12 chiffres)
            out["DATE_HORODATE"] = parts[1]


def _std_horodate_to_epoch(h: str | None) -> int | None:
    """Horodate compteur standard 'SAAMMJJhhmmss' → epoch UTC, ou None si absente/dégradée.
    Le compteur DIT la saison (E=été UTC+2 / H=hiver UTC+1) → conversion locale→UTC sans
    base de fuseaux. Saison minuscule (e/h) = horloge dégradée → None (pas fiable)."""
    if not h or len(h) < 13 or h[0] not in ("E", "H"):
        return None
    try:
        yy, mo, da = int(h[1:3]), int(h[3:5]), int(h[5:7])
        hh, mi, se = int(h[7:9]), int(h[9:11]), int(h[11:13])
        offset = 2 if h[0] == "E" else 1
        import calendar
        return calendar.timegm((2000 + yy, mo, da, hh, mi, se, 0, 0, 0)) - offset * 3600
    except (ValueError, OverflowError):
        return None


def read_frame(ser: serial.Serial, checksum_ok, parse_label) -> dict | None:
    """
    Lit une trame TIC complète (STX..ETX), mode-agnostique.
    `checksum_ok(line)` valide la ligne ; `parse_label(line, labels)` la décode.
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
                if checksum_ok(line):
                    parse_label(line, labels)
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
def select_active_index(ptec: str, labels: dict) -> tuple[int | None, str | None, int | None]:
    """Miroir de selectActiveIndex() tic-reader.ino — sélectionne l'index selon PTEC.

    Retourne (id, name, value) : id = rang PTEC_MAP (0x00..0x0A, pour index_id générique).
    (None, None, None) si PTEC inconnu ; (id, name, None) si l'étiquette index est absente
    du dict (checksum KO ou timeout).
    """
    p = ptec.strip()
    for prefix, n, _id, name in PTEC_MAP:
        if p[:n] == prefix:
            return _id, name, labels.get(name)  # value None si etiquette non vue
    return None, None, None


def build_flags(labels: dict) -> tuple[str | None, bool, bool]:
    """Miroir de buildFlags() tic-reader.ino — retourne (demain, adps, pejp)."""
    demain = DEMAIN_NAMES.get(labels.get("DEMAIN", ""))
    adps   = bool(labels.get("ADPS", False))
    pejp   = bool(labels.get("PEJP", False))
    return demain, adps, pejp

# ---------------------------------------------------------------------------
# Modes TIC + auto-détection
# ---------------------------------------------------------------------------
# Un descripteur par mode : débit + fonctions de validation/décodage.
MODES = {
    "historique": dict(baud=UART_BAUD_HISTO, checksum=tic_checksum_ok,     parse=_parse_label),
    "standard":   dict(baud=UART_BAUD_STD,   checksum=tic_checksum_std_ok, parse=_parse_label_std),
}


def open_serial(baud: int) -> serial.Serial:
    """Ouvre l'UART au débit donné, 8N1 + masque 0x7F (la TIC est 7E1)."""
    return serial.Serial(
        port=UART_DEV,
        baudrate=baud,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=1,
    )


def count_valid_groups(ser: serial.Serial, checksum_ok, window_s: float) -> int:
    """Compte les groupes (lignes LF..CR) au checksum valide pendant window_s.

    Au mauvais débit on lit du bruit → quasi aucun checksum ne passe. Sert de
    discriminant de mode sans parser ni décoder (juste valider l'intégrité)."""
    deadline = time.time() + window_s
    current = bytearray()
    in_line = False
    valid = 0
    while time.time() < deadline:
        raw = ser.read(1)
        if not raw:
            continue
        b = raw[0] & 0x7F
        if b == LF:
            current = bytearray()
            in_line = True
        elif b == CR:
            if in_line and current:
                if checksum_ok(current.decode("ascii", errors="replace")):
                    valid += 1
            in_line = False
        elif b in (STX, ETX):
            in_line = False
        elif in_line:
            current.append(b)
    return valid


def detect_mode(persisted: str | None) -> str | None:
    """Sonde les débits et renvoie le nom du mode détecté ('historique' /
    'standard'), ou None si aucun. Le mode persisté est sondé en premier (reboot
    rapide). Au mauvais débit → < DETECT_MIN_GROUPS groupes valides → on bascule.
    """
    order = ["historique", "standard"]
    if persisted in MODES:
        order = [persisted] + [m for m in order if m != persisted]

    for name in order:
        m = MODES[name]
        ser = open_serial(m["baud"])
        try:
            n = count_valid_groups(ser, m["checksum"], DETECT_WINDOW_S)
        finally:
            ser.close()
        log.info(f"détection mode : {name} ({m['baud']} baud) → {n} groupes valides "
                 f"(seuil {DETECT_MIN_GROUPS})")
        if n >= DETECT_MIN_GROUPS:
            return name
    return None

# ---------------------------------------------------------------------------
# État persistant
# ---------------------------------------------------------------------------
def load_state() -> dict:
    try:
        with open(STATE_PATH) as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        raw = {}
    mode = raw.get("mode")
    return {
        "adco": raw.get("adco", ""),
        "mode": mode if mode in MODES else None,  # dernier mode auto-détecté
    }


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

# Auto-détection du mode (historique 1200 / standard 9600). Le mode persisté est
# sondé en premier. Si rien n'est détecté (compteur muet au boot, NTP/TIC pas
# encore là), on retombe sur le mode persisté ou, à défaut, historique (sortie
# d'usine) — et le watchdog relancera la détection si aucune trame ne vient.
detected = detect_mode(state.get("mode"))
if detected is None:
    mode_name = state.get("mode") or "historique"
    log.warning(f"Aucun mode détecté au boot — repli sur '{mode_name}' "
                f"(le watchdog re-sondera si rien ne vient)")
else:
    mode_name = detected
    if mode_name != state.get("mode"):
        log.info(f"Mode TIC : {mode_name} (changement vs persisté={state.get('mode') or 'aucun'})")
        state["mode"] = mode_name
        save_state(state)

mode = MODES[mode_name]
checksum_ok = mode["checksum"]
parse_label = mode["parse"]
is_standard = mode_name == "standard"

ser = open_serial(mode["baud"])
log.info(f"Série ouvert : {UART_DEV} {mode['baud']} 8N1 (masque 0x7F) proto TIC {mode_name}")
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
last_isousc: int | None = None  # garde RAM : record_isousc seulement sur changement
last_pref: int | None = None    # garde RAM : record_pref (abonnement standard, kVA) sur changement


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

# En mode standard, on logge la PREMIÈRE trame valide en INFO (tous les champs
# parsés) pour valider le décodage sur un vrai compteur standard ; ensuite on
# repasse en DEBUG (cadence ~1 s, ne pas noyer journald — cf. preshipping).
std_first_logged = False

try:
    while True:
        frame_ok = False
        try:
            # Au fil de l'eau : read_frame se cale sur la cadence du compteur.
            # PAS de reset_input_buffer (on lit le flux en continu) ni de sleep
            # (la trame suivante nous attend déjà dans le port).
            labels = read_frame(ser, checksum_ok, parse_label)

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

                # ISOUSC (abonnement) — écrit SUR CHANGEMENT seulement (garde RAM
                # + record_isousc fait aussi sa garde DB). Indépendant de la
                # validité PTEC/PAPP de la trame.
                isousc = labels.get("ISOUSC")
                if (isousc is not None and isousc != last_isousc
                        and measurements_db is not None):
                    if db.record_isousc(measurements_db, PDL_INDEX, isousc):
                        log.info(f"ISOUSC={isousc} A enregistré (maxVa≈{isousc * 230} VA)")
                    last_isousc = isousc

                # PREF (abonnement en mode STANDARD, kVA) — record-on-change (chantier ISOUSC std).
                # Le standard ne donne pas ISOUSC ; PREF×1000 calibre la jauge (arbitré par /live).
                pref = labels.get("PREF")
                if (pref is not None and pref != last_pref
                        and measurements_db is not None):
                    if db.record_pref(measurements_db, PDL_INDEX, pref):
                        log.info(f"PREF={pref} kVA enregistré (maxVa≈{pref * 1000} VA)")
                    last_pref = pref

                iinst  = labels.get("IINST")
                papp   = labels.get("PAPP")

                if not is_standard:
                    # --- Mode HISTORIQUE : index actif déduit de PTEC ----------
                    ptec = labels.get("PTEC")
                    active_id = active_name = active_value = None
                    if ptec:
                        active_id, active_name, active_value = select_active_index(ptec, labels)

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
                        # Clé générique (chantier index bi-mode) : index_id = rang PTEC.
                        labels["_src_standard"] = 0
                        labels["_index_id"] = active_id
                        labels["_index_value"] = active_value
                        log.debug(f"OK pdl_index={PDL_INDEX} PTEC={ptec} {active_name}={active_value} "
                                  f"IINST={iinst} PAPP={papp} demain={demain} adps={adps} pejp={pejp}")
                        # On EMPILE dans le batch ; l'écriture BDD se fait par lot
                        # (flush plus bas, volet B). Log en DEBUG : à ~1,7 s/trame, un
                        # INFO par trame noierait journald (cf. preshipping).
                        if measurements_db is not None:
                            batch.append((PDL_INDEX, labels, int(time.time())))
                        frame_ok = True
                else:
                    # --- Mode STANDARD : papp←SINSTS, iinst←IRMS1 --------------
                    # Stockage GÉNÉRIQUE (chantier index bi-mode) : (src_standard=1,
                    # index_id=NTARF, index_value=EASF[NTARF]) + inject_total=EAIT +
                    # meter_ts (horodate compteur). papp+iinst aussi (courbe + jauge).
                    # PREF (kVA) ≠ ISOUSC (A) → pas mappé dans la jauge (maxVa fausse).
                    ntarf = labels.get("NTARF")
                    east  = labels.get("EAST")
                    easf  = labels.get("EASF", {})
                    active_value = easf.get(f"EASF{ntarf:02d}") if ntarf else None

                    if papp is None:
                        log.warning("SINSTS absent de la trame standard (checksum KO?) — trame ignorée")
                    elif iinst is None:
                        log.warning("IRMS1 absent de la trame standard (checksum KO?) — trame ignorée")
                    else:
                        # _src_standard TOUJOURS posé (c'est le mode → tic_mode/lecture papp).
                        # index_id/index_value seulement si l'index actif a été vu (sinon NULL
                        # sur cette ligne → carry-forward au calcul conso, pas de point perdu).
                        labels["_src_standard"] = 1
                        if ntarf is not None and active_value is not None:
                            labels["_index_id"] = ntarf
                            labels["_index_value"] = active_value
                        if labels.get("EAIT") is not None:
                            labels["_inject_total"] = labels["EAIT"]
                        mts = _std_horodate_to_epoch(labels.get("DATE_HORODATE"))
                        if mts is not None:
                            labels["_meter_ts"] = mts
                        if not std_first_logged:
                            # 1re trame valide : dump complet en INFO pour valider
                            # le décodage sur un vrai compteur standard.
                            log.info("Première trame STANDARD décodée : "
                                     f"ADSC={labels.get('ADCO')} VTIC={labels.get('VTIC')} "
                                     f"SINSTS={papp} IRMS1={iinst} EAST={east} NTARF={ntarf} "
                                     f"EASF[{ntarf}]={active_value} LTARF={labels.get('LTARF')!r} "
                                     f"PREF={labels.get('PREF')} SINSTI={labels.get('SINSTI')} "
                                     f"EAIT={labels.get('EAIT')}")
                            std_first_logged = True
                        log.debug(f"OK[std] pdl_index={PDL_INDEX} SINSTS(PAPP)={papp} "
                                  f"IRMS1(IINST)={iinst} EAST={east} NTARF={ntarf} "
                                  f"EASF[{ntarf}]={active_value} SINSTI={labels.get('SINSTI')}")
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
