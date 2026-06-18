# Chantier — Trame LoRa v0x04 (courbe PAPP batchée) : aligner le LoRa sur le wired

> Statut : **implémenté côté code, validé hors device** (2026-06-16) · Touche :
> émetteur Arduino + récepteur Pi (pas de modif `db.py`) + release `pi0-lora`.
> Branche : `feat/lora-v0x04-curve`. Spec protocole : `rasperry/LORA_PROTOCOL.md §17`.

---

## 1. Objectif

Le chemin **wired** (`pi0-wired`) lit la TIC au fil de l'eau (~2 s), batche en BDD et
sert une **courbe PAPP fine** via `/curve`. Le chemin **LoRa** était resté sur la trame
**v0x02** (1 index cumulatif / ~32 s) — bon pour la jauge, **trop grossier** pour la
reconnaissance d'appareils (NILM). Objectif : faire délivrer au LoRa **la même courbe
fine que le wired**, stockée dans la **même table `measurements`** → `/curve` et
`/measurements` identiques sur les deux modèles. Et « récupérer autant que l'historique
peut délivrer » (toutes les trames TIC, ~1,7 s), tout en **préparant le mode standard**.

---

## 2. Décisions (verrouillées)

1. **v0x04 REMPLACE v0x02** côté émetteur. Le keyframe v0x04 est un **sur-ensemble** de
   v0x02 (`index_id` + `index_value` absolu + `papp_ref` + la courbe) → aucune perte
   fonctionnelle. Le récepteur **garde** le décodage v0x02/v0x01 pour la rétro-compat
   (Arduino pas encore reflashé).
2. **Keyframe = un seul index actif** (comme v0x02), pas tous les registres.
3. **Changement d'index (PTEC) pendant un batch → flush immédiat + nouveau keyframe.**
   Conséquence : chaque batch est **homogène en `index_id` par construction** (§17.3).
4. **Mode historique d'abord** : `ts_valid=0`, `period_ds=20` (2,0 s), `src_standard=0`,
   `has_ext=0`. Le **mode standard** (URMS/IRMS/SINSTI, horodate DATE absolue, bloc
   extension) est **différé** — bits et codec structurés pour l'accueillir sans refonte.
5. **Capture continue** (pas de deep-sleep) : on lit toutes les trames TIC. `LowPower`
   retiré du sketch (le watchdog vient de `<avr/wdt.h>`, pas de LowPower).
6. **Flush par batch toutes les ~60 s** (`CURVE_FLUSH_MS`) — cf. §6 (duty cycle).

---

## 3. Format de trame (résumé — détail dans §17.4)

```
0      version=0x04 · 1 flags(DEMAIN/ADPS/PEJP + ts_valid/src_standard/has_ext)
2-3    batch_seq · 4 index_id · 5-8 index_value(abs, 1er éch.) · 9-10 papp_ref
11     N · 12 period_ds · 13 ts_season · 14-19 ts(0 en histo)
20..   (N-1) deltas PAPP varint zig-zag (1-3 o) · [ext si has_ext] · len-8 HMAC(8)
```

HMAC-SHA256 tronqué 8, sur les octets `0..len-9` (longueur variable).

---

## 4. Implémentation

| Fichier | Rôle |
|---|---|
| `src/arduino/tic-reader/tic-reader.ino` | Émetteur : `curveStart/curveAdd/curveFlush`, coupe au changement d'index, capture continue, flush 60 s. v0x02 retiré. |
| `src/pi/lora-receiver/curve_codec.py` | Décodeur **pur** (HMAC, varint zig-zag, reconstruction, `anchor_timestamps`). Testable hors device. |
| `src/pi/lora-receiver/main.py` | Dispatch v0x04 (avant le gate de longueur fixe), `on_recv_curve`, `detect_batch_seq_event`, état `last_batch_seq`. |
| `src/pi/store/db.py` | **Inchangé** — réutilise `record_measurements_batch` + `curve_buckets`. |

### Stockage — l'index estampillé sur CHAQUE échantillon
`curve_buckets()` récupère l'index (`base/hchc/hchp/tariff`) via un JOIN sur le `ts_max`
de chaque bucket. Si seul le keyframe portait l'index, les buckets dont le dernier
échantillon n'est pas le keyframe renverraient un index **NULL** → coût-par-différence
cassé côté app. **Solution** : comme l'`index_id` est constant sur le batch (décision §3),
`on_recv_curve` estampille `{active_name: index_value}` sur **tous** les rows du batch.
→ `db.py` non modifié, release **`pi0-lora` seule** (pas `_shared`). *(Limite assumée,
identique au v0x02 : `_tariff_from_labels` ne mappe pas EJP/Tempo — cas courant BASE/HC/HP OK.)*

---

## 5. Bilan énergétique — pourquoi la capture continue passe

