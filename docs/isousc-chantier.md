# Chantier — Remonter ISOUSC (abonnement souscrit) et calibrer la jauge + l'humeur de BEN

> Statut : **à faire** · Touche : firmware Pi (2 modèles) + Arduino émetteur + app.
> Objectif : récupérer l'**abonnement souscrit** du compteur (ISOUSC) sur les deux
> modèles, et s'en servir pour **calibrer la jauge de puissance** et (à articuler)
> **l'humeur de BEN** (le niveau 1–4).

---

## 1. Pourquoi

Aujourd'hui la jauge de puissance de l'app a un **maximum codé en dur à 9000 VA**
(`PowerGauge.maxVa = 9000`, non surchargé par le dashboard). Conséquence : un foyer
en 6 kVA voit du vert alors qu'il est proche de sa limite → **trompeur**.

`ISOUSC` (intensité souscrite, en A) est la **limite physique de l'abonnement**.
Couplée à la tension (~230 V en mono), elle donne la **puissance souscrite** :
`P_souscrite ≈ ISOUSC × 230` (VA). C'est la bonne référence pour dire « tu es à
X % de ton abonnement » (et donc du risque de disjonction).

Bonus : ISOUSC est une donnée **statique** (l'abonnement ne change quasi jamais).

> ### 🧭 Orientation (2026-06-09) — calibration UNIFIÉE jauge + perso, PAS sur ISOUSC
>
> Décision : on calibre la **jauge** et le **perso (humeur BEN)** **de la même
> façon**, sur le modèle **[talon, plafond]** que le firmware calcule déjà (les
> **habitudes du foyer**) — **pas** sur ISOUSC.
>
> *Pourquoi pas ISOUSC :* c'est la limite absolue, mais on ne s'en approche presque
> jamais → jauge scotchée dans le vert, perso figé. Le modèle talon/plafond est
> **relatif aux habitudes** → vivant, il bouge au quotidien.
>
> *Le signal partagé :* le ratio `(PAPP − talon)/(plafond − talon)` (déjà calculé,
> par PDL).
> - **Perso** = le niveau 1–4 (bandes du ratio) — déjà le cas.
> - **Jauge** = le **même ratio** en remplissage (talon = vide → plafond = plein),
>   **mêmes seuils de couleur** que les bandes de niveau.
> - → l'arc et la mascotte **disent toujours la même chose**, et c'est **dynamique**.
>
> *Implémentation :* le firmware expose le **ratio continu** (`level_ratio` 0..1)
> dans `/live`, en plus de `level`. App : arc = `level_ratio`, visage = `level`.
> (Gérer le **cold-start** comme le modèle de niveau : pas encore de plafond → ratio
> neutre / niveau 2.)
>
> *Rôle d'ISOUSC = l'ÉTALONNAGE (pas le remplissage direct).* Le remplissage au
> quotidien vient du ratio talon/plafond (vivant). Mais **ISOUSC calibre l'échelle**
> du modèle (cf. §4.3) :
> 1. **borne le plafond** : `plafond ≤ ISOUSC × 230` (limite physique) ;
> 2. **amorce le cold-start** : `ISOUSC × 230` comme plafond initial → juste dès le
>    jour 1, sans attendre d'avoir des données ;
> 3. donne une **échelle absolue** cohérente.
>
> *Conséquence pratique :* une **v1** de la jauge unifiée peut tourner **sans
> ISOUSC** (sur le plafond observé) → pas bloquée par le reflash Arduino. Mais le
> chantier ISOUSC **reste pleinement pertinent** : il rend l'étalonnage **juste et
> immédiat** (surtout au cold-start). Ordre : v1 sans ISOUSC, puis ISOUSC pour
> affiner l'étalonnage.

---

## 2. État actuel (constat code)

- **`grep isousc` dans tout le firmware = rien.** ISOUSC n'est lu/stocké/exposé
  **nulle part**, ni filaire ni LoRa.
- **Filaire** (`tic-reader/main_uart.py`) ne parse que les index (BASE/HC/HP/EJP/BBR),
  `IINST` et `PAPP`.
- **LoRa** — deux trames binaires :
  - **`v0x02` (mesure, 20 octets, SATURÉE)** :
    `version(1) · flags(1) · boot_seq(2) · index_id(1) · index(4 Wh) · IINST(1 A) · PAPP(2 VA) · HMAC(8)`
    → aucune place libre.
  - **`v0x01` (identité/boot)** :
    `version(1) · ADCO(12 ASCII)` → **c'est ici qu'on ajoute ISOUSC** (donnée
    statique, envoyée rarement, pas dans chaque mesure).
- **App** : `PowerGauge.maxVa` défaut **9000**, ratio = `PAPP / maxVa`, couleur
  vert < 60 % / orange < 85 % / rouge ≥ 85 %.
- **Niveau / humeur** (la mascotte 1–4) : calculé **côté firmware**, modèle
  « course [talon, plafond] » (percentiles de la conso du foyer), exposé dans
  `/live` (`level`). **Indépendant de la jauge.**

---

## 3. Le chantier — 3 volets

### Volet A — Acquisition filaire (rapide)
`tic-reader/main_uart.py` : ajouter le parsing de l'étiquette **`ISOUSC`** (entier,
en A). C'est dans chaque trame TIC, statique → on la lit, on la garde.

