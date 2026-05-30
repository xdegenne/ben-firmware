"""
update_lib.py — helpers for the BEN OTA update agent.

Covers: device.json I/O, compatibility.yaml parsing, git operations,
GPG tag verification, SHA256 script verification.
"""

import hashlib
import json
import logging
import subprocess
import yaml
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# device.json
# ---------------------------------------------------------------------------

def load_device_json(path: str = "/etc/ben-firmware/device.json") -> dict:
    with open(path) as f:
        return json.load(f)


def save_device_json(data: dict, path: str = "/etc/ben-firmware/device.json") -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


# ---------------------------------------------------------------------------
# compatibility.yaml
# ---------------------------------------------------------------------------

def load_compatibility_from_remote(repo_path: str = "/opt/ben/repo") -> dict:
    """Read compatibility.yaml from origin/main without touching the working tree."""
    result = subprocess.run(
        ["git", "-C", repo_path, "show", "origin/main:compatibility.yaml"],
        check=True,
        capture_output=True,
        text=True,
    )
    return yaml.safe_load(result.stdout)


# ---------------------------------------------------------------------------
# Transition resolution
# ---------------------------------------------------------------------------

def _rev_num(rev: str) -> int:
    """'rev03' → 3"""
    return int(rev.lstrip("rev"))


def find_next_transition(compat: dict, device: dict) -> Optional[dict]:
    """
    Return the first applicable transition for this device, or None.

    A transition is applicable when:
    - model matches
    - `from` equals the current softwareVersion
    - hardwareRevision satisfies the minimum requirement
    """
    model = device["model"]
    hw_rev = device["hardwareRevision"]
    current = device["softwareVersion"]

    transitions = compat.get("updates", {}).get(model) or []
    for t in transitions:
        if t["from"] != current:
            continue
        min_rev = t.get("requires", {}).get("hardwareRevision", {}).get("minimum")
        if min_rev and _rev_num(hw_rev) < _rev_num(min_rev):
            log.warning(
                "Transition %s→%s skipped: hardware %s below required %s",
                t["from"], t["to"], hw_rev, min_rev,
            )
            continue
        return t
    return None


# ---------------------------------------------------------------------------
# Git operations
# ---------------------------------------------------------------------------

def fetch_origin(repo_path: str = "/opt/ben/repo") -> None:
    subprocess.run(
        ["git", "-C", repo_path, "fetch", "--tags", "origin", "main"],
        check=True,
        capture_output=True,
        text=True,
    )


def verify_tag(tag: str, repo_path: str = "/opt/ben/repo") -> None:
    """
    Verify the GPG signature of a git tag.

    Requires the BEN release public key to be imported in the system GPG keyring.
    The key is installed at provisioning time by install.sh from
    /etc/ben-firmware/gpg/ben-releases.pub.

    Raises subprocess.CalledProcessError on verification failure.
    """
    result = subprocess.run(
        ["git", "-C", repo_path, "verify-tag", tag],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        log.error("GPG verification failed for tag %s:\n%s", tag, result.stderr)
        raise subprocess.CalledProcessError(result.returncode, result.args, result.stderr)
    log.debug("GPG output: %s", result.stderr.strip())


def checkout_tag(tag: str, repo_path: str = "/opt/ben/repo") -> None:
    subprocess.run(
        ["git", "-C", repo_path, "checkout", tag],
        check=True,
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# Script verification and execution
# ---------------------------------------------------------------------------

def verify_sha256(script_path: str) -> None:
    """
    Verify SHA256 of update.sh against the adjacent .sha256 file.

    The .sha256 file contains the hex digest (optionally followed by a filename).
    Raises ValueError on mismatch, FileNotFoundError if the checksum file is absent.
    """
    checksum_path = script_path + ".sha256"
    expected = Path(checksum_path).read_text().split()[0].strip()
    actual = hashlib.sha256(Path(script_path).read_bytes()).hexdigest()
    if actual != expected:
        raise ValueError(
            f"SHA256 mismatch for {script_path}: expected {expected}, got {actual}"
        )


def run_update_script(script_path: str) -> None:
    """Execute update.sh with bash. Raises subprocess.CalledProcessError on failure."""
    subprocess.run(["bash", script_path], check=True)
