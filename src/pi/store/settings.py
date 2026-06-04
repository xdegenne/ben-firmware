"""
settings.py — Réglages utilisateur du device (lus/écrits via l'API locale).

Stockés dans /var/lib/ben-firmware/settings.json : écrit par `ben-local-api`
(tournant en `ben`), lu par TOUS les services — y compris ceux en root
(provisioner, network-check), qui lisent sans souci les fichiers de `ben`.

Réglages :
  led_level : 0..5  — 0 = LED éteinte ; 1..5 = crans de luminosité croissants.

La luminosité est appliquée au plus bas niveau (couche LED) : un blink passe sa
couleur « pleine » (duty 0..100) et la couche multiplie par le facteur du cran
courant. Espacement perceptuel (gamma) : chaque cran « se voit » autant que le
précédent. Le cran par défaut (3) reproduit le rendu discret actuel.

`bypass=True` (cf. led_factor) → ignore le réglage et garantit une VISIBILITÉ
MINIMALE (cas erreur + provisioning) : factor = max(level_factor, FLOOR). Même
LED éteinte, une erreur ou le mode provisioning reste lisible.
"""

import json
from pathlib import Path

SETTINGS_PATH = "/var/lib/ben-firmware/settings.json"

LED_LEVEL_MIN = 0
LED_LEVEL_MAX = 5
LED_LEVEL_DEFAULT = 3  # ≈ le rendu discret actuel

# Facteur de luminosité maître par cran (espacement ~gamma, à régler à l'œil).
# Cran 3 = 1.0 → rendu actuel ; en dessous plus discret, au dessus plus visible.
# La sortie est plafonnée (duty 0..100) par la couche LED.
LED_LEVEL_FACTOR = [0.0, 0.30, 0.60, 1.0, 1.8, 3.0]

# Plancher de visibilité quand bypass=True (erreur / provisioning).
LED_BYPASS_FLOOR = 1.0

_DEFAULTS = {"led_level": LED_LEVEL_DEFAULT}


def _clamp_level(v) -> int:
    try:
        v = int(v)
    except (TypeError, ValueError):
        return LED_LEVEL_DEFAULT
    return max(LED_LEVEL_MIN, min(LED_LEVEL_MAX, v))


def load() -> dict:
    """Réglages courants (defaults si fichier absent/corrompu)."""
    try:
        with open(SETTINGS_PATH) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = {}
    out = dict(_DEFAULTS)
    if "led_level" in data:
        out["led_level"] = _clamp_level(data["led_level"])
    return out


def save(new: dict) -> dict:
    """Valide + persiste un sous-ensemble de réglages (écriture atomique).
    Renvoie l'état complet à jour."""
    cur = load()
    if "led_level" in new:
        cur["led_level"] = _clamp_level(new["led_level"])
    Path(SETTINGS_PATH).parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cur, f)
    Path(tmp).replace(SETTINGS_PATH)
    return cur


def led_factor(bypass: bool = False) -> float:
    """Facteur de luminosité courant à appliquer aux duty cycles LED.
    bypass=True → plancher de visibilité (erreur / provisioning)."""
    level = load()["led_level"]
    f = LED_LEVEL_FACTOR[level]
    return max(f, LED_BYPASS_FLOOR) if bypass else f