### Volet B — Acquisition LoRa (protocole — touche l'Arduino)
1. **Arduino émetteur** (`src/arduino/tic-reader/tic-reader.ino`) : lire `ISOUSC`
   dans la TIC et l'**ajouter à la trame `v0x01`** (la trame d'identité, envoyée au
   boot / périodiquement) :
   ```
   v0x01 étendu :
     0       version = 0x01
     1-12    ADCO (12 ASCII)
     13      ISOUSC  (uint8, A)        ← NOUVEAU
     [+ HMAC si v0x01 est signée — à vérifier/ajouter]
   ```
   > ISOUSC mono résidentiel tient sur un **uint8** (≤ 255 A). Si on veut couvrir
   > le triphasé / marges, passer en `uint16 LE`.
2. **Pi récepteur** (`lora-receiver/main.py`, bloc `version == 0x01`) : décoder le
   nouvel octet ISOUSC et le ranger dans l'état (`lora-state.json`) à côté d'`adco`.

> ⚠️ Implique un **reflash de l'Arduino** (cf. `ben-ops/flash-arduino.sh`) — donc
> ça ne se déploie pas par OTA Pi seul. À cadrer (parc LoRa = ben01 pour l'instant).

### Volet C — Stockage & exposition (commun aux 2 modèles)
- **Stocker** ISOUSC par PDL. Pistes : colonne dans une table `pdls`/profil, ou
  champ dans `device.json` (mais c'est par PDL, pas par device → plutôt en base).
- **Exposer** dans l'API locale : l'ajouter à **`/live`** (à côté de `papp`,
  `level`) et/ou à **`/health`**. L'app lit déjà `/live`.
- Migration SQLite si nouvelle colonne (`PRAGMA table_info` + `ALTER TABLE ADD`,
  comme pour `tariff` / `talon`).

---

## 3 bis. Transversal — ISOUSC est PAR PDL (lien avec le chantier multi-PDL)

ISOUSC dépend du **compteur (PDL)**, pas du boîtier. Un boîtier peut suivre
**plusieurs PDL** (ex. **ben-0003** branché sur 3 lignes → 3 PDL, chacun avec son
propre abonnement). Donc tout ce chantier doit être pensé **par PDL** :

- ISOUSC se stocke et s'expose **keyé par `pdl_index`** (comme `measurements`, déjà
  keyé pdl_index) — surtout **pas** un champ global du device.
- La **jauge** et l'**humeur** sont **par PDL** : quand l'app affiche le PDL
  « Maison », elle prend l'ISOUSC, le niveau et la calibration **de ce PDL**.
- Ça s'inscrit dans le **chantier multi-PDL** (direction produit : « le contexte de
  l'app, c'est le PDL ; le boîtier est sous-jacent »). ISOUSC est **une donnée de
  plus à porter par PDL** → à intégrer dans le **même modèle** que le reste (pas un
  cas à part). Quand on fera le multi-PDL, ISOUSC suit.
- Côté **LoRa**, c'est naturel : `pdl_index` est déjà dérivé de l'adresse émettrice
  (`sources.json`) et la trame `v0x01` porte l'**ADCO** du PDL → l'ISOUSC ajouté à
  cette trame est rattaché **au bon PDL** automatiquement.

> 👉 À faire **ensemble** : ne pas câbler ISOUSC « mono-PDL en dur » comme l'app
> l'est encore aujourd'hui — le poser directement dans la logique **par PDL**.

---

## 4. Usage app — jauge + humeur BEN, **un seul signal** (décidé)

**Décision (cf. orientation §1).** La jauge et le perso sont pilotés par le **même
signal** : le ratio `(PAPP − talon)/(plafond − talon)` du modèle de niveau. ISOUSC
ne pilote pas le remplissage — il **étalonne l'échelle** (§4.3).

### 4.1 La jauge (l'arc)
- Remplissage = le **ratio talon/plafond** (vide au talon, plein au plafond) —
  **pas** `PAPP / maxVa`. → l'arc bouge dans la plage réelle du foyer.
- Couleur = **les mêmes bandes** que le niveau (seuils `0.10 / 0.40 / 0.70` du
  modèle) → l'arc et le visage sont **toujours cohérents**.
