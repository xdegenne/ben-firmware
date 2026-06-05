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
    global _verify_code
    _verify_code = "".join(secrets.choice(VERIFY_TOKENS) for _ in range(VERIFY_CODE_LEN))
    log.info("code couleur: %s", _verify_code)
    try:
        colors = [led.VERIFY_PALETTE[t] for t in _verify_code]
        led.start_sequence(colors)
    except Exception as e:
        log.warning("affichage séquence LED impossible: %s", e)


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
            led.flash_pattern(led.VERT, n=3, flash_sec=0.15, hold_after=True,
                              bypass=True)
        elif new_state.startswith("failed:"):
            led.flash_pattern(led.ROUGE, n=3, flash_sec=0.15, hold_after=False,
                              bypass=True)
            led.start_blink(led.VIOLET, led.JAUNE, period_sec=1.2, bypass=True)
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


def _wifi_scan_loop() -> None:
    while True:
        _refresh_wifi_scan()
        time.sleep(WIFI_SCAN_REFRESH_SEC)


# ---------------------------------------------------------------------------
# GATT callbacks
# ---------------------------------------------------------------------------
def on_status_read() -> bytes:
    return _state.encode("utf-8")


def on_wifi_scan_read() -> bytes:
    with _wifi_scan_lock:
        return _wifi_scan_cache


def on_device_info_read() -> bytes:
    return json.dumps(read_device_info()).encode("utf-8")


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
        try:
            led.stop_blink()
            led.set_color(*led.VERT, bypass=True)  # vert fixe = vérifié
        except Exception:
            pass
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

    # Pré-remplit le cache WiFi scan + démarre le rafraîchissement périodique.
    threading.Thread(target=_wifi_scan_loop, daemon=True).start()

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
        global _verified, _verify_attempts
        _verified = False
        _verify_attempts = 0
        log.info("BLE connecté — génération du code de vérification couleur")
        _new_verify_code()
        set_verify_status(VS_PENDING)
    ben.on_connect = _on_connect

    global _status_char, _verify_status_char
    _status_char = ben.characteristics[1]         # la 2e ajoutée
    _verify_status_char = ben.characteristics[5]  # la 6e ajoutée

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
