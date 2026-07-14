"""
ble-provisioner — BEN Pi BLE WiFi provisioning agent (POC niveau 3)

Expose un service GATT BLE permettant à l'app (ou au simulateur Mac) de
configurer le WiFi du device sans contact physique.

Service GATT (UUIDs custom BEN — préfixe `b3e7e511-...`)
  Service       b3e7e511-0001-4bea-9b15-000000000000
  WIFI_CONFIG   …001  write          JSON {ssid, password} — REFUSÉ tant que non vérifié
  STATUS        …002  read|notify    texte: idle|configuring|connecting|connected|failed:<raison>
  WIFI_SCAN     …003  read           JSON liste des SSID 2.4 GHz visibles (cache rafraîchi /30s)
  DEVICE_INFO   …004  read           JSON {deviceId, model, hardwareRevision, softwareVersion}
  VERIFY        …005  write          code couleur deviné (3 lettres parmi BYWR)
  VERIFY_STATUS …006  read|notify    texte: pending|verified|wrong|locked
  PREVIEW_CMD   …007  write          "1" = jouer les 4 couleurs en boucle (apprentissage), "0" = stop → test
  PREVIEW_COLOR …008  read|notify    couleur affichée pendant l'apprentissage: B|Y|W|R|- (- = noir)

Vérification couleur (association) : à la connexion BLE, le device affiche un code de 3
couleurs sur sa LED ; l'app le renvoie via VERIFY ; tant que VERIFY_STATUS != verified,
WIFI_CONFIG est refusé (failed:not_verified). Cf. docs/ble-color-verification.md.

Identité BLE : nom advertising = deviceId (lu dans /etc/ben-firmware/device.json,
fallback `ben-poc01`).

⚠ Pairing/chiffrement BLE — REPORTÉ POUR LE POC :
  Le flag `encrypt-write` (qui impose une connexion chiffrée Just Works) fonctionne
  côté BlueZ mais ne déclenche PAS le pairing côté Mac (Core Bluetooth n'expose pas
  l'API de pairing à `bleak`, on récupère un `Insufficient Encryption`).
  → Le POC utilise `write` simple. Le password WiFi transite en clair sur l'air BLE.
  → En prod, on réactivera `encrypt-write` quand l'app Flutter (qui gère le pairing
    via `flutter_blue_plus` sur iOS et Android) sera disponible.
  L'adaptateur reste `Pairable=True` pour faciliter cette future bascule.

Lance la configuration WiFi via NetworkManager (`nmcli`) dans un thread séparé
pour ne pas bloquer le callback GATT.
"""

import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import time

from bluezero import adapter, peripheral

import led
from wifi_config import configure_wifi

GRACE_PERIOD_AFTER_SUCCESS_SEC = 5  # LED verte fixe avant reboot, le temps de voir le signal

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("ble-provisioner")

# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
DEVICE_JSON = "/etc/ben-firmware/device.json"
FALLBACK_DEVICE_ID = "ben-poc01"


def read_device_id() -> str:
    try:
        with open(DEVICE_JSON, "r") as f:
            return json.load(f)["deviceId"]
    except (FileNotFoundError, KeyError, json.JSONDecodeError) as e:
        log.warning("device.json indisponible (%s) — fallback %s", e, FALLBACK_DEVICE_ID)
        return FALLBACK_DEVICE_ID


