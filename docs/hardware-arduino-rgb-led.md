# LED RGB Arduino — Câblage émetteur TIC/LoRa

L'Arduino Pro Mini émetteur utilise une LED RGB cathode commune à la place des 4 LEDs individuelles d'origine (LED_HB, LED_TIC, LED_LORA, LED_ERR).

## Brochage

| Couleur | Pin Arduino | PWM | Timer |
|---------|------------|-----|-------|
| 🟢 Vert  | 3          | Oui | Timer 2 |
| 🔴 Rouge | 5          | Oui | Timer 0 |
| 🔵 Bleu  | 6          | Oui | Timer 0 |

## Résistances

| Couleur | Résistance |
|---------|------------|
| 🔴 Rouge | 150Ω |
| 🟢 Vert  | 100Ω |
| 🔵 Bleu  | 100Ω |

## Schéma

```
Pin3 ── 100Ω ── anode G (🟢) ─┐
Pin5 ── 150Ω ── anode R (🔴) ─┤ LED RGB cathode commune
Pin6 ── 100Ω ── anode B (🔵) ─┘
                    cathode ── GND
```

## Signification des couleurs

| Événement | Couleur | Pattern |
|-----------|---------|---------|
| Heartbeat (début de cycle) | ⚪ Blanc | 1 flash 80ms |
| TIC valide | 🔵 Bleu | 1 flash 80ms |
| LoRa émis | 🟢 Vert | 1 flash 80ms |
| LoRa ACK reçu | 🟢 Vert | 2e flash 80ms (120ms après le 1er) |
| Erreur Vcc bas | 🔴 Rouge | 1 blink |
| Erreur TIC KO | 🔴 Rouge | 2 blinks |
| Erreur LoRa KO | 🔴 Rouge | 3 blinks |
| PTEC inconnu | 🔴 Rouge | 4 blinks |

## Notes

- Pin 3 était `ticOut` (enable TIC) dans les versions antérieures — déplacé en pin 4 pour libérer pin 3 pour le vert.
- Pins 5/6 partagent Timer 0 avec `millis()`/`delay()` — pas d'impact sur la LED RGB mais à garder en tête si on modifie les fréquences PWM.
- Pin 2 = interrupt LoRa (INT0) — ne pas utiliser.
- Pins 10/11/12/13 = SPI (RFM95) — ne pas utiliser.
