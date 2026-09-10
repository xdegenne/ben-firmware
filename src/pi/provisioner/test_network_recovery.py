#!/usr/bin/env python3
"""Régression : un boîtier hors ligne au boot doit ALTERNER, pas rester en BLE.

Le défaut d'origine (corrigé en pi-0.9.11) : `check_network` est un oneshot, il tranchait
une fois au boot et rendait la main. Un device déjà provisionné qui démarrait sans réseau —
coupure de courant, le boîtier reboote plus vite que la box — partait en provisioning BLE
et personne ne revenait jamais tester. Le WiFi remontait bel et bien (wifi-watchdog), mais
rien ne repassait en mode normal : violet-jaune et zéro mesure jusqu'à un débranchement.

CE DÉFAUT EST INVISIBLE EN EXPLOITATION : il ne se manifeste qu'au boot, sans réseau, donc
sans personne pour lire un journal. D'où ce banc, qui déroule les cycles hors cible en
stubbant GPIO, systemctl et ping.

    python3 src/pi/provisioner/test_network_recovery.py
"""
import os
import pathlib
import sys
import tempfile
import types

HERE = pathlib.Path(__file__).resolve().parent               # …/src/pi/provisioner
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))                         # …/src/pi

# ── stubs : pas de GPIO ni de réglages persistés hors cible ───────────────────
_gpio = types.ModuleType("RPi.GPIO")
for _n in ("BCM", "OUT", "LOW", "HIGH"):
    setattr(_gpio, _n, 0)
_gpio.setmode = _gpio.setwarnings = _gpio.setup = lambda *a, **k: None
_gpio.output = _gpio.cleanup = lambda *a, **k: None
_gpio.PWM = lambda *a: types.SimpleNamespace(
    start=lambda *a: None, stop=lambda *a: None, ChangeDutyCycle=lambda *a: None)
_rpi = types.ModuleType("RPi")
_rpi.GPIO = _gpio
sys.modules.setdefault("RPi", _rpi)
sys.modules.setdefault("RPi.GPIO", _gpio)

_settings = types.ModuleType("settings")
_settings.led_factor = lambda bypass=False: 1.0
sys.modules.setdefault("settings", _settings)

import provisioning_state                                    # noqa: E402
import network_recovery as nr                                # noqa: E402

provisioning_state.FLAG_DIR = tempfile.mkdtemp()
provisioning_state.FLAG_PATH = os.path.join(provisioning_state.FLAG_DIR, "flag")

nr.BLE_WINDOW_SEC = 2           # raccourci pour le banc (300 s en production)
nr.POLL_INTERVAL_SEC = 1


def run(ping_results, ble_session=False, session_ends_after=None, readers_active=()):
    """Déroule main() avec un monde simulé. Retourne (journal, issue).

    `session_ends_after` : nombre de consultations du drapeau après lesquelles la session
    BLE simulée se termine (None = elle dure indéfiniment)."""
    journal, state, pings = [], {"prov": False}, list(ping_results)

    nr._is_active = lambda u: u in readers_active or (u == nr.PROVISIONER and state["prov"])

    def start_prov():
        if not state["prov"]:
            state["prov"] = True
            journal.append("start-BLE")

    def stop_prov():
        if state["prov"]:
            state["prov"] = False
            journal.append("stop-BLE")

    nr._start_provisioning = start_prov
    nr._stop_provisioning = stop_prov
    nr._ping_once = lambda: pings.pop(0) if pings else False
    nr._last_chance_wifi = lambda: journal.append("relance-WiFi")
    nr._reader_units = lambda: ("ben-radio.service", "ben-telemetry.service")
    # INVARIANT vérifié à la source : les readers ne doivent JAMAIS démarrer pendant que
    # le provisioner tourne — ils se disputeraient la LED, le GPIO et l'UART/SPI.
    nr.check_network._start_readers = lambda: journal.append(
        "START-READERS-AVEC-BLE-ACTIF" if state["prov"] else "START-READERS")

    # Drapeau de session simulé : on compte les consultations pour pouvoir faire
    # « finir » la session, et pour détecter une attente qui ne se termine jamais.
    looks = {"n": 0}

    def session_active():
        if not ble_session:
            return False
        looks["n"] += 1
        if session_ends_after is not None and looks["n"] > session_ends_after:
            return False
        if looks["n"] > 30:
            raise SystemExit("ATTENTE-INFINIE")
        journal.append("attente-session")
        return True

    provisioning_state.clear_ble_session()
    nr.provisioning_state = types.SimpleNamespace(ble_session_active=session_active)
    try:
        return journal, f"rc={nr.main()}"
    except SystemExit as e:
        return journal, str(e)