def read_device_info() -> dict:
    try:
        with open(DEVICE_JSON, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        log.warning("device.json indisponible (%s)", e)
        return {"deviceId": FALLBACK_DEVICE_ID}


# ---------------------------------------------------------------------------
# GATT UUIDs
# ---------------------------------------------------------------------------
SERVICE_UUID     = "b3e7e511-0001-4bea-9b15-000000000000"
WIFI_CONFIG_UUID = "b3e7e511-0001-4bea-9b15-000000000001"
STATUS_UUID      = "b3e7e511-0001-4bea-9b15-000000000002"
WIFI_SCAN_UUID   = "b3e7e511-0001-4bea-9b15-000000000003"
DEVICE_INFO_UUID = "b3e7e511-0001-4bea-9b15-000000000004"
VERIFY_UUID         = "b3e7e511-0001-4bea-9b15-000000000005"
VERIFY_STATUS_UUID  = "b3e7e511-0001-4bea-9b15-000000000006"
PREVIEW_CMD_UUID    = "b3e7e511-0001-4bea-9b15-000000000007"
PREVIEW_COLOR_UUID  = "b3e7e511-0001-4bea-9b15-000000000008"

# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------
STATE_IDLE        = "idle"
STATE_CONFIGURING = "configuring"
STATE_CONNECTING  = "connecting"
STATE_CONNECTED   = "connected"
# failed:<raison>

_state = STATE_IDLE
_status_char = None  # set after publish()
_provisioning_succeeded = False  # passe à True dès qu'on atteint STATE_CONNECTED


# ---------------------------------------------------------------------------
# Vérification couleur (association : confirmer le bon boîtier)
# Cf. docs/ble-color-verification.md
# ---------------------------------------------------------------------------
VERIFY_TOKENS       = "BYWR"   # palette daltonien (cf. led.VERIFY_PALETTE)
VERIFY_CODE_LEN     = 3
VERIFY_MAX_ATTEMPTS = 5
VERIFY_COOLDOWN_SEC = 30
VERIFY_DISPLAY_DELAY_SEC = 10   # délai avant d'afficher le code couleur à la connexion BLE
# Délai (LED éteinte) entre la fin de l'apprentissage (« Suivant ») et le
# démarrage du code de test → laisse le temps d'arriver sur l'écran de test et
# de ne pas rater le début de la séquence.
VERIFY_AFTER_PREVIEW_SEC = 3.0
# Durée d'affichage de chaque couleur du CODE de test (allongée pour laisser le
# temps de lire ; séparées par un noir court).
VERIFY_ON_SEC       = 1.3

# Phase d'apprentissage des couleurs (avant le test) : on joue les 4 couleurs
# dans un ORDRE FIXE, en boucle, et on notifie la couleur courante (synchro app).
PREVIEW_ORDER       = "BYWR"
PREVIEW_ON_SEC      = 1.3
PREVIEW_GAP_SEC     = 0.45
PREVIEW_LOOP_GAP_SEC = 0.9
_preview_color_char = None  # set after publish()
_preview_active = False
_test_displayed = False   # le code de test est-il déjà à l'écran ? (anti-double)

VS_PENDING  = "pending"
VS_VERIFIED = "verified"
VS_WRONG    = "wrong"
VS_LOCKED   = "locked"

_verify_code = ""
_verified = False
_verify_attempts = 0
_verify_status = VS_PENDING
_verify_status_char = None  # set after publish()


def _new_verify_code() -> None:
    """Génère un nouveau code couleur et lance son affichage LED en boucle."""
    global _verify_code, _test_displayed
    _verify_code = "".join(secrets.choice(VERIFY_TOKENS) for _ in range(VERIFY_CODE_LEN))
    log.info("code couleur: %s", _verify_code)
    _test_displayed = True
    try:
        colors = [led.VERIFY_PALETTE[t] for t in _verify_code]
        led.start_sequence(colors, on_sec=VERIFY_ON_SEC)
    except Exception as e:
        log.warning("affichage séquence LED impossible: %s", e)


# ---------------------------------------------------------------------------
# Apprentissage des couleurs (phase avant le test) — joue les 4 couleurs dans un
# ordre fixe en boucle, et notifie la couleur courante pour synchroniser l'app.
# ---------------------------------------------------------------------------
def _notify_preview_color(token) -> None:
    """Appelé par led._sequence_loop : pousse la couleur affichée (B|Y|W|R) ou
    '-' (noir) sur PREVIEW_COLOR → l'app surligne la pastille correspondante."""
    if _preview_color_char is None:
        return
    value = token if token else "-"
    try:
        _preview_color_char.set_value(value.encode("utf-8"))
    except Exception as e:
        log.warning("notify preview color failed: %s", e)


def on_preview_color_read() -> bytes:
    return b"-"


def on_preview_cmd_write(value, options):
    """PREVIEW_CMD : '1' = apprentissage (4 couleurs en boucle, synchro), '0' =
    fin → affiche le code de test."""
    global _preview_active
    try:
        cmd = bytes(value).decode("utf-8", "ignore").strip()
    except Exception:
        return
    if cmd == "1":
        _preview_active = True
        log.info("apprentissage couleurs: démarrage (ordre %s)", PREVIEW_ORDER)
        try:
            colors = [led.VERIFY_PALETTE[t] for t in PREVIEW_ORDER]
            led.start_sequence(
                colors,
                on_sec=PREVIEW_ON_SEC, gap_sec=PREVIEW_GAP_SEC,
                loop_gap_sec=PREVIEW_LOOP_GAP_SEC,
                tokens=list(PREVIEW_ORDER), on_show=_notify_preview_color)
        except Exception as e:
            log.warning("apprentissage couleurs impossible: %s", e)
    elif cmd == "0":
        if _preview_active:
            log.info("apprentissage couleurs: fin → code de test dans %.1fs",
                     VERIFY_AFTER_PREVIEW_SEC)
        _preview_active = False
        # Arrête la boucle d'apprentissage et éteint, PUIS lance le code de test
        # après un délai (le temps d'arriver sur l'écran de test). LED en thread :
        # stop_blink() fait un join(timeout=2) qui BLOQUERAIT le callback GATT (boucle
        # BLE) → réponse ATT au « Suivant » retardée. On ne bloque jamais le callback.
        def _led_off():
            try:
                led.stop_blink()
                led.off()
            except Exception:
                pass
        threading.Thread(target=_led_off, daemon=True).start()
        threading.Timer(VERIFY_AFTER_PREVIEW_SEC, _new_verify_code).start()


def set_verify_status(new_status: str) -> None:
    """Met à jour l'état de vérification + pousse la notification BLE."""
    global _verify_status
    _verify_status = new_status
    log.info("verify -> %s", new_status)
    if _verify_status_char is not None:
        try:
            _verify_status_char.set_value(new_status.encode("utf-8"))
        except Exception as e:
            log.warning("notify verify status failed: %s", e)


def _verify_cooldown() -> None:
    """Après VERIFY_MAX_ATTEMPTS échecs : rouge fixe, pause, puis nouveau code."""
    global _verify_attempts
    try:
        led.flash_pattern(led.ROUGE, n=3, flash_sec=0.15, hold_after=True, bypass=True)
    except Exception:
        pass
    time.sleep(VERIFY_COOLDOWN_SEC)
    _verify_attempts = 0
    _new_verify_code()
    set_verify_status(VS_PENDING)


def set_status(new_state: str) -> None:
    """Met à jour l'état + pousse la notification BLE + signale visuellement via LED.

    L'état de succès peut porter l'IP locale du device en suffixe
    (`connected:192.168.1.74`) pour que le central puisse se connecter
    directement sur le LAN après le provisioning. La détection de succès
    tolère donc ce suffixe.
    """
    global _state, _provisioning_succeeded
    _state = new_state
    is_connected = new_state.split(":", 1)[0] == STATE_CONNECTED
    # Position du flag AVANT tout traitement long (LED flash, notification) : sinon
    # une déconnexion BLE qui survient pendant le flash 0.9s ferait croire au
    # handler on_disconnect qu'on a échoué → exit 1 → restart violet-jaune.
    if is_connected:
        _provisioning_succeeded = True
    log.info("status -> %s", new_state)
    if _status_char is not None:
        try:
            _status_char.set_value(new_state.encode("utf-8"))
        except Exception as e:
            log.warning("notify status failed: %s", e)

    # Feedback LED sur les transitions terminales
    try:
        if is_connected:
            # Succès connexion réseau : 2 flashs verts rapides (pas de vert tenu).
            led.flash_pattern(led.VERT, n=2, flash_sec=0.08, hold_after=False,
                              bypass=True)
        elif new_state.startswith("failed:"):
            # Échec WiFi (ex. mauvais mot de passe) : le BLE reste connecté,
            # l'utilisateur peut re-saisir. On signale l'échec par 3 flashs
            # rouges puis on ÉTEINT — surtout PAS le violet/jaune « à configurer »
            # (aucun téléphone connecté), qui laisserait croire à un reset alors
            # qu'on est toujours lié et prêt pour un nouvel essai.
            led.flash_pattern(led.ROUGE, n=3, flash_sec=0.15, hold_after=False,
                              bypass=True)
            led.off()
    except Exception as e:
        log.warning("LED feedback failed: %s", e)

    # Sur succès : armer le shutdown gracieux (en thread, ne bloque pas).
    # Le flag _provisioning_succeeded a déjà été positionné au début de la fonction.
    if is_connected:
        threading.Thread(
            target=_graceful_shutdown_after_success,
            daemon=True,
        ).start()


def _graceful_shutdown_after_success() -> None:
    """Après un succès de provisioning : courte attente pour le feedback visuel
    (LED verte fixe), puis reboot complet du device pour repartir propre.
    Le reboot a 2 avantages sur un simple restart de tic-reader :
      - tous les services (publisher, registrar, …) repartent dans leur ordre normal
      - on revalide le pipeline check_network → tic-reader à chaque succès
    """
    log.info("provisioning OK — reboot dans %ds", GRACE_PERIOD_AFTER_SUCCESS_SEC)
    time.sleep(GRACE_PERIOD_AFTER_SUCCESS_SEC)
    log.info("reboot pour bascule mode normal")
    try:
        led.cleanup()
    except Exception:
        pass
    subprocess.run(["systemctl", "reboot"], capture_output=True)
    # Le reboot tue le process avant qu'on arrive ici, mais sécurité :
    os._exit(0)


# ---------------------------------------------------------------------------
# WiFi scan cache (rafraîchi en background)
# ---------------------------------------------------------------------------
_wifi_scan_cache: bytes = b"[]"
_wifi_scan_lock = threading.Lock()
WIFI_SCAN_REFRESH_SEC = 30


WIFI_SCAN_MAX_NETWORKS = 3  # limite stricte pour rester sous la MTU ATT
                            # single-PDU (~185 octets observé) — bluezero/Pi
                            # ne gère pas correctement Read Long Request.
                            # 3 SSID de 30 chars → ~118 octets JSON compact, OK.


def _refresh_wifi_scan() -> None:
    """Lance un scan nmcli, filtre à 2.4 GHz (Pi Zero W = 2.4 GHz only),
    met à jour le cache JSON. Format compact pour rester sous MTU :
    `[{"s": "<ssid>", "g": <signal>, "f": <freq>}, ...]` (top N par signal)."""
    global _wifi_scan_cache
    try:
        subprocess.run(
            ["nmcli", "device", "wifi", "rescan"],
            capture_output=True, timeout=15,
        )
        time.sleep(3)
        r = subprocess.run(
            ["nmcli", "-t", "-f", "SSID,FREQ,SIGNAL", "device", "wifi", "list"],
            capture_output=True, text=True, timeout=10,
        )
        networks, seen = [], set()
        for line in r.stdout.splitlines():
            parts = line.split(":")
            if len(parts) < 3:
                continue
            ssid = parts[0].strip()
            if not ssid or ssid in seen:
                continue
            try:
                freq = int(parts[1].strip().split()[0])
                signal = int(parts[2].strip())
            except (ValueError, IndexError):
                continue
            if 2400 <= freq <= 2500:  # Pi Zero W ne peut joindre que 2.4 GHz
                networks.append({"s": ssid, "g": signal, "f": freq})
                seen.add(ssid)
        networks.sort(key=lambda n: -n["g"])
        networks = networks[:WIFI_SCAN_MAX_NETWORKS]
        # separators=(",",":") supprime les espaces dans le JSON pour gagner des octets
        new_cache = json.dumps(networks, separators=(",", ":")).encode("utf-8")
        with _wifi_scan_lock:
            _wifi_scan_cache = new_cache
        log.info("wifi scan: %d réseaux 2.4 GHz (%d octets JSON)",
                 len(networks), len(new_cache))
    except Exception as e:
        log.warning("refresh wifi scan: %s", e)


def _wifi_scan_once() -> None:
    # UN SEUL scan au démarrage du mode provisioning, pour peupler la liste SSID
    # que l'app lit une fois à la connexion. On NE rescanne PAS ensuite : sur le
    # Pi Zero W la radio WiFi+BLE est PARTAGÉE, et un `nmcli device wifi rescan`
    # pendant une connexion BLE affame le lien → décrochage (un téléphone à
    # supervision timeout court, ~5 s, le perd → « erreur »/reset pendant la reco
    # couleurs). La liste d'un device fraîchement booté reste fraîche le temps de
    # l'unboxing, et l'app permet une saisie SSID manuelle en secours.
    _refresh_wifi_scan()


# ---------------------------------------------------------------------------
# GATT callbacks
# ---------------------------------------------------------------------------
def on_status_read() -> bytes:
    return _state.encode("utf-8")


def on_wifi_scan_read() -> bytes:
    with _wifi_scan_lock:
        return _wifi_scan_cache


def on_device_info_read() -> bytes:
    # On ne renvoie que les champs utiles à l'app, en JSON compact. Le device.json
    # complet (avec `capabilities`) dépasse 184 octets, or iOS négocie une MTU BLE
    # de ~185 → la lecture ATT est tronquée (bluezero ne gère pas Read Long, donc
    # la valeur DOIT tenir dans un seul PDU) → l'app échoue au parse JSON. Android
    # (MTU 512) ne voyait pas le problème. Compact = ~100 octets, safe partout.
    info = read_device_info()
    keys = ("deviceId", "model", "hardwareRevision", "softwareVersion")
    compact = {k: info[k] for k in keys if k in info}
    return json.dumps(compact, separators=(",", ":")).encode("utf-8")


def on_verify_status_read() -> bytes:
    return _verify_status.encode("utf-8")


def on_verify_write(value, options):
    """Reçoit le code couleur deviné par l'app et le compare à celui affiché."""
    global _verified, _verify_attempts
    if _verified:
        return  # déjà vérifié, on ignore
    if _verify_status == VS_LOCKED:
        return  # en cooldown, on ignore les tentatives
    try:
        guess = bytes(value).decode("utf-8").strip().upper()
    except UnicodeDecodeError:
        set_verify_status(VS_WRONG)
        return

    if guess == _verify_code:
        _verified = True
        log.info("vérification couleur OK")
        # TOUT le travail LED en thread : `led.stop_blink()` fait un join(timeout=2)
        # qui BLOQUERAIT le callback GATT (= la boucle BLE bluezero) → la réponse ATT
        # à l'app est retardée → GEL de l'écran de vérif côté iOS. On ne bloque JAMAIS
        # le callback : la notif VERIFY_STATUS part tout de suite, la LED suit en async.
        def _led_verified():
            try:
                led.stop_blink()  # stoppe la séquence du code de test
                # 3 blinks verts rapides = vérifié (puis éteint).
                led.flash_pattern(led.VERT, n=3, flash_sec=0.12,
                                  hold_after=False, bypass=True)
            except Exception:
                pass
        threading.Thread(target=_led_verified, daemon=True).start()
        set_verify_status(VS_VERIFIED)
        return

    _verify_attempts += 1
    log.info("vérification couleur KO (%d/%d)", _verify_attempts, VERIFY_MAX_ATTEMPTS)
    # On garde le MÊME code affiché entre les essais : une faute de frappe honnête
    # → on retape, pas besoin de relire le boîtier.
    if _verify_attempts >= VERIFY_MAX_ATTEMPTS:
        set_verify_status(VS_LOCKED)
        threading.Thread(target=_verify_cooldown, daemon=True).start()
    else:
        set_verify_status(VS_WRONG)


def on_wifi_config_write(value, options):
    """Reçoit un JSON {ssid, password} et lance la configuration WiFi async."""
    if not _verified:
        log.warning("WIFI_CONFIG reçu avant vérification couleur — rejeté")
        set_status("failed:not_verified")
        return
    try:
        payload = bytes(value).decode("utf-8")
        log.info("wifi config reçue (%d octets)", len(payload))
        data = json.loads(payload)
        ssid = data["ssid"]
        password = data["password"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError) as e:
        log.error("payload invalide: %s", e)
        set_status(f"failed:invalid_payload:{type(e).__name__}")
        return

    # Pas de blocage du thread GATT — exécution en arrière-plan
    threading.Thread(
        target=_apply_wifi_config,
        args=(ssid, password),
        daemon=True,
    ).start()


def _apply_wifi_config(ssid: str, password: str) -> None:
    set_status(STATE_CONFIGURING)
    set_status(STATE_CONNECTING)
    success, message = configure_wifi(ssid, password)
    if success:
        # message = IP locale → on la renvoie au central : `connected:<ip>`.
        set_status(f"{STATE_CONNECTED}:{message}" if message else STATE_CONNECTED)
    else:
        set_status(f"failed:{message}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    device_id = read_device_id()
    log.info("démarrage BLE provisioner — deviceId=%s", device_id)

    # Au boot, bluetoothd peut mettre quelques secondes à exposer son adapter
    # sur D-Bus. On retry pendant 30s avant d'abandonner (évite la boucle
    # de crash systemd qui prend 2s entre chaque tentative).
    adapters = []
    for attempt in range(15):
        try:
            adapters = list(adapter.Adapter.available())
            if adapters:
                break
        except Exception as e:
            log.info("attente bluetoothd (tentative %d): %s", attempt + 1, e)
        time.sleep(2)
    if not adapters:
        log.error("aucun adaptateur Bluetooth après 30s d'attente")
        return 1
    adapter_obj = adapters[0]
    adapter_addr = adapter_obj.address
    log.info("adaptateur BLE: %s", adapter_addr)

    # Pairable=True → BlueZ accepte les demandes de pairing. Combiné au flag
    # encrypt-write sur WIFI_CONFIG, ça impose une connexion chiffrée
    # (Just Works LE Secure Connections par défaut sur BlueZ moderne).
    try:
        adapter_obj.pairable = True
    except Exception as e:
        log.warning("set pairable: %s", e)

    # Peuple le cache WiFi scan (UN seul scan, cf. _wifi_scan_once — pas de
    # rafraîchissement périodique pour ne jamais affamer le lien BLE).
    threading.Thread(target=_wifi_scan_once, daemon=True).start()

    ben = peripheral.Peripheral(adapter_addr, local_name=device_id)
    ben.add_service(srv_id=1, uuid=SERVICE_UUID, primary=True)
    ben.add_characteristic(
        srv_id=1, chr_id=1, uuid=WIFI_CONFIG_UUID,
        value=[], notifying=False,
        # POC : `write` simple. À remplacer par `encrypt-write` quand l'app
        # Flutter (qui gère le pairing) sera dispo — cf. docstring du module.
        flags=["write"],
        write_callback=on_wifi_config_write,
    )
    ben.add_characteristic(
        srv_id=1, chr_id=2, uuid=STATUS_UUID,
        value=list(STATE_IDLE.encode("utf-8")),
        notifying=False,
        flags=["read", "notify"],
        read_callback=on_status_read,
    )
    ben.add_characteristic(
        srv_id=1, chr_id=3, uuid=WIFI_SCAN_UUID,
        value=[], notifying=False,
        flags=["read"],
        read_callback=on_wifi_scan_read,
    )
    ben.add_characteristic(
        srv_id=1, chr_id=4, uuid=DEVICE_INFO_UUID,
        value=[], notifying=False,
        flags=["read"],
        read_callback=on_device_info_read,
    )
    ben.add_characteristic(
        srv_id=1, chr_id=5, uuid=VERIFY_UUID,
        value=[], notifying=False,
        flags=["write"],
        write_callback=on_verify_write,
    )
    ben.add_characteristic(
        srv_id=1, chr_id=6, uuid=VERIFY_STATUS_UUID,
        value=list(VS_PENDING.encode("utf-8")),
        notifying=False,
        flags=["read", "notify"],
        read_callback=on_verify_status_read,
    )
    ben.add_characteristic(
        srv_id=1, chr_id=7, uuid=PREVIEW_CMD_UUID,
        value=[], notifying=False,
        flags=["write"],
        write_callback=on_preview_cmd_write,
    )
    ben.add_characteristic(
        srv_id=1, chr_id=8, uuid=PREVIEW_COLOR_UUID,
        value=list(b"-"), notifying=False,
        flags=["read", "notify"],
        read_callback=on_preview_color_read,
    )

    # Gestion de la déconnexion BLE :
    #   - Si on a déjà réussi → on reste vivant pour la grace period
    #     (le thread _graceful_shutdown_after_success gère la sortie)
    #   - Sinon → exit 1 pour forcer un restart systemd (Restart=on-failure)
    #     qui ré-initialise l'advertising bluezero (bug connu : sans restart,
    #     les centraux suivants ne nous voient plus).
    def _on_disconnect(*_args, **_kwargs):
        if _provisioning_succeeded:
            log.info("BLE déconnecté après succès — on attend la grace period")
            return
        log.info("BLE déconnecté sans succès — exit 1 (systemd va relancer)")
        led.cleanup()
        os._exit(1)
    ben.on_disconnect = _on_disconnect

    # Au connect d'un central : on génère un code couleur frais et on l'affiche.
    # C'est le moment où la vérification visuelle a un sens (un téléphone est là).
    def _on_connect(*_args, **_kwargs):
        global _verified, _verify_attempts, _preview_active, _test_displayed
        _verified = False
        _verify_attempts = 0
        _preview_active = False
        _test_displayed = False
        log.info("BLE connecté — 2 flashs verts puis code couleur dans %ds", VERIFY_DISPLAY_DELAY_SEC)
        set_verify_status(VS_PENDING)
        # À la connexion BLE : on ARRÊTE le blink d'attente violet/jaune et on
        # fait 2 flashs verts rapides (= connecté). flash_pattern() appelle
        # stop_blink() en interne, donc le blink s'arrête tout seul. En thread :
        # ne pas bloquer le callback GATT pendant les flashs.
        threading.Thread(
            target=led.flash_pattern, args=(led.VERT,),
            kwargs={"n": 2, "flash_sec": 0.08, "hold_after": False, "bypass": True},
            daemon=True,
        ).start()
        # Repli : si l'app ne pilote PAS la phase d'apprentissage (PREVIEW_CMD),
        # on affiche quand même le code de test après le délai. Avec une app à
        # jour, c'est le « Suivant » (PREVIEW_CMD="0") qui déclenche l'affichage,
        # et ce repli ne fait rien (preview actif ou code déjà affiché).
        def _fallback_show_code():
            if not _preview_active and not _test_displayed:
                _new_verify_code()
        threading.Timer(VERIFY_DISPLAY_DELAY_SEC, _fallback_show_code).start()
    ben.on_connect = _on_connect

    global _status_char, _verify_status_char, _preview_color_char
    _status_char = ben.characteristics[1]          # la 2e ajoutée
    _verify_status_char = ben.characteristics[5]   # la 6e ajoutée
    _preview_color_char = ben.characteristics[7]   # la 8e ajoutée

    log.info("advertising sous le nom: %s", device_id)
    log.info("service UUID: %s", SERVICE_UUID)

    # LED : signale visuellement qu'on est en mode provisioning BLE
    try:
        led.setup()
        led.start_blink(led.VIOLET, led.JAUNE, period_sec=1.2, bypass=True)
    except Exception as e:
        log.warning("init LED impossible (%s) — on continue sans LED", e)

    log.info("en attente de connexion BLE…")
    try:
        ben.publish()  # boucle GLib bloquante
    except KeyboardInterrupt:
        log.info("arrêt demandé")
    finally:
        led.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
