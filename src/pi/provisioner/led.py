"""
led — Pilotage minimal de la LED RGB du PCB BEN (cathode commune, PCB rev01).

  R = GPIO12  (HW PWM0)
  G = GPIO13  (HW PWM1 + boot indicator)
  B = GPIO16  (PWM software)

Fournit un clignotement bicolore en thread daemon pour signaler visuellement
"mode provisioning BLE actif". Stop propre via stop_blink() + cleanup().

⚠ Conflit avec ben-tic-reader.service qui pilote la même LED — celui-ci doit
être stoppé avant de lancer le provisioner.
"""

import logging
import sys
import threading
import time
from pathlib import Path

import RPi.GPIO as GPIO

# Module store partagé (réglages utilisateur — luminosité LED)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "store"))
import settings  # noqa: E402

log = logging.getLogger("ble-provisioner.led")

RGB_R = 12
RGB_G = 13
RGB_B = 16

_pwm_r = None
_pwm_g = None
_pwm_b = None
_blink_thread: threading.Thread | None = None
_stop_evt: threading.Event = threading.Event()


def setup() -> None:
    """Init pins + PWM 500 Hz + éteint le boot indicator vert."""
    global _pwm_r, _pwm_g, _pwm_b
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    for pin in (RGB_R, RGB_G, RGB_B):
        GPIO.setup(pin, GPIO.OUT)
        GPIO.output(pin, GPIO.LOW)
    _pwm_r = GPIO.PWM(RGB_R, 500); _pwm_r.start(0)
    _pwm_g = GPIO.PWM(RGB_G, 500); _pwm_g.start(0)
    _pwm_b = GPIO.PWM(RGB_B, 500); _pwm_b.start(0)
    log.info("LED RGB initialisée")


def set_color(r: int, g: int, b: int, bypass: bool = False) -> None:
    """Couleur instantanée (0..100 par canal — duty cycle PWM).

    La luminosité réglée par l'utilisateur (`led_level`) est appliquée ICI, au
    plus bas niveau, donc héritée par tous les blinks. `bypass=True` (décidé par
    l'appelant) l'ignore et garantit un plancher de visibilité — pour les
    erreurs et le mode provisioning, lisibles même LED « éteinte »."""
    if _pwm_r is None:
        return
    f = settings.led_factor(bypass)
    _pwm_r.ChangeDutyCycle(max(0, min(100, round(r * f))))
    _pwm_g.ChangeDutyCycle(max(0, min(100, round(g * f))))
    _pwm_b.ChangeDutyCycle(max(0, min(100, round(b * f))))


def off() -> None:
    set_color(0, 0, 0)


# ---------------------------------------------------------------------------
# Clignotement bicolore (thread daemon)
# ---------------------------------------------------------------------------
def _blink_loop(color_a: tuple[int, int, int],
                color_b: tuple[int, int, int],
                period_sec: float,
                bypass: bool) -> None:
    half = period_sec / 2.0
    while not _stop_evt.is_set():
        set_color(*color_a, bypass=bypass)
        if _stop_evt.wait(half):
            break
        set_color(*color_b, bypass=bypass)
        if _stop_evt.wait(half):
            break
    off()


def start_blink(color_a: tuple[int, int, int],
                color_b: tuple[int, int, int],
                period_sec: float = 1.2,
                bypass: bool = False) -> None:
    """Démarre un clignotement bicolore en background. Idempotent.
    bypass=True → ignore la luminosité réglée (provisioning)."""
    global _blink_thread
    stop_blink()
    _stop_evt.clear()
    _blink_thread = threading.Thread(
        target=_blink_loop,
        args=(color_a, color_b, period_sec, bypass),
        daemon=True,
    )
    _blink_thread.start()


def stop_blink() -> None:
    global _blink_thread
    if _blink_thread is not None and _blink_thread.is_alive():
        _stop_evt.set()
        _blink_thread.join(timeout=2)
    _blink_thread = None


def cleanup() -> None:
    stop_blink()
    try:
        if _pwm_r is not None: _pwm_r.stop()
        if _pwm_g is not None: _pwm_g.stop()
        if _pwm_b is not None: _pwm_b.stop()
        GPIO.cleanup([RGB_R, RGB_G, RGB_B])
    except Exception as e:
        log.warning("cleanup LED: %s", e)