CASES = []


def case(title, check, **kw):
    CASES.append((title, check, kw))


case("la box revient pendant la fenêtre BLE — on écourte sans rien couper",
     lambda j, o: j == ["start-BLE", "stop-BLE", "relance-WiFi", "START-READERS"]
                  and o == "rc=0",
     ping_results=[False, True])

case("hors ligne — fenêtre écoulée, puis COLLECTE quand même",
     lambda j, o: j == ["start-BLE", "stop-BLE", "relance-WiFi", "START-READERS"]
                  and o == "rc=0",
     ping_results=[False] * 20)

case("session BLE en cours — la radio n'est JAMAIS coupée sous l'utilisateur",
     lambda j, o: "stop-BLE" not in j and "START-READERS" not in j
                  and o == "ATTENTE-INFINIE",
     ping_results=[False] * 60, ble_session=True)

case("session BLE terminée — la collecte reprend son cours",
     lambda j, o: "attente-session" in j and j[-1] == "START-READERS" and o == "rc=0",
     ping_results=[False] * 60, ble_session=True, session_ends_after=2)

case("un agent normal tourne déjà — on s'efface sans relancer le BLE par-dessus",
     lambda j, o: j == [] and o == "rc=0",
     ping_results=[False] * 10, readers_active=("ben-radio.service",))

case("le passage en collecte rend LED et GPIO : jamais de reader sur un BLE actif",
     lambda j, o: "START-READERS-AVEC-BLE-ACTIF" not in j
                  and j.index("stop-BLE") < j.index("START-READERS"),
     ping_results=[False] * 20)


def aiguillage(provisioned, online):
    """Rejoue check_network.main() : QUI prend la main selon l'état du boîtier.

    Le point capital est le PREMIER UNBOXING : un boîtier jamais provisionné doit partir
    en BLE **sans** récupération — y rester indéfiniment est son mode NOMINAL, et une
    alternance le rendrait fuyant pour l'app pendant tout le déballage.
    """
    cn = nr.check_network
    called = []
    cn._has_been_provisioned = lambda: provisioned
    cn.has_internet = lambda: online
    cn._start_provisioning = lambda: called.append("BLE")
    cn._start_recovery = lambda: called.append("RECOVERY")
    cn._start_readers = lambda: called.append("READERS")
    cn.led = types.SimpleNamespace(
        setup=lambda: None, start_blink=lambda *a, **k: None,
        flash_pattern=lambda *a, **k: None, cleanup=lambda: None,
        VERT=(0, 1, 0), ROUGE=(1, 0, 0))
    cn.main()
    return called


def main() -> int:
    ok = True

    print("\n  aiguillage au boot (check_network)")
    for title, kw, expected in [
        ("jamais provisionné → BLE SEUL, aucune récupération",
         dict(provisioned=False, online=False), ["BLE"]),
        ("provisionné + en ligne → agents normaux directement",
         dict(provisioned=True, online=True), ["READERS"]),
        ("provisionné + hors ligne → récupération (et pas le BLE en direct)",
         dict(provisioned=True, online=False), ["RECOVERY"]),
    ]:
        got = aiguillage(**kw)
        good = got == expected
        ok &= good
        print(f"    [{'OK' if good else 'KO'}] {title}")
        print(f"           appelé : {got or '(rien)'}")

    print("\n  cycles de récupération")
    for title, check, kw in CASES:
        journal, issue = run(**kw)
        good = check(journal, issue)
        ok &= good
        print(f"    [{'OK' if good else 'KO'}] {title}")
        print(f"           {' → '.join(journal) or '(rien)'}   [{issue}]")

    print("\n  drapeau de session BLE")
    provisioning_state.clear_ble_session()
    checks = [("absent au départ", not provisioning_state.ble_session_active())]
    provisioning_state.set_ble_session()
    checks.append(("posé et lu", provisioning_state.ble_session_active()))
    saved, provisioning_state.SESSION_MAX_SEC = provisioning_state.SESSION_MAX_SEC, 0
    checks.append(("EXPIRE (anti-blocage)", not provisioning_state.ble_session_active()))
    provisioning_state.SESSION_MAX_SEC = saved
    provisioning_state.clear_ble_session()
    provisioning_state.clear_ble_session()      # doit rester idempotent
    checks.append(("effacement idempotent", not provisioning_state.ble_session_active()))
    for label, good in checks:
        ok &= good
        print(f"    [{'OK' if good else 'KO'}] {label}")

    print("\n  ==> TOUT PASSE" if ok else "\n  ==> REGRESSION")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