- Source : le firmware expose le **ratio continu** `level_ratio` (0..1) dans `/live`,
  en plus de `level`. App : `arc = level_ratio`, `visage = level`.
- Cold-start (pas encore de plafond) : ratio neutre / niveau 2, comme le modèle.

### 4.2 L'humeur de BEN (le niveau 1–4)
Inchangée : c'est le `level` 1–4 (bandes du même ratio). Donc « endormi → chaud »
suit exactement le remplissage de l'arc. **Un seul signal, deux représentations.**

### 4.3 ISOUSC dans l'étalonnage du modèle
ISOUSC ne remplit pas la jauge, mais **calibre l'échelle** `[talon, plafond]` :
1. **Borne haute** : `plafond ≤ ISOUSC × 230` — physiquement on ne peut pas tirer
   plus que l'abonnement (sinon le disjoncteur saute). Garde-fou anti-plafond aberrant.
2. **Amorce cold-start** : avant assez de données, `plafond initial = ISOUSC × 230`
   → jauge et perso **justes dès le jour 1**, au lieu du défaut « niveau 2 ».
3. **Échelle absolue** cohérente (comparable entre foyers si besoin).

> Donc : **v1 sans ISOUSC** (sur le plafond observé) possible tout de suite ; puis
> **ISOUSC** pour rendre l'étalonnage **juste et immédiat** (surtout au démarrage).
> Là où ISOUSC change vraiment les choses : le **cold-start** et le garde-fou.

---

## 5. Points d'attention

- **HMAC trame v0x01** : vérifier si elle est signée (la v0x02 l'est, octets 12-19).
  Si on ajoute ISOUSC à v0x01, l'intégrer à la zone signée. Sinon un nouveau PDL +
  ISOUSC pourrait être falsifié.
- **Rétro-compat** : un récepteur à jour doit tolérer une **ancienne** v0x01 (sans
  ISOUSC) → ISOUSC optionnel, longueur de trame variable gérée proprement.
- **OTA** : volet A + C (Pi) passent par l'OTA firmware normal. Volet B (Arduino)
  nécessite un **reflash physique** → à planifier (parc LoRa restreint aujourd'hui).
- **Triphasé** : ISOUSC peut être par phase. Le mono couvre le gros du parc ; noter
  le cas tri pour plus tard (uint16, et puissance = ISOUSC × 230 × √3 ou par phase).

---

## 6. Étapes (ordre conseillé)

**Lot 1 — Jauge unifiée (sans ISOUSC) — priorité, gain immédiat**
1. Firmware : exposer le **ratio continu** `level_ratio` (0..1) dans `/live`
   (à côté de `level`) — `levels.py` + `local_api.py`. Pas de nouvelle donnée
   compteur, juste exposer ce qui est déjà calculé.
2. App : `PowerGauge` piloté par `level_ratio` (remplissage) + bandes de couleur
   alignées sur le niveau ; visage = `level`. Retirer le `maxVa = 9000` en dur.
   Cold-start = ratio neutre / niveau 2.
   → jauge + perso cohérents et vivants, **sans toucher au protocole LoRa**.

**Lot 2 — ISOUSC pour l'étalonnage**
3. **Filaire** (volet A + C) : parse `ISOUSC`, stocke **par PDL**, expose dans `/live`.
4. `levels.py` : utiliser ISOUSC pour **borner le plafond** (`≤ ISOUSC×230`) et
   **amorcer le cold-start** (plafond initial = ISOUSC×230). → étalonnage juste.
5. **LoRa** (volet B) : étendre `v0x01` (Arduino + récepteur), reflash Arduino,
   vérifier sur ben01. *(Sépare bien : le reflash Arduino n'est nécessaire que
   pour ISOUSC en LoRa, pas pour la jauge unifiée du Lot 1.)*

**Transverse** : poser ISOUSC + ratio **par PDL** dès le départ (cf. §3 bis,
chantier multi-PDL).

---

## 7. Fichiers concernés

| Fichier | Volet |
|---|---|
| `src/pi/tic-reader/main_uart.py` | A — parse ISOUSC (filaire) |
| `src/arduino/tic-reader/tic-reader.ino` | B — ISOUSC dans la trame v0x01 |
| `src/pi/lora-receiver/main.py` | B — décode ISOUSC (bloc `version == 0x01`) |
| `src/pi/store/db.py` | C — stockage (colonne / migration) |
| `src/pi/store/local_api.py` | C — exposer dans `/live` (et/ou `/health`) |
| `ben-app/.../widgets/power_gauge.dart` + `dashboard_screen.dart` | App — `maxVa` = ISOUSC×230 |
| `src/pi/store/levels.py` | (si Option C) borner le plafond par ISOUSC |
