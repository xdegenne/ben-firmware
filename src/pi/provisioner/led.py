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
import threading
import time

import RPi.GPIO as GPIO

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


def set_color(r: int, g: int, b: int) -> None:
    """Couleur instantanée (0..100 par canal — duty cycle PWM)."""
    if _pwm_r is None:
        return
    _pwm_r.ChangeDutyCycle(max(0, min(100, r)))
    _pwm_g.ChangeDutyCycle(max(0, min(100, g)))
    _pwm_b.ChangeDutyCycle(max(0, min(100, b)))


def off() -> None:
    set_color(0, 0, 0)


# ---------------------------------------------------------------------------
# Clignotement bicolore (thread daemon)
# ---------------------------------------------------------------------------
def _blink_loop(color_a: tuple[int, int, int],
                color_b: tuple[int, int, int],
                period_sec: float) -> None:
    half = period_sec / 2.0
    while not _stop_evt.is_set():
        set_color(*color_a)
        if _stop_evt.wait(half):
            break
        set_color(*color_b)
        if _stop_evt.wait(half):
            break
    off()


def start_blink(color_a: tuple[int, int, int],
                color_b: tuple[int, int, int],
                period_sec: float = 1.2) -> None:
    """Démarre un clignotement bicolore en background. Idempotent."""
    global _blink_thread
    stop_blink()
    _stop_evt.clear()
    _blink_thread = threading.Thread(
        target=_blink_loop,
        args=(color_a, color_b, period_sec),
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
                  hold_after: bool = True) -> None:
    """Stoppe le blink en cours, fait N flashs de `color`, puis maintient (ou éteint)."""
    stop_blink()
    for _ in range(n):
        off()
        time.sleep(flash_sec)
        set_color(*color)
        time.sleep(flash_sec)
    if hold_after:
        set_color(*color)
    else:
        off()


# ---------------------------------------------------------------------------
# Palettes pré-définies (duty cycle 0..100)
# ---------------------------------------------------------------------------
VIOLET = (20, 0, 40)   # plus de bleu que de rouge pour un violet franc
JAUNE  = (35, 20, 0)   # vert tiré pour rester chaud sans virer vert
VERT   = (0, 30, 0)
ROUGE  = (40, 0, 0)
