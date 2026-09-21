"""
check_network — Boot-time connectivity check.

Lancé en oneshot au boot. Décide qui prend la main :
  • Réseau OK        → démarre les services normaux (ben-tic-reader, etc.)
  • Jamais provisionné → ben-ble-provisioner.service (unboxing, BLE indéfiniment)
  • Provisionné, réseau KO → ben-network-recovery.service : fenêtre de provisioning
    BLE bornée (veille passive par ping), puis COLLECTE, réseau ou pas.

⚠️ Ce module est un ONESHOT : il tranche une fois et rend la main. Toute décision
qui doit être RÉVISÉE plus tard appartient à `network_recovery`, pas ici.

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
from pathlib import Path

import led

DEVICE_JSON = "/etc/ben-firmware/device.json"

# Nom de la connexion WiFi créée par le provisioning BLE (= marqueur "déjà
# unboxé"). DOIT rester aligné avec wifi_config.CONNECTION_NAME.
CONNECTION_NAME = "ben-provisioned"

# Les agents "normaux" n'ont PLUS d'autostart systemd (pas de WantedBy) : c'est
# `_start_readers()`, et seulement lui, qui les lance — sinon ils démarrent au boot
# en doublon et tuent ben-ble-provisioner via Conflicts (bug "code couleur en boucle").
# QUELS agents : `capabilities.py`, source de vérité unique. Il n'y a plus de mapping
# par modèle ici (supprimé en 0.9.12) — voir _start_readers().

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


def _start_recovery() -> None:
    """Mode RÉCUPÉRATION : fenêtre BLE bornée, puis collecte hors ligne.

    Réservé au device DÉJÀ provisionné. C'est `network_recovery` qui démarre le
    provisioner, pas nous : il doit rester seul maître de la fenêtre, sinon on
    relancerait le BLE dans son dos juste après qu'il l'a coupé pour collecter."""
    _start("ben-network-recovery.service")


def _start_readers() -> None:
    """Démarre les agents normaux (device provisionné + réseau up). Ces services n'ont
    pas d'autostart : ils ne tournent QUE par cet appel.

    Les CAPABILITIES du `device.json` sont la seule source (`capabilities.py`). Le repli
    par modèle a été supprimé en 0.9.12 : il était mort ET nuisible. Mort, parce que tout
    boîtier ayant atteint la 0.9.x est passé par la migration 0.6.1 → 0.7.0 qui écrit les
    capabilities. Nuisible, parce que `device.json.model` porte depuis la 0.8.0 le LABEL
    COMMERCIAL (« Filaire », « Radio ») et non le modèle technique : la table ne matchait
    plus rien, on tombait dans le défaut « modèle inconnu » et on démarrait le monolithe
    `ben-lora-receiver` — supprimé en 0.9.12 — au lieu de la façade radio.

    Sans capabilities lisibles il n'y a RIEN à démarrer, et il faut le dire fort : c'est
    un boîtier qui ne mesurera pas."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # src/pi
        import capabilities as caps
        declared = caps.capabilities()
    except Exception as e:
        log.error("capabilities illisibles (%s) — AUCUN agent de mesure ne peut démarrer", e)
        return
    if not declared:
        log.error("device.json sans capabilities — AUCUN agent de mesure ne peut démarrer "
                  "(fichier=%s)", caps.DEVICE_JSON)
        return
    for cap in declared:
        caps.start(cap)
    log.info("readers démarrés via capabilities: %s", list(declared))

    # Le PUBLISHER n'est PAS une capability : publier n'est pas une propriété du
    # matériel (comme l'est une radio ou une entrée TIC), c'est une fonction de
    # flotte, identique sur tous les boîtiers. Il est donc démarré ici, en dur, et
    # dans la MÊME branche que les lecteurs — un boîtier non provisionné ou hors
    # ligne n'a rien à publier, et le laisser tourner ne ferait que remplir le
    # journal d'échecs de connexion.
    subprocess.run(["systemctl", "start", "--no-block", "ben-publisher.service"],
                   capture_output=True)
    log.info("ben-publisher démarré")


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
        # PAS le provisioner directement : ce oneshot ne repassera jamais ici, et
        # un device laissé en BLE y restait indéfiniment même une fois la box
        # revenue (coupure de courant : le boîtier reboote avant la box).
        # `network_recovery` prend le relais : fenêtre BLE, puis collecte.
        log.info("provisionné mais réseau KO → récupération (fenêtre BLE, puis collecte)")
        _start_recovery()
    return 0


if __name__ == "__main__":
    sys.exit(main())
