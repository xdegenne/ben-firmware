# Chantier — Stockage générique des index d'énergie, bi-mode TIC (historique + standard)

> Statut : **préparé, pas lancé** (2026-06-12) · Touche : **schéma DB** (→ OTA +
> migration) + reader (filaire & LoRa) + app. Objectif : **stocker TOUS les index
> d'énergie** (tous tarifs, les deux modes TIC, l'injection) sous une forme
> **générique unique**, pour permettre à terme un coût **à l'euro près** — sans
> jamais perdre de donnée en route.
>
> Croise : `ben-firmware/docs/tic-standard-mode.md`, `rasperry/LORA_PROTOCOL.md`
> §12 (v0x02 `index_id`) et §17.9 (v0x04 bi-mode), `chantier-courbe-temps-reel.md`,
> `isousc-chantier.md`.

---

## 1. Le problème (constat code)

Le reader **parse tous les index** mais le **schéma n'en garde que 3** :

- `main_uart.py` : `INDEX_LABELS` couvre BASE, HCHC, HCHP, EJPHN, EJPHPM, et les
  **6 registres Tempo/BBR** (`BBRHCJB/HPJB/HCJW/HPJW/HCJR/HPJR`). Il calcule même
  l'index actif : `active_name, active_value = select_active_index(ptec, labels)`.
- `db.py` : table `measurements(... base, hchc, hchp ...)` → **on ne stocke que
  BASE + HC/HP**. EJP et **Tempo** sont **parsés puis jetés**.

**Conséquence** : un foyer **Tempo** (offre EDF qui monte) ou **EJP** (legacy) a
`base/hchc/hchp` tous `null` → `totalIndex = null` → **conso/coût impossibles**.
La courbe PAPP marche (papp stocké), mais le « X Wh ≈ Y € » est mort pour eux.

**Ambition** : estimer le coût **à l'euro près** → il faut **chaque registre** (chacun
à son prix), donc **tout stocker**. Une simple somme (`index_total`) détruirait la
ventilation par tarif → écartée.

---

## 2. L'insight : registre inactif = gelé → un seul `(index_id, index_value)` suffit

Un registre tarifaire **n'incrémente que pendant sa période**. Inactif, il **ne bouge
pas**. Donc il suffit de stocker, à chaque mesure, **le registre ACTIF** sous forme
`(index_id, index_value)` : la **dernière valeur connue de chaque `index_id` = sa
valeur courante** (les autres sont gelés à leur dernière valeur active).

→ **Pas de table sparse à 11 colonnes**, pas de mapping lossy. Reconstruction par
**report en avant** (carry-forward) : valeur du registre X à l'instant t = dernière
valeur stockée d'`index_id=X` ≤ t.

- **HC/HP** : conso = `ΔHCHC + ΔHCHP` (et plus tard `×prix` chacun) → exact.
- **Tempo** : 6 registres, chacun capturé quand actif → exact.
- **Base / EJP** : idem.

C'est **déjà** ce que fait le LoRa v0x02 (`index_id` + `index` 4 o, table §12.4).
On le **généralise** au filaire et au standard.

---

## 3. Le mode STANDARD valide l'approche (il EST indexé nativement)

| | Historique | Standard |
|---|---|---|
| Registres | nommés : `BASE` / `HCHC`,`HCHP` / 6× `BBR…` | **`EASF01`…`EASF10`** (10 index fournisseur) |
| Total | (à sommer) | **`EAST`** (total direct) |
| Index actif | déduit de `PTEC` (`select_active_index`) | **`NTARF`** (n° tarif courant → quel `EASFxx`) |
| Injection (solaire) | — | **`EAIT`** |

Standard fournit **explicitement** `(index_id = NTARF, index_value = EASF[NTARF])`.
Historique le **reconstruit** via `select_active_index`. **Même forme de stockage.**

---

## 4. Le modèle de stockage — `(src_standard, index_id, index_value)`

**Principe directeur : le firmware stocke du GÉNÉRIQUE et BRUT — il n'a AUCUNE
intelligence tarifaire.** C'est ça qui facilite la compat bi-mode : seule la couche
**lecture** (parseur) est mode-spécifique ; le **schéma** est mode-agnostique.

