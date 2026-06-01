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

import logging
import subprocess
import sys
import time

import led

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


def _start_service(name: str) -> None:
    log.info("systemctl start %s", name)
    r = subprocess.run(["systemctl", "start", name], capture_output=True, text=True)
    if r.returncode != 0:
        log.error("start %s échec: %s", name, (r.stderr or r.stdout).strip())


def main() -> int:
    log.info("vérification connectivité Internet (timeout %ds)", TOTAL_TIMEOUT_SEC)

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
        # (tic-reader si online, ble-provisioner sinon).
        try:
            led.cleanup()
        except Exception:
            pass

    if online:
        log.info("réseau OK — rien à faire (ben-tic-reader et compagnie démarrent "
                 "en parallèle au boot, ils n'ont pas besoin d'Internet)")
    else:
        log.info("pas de réseau → démarrage mode provisioning BLE")
        subprocess.run(
            ["systemctl", "start", "--no-block", "ben-ble-provisioner.service"],
            capture_output=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
