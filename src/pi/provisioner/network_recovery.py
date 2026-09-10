"""
network_recovery — fenêtre de provisioning BLE au boot, puis COLLECTE.

LE DÉFAUT CORRIGÉ. `check_network.py` est un ONESHOT : il tranche UNE fois au boot,
puis rend la main. Sur un device déjà provisionné qui démarre sans réseau — cas
banal d'une coupure de courant, le boîtier reboote plus vite que la box — il
partait en provisioning BLE et PERSONNE ne revenait jamais. Le boîtier restait
en violet-jaune, sans lire la TIC, jusqu'à un débranchement manuel. Une coupure
de trente secondes pouvait coûter des jours de données.

CE QUI TOURNE ICI. Lancé par `check_network` dans la SEULE branche « déjà
provisionné mais réseau KO ». Un boîtier jamais unboxé n'entre jamais ici : son
provisioning initial doit rester en BLE indéfiniment, c'est le nominal.

LE DÉROULÉ, VOLONTAIREMENT LINÉAIRE (pas de boucle, pas de compteur) :

  1. provisioning BLE pendant BLE_WINDOW_SEC — le boîtier reste offert à l'app :
     une coupure peut aussi être un vrai changement de box, l'utilisateur doit
     pouvoir reconfigurer. C'est la SEULE raison d'attendre avant de collecter ;
  2. veille passive pendant cette fenêtre, un ping toutes les POLL_INTERVAL_SEC.
     NetworkManager se reconnecte seul quand la box revient : on le voit sans
     rien couper, et on écourte l'attente ;
  3. fenêtre écoulée → on arrête le BLE et ON COLLECTE, réseau ou pas.

POURQUOI COLLECTER SANS RÉSEAU. Le boîtier n'en a pas besoin pour travailler :
la base SQLite est LOCALE et l'app lit l'API locale du device. Mieux — un
boîtier qui perd le réseau EN MARCHE continue de collecter, rien ne l'arrête.
Refuser de le faire après un REDÉMARRAGE était une INCOHÉRENCE, pas une
précaution : la même panne donnait deux comportements opposés selon qu'elle
tombait avant ou après le boot.

⚠️ L'horodatage hors ligne est DÉCALÉ, pas corrompu. Pas de RTC sur Pi Zero, mais
systemd restaure la dernière heure connue au boot et seulement vers l'AVANT
(« System time advanced to… ») : aucun point ne peut s'écrire dans le passé de
la base. L'erreur vaut la durée de la coupure, et le NTP la rattrape d'un saut.
En mode STANDARD elle est évitable — `meter_ts` porte déjà l'horodate du
COMPTEUR sur 100 % des points — mais c'est un chantier à part.

LE BLE N'EST PAS SACRIFIÉ. Démarrer les agents éteint le provisioning (Conflicts
LED/GPIO), mais CHAQUE boot sans réseau rejoue cette fenêtre : un débranchement
rouvre le re-provisionnement. Le BLE étant le seul chemin de configuration hors
ligne, il fallait garantir qu'il reste atteignable avant de rendre la mesure
prioritaire.

CE QU'ON NE COUPE JAMAIS. Une session BLE en cours (`provisioning_state`) :
arrêter la radio pendant qu'un utilisateur saisit son mot de passe changerait la
récupération en panne. On patiente tant qu'elle dure — le drapeau EXPIRE, donc
une session oubliée ne peut pas retenir la collecte indéfiniment.
"""

import json
import logging
import subprocess
import sys
import time
from pathlib import Path

import check_network
import provisioning_state
from wifi_config import CONNECTION_NAME

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("network-recovery")

PROVISIONER = "ben-ble-provisioner.service"

# Fenêtre offerte à l'app avant de passer au travail utile. Assez longue pour
# qu'un utilisateur qui voit le violet-jaune aille chercher son téléphone,
# assez courte pour ne pas sacrifier la mesure à une attente sans objet.
BLE_WINDOW_SEC = 300
# Veille passive : un ping ne coûte rien et n'inquiète pas le lien BLE,
# contrairement à un rescan WiFi (radio PARTAGÉE sur Pi Zero W — c'est ce qui a
# fait supprimer le rescan périodique du provisioner en 0.8.2).
POLL_INTERVAL_SEC = 30
# Ultime relance avant de collecter, au cas où NM aurait épuisé ses
# `autoconnect-retries`. Best-effort : elle ne DÉCIDE de rien (on collecte quoi
# qu'il arrive), elle améliore seulement les chances d'avoir l'heure NTP juste
# dès le premier point. D'où le timeout court.
NMCLI_UP_TIMEOUT_SEC = 20

# Repli si les capabilities sont illisibles. ⚠️ NE PAS s'en contenter : sur un
# boîtier LoRa en capabilities, le mode normal est `ben-radio` + `ben-telemetry`
# et non `ben-lora-receiver` (monolithe découpé en 0.9.1) — une garde codée sur
# ces deux seuls noms ne verrait jamais un boîtier moderne déjà passé en normal.
LEGACY_READERS = ("ben-tic-reader.service", "ben-lora-receiver.service")


