#!/usr/bin/env python3
"""
capabilities.py — source de vérité UNIQUE « capability → services » + helpers.

Le `device.json` déclare les CAPABILITIES du device (ce qu'il fait). Ce module traduit
chaque capability en service(s) systemd — en CODE, pas dans la data du registre. Il est
appelé aux 3 moments (même source de vérité) :
  - boot   : l'orchestrateur démarre les services des capas déclarées ;
  - OTA    : update.sh (re)start les services des capas présentes ;
  - ajout  : install/enable/start les services d'une nouvelle capa (flux à venir).

CLI (pour update.sh / bash) :
  capabilities.py has <cap>       exit 0 si le device a la capa, 1 sinon
  capabilities.py hw  <cap>       imprime la rev HW de la capa (vide si absente)
  capabilities.py list            imprime les capas déclarées (une par ligne)
  capabilities.py services <cap>  imprime les services systemd de la capa
  capabilities.py start   <cap>   start   les services de la capa
  capabilities.py restart <cap>   restart les services de la capa
  capabilities.py boot            start les services de TOUTES les capas déclarées

Le fichier device.json est /etc/ben-firmware/device.json (override BEN_DEVICE_JSON pour les tests).
"""

import json
import os
import subprocess
import sys

DEVICE_JSON = os.environ.get("BEN_DEVICE_JSON", "/etc/ben-firmware/device.json")

# ── Source de vérité : capability → service(s) systemd ────────────────────────
# En CODE (pas dans le registre-data) : une capa peut mapper 0..N services, ou une
# logique conditionnelle. `lora` (radio SX127x) n'a PAS de service dédié — c'est du
# HW utilisé par les services des capas `kind: lora` (ex. ben-lora-receiver pour
# lora-tic-receiver). Étendre ici quand on ajoute une capability.
CAP_SERVICES = {
    "tic-uart":          ["ben-tic-reader.service"],
    "lora-tic-receiver": ["ben-lora-receiver.service"],
    "lora":              [],   # radio SX127x — HW partagé, pas de service dédié
    "rgb-led-indicator": [],   # LED RGB — HW partagé (check_network, ben-led-release, readers)
    # à venir : "pilot-wire": ["ben-pilot-wire.service"], "lora-plug": [...], ...
}


# ── model → capabilities : source de vérité UNIQUE (provisioning + migration) ─
# Table historique dérivée de check_network.READERS_BY_MODEL. `hw` seedé depuis le
# hardwareRevision ; `fw` = firmware du satellite (émetteur) connu à la date.
def caps_for_model(model: str, hw: str = "rev01") -> dict:
    # rgb-led-indicator : présente sur TOUS les modèles (RGB LED soudée GPIO 12/13/16).
    return {
        "pi0-wired":      {"rgb-led-indicator": {"hw": hw}, "tic-uart": {"hw": hw}},
        "pi0-lora":       {"rgb-led-indicator": {"hw": hw}, "lora": {"hw": hw},
                           "lora-tic-receiver": {"hw": hw, "fw": "0.1.3"}},
        "pi0-lora-wired": {"rgb-led-indicator": {"hw": hw}, "lora": {"hw": hw},
                           "lora-tic-receiver": {"hw": hw, "fw": "0.1.3"}, "tic-uart": {"hw": hw}},
    }.get(model)


# model technique → label COMMERCIAL affiché (device.json.model, exposé par /info à l'app).
# Source de vérité unique, utilisée par le provisioning ET la migration 0.8.0. None = pas un
# model technique connu (ex. déjà relabellé) → l'appelant ne touche pas.
def label_for_model(model: str) -> str:
    return {"pi0-wired": "Filaire", "pi0-lora": "Radio", "pi0-lora-wired": "Mixte"}.get(model)


def load_device(path: str = None) -> dict:
    try:
        with open(path or DEVICE_JSON) as f:
            return json.load(f)
    except Exception:
        return {}


def capabilities(dev: dict = None) -> dict:
    if dev is None:
        dev = load_device()
    caps = dev.get("capabilities", {})
    return caps if isinstance(caps, dict) else {}


def has(cap: str, dev: dict = None) -> bool:
    return cap in capabilities(dev)


def hw(cap: str, dev: dict = None) -> str:
    return capabilities(dev).get(cap, {}).get("hw", "")


def services_for(cap: str) -> list:
    return CAP_SERVICES.get(cap, [])


def _systemctl(action: str, service: str) -> None:
    subprocess.run(["systemctl", action, "--no-block", service], capture_output=True)


def start(cap: str) -> None:
    for s in services_for(cap):
        _systemctl("start", s)


def restart(cap: str) -> None:
    for s in services_for(cap):
        _systemctl("restart", s)


def boot() -> None:
    """Boot : démarre les services de TOUTES les capabilities déclarées."""
    for cap in capabilities():
        start(cap)


def _cli(argv) -> int:
    if not argv:
        print(__doc__)
        return 2
    cmd, arg = argv[0], (argv[1] if len(argv) > 1 else None)
    if cmd == "has":
        return 0 if has(arg) else 1
    if cmd == "hw":
        print(hw(arg))
        return 0
    if cmd == "list":
        for c in capabilities():
            print(c)
        return 0
    if cmd == "services":
        for s in services_for(arg):
            print(s)
        return 0
    if cmd == "start":
        start(arg)
        return 0
    if cmd == "restart":
        restart(arg)
        return 0
    if cmd == "boot":
        boot()
        return 0
    if cmd == "device-json":                  # device-json <deviceId> <model> <hw> <version> → device.json complet (provisioning)
        if len(argv) < 5:
            print("usage: device-json <deviceId> <model> <hw> <version>", file=sys.stderr)
            return 2
        did, model, hw, ver = argv[1], argv[2], argv[3], argv[4]
        caps = caps_for_model(model, hw)
        if caps is None:
            print(f"model inconnu: {model}", file=sys.stderr)
            return 1
        # `model` = LABEL commercial (Filaire/Radio) affiché par l'app ; `hardwareRevision`
        # conservé le temps de la transition. Le model TECHNIQUE ne sert qu'ici (dérivation caps).
        print(json.dumps({"deviceId": did, "model": label_for_model(model), "hardwareRevision": hw,
                          "softwareVersion": ver, "capabilities": caps}, indent=2))
        return 0
    print(f"commande inconnue: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