La capture continue interdit le deep-sleep (l'AVR reste éveillé ~5 mA), mais la **borne A
du Linky** est une vraie alim auxiliaire **~5 V / 50-100 mA** (§8.1), pas la sortie TIC.

| | Aujourd'hui (deep-sleep) | v0x04 (continu) |
|---|---|---|
| Courant moyen côté ligne | ~0,6 mA | **~6 mA** (MCU ~5 mA + TX ~1 mA) |

→ ~10× sous la capacité de la borne. Le **supercap 0,47 F** garde son rôle (tampon du pic
TX ~120 mA) ; la trame v0x04 plus grosse allonge le pic (~0,5 s) mais le droop reste
~0,1-0,2 V → bien au-dessus du brownout 2,7 V. **Pas de problème d'alim sur la topologie
borne A (ben01).** Un futur device **sans** borne A (batterie) imposerait une capture
**fenêtrée** (réintroduire LowPower) — hors v1.

---

## 6. Duty cycle & loi — pourquoi 60 s (et pas 30/15 s)

Bande **868,0 MHz = 1 % de duty cycle** (max **36 s d'antenne/h**). Le flush ne change PAS
la résolution de courbe (toujours ~2 s) — seulement la **fraîcheur de `/live`**. Airtime
SF9/BW125 :

| Cadence | Trame | Airtime | TX/h | Antenne/h | Duty |
|---|---|---|---|---|---|
| v0x02 (aujourd'hui) | 20 o | ~185 ms | 112 | ~21 s | 0,58 % ✓ |
| **v0x04 @ 60 s** | ~63 o | ~390 ms | 60 | ~23 s | **0,65 % ✓** |
| v0x04 @ 30 s | ~45 o | ~310 ms | 120 | ~37 s | ~1,03 % ✗ |
| v0x04 @ 15 s | ~36 o | ~270 ms | 240 | ~65 s | ~1,8 % ✗ |

Et c'est **avant** l'ACK + les retries (`acks=True`). **60 s** garde de la marge pour les
retries. 30 s est pile au plafond ; 15 s est ~2× au-dessus (même 20 o / 15 s ≈ 1,23 %).

**Levier pour viser 15 s** (différé) : passer émetteur+récepteur sur la sous-bande **g3
869,4-869,65 MHz (10 % duty)** → 15 s devient légal. **Changement 100 % logiciel** : le
RFM95 (SX1276) couvre 862-1020 MHz, même module / même antenne (écart 0,2 %, désaccord
négligeable) → juste `setFrequency()` des deux côtés. Bénéfices : 10 % de duty + **régularise
les 20 dBm actuels** (légaux en g3 ; au-dessus du max 14 dBm à 868,0). ⚠️ g3 autorise 27 dBm
mais le RFM95 plafonne à **+20 dBm** → pas de portée gratuite sans **PA externe (hardware)**.
= chantier radio séparé (les deux firmwares + revue de conformité), pas de nouveau matériel
pour le seul changement de fréquence.

---

## 7. Régression assumée — IINST

v0x02 portait IINST par trame ; le keyframe v0x04 historique non (IINST/IRMS est un champ
du **bloc extension**, différé). `/live.iinst` devient NULL côté LoRa. Mineur (PAPP est le
signal jauge ; IINST ≈ PAPP/230). Réintégrable à coût quasi nul via le bloc extension
(`has_ext=1`, bit IRMS, 1×/batch) quand le mode standard arrivera.

---

## 8. Déploiement & versions

- **Pi (OTA normal, `pi0-lora`)** : `pi.latest` 0.0.39 → **0.0.41** (le tag `pi-0.0.40`
  existe déjà pour le wired ; tags globaux → on saute à 0.0.41). Transition
  `updates/pi0-lora/0.0.39_to_0.0.41/update.sh` = restart `ben-lora-receiver` (pas de
  migration BDD). `.sha256` obligatoire. Commit sans `Co-Authored-By` ; tag GPG signé par
  Xavier.
- **Arduino (reflash manuel)** : `arduino.latest` 0.0.1 → **0.0.2** ; `minimum` reste
  0.0.1 (vieux Arduino v0x02 toujours compatibles). Reflash via `ben-ops/flash-arduino.sh`.
  La lib *Low-Power* n'est plus nécessaire (nettoyable côté ben-ops, sans urgence).
- **⚠️ Ordre CRITIQUE : Pi d'abord, Arduino ensuite.** Le nouvel Arduino n'émet plus de
  v0x02 ; un Pi pas encore en 0.0.41 rejetterait le v0x04 (gate de longueur) → **perte
  totale de données** en attendant.

---

## 9. Tests

- `src/pi/lora-receiver/test_v04_roundtrip.py` — codec : round-trip courbe (deltas 1/2/3 o,
  saut +2000/−2018 VA), HMAC/clé/longueur falsifiés rejetés, ancrage temps historique.
- `src/pi/lora-receiver/test_v04_store.py` — intégration `db.py` réel : v0x04 →
  `record_measurements_batch` → `curve_buckets` : index/tariff **non-NULL sur chaque
  bucket**, pic PAPP préservé, high-water mark alimenté.
- **Sur device** : compiler l'Arduino (vérifier la ligne RAM `… (2048 max)`), flasher
  ben01, regarder le série (`v04 seq= n= idx= len=`) ; OTA le Pi puis `journalctl -u
  ben-lora-receiver -f` (`v0x04 OK … batch_seq …`) ; `GET :8087/curve` → courbe ~2 s,
  index/tariff non-NULL, forme identique au wired ; `/lora-link` = 1 point RSSI/SNR par batch.

---

## 10. Statut

> Code écrit et validé hors device (2026-06-16). **Compile Arduino + validation terrain
> ben01 à faire.** Release non préparée (attend la validation terrain). Mode standard,
> bloc extension (IINST/URMS/IRMS/SINSTI) et sous-bande g3 = chantiers ultérieurs.