```
mesure → (src_standard, index_id, index_value)
```

- **`src_standard`** (flag 0/1, déjà dans v0x04 §17.9) — quel namespace d'index_id.
- **`index_id`** — registre actif dans son **namespace natif** :
  - `src_standard=0` (historique) : `PTEC_MAP` `0x00..0x0A` (BASE=0, HCHC=1, HCHP=2,
    EJPHN=3, EJPHPM=4, BBRHCJB=5 … BBRHPJR=10).
  - `src_standard=1` (standard) : **n° EASF** `1..10` (= `NTARF`).
- **`index_value`** — valeur Wh du registre actif (`active_value` filaire / `index`
  LoRa / `EASF[NTARF]` standard).

> Le couple **`(src_standard, index_id)`** identifie un registre de façon globale et
> non ambiguë. C'est la clé du modèle de prix (§7).

### Injection (autoconso/revente) — extension naturelle

`EAIT` (standard) n'est pas un registre tarifaire « actif » mais un **total injecté**
concurrent du soutirage. On le capte comme une **donnée à part** (colonne dédiée
`inject_total` ou `index_id` réservé), présent seulement en standard. → branche
l'autoconso (cf. l'angle mort PV de `tic-standard-mode.md` §annexe) **dans le même
modèle**, sans cas spécial lourd.

---

## 5. Schéma & migration

Schéma actuel `measurements(ts, pdl_index, base, hchc, hchp, papp, iinst, tariff, sent)`.

**Cible** : remplacer `base/hchc/hchp` par **`index_id` + `index_value`** (+ éventuel
`inject_total`). `tariff` existe déjà mais via `tariff_from_ptec` qui **simplifie à
0–4 et ignore Tempo** → à **généraliser** sur tout `PTEC_MAP` (ou fusionner avec
`index_id`).

**Migration (idempotente, comme `tariff`/`talon`)** :
1. `ALTER TABLE ADD COLUMN index_id INTEGER`, `index_value INTEGER`,
   `src_standard INTEGER DEFAULT 0` (+ `inject_total INTEGER` si retenu).
2. **Garder `base/hchc/hchp`** le temps de la transition (double écriture possible)
   → pas de rupture pour l'app pas-à-jour (degrade-safe, cf. courbe).
3. Backfill optionnel : `index_id/index_value` dérivables de `tariff`+colonnes
   existantes pour l'historique déjà stocké.

> ⚠️ Schéma = **OTA + migration** → release signée, prudence (cf. `isousc-chantier.md`
> qui touche aussi schéma). Tester sur ben-0003 d'abord.

---

## 6. Reconstruction & conso (app + cloud)

Maintenir, par `(pdl_index, src_standard, index_id)`, la **dernière valeur**. Pour une
période `[t1, t2]` :

```
conso_registre(X) = valeur(X ≤ t2) − valeur(X ≤ t1)        (carry-forward)
conso_totale      = Σ_X conso_registre(X)
```

Cross-checks gratuits en standard : `EAST` (total) ≈ Σ EASF ; `EAIT` pour l'injection.

---

## 7. Le prix — DIFFÉRÉ (moyenne unique pour l'instant)

**Décision (2026-06-12) : le modèle de prix par tarif est un chantier ULTÉRIEUR.**
Pour l'instant on **reste sur une moyenne unique** (`avgKwhPrice` côté app).

**Mais le point clé : on stocke TOUT dès maintenant (lossless).** Donc :

- **Gain immédiat** : `conso_totale` (Σ de tous les registres) devient correcte pour
  **100 % des tarifs** (y compris Tempo/EJP), avec le prix moyen → coût total juste.
  C'est ce qui **débloque** les foyers Tempo/EJP, tout de suite.
