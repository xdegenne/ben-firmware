"""
provisioning_state — drapeau « un téléphone est connecté en BLE ».

Un seul rôle : permettre à `network_recovery` de ne JAMAIS couper le provisioner
pendant qu'un utilisateur s'en sert. La récupération réseau doit arrêter le BLE
pour retenter le WiFi (radio PARTAGÉE sur Pi Zero W) — mais couper la radio au
milieu d'une saisie de mot de passe transformerait la récupération en panne.

Écrit par `main.py` (le provisioner), lu par `network_recovery.py`. Le fichier vit
dans /run — tmpfs, donc effacé à chaque boot : aucun drapeau ne survit à un
redémarrage, ce qui est exactement ce qu'on veut d'un état de session.

Anti-blocage. Un drapeau resté à `1` interdirait toute récupération, donc :
  - `main()` l'efface à CHAQUE démarrage du provisioner (et systemd le relance à
    chaque déconnexion BLE : `os._exit(1)` + `Restart=on-failure`) ;
  - il porte son horodate et EXPIRE (`SESSION_MAX_SEC`). Un provisioner tué net
    en pleine session ne peut donc pas condamner la récupération au silence.
"""

import logging
import os
import time

log = logging.getLogger("provisioning-state")

FLAG_DIR = "/run/ben"
FLAG_PATH = os.path.join(FLAG_DIR, "ble-central-connected")

# Au-delà, on considère le drapeau ABANDONNÉ et non « session très longue ». Un
# unboxing (apprentissage des couleurs + code de test + saisie WiFi) tient
# largement dans ce délai ; ce qui dure plus longtemps est un provisioner mort.
SESSION_MAX_SEC = 15 * 60


def set_ble_session() -> None:
    """Un central BLE est connecté — la récupération réseau doit patienter."""
    try:
        os.makedirs(FLAG_DIR, exist_ok=True)
        with open(FLAG_PATH, "w") as f:
            f.write(str(time.time()))
    except OSError as e:
        # Sans drapeau, la récupération pourra couper une session en cours. C'est
        # dégradé, jamais bloquant : on ne fait pas échouer le provisioning pour ça.
        log.warning("drapeau session BLE non posé (%s)", e)


def clear_ble_session() -> None:
    try:
        os.unlink(FLAG_PATH)
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning("drapeau session BLE non effacé (%s)", e)


def ble_session_active() -> bool:
    """Vrai si un central BLE est connecté ET que le drapeau n'a pas expiré."""
    try:
        with open(FLAG_PATH) as f:
            started = float(f.read().strip())
    except (FileNotFoundError, ValueError):
        return False
    except OSError as e:
        log.warning("drapeau session BLE illisible (%s) — on suppose libre", e)
        return False

    age = time.time() - started
    if age > SESSION_MAX_SEC:
        log.warning("drapeau session BLE périmé (%.0f min) — ignoré", age / 60)
        return False
    return True
