# LED RGB — Câblage standard

Tous les modèles Pi BEN utilisent une LED RGB cathode commune à la place de 4 LEDs individuelles.

## Brochage

| Couleur | GPIO BCM | Pin physique | PWM |
|---------|----------|-------------|-----|
| Rouge   | 12       | 32          | Hardware PWM0 |
| Vert    | 13       | 33          | Hardware PWM1 |
| Bleu    | 16       | 36          | Software PWM  |

## Résistances

Une résistance par anode (côté GPIO), pas de résistance sur la cathode.

| Couleur | Résistance |
|---------|------------|
| Rouge   | 150Ω |
| Vert    | 100Ω |
| Bleu    | 100Ω |

## Schéma

```
GPIO12 ── 150Ω ── anode R ─┐
GPIO13 ── 100Ω ── anode G ─┤ LED RGB cathode commune
GPIO16 ── 100Ω ── anode B ─┘
                   cathode ── GND
```

## Notes

- LED RGB **diffusée** recommandée pour le mélange des couleurs. Une LED claire peut être poncée (papier de verre 400+).
- Sur Pi Zero W : `dtoverlay=miniuart-bt` requis dans `config.txt` pour libérer GPIO12 (PWM0) du Bluetooth. Configuré automatiquement par `install.sh`.
- Le bleu (GPIO16) est en software PWM — imperceptible pour un usage LED de statut.

## Indicateur de boot (blanc au démarrage)

Ajouter dans `config.txt` pour allumer la LED en blanc dès le firmware, avant l'OS :

```
gpio=12=op,dh
gpio=13=op,dh
gpio=16=op,dh
```

La LED s'allume en blanc pleine luminosité pendant le boot (~20s). Le service éteint tout proprement dans `setup_led()` avant de passer en PWM. Pas de configuration supplémentaire nécessaire.
