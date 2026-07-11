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
import sys
from pathlib import Path

import update_lib

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

        # 9. Commit the new version — only on success
        device["softwareVersion"] = transition["to"]
        update_lib.save_device_json(device, DEVICE_JSON)
        log.info("device.json updated: softwareVersion = %s", transition["to"])

    except Exception:
        log.exception("Update failed — device.json not modified, will retry next tick")
        sys.exit(1)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    main()
