# LoRa — Spreading Factor & TX Power

## Spreading Factor (SF)

Le SF détermine le compromis vitesse / sensibilité. Plus le SF est élevé, plus la trame est longue mais plus le signal peut être faible.

| SF | Constante RadioHead        | Sensibilité | Durée trame (~20o) | Gain vs SF7 |
|----|----------------------------|-------------|---------------------|-------------|
| 7  | `Bw125Cr45Sf128`           | -123 dBm    | ~50 ms              | référence   |
| 9  | config custom BW125 (voir §SF9) | -129 dBm | ~120 ms           | +6 dB       |
| 12 | `Bw125Cr48Sf4096`          | -137 dBm    | ~2800 ms            | +15 dB      |

> RadioHead (version embarquée) ne propose pas SF9+BW125 en constante prédéfinie.
> La constante `Bw31_25Cr48Sf512` existe mais utilise BW=31.25 kHz — **à ne pas utiliser** (voir §SF9).

Le SF doit être **identique sur l'émetteur et le receiver** — une divergence = silence total.

### Durées réelles — formule

```
Durée symbole = 2^SF / BW_Hz

SF7  BW125 : 2^7  / 125000 = 1ms/symbole  → trame 20o ≈ 50ms
SF9  BW125 : 2^9  / 125000 = 4ms/symbole  → trame 20o ≈ 120ms
SF9  BW31  : 2^9  / 31250  = 16ms/symbole → trame 20o ≈ 500ms  ← PIÈGE
SF12 BW125 : 2^12 / 125000 = 33ms/symbole → trame 20o ≈ 2800ms
```

---

## TX Power

| Puissance | Courant de pointe RFM95 | Risque brownout (sans supercap) |
|-----------|--------------------------|----------------------------------|
| 5 dBm     | ~25 mA                  | Aucun                           |
| 14 dBm    | ~60 mA                  | Faible avec 100nF               |
| 20 dBm    | ~120 mA                 | Élevé sans supercap             |

Le brownout (reset Arduino au TX) survient quand le pic de courant fait chuter VCC sous 2.7V (seuil BOD du Pro Mini 3.3V — marge : 3.3 - 2.7 = 0.6V seulement).

**Protections hardware :**
- Condensateur 100 nF céramique entre VCC et GND au plus près du RFM95 — découplage HF uniquement (filtre les parasites ns, n'empêche pas le brownout)
- Condensateur **1 000–2 200 µF électrolytique** près du RFM95 — dimensionné pour fournir le courant manquant pendant le pic TX (calcul Pro Mini 3.3V : C = I×t/ΔV = 0,12×0,050/0,6 ≈ 10 000 µF pour SF7 seul — en pratique la supercap prend le relai). Tension nominale : 6.3V ou 10V suffisent (circuit 3.3V).
- **Supercap sur le rail RAW + fil direct court vers RFM95** — solution large spectre, couvre SF12 et alimentations limitées. Voir BROWNOUT_EMETTEUR.md pour le détail.

**Avec supercap 0.47F + fil direct (fix brownout déjà appliqué) :**
```
ΔV = I × R_chemin
SF9 14dBm : 60mA × ~20mΩ = 1.2mV → négligeable
SF7 20dBm : 120mA × ~20mΩ = 2.4mV → négligeable
```
Aucun condensateur supplémentaire requis pour passer SF9.

---

## SF9 — config custom BW125 (⚠ pas de constante RadioHead prédéfinie)

### Piège `Bw31_25Cr48Sf512`

La seule constante RadioHead approchant SF9 utilise **BW=31.25 kHz** au lieu de 125 kHz.
Conséquence : durée TX ×10 (~500ms au lieu de ~120ms) → brownout systématique même avec supercap.

**Validé terrain (2026-05-26) : tous les tests SF9 avec `Bw31_25Cr48Sf512` = reboot à chaque send.**

### Config correcte — registres manuels

```cpp
// Arduino — à la place de driver.setModemConfig(...)
RH_RF95::ModemConfig cfg = {
    0x72,  // REG_1D : BW=125kHz (0111), CR=4/5 (001), explicit header
    0x94,  // REG_1E : SF=9 (1001), CRC on
    0x00   // REG_26 : LowDataRateOptimize off (symbol time 4ms < seuil 16ms)
};
driver.setModemRegisters(&cfg);
driver.setTxPower(20, false);  // 20 dBm — 120mA, couvert par supercap 0.47F
```

```python
# Pi — raspi_lora, après init LoRa(...)
lora._spi_write(0x1D, 0x72)  # BW125 (0111), CR4/5 (001), explicit header
lora._spi_write(0x1E, 0x94)  # SF9 (1001), CRC on
lora._spi_write(0x26, 0x00)  # LowDataRateOptimize off
```

> **Validé terrain (2026-05-28)** — RSSI -111 à -123 dBm, trames consécutives sans perte.
> Note : 0x92 est incorrect (= BW 500 kHz, pas 125 kHz).

---

## Config actuelle (2026-05-29)

| Paramètre | Valeur | Fichier |
|-----------|--------|---------|
| SF        | SF9 BW125 (registres custom `{0x72, 0x94, 0x00}`) | `LinkyReader_RH_step3.ino`, `lora-receiver/main.py` |
| TX Power  | 20 dBm | `LinkyReader_RH_step3.ino` |

**Observations terrain (2026-05-29, chez Xavier) :**
- Maison (intérieur) : RSSI ≈ -96 à -105 dBm, SNR ≈ +7 à +10 dB — excellent
- Fond du jardin : RSSI ≈ -118 à -120 dBm, SNR ≈ -7 à -9 dB — limite mais stable
- 0 trame perdue sur 9 trames consécutives au fond du jardin
- Marge restante au fond : ~9 dB avant décrochage SF9

---

## Recommandations

- **Installation standard** (maisons classiques) : SF9 + 20 dBm — config de référence validée terrain
- **Obstacles extrêmes / cage de Faraday** : SF12 + 20 dBm — +9 dB supplémentaires, supercap impérative
- **Sans supercap** : rester à 5 dBm maximum pour éviter le brownout

---

## TODO

- [x] Valider les valeurs registres SF9 BW125 — validé terrain 2026-05-28
- [x] Implémenter SF9 BW125 sur l'émetteur + lora_v6.py — déployé 2026-05-28
- [x] Migrer vers SF9 — fait, marge insuffisante confirmée en SF7
