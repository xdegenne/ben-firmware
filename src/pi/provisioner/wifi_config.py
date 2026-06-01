"""
wifi_config — Configuration WiFi via NetworkManager (nmcli).

Pi OS Bookworm/Trixie (golden image BEN) utilise NetworkManager par défaut.

Stratégie safe : on **NE supprime PAS** l'ancienne connexion avant de tenter
la nouvelle. Si la nouvelle échoue (mauvais password, SSID introuvable, etc.),
NM peut auto-reconnecter à l'ancienne tant qu'elle existe encore.

  1. Note la connexion WiFi active actuelle (`prev_active`)
  2. Force un rescan WiFi (les hotspots smartphone n'advertise pas en continu)
  3. Tente la nouvelle connexion sous un nom temporaire unique
  4. Si échec → supprime la tentative ratée + remet l'ancienne en place
  5. Si succès → cleanup des anciens `ben-provisioned*` + rename temp → `ben-provisioned`

Retour : (success: bool, message: str)
  - success=True  → "connected to <ssid> (IP <addr>)"
  - success=False → "<raison>" (timeout, auth_failed, ssid_not_found, no_ip_assigned, …)
"""

import logging
import re
import subprocess
import time

log = logging.getLogger("ble-provisioner.wifi")

CONNECTION_NAME = "ben-provisioned"
CONNECT_TIMEOUT_SEC = 45
IP_POLL_INTERVAL_SEC = 2


def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str, str]:
    log.debug("nmcli: %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except FileNotFoundError:
        return -2, "", "nmcli_not_found"


def _active_wifi_connection() -> str | None:
    """Nom de la connexion WiFi actuellement active sur wlan0, ou None."""
    rc, out, _ = _run(
        ["nmcli", "-t", "-f", "DEVICE,NAME", "connection", "show", "--active"]
    )
    if rc != 0:
        return None
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) >= 2 and parts[0] == "wlan0":
            return parts[1]
    return None


def _wait_for_ip(iface: str = "wlan0") -> str | None:
    """Attend qu'une IPv4 soit assignée sur l'interface."""
    deadline = time.monotonic() + CONNECT_TIMEOUT_SEC
    while time.monotonic() < deadline:
        rc, out, _ = _run(["nmcli", "-t", "-f", "IP4.ADDRESS", "device", "show", iface])
        if rc == 0:
            for line in out.splitlines():
                if line.startswith("IP4.ADDRESS"):
                    m = re.search(r"(\d+\.\d+\.\d+\.\d+)", line)
                    if m:
                        return m.group(1)
        time.sleep(IP_POLL_INTERVAL_SEC)
    return None


def _classify_nmcli_error(rc: int, msg: str) -> str:
    low = msg.lower()
    if "secrets were required" in low or "auth" in low or "psk" in low:
        return "auth_failed"
    if "timeout" in low or rc == -1:
        return "timeout"
    if "not found" in low or "no network" in low:
        return "ssid_not_found"
    return f"nmcli_error:{rc}"


def _rollback(tmp_name: str, prev_active: str | None) -> None:
    """Après échec : supprime la tentative ratée + remet l'ancienne connexion."""
    _run(["nmcli", "connection", "delete", tmp_name])
    if prev_active and prev_active != tmp_name:
        log.info("rollback : tente de remonter %s", prev_active)
        _run(["nmcli", "connection", "up", prev_active], timeout=20)


def _cleanup_old_ben_provisioned(keep_name: str) -> None:
    """Supprime tous les ben-provisioned* SAUF `keep_name`. Appelé après
    un succès pour éviter l'accumulation de profils morts."""
    rc, out, _ = _run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"])
    if rc != 0:
        return
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) < 2 or parts[1] != "802-11-wireless":
            continue
        name = parts[0]
        if name.startswith("ben-provisioned") and name != keep_name:
            log.info("cleanup ancien profil: %s", name)
            _run(["nmcli", "connection", "delete", name])


def configure_wifi(ssid: str, password: str) -> tuple[bool, str]:
    if not ssid:
        return False, "empty_ssid"
    log.info("configuration WiFi SSID=%s", ssid)

    prev_active = _active_wifi_connection()
    log.info("connexion WiFi active avant tentative: %s", prev_active)

    # Force un scan WiFi frais avant le connect : les hotspots smartphone
    # (iOS/Android) arrêtent souvent leur advertising après quelques secondes
    # d'inactivité. Sans ce rescan, nmcli connect échoue souvent en
    # 'ssid_not_found' alors que le réseau est bien là.
    log.info("rescan WiFi avant connect…")
    _run(["nmcli", "device", "wifi", "rescan"], timeout=15)
    time.sleep(3)

    # Nom temporaire unique pour ne pas écraser l'ancienne en cas d'échec.
    # On renommera vers CONNECTION_NAME canonique après succès.
    tmp_name = f"{CONNECTION_NAME}-tmp-{int(time.time())}"

    rc, out, err = _run(
        ["nmcli", "device", "wifi", "connect", ssid, "password", password,
         "name", tmp_name],
        timeout=CONNECT_TIMEOUT_SEC,
    )

    if rc != 0:
        msg = err or out or "unknown_error"
        log.error("nmcli connect échec (rc=%d): %s", rc, msg)
        _rollback(tmp_name, prev_active)
        return False, _classify_nmcli_error(rc, msg)

    ip = _wait_for_ip()
    if ip is None:
        log.error("associé mais pas d'IP — rollback")
        _rollback(tmp_name, prev_active)
        return False, "no_ip_assigned"

    # Succès — cleanup des anciens ben-provisioned + rename temp → canonique.
    _cleanup_old_ben_provisioned(keep_name=tmp_name)
    _run(["nmcli", "connection", "modify", tmp_name,
          "connection.id", CONNECTION_NAME])

    log.info("provisioning OK — IP=%s, connexion renommée %s", ip, CONNECTION_NAME)
    return True, f"connected to {ssid} ({ip})"