def flash_pattern(color: tuple[int, int, int],
                  n: int = 3,
                  flash_sec: float = 0.15,
                  hold_after: bool = True,
                  bypass: bool = False) -> None:
    """Stoppe le blink en cours, fait N flashs de `color`, puis maintient (ou éteint).
    bypass=True → ignore la luminosité réglée (erreur / provisioning)."""
    stop_blink()
    for _ in range(n):
        off()
        time.sleep(flash_sec)
        set_color(*color, bypass=bypass)
        time.sleep(flash_sec)
    if hold_after:
        set_color(*color, bypass=bypass)
    else:
        off()


# ---------------------------------------------------------------------------
# Séquence de couleurs (vérification visuelle au provisioning)
# ---------------------------------------------------------------------------
def _sequence_loop(colors: list[tuple[int, int, int]],
                   on_sec: float,
                   gap_sec: float,
                   loop_gap_sec: float,
                   bypass: bool) -> None:
    """Boucle l'affichage d'une séquence de couleurs.

    Deux niveaux de noir, indispensables à la lisibilité :
      - `gap_sec` (court) entre chaque couleur → distingue deux couleurs
        identiques qui se suivent et rend les transitions nettes ;
      - `loop_gap_sec` (long) avant chaque répétition → marque le DÉBUT du code
        (sinon on ne sait pas où la séquence commence).
    La LED reste éteinte pendant tous les gaps.
    """
    while not _stop_evt.is_set():
        for c in colors:
            if _stop_evt.is_set():
                break
            set_color(*c, bypass=bypass)
            if _stop_evt.wait(on_sec):
                break
            off()
            if _stop_evt.wait(gap_sec):
                break
        # noir long = délimiteur de séquence
        if _stop_evt.wait(loop_gap_sec):
            break
    off()


def start_sequence(colors: list[tuple[int, int, int]],
                   on_sec: float = 0.7,
                   gap_sec: float = 0.3,
                   loop_gap_sec: float = 1.5,
                   bypass: bool = True) -> None:
    """Démarre l'affichage en boucle d'une séquence de couleurs (background).
    Idempotent (stoppe la séquence/blink en cours). bypass=True par défaut :
    une vérification doit rester visible même LED « éteinte » par l'utilisateur."""
    global _blink_thread
    stop_blink()
    _stop_evt.clear()
    _blink_thread = threading.Thread(
        target=_sequence_loop,
        args=(colors, on_sec, gap_sec, loop_gap_sec, bypass),
        daemon=True,
    )
    _blink_thread.start()


# ---------------------------------------------------------------------------
# Palettes pré-définies (duty cycle 0..100)
# ---------------------------------------------------------------------------
VIOLET = (20, 0, 40)   # plus de bleu que de rouge pour un violet franc
JAUNE  = (35, 20, 0)   # vert tiré pour rester chaud sans virer vert
VERT   = (0, 30, 0)
ROUGE  = (40, 0, 0)

# Palette de vérification (lisible daltonien) : JAMAIS rouge ET vert ensemble
# → on supprime le vert (confusion deutan/protan, ~8 % des hommes). On joue sur
# l'axe bleu↔jaune (perçu par les dichromates) + la luminosité (blanc clair /
# rouge sombre). Duties poussés (séquence en bypass, plein contraste).
# IMPORTANT — le BLANC : la LED bleue est perceptuellement plus vive, donc à
# canaux égaux le « blanc » vire au bleu (confusion blanc/bleu constatée). On
# fait un BLANC CHAUD en écrasant le bleu (B ≈ 0,25-0,35 × R). À ré-ajuster sur
# device réel si besoin.
VERIFY_PALETTE = {
    "B": (0,   0,   100),  # Bleu  — pur
    "Y": (100, 80,  0),    # Jaune — vif, zéro bleu
    "W": (100, 85,  25),   # Blanc CHAUD — bleu écrasé pour ne plus virer bleu
    "R": (100, 0,   0),    # Rouge — poussé (les protans le voient sombre)
}