- **Plus tard** : une **config prix keyée par `(src_standard, index_id)`** (issue de
  l'abonnement) → coût **par registre** = `Σ conso_registre(X) × prix(X)` → **à l'euro
  près**, **rétroactivement** sur la donnée déjà stockée. Aucune donnée perdue à
  rattraper.

→ On ne code PAS le modèle de prix dans ce chantier. On garantit juste que la donnée
nécessaire est **là** et **complète**.

---

## 8. Compat bi-mode — ce que l'approche facilite

- **Schéma identique** historique ↔ standard (générique) → un seul stockage, une
  seule logique app/cloud de conso.
- **Seul le parseur diffère** (il diffère déjà : 1200 vs 9600, étiquettes) → il remplit
  le même `(src_standard, index_id, index_value)`.
- **Auto-détection du mode** (cf. `tic-standard-mode.md`) → `src_standard` est posé au
  boot, transparent pour le reste.
- **Aligné LoRa** : `index_id` + `src_standard` existent déjà dans v0x04 §17.9 → le
  filaire et le LoRa convergent sur le même modèle.

---

## 9. Étapes (quand on lancera)

1. **Schéma** (`db.py`) : colonnes `index_id`/`index_value`/`src_standard`
   (+ `inject_total` ?), migration idempotente, double écriture transitoire.
2. **Reader filaire** (`main_uart.py`) : stocker `(index_id = rang PTEC_MAP,
   index_value = active_value, src_standard=0)`. Généraliser le mapping tarif sur tout
   `PTEC_MAP`.
3. **Reader LoRa** (`lora-receiver`) : déjà `index_id`/`index` → mapper vers le même
   schéma ; poser `src_standard` selon flag v0x04.
4. **Parseur standard** (quand le mode standard sera lu, cf. `tic-standard-mode.md`) :
   `index_id = NTARF`, `index_value = EASF[NTARF]`, `src_standard=1`, `inject_total =
   EAIT`.
5. **App / API** : conso par carry-forward (Σ registres) × prix moyen (inchangé pour
   l'instant). Exposer la conso totale correcte tous tarifs.
6. *(Chantier ultérieur)* : modèle de prix par `(src_standard, index_id)` → coût à
   l'euro près, rétroactif.

---

## 10. Fichiers concernés

| Fichier | Rôle |
|---|---|
| `src/pi/store/db.py` | schéma + migration + écriture `(index_id, index_value, src_standard)` |
| `src/pi/tic-reader/main_uart.py` | stocker l'index actif générique (filaire) |
| `src/pi/lora-receiver/main.py` | mapper `index_id`/`index` LoRa → schéma |
| `src/arduino/.../*.ino` | (déjà) émet `index_id` ; standard → ajouter EASF/NTARF/EAIT |
| `ben-app/.../live_reading.dart` + conso/coût | conso par carry-forward, prix moyen |

---

## 11. Statut

> Conception (2026-06-12). **DORSALE STOCKAGE IMPLÉMENTÉE en pi-0.0.43 (2026-06-23)**,
> dans la foulée du chantier mode standard (`tic-standard-mode.md`). Fait :
> - **Schéma** (`db.py`) : colonnes `src_standard`, `index_id`, `index_value`,
>   `inject_total`, `meter_ts` ajoutées (migration `ALTER` ×5 idempotente, non
>   bloquante, double-écriture `base/hchc/hchp` conservée). Validé live sur ben-0004.
> - **Reader wired** (`main_uart.py`) : remplit le générique (histo : index_id=rang
>   PTEC ; standard : index_id=NTARF, index_value=EASF[NTARF]) + inject_total + meter_ts.
> - **Récepteur LoRa** (`main.py`/`curve_codec.py`) : idem depuis le v0x04 (NTARF non
>   mappé sur INDEX_NAMES — opaque) ; meter_ts depuis l'horodate de la trame.
> - **API** : `/live` expose `tic_mode` (standard/historique).
>
> **Toujours différé** (cf. §7) : le **modèle de prix par tarif** (coût « à l'euro
> près ») → on reste sur la **moyenne unique** ; et la **reconstruction conso par
> carry-forward côté app/cloud** (Σ registres). On capture TOUT sans perte ; le calcul
> de coût par registre viendra (rétroactif sur la donnée déjà stockée).
