"""
check_network — Boot-time connectivity check.

Lancé en oneshot au boot. Décide qui prend la main :
  • Réseau OK   → démarre les services normaux (ben-tic-reader, etc.)
  • Réseau KO   → démarre ben-ble-provisioner.service (mode provisioning BLE)

Le test consiste à pinguer une cible Internet pendant un délai borné, le temps
que NetworkManager finisse de monter wlan0 et obtienne une IP.

Aucune décision n'est prise sur la base de l'état nmcli seul : un wlan0
"connected" sans Internet (captive portal, DNS HS) doit aussi basculer en
mode provisioning.
"""

import json
import logging
import subprocess
import sys
import time
from pathlib import Path

import led

DEVICE_JSON = "/etc/ben-firmware/device.json"

# Nom de la connexion WiFi créée par le provisioning BLE (= marqueur "déjà
# unboxé"). DOIT rester aligné avec wifi_config.CONNECTION_NAME.
CONNECTION_NAME = "ben-provisioned"

# Agents "normaux" à démarrer selon le modèle quand le réseau est up.
# Ils n'ont PLUS d'autostart systemd (pas de WantedBy) : c'est ici, et
# seulement ici, qu'ils sont lancés — sinon ils démarrent au boot en doublon
# et tuent ben-ble-provisioner via Conflicts (bug "code couleur en boucle").
READERS_BY_MODEL = {
    "pi0-wired": ["ben-tic-reader.service"],
    "pi0-lora": ["ben-lora-receiver.service"],
    "pi0-lora-wired": ["ben-lora-receiver.service", "ben-tic-reader.service"],
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("network-check")

# Palette LED dédiée check_network — bleu pendant le check pour bien le
# distinguer du violet-jaune de ble-provisioner et des couleurs tic-reader.
BLEU       = (0,  0, 30)
BLEU_CIEL  = (0, 20, 30)

PING_TARGET = "1.1.1.1"
PING_TIMEOUT_SEC = 2
TOTAL_TIMEOUT_SEC = 30
RETRY_INTERVAL_SEC = 3


def has_internet() -> bool:
    deadline = time.monotonic() + TOTAL_TIMEOUT_SEC
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        r = subprocess.run(
            ["ping", "-c", "1", "-W", str(PING_TIMEOUT_SEC), PING_TARGET],
            capture_output=True,
        )
        if r.returncode == 0:
            log.info("ping %s OK (tentative %d)", PING_TARGET, attempt)
            return True
        log.info("ping %s KO (tentative %d) — retry dans %ds",
                 PING_TARGET, attempt, RETRY_INTERVAL_SEC)
        time.sleep(RETRY_INTERVAL_SEC)
    return False


def _start(name: str) -> None:
    log.info("systemctl start %s", name)
    subprocess.run(["systemctl", "start", "--no-block", name], capture_output=True)


def _start_provisioning() -> None:
    """Mode BLE provisioning."""
    _start("ben-ble-provisioner.service")


def _start_readers() -> None:
    """Démarre les agents normaux (device provisionné + réseau up). Ces services n'ont
    pas d'autostart : ils ne tournent QUE par cet appel.

    PRIORITÉ au device.json CAPABILITIES (source de vérité = capabilities.py) ; FALLBACK
    sur le mapping par modèle pour un device pas encore migré."""
    # Nouveau modèle : capabilities → services.
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # src/pi
        import capabilities as caps
        declared = caps.capabilities()
        if declared:
            for cap in declared:
                caps.start(cap)
            log.info("readers démarrés via capabilities: %s", list(declared))
            return
    except Exception as e:
        log.warning("capabilities indisponible (%s) — fallback modèle", e)

    # Fallback (device pas encore migré : device.json a `model`, pas `capabilities`).
    model = ""
    try:
        with open(DEVICE_JSON) as f:
            model = json.load(f).get("model", "")
    except Exception as e:
        log.warning("lecture %s: %s", DEVICE_JSON, e)
    services = READERS_BY_MODEL.get(model)
    if not services:
        log.warning("modèle inconnu (%r) → démarrage tic-reader + lora-receiver par défaut", model)
        services = ["ben-tic-reader.service", "ben-lora-receiver.service"]
    for s in services:
        _start(s)


def _has_been_provisioned() -> bool:
    """Vrai si une connexion WiFi `ben-provisioned` existe (= déjà unboxé).

    Premier boot d'un device jamais provisionné → pas de connexion → on sait
    d'avance qu'il n'y a pas de réseau, inutile de pinguer 30s : on va direct
    en BLE. Une fois provisionné, la connexion existe et on teste le réseau
    (qui peut être temporairement down → fallback BLE de récupération)."""
    r = subprocess.run(
        ["nmcli", "-t", "-f", "NAME", "connection", "show"],
        capture_output=True, text=True,
    )
    return any(line.strip() == CONNECTION_NAME for line in r.stdout.splitlines())


def main() -> int:
    # Premier boot (jamais unboxé) : on ne teste même pas le réseau.
    if not _has_been_provisioned():
        log.info("aucune connexion '%s' → device jamais provisionné → BLE direct "
                 "(pas de test réseau)", CONNECTION_NAME)
        _start_provisioning()
        return 0

    log.info("device déjà provisionné — vérification connectivité (timeout %ds)",
             TOTAL_TIMEOUT_SEC)

    # LED : signale visuellement le check en cours (bleu clignotant)
    led_ok = False
    try:
        led.setup()
        led.start_blink(BLEU, BLEU_CIEL, period_sec=0.6)
        led_ok = True
    except Exception as e:
        log.warning("LED indisponible (%s) — on continue sans LED", e)

    online = has_internet()

    try:
        if led_ok:
            # Signal franc dans les deux cas : 5 flashs longs (~5s d'animation)
            color = led.VERT if online else led.ROUGE
            led.flash_pattern(color, n=5, flash_sec=0.5, hold_after=False)
    except Exception as e:
        log.warning("LED flash final: %s", e)
    finally:
        # IMPORTANT : libère les pins GPIO pour le service suivant
        # (reader si online, ble-provisioner sinon).
        try:
            led.cleanup()
        except Exception:
            pass

    if online:
        log.info("réseau OK → démarrage des agents normaux")
        _start_readers()
    else:
        log.info("provisionné mais réseau KO → mode provisioning BLE (récupération)")
        _start_provisioning()
    return 0


if __name__ == "__main__":
    sys.exit(main())