def _systemctl(*args: str, timeout: int = 60) -> int:
    cmd = ["systemctl", *args]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout).returncode
    except subprocess.TimeoutExpired:
        log.warning("timeout: %s", " ".join(cmd))
        return -1


def _is_active(unit: str) -> bool:
    return _systemctl("is-active", "--quiet", unit, timeout=10) == 0


def _reader_units() -> tuple:
    """Les units du mode NORMAL pour CE boîtier — mêmes sources que
    `check_network._start_readers()` : capabilities d'abord, modèle ensuite."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # src/pi
        import capabilities as caps
        declared = caps.capabilities()
        if declared:
            units = [s for cap in declared for s in caps.services_for(cap)]
            if units:
                return tuple(dict.fromkeys(units))   # dédoublonne, ordre conservé
    except Exception as e:
        log.warning("capabilities indisponible (%s) — repli modèle", e)

    try:
        with open(check_network.DEVICE_JSON) as f:
            model = json.load(f).get("model", "")
        units = check_network.READERS_BY_MODEL.get(model)
        if units:
            return tuple(units)
    except Exception as e:
        log.warning("lecture modèle impossible (%s)", e)

    return LEGACY_READERS


def _ping_once() -> bool:
    """Un seul ping — la sonde de la veille passive."""
    r = subprocess.run(
        ["ping", "-c", "1", "-W", str(check_network.PING_TIMEOUT_SEC),
         check_network.PING_TARGET],
        capture_output=True,
    )
    return r.returncode == 0


def _start_provisioning() -> None:
    log.info("→ provisioning BLE offert pendant %d s", BLE_WINDOW_SEC)
    _systemctl("start", "--no-block", PROVISIONER)


def _stop_provisioning() -> None:
    """Arrêt BLOQUANT (pas de --no-block) : les readers reprennent la LED et les
    pins GPIO juste après, il faut que le provisioner soit vraiment parti."""
    log.info("← arrêt du provisioning BLE")
    _systemctl("stop", PROVISIONER)


def _watch_window() -> bool:
    """Veille passive pendant la fenêtre BLE. True si le réseau revient seul."""
    deadline = time.monotonic() + BLE_WINDOW_SEC
    while time.monotonic() < deadline:
        time.sleep(min(POLL_INTERVAL_SEC, max(1, deadline - time.monotonic())))
        if _ping_once():
            return True
    return False


def _wait_for_ble_session_end() -> None:
    """Ne JAMAIS couper la radio sous un utilisateur en pleine configuration.
    Le drapeau expire (cf. provisioning_state), donc une session oubliée ne peut
    pas retenir la collecte indéfiniment."""
    while provisioning_state.ble_session_active():
        log.info("session BLE en cours — on patiente avant de basculer en collecte")
        time.sleep(POLL_INTERVAL_SEC)


def _last_chance_wifi() -> None:
    """Best-effort, ne décide de rien : on collecte de toute façon."""
    try:
        r = subprocess.run(
            ["nmcli", "connection", "up", CONNECTION_NAME],
            capture_output=True, text=True, timeout=NMCLI_UP_TIMEOUT_SEC,
        )
        log.info("relance WiFi : %s", "OK" if r.returncode == 0
                 else (r.stderr or r.stdout).strip()[:100] or "KO")
    except subprocess.TimeoutExpired:
        log.info("relance WiFi : timeout %ds", NMCLI_UP_TIMEOUT_SEC)
    except FileNotFoundError:
        log.warning("nmcli introuvable")


def main() -> int:
    readers = _reader_units()
    log.info("récupération démarrée (fenêtre BLE %d s, veille %d s, mode normal = %s)",
             BLE_WINDOW_SEC, POLL_INTERVAL_SEC, ", ".join(readers))

    # Quelqu'un d'autre a déjà rendu le mode normal : ne surtout pas relancer le
    # provisioner par-dessus un reader (Conflicts= les rendrait mutuellement tueurs).
    active = [r for r in readers if _is_active(r)]
    if active:
        log.info("agent normal déjà actif (%s) — rien à faire", ", ".join(active))
        return 0

    _start_provisioning()

    if _watch_window():
        log.info("réseau revenu pendant la fenêtre BLE (sans rien couper)")
    else:
        log.info("fenêtre écoulée sans réseau — la mesure passe devant l'attente")
        _wait_for_ble_session_end()

    _stop_provisioning()          # libère LED/GPIO AVANT que les readers les prennent
    _last_chance_wifi()
    log.info("démarrage des agents de mesure (la base est locale : la collecte "
             "n'a pas besoin du réseau ; rebrancher le boîtier rouvrira une fenêtre BLE)")
    check_network._start_readers()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log.info("arrêt demandé")
        sys.exit(0)
