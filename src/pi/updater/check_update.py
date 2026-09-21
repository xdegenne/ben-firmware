#!/usr/bin/env python3
"""
check_update.py — BEN Device OTA update agent.

Triggered by ben-update.timer (once a day + randomized delay).
Applies at most one version transition per run. A device multiple versions
behind will catch up over successive ticks.

Exit codes:
  0 — already up to date, or update applied successfully
  1 — update failed (device.json not modified)
"""

import fcntl
import logging
import subprocess
import sys
from pathlib import Path

import update_lib

# capabilities.py vit dans src/pi/ (parent) — source de vérité model technique → libellé.
# Import défensif : l'agent OTA ne doit JAMAIS hard-crasher sur un import (sinon plus d'OTA
# pour se réparer). Fallback no-op si absent (checkout partiel improbable).
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from capabilities import label_for_model
except Exception:
    def label_for_model(_model):
        return None

LOCK_PATH = "/var/lib/ben-firmware/update.lock"  # /var/lib/ben-firmware/ owned ben
DEVICE_JSON = "/etc/ben-firmware/device.json"
REPO_PATH = "/opt/ben/repo"
LOG_PATH = "/var/log/ben-firmware/update.log"


def setup_logging() -> None:
    Path(LOG_PATH).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s][%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(LOG_PATH),
            logging.StreamHandler(sys.stdout),
        ],
    )


def main() -> None:
    setup_logging()
    log = logging.getLogger(__name__)

    # 1. Acquire lockfile — exit immediately if another instance is running
    try:
        lock = open(LOCK_PATH, "w")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("Already running, exiting")
        sys.exit(0)

    try:
        # 2. Read device identity
        device = update_lib.load_device_json(DEVICE_JSON)
        log.info(
            "Device: %s  model=%s  hw=%s  version=%s  caps=%s",
            device["deviceId"], device.get("model", "-"),
            device.get("hardwareRevision", "-"), device["softwareVersion"],
            list(device.get("capabilities", {})) or "-",
        )

        # 2b. Self-heal : normalise device.json.model → libellé commercial (Filaire/Radio/
        #     Mixte). Rétroactif pour les devices bumpés à 0.8.0 sous le bug de clobber
        #     (l'agent réécrivait une copie mémoire d'AVANT update.sh → relabel annulé).
        #     Idempotent : label_for_model=None si déjà relabellé → aucune écriture.
        #     Tourne à CHAQUE tick, indépendamment des transitions → pas besoin d'une 2e release.
        label = label_for_model(device.get("model"))
        if label is not None:
            log.info("Self-heal: relabel model %s → %s", device.get("model"), label)
            device["model"] = label
            update_lib.save_device_json(device, DEVICE_JSON)

        # 3. Fetch origin (tags + main branch)
        log.info("Fetching origin...")
        update_lib.fetch_origin(REPO_PATH)

        # 4. Load compatibility.yaml from origin/main and find next transition
        compat = update_lib.load_compatibility_from_remote(REPO_PATH)
        transition = update_lib.find_next_transition(compat, device)

        if transition is None:
            log.info("Already up to date (%s)", device["softwareVersion"])
            sys.exit(0)

        tag = transition["tag"]
        script = str(Path(REPO_PATH) / transition["script"])
        log.info(
            "Update available: %s → %s (tag %s)",
            transition["from"], transition["to"], tag,
        )

        # 5. Verify GPG signature of the target tag
        log.info("Verifying GPG signature of tag %s...", tag)
        update_lib.verify_tag(tag, REPO_PATH)
        log.info("GPG OK")

        # 6. Checkout the tag so update.sh is present at the expected path
        log.info("Checking out %s...", tag)
        update_lib.checkout_tag(tag, REPO_PATH)

        # 7. Verify SHA256 of update.sh
        log.info("Verifying SHA256 of update script...")
        update_lib.verify_sha256(script)
        log.info("SHA256 OK")

        # 8. Execute update script
        log.info("Running %s...", script)
        update_lib.run_update_script(script)
        log.info("Update script completed")

        # 9. Commit the new version — only on success.
        #    Re-lire device.json depuis le DISQUE : update.sh a pu le modifier (relabel,
        #    capabilities, …). Réutiliser la copie mémoire d'AVANT update.sh écraserait ces
        #    edits — c'est le bug qui annulait le relabel 0.8.0 (corrigé en 0.8.1).
        device = update_lib.load_device_json(DEVICE_JSON)
        device["softwareVersion"] = transition["to"]
        update_lib.save_device_json(device, DEVICE_JSON)
        log.info("device.json updated: softwareVersion = %s", transition["to"])

        # 10. Redémarrer le PUBLISHER — SYSTÉMATIQUEMENT, après CHAQUE update.
        #
        # 🚨 POURQUOI ICI ET PAS DANS update.sh : le publisher exécute le code de
        #    /opt/ben/repo. Après le `git checkout <tag>` de l'étape 6, le processus
        #    VIVANT tourne toujours sur l'ANCIEN code — comme tout service Python.
        #    Sans redémarrage il resterait sur la version précédente INDÉFINIMENT,
        #    en se déclarant à jour (device.json, lui, est bumpé). Le laisser à la
        #    charge de chaque update.sh reviendrait à l'oublier un jour.
        #
        #    Les LECTEURS, eux, restent redémarrés par update.sh : eux seuls savent
        #    si leur correctif l'exige, et un restart de lecteur coûte des mesures.
        #    Le publisher n'a pas d'état : le redémarrer ne coûte rien, il reprend
        #    au premier point non envoyé.
        #
        # Best-effort : si le service n'existe pas (avant pi-0.9.15) ou n'est pas
        # actif, on ne fait PAS échouer l'update pour autant — l'update elle-même
        # a réussi, et un échec ici la rejouerait à chaque tick.
        try:
            if subprocess.run(["systemctl", "is-active", "--quiet",
                               "ben-publisher.service"]).returncode == 0:
                subprocess.run(["sudo", "systemctl", "restart", "ben-publisher.service"],
                               check=True, capture_output=True)
                log.info("ben-publisher redémarré (nouveau code)")
            else:
                log.info("ben-publisher inactif — rien à redémarrer")
        except Exception as e:
            log.warning("redémarrage de ben-publisher impossible (%s) — sans conséquence "
                        "sur l'update, mais il tourne sur l'ancien code", e)

    except Exception:
        log.exception("Update failed — device.json not modified, will retry next tick")
        sys.exit(1)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    main()
