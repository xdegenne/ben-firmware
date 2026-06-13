# Chantier — Courbe temps réel : lecture UART au fil de l'eau, écritures batchées, app intelligente, mode anti-Hawthorne

> Statut : **préparé, pas lancé** (2026-06-11) · Touche : firmware Pi filaire
> (`pi0-wired`) + app. Objectif : transformer le pipeline de courbe pour qu'il
> **capte tout** (lecture continue), **écrive peu** (batch), **serve léger**
> (downsampling à la lecture) et **n'influence pas la baseline au début** (mode
> anti-Hawthorne, désactivable en douce).
>
> ### 🎯 Périmètre & répartition (décidé 2026-06-11) — on fait tout, D côté app
>
> Lancement complet. **Répartition firmware / app tranchée** :
> - **A (lecture UART fil de l'eau)** + **B (batch BDD + snapshot live tmpfs)** +
>   **C (`/curve` agrégé, `/measurements` degrade-safe, `first_ts` par PDL)** =
>   **firmware**.
> - **D (fenêtre dégressive anti-Hawthorne + toggle caché)** = **APP (Dart)** —
>   décision : **c'est de la présentation pure**, donc côté app (pushable Shorebird,
>   itération rapide). Le firmware **ne lisse pas** ; il sert la courbe fidèle et
>   expose `first_ts` pour que l'app calcule l'âge d'apprentissage du PDL et
>   dimensionne SA fenêtre.
>
> **A et B livrés ensemble** (A seul augmente le débit d'écriture → B compense
> l'usure SD ; indissociables en prod).
>
> **Compat firmware-sans-app-à-jour** : `/live` reste compatible (mêmes champs +
> temps réel via le snapshot). L'anti-Hawthorne n'étant plus firmware, plus de
> risque de « protection contournée ». Seul reste l'afflux de points sur
> `/measurements` → réglé en le rendant **degrade-safe** (agrège à `limit` buckets
> au lieu de tronquer aux plus vieux). → firmware **déployable sans coupler l'app**.
>
> Préalable conceptuel : toute la réflexion qui fonde ce chantier est dans la
> conversation TIC/courbe — voir aussi `docs/learning-period-curve-smoothing.md`
> (lissage dégressif), `rasperry/LORA_PROTOCOL.md` §17 (v0x04 = pendant LoRa, dont
> ce chantier est le **pendant UART**), et `ben-firmware/docs/tic-standard-mode.md`
> (cadence/finesse selon le mode).

---

## 1. Pourquoi

Quatre besoins, un seul pipeline :

1. **Lire au fil de l'eau** — la TIC est un flux continu (une trame ~toutes les
   1,5–2 s en historique, ~1 s en standard). Aujourd'hui on **poll avec un
   `sleep(PERIOD_S)`** → on traite une trame puis on dort, les suivantes rancissent
   dans le buffer série → **jitter d'échantillonnage + staleness**. On veut suivre
   le rythme du compteur, sans timer.
2. **Batcher les écritures BDD** — aujourd'hui **un `commit()` par mesure** = un
   fsync par trame. À ~0,5–1 Hz, en continu, c'est de l'**usure SD** et des I/O
   inutiles. Grouper les écritures = ×N durée de vie SD + perf.
3. **App plus intelligente** — une plage large (1 semaine à 1 Hz ≈ **600 k points**)
   ne doit **jamais** être servie brute : requête lourde, transfert lourd, écran
   ~1000 px. Il faut **rééchantillonner à la lecture** (agrégats par bucket).
4. **Mode anti-Hawthorne** — montrer une courbe fine et datée dès le jour 1 change
   le comportement de l'utilisateur (effet d'observation) → on perd la **baseline
   naturelle**. On veut une courbe **volontairement lissée au début**, qui se
   resserre semaine après semaine, **avec un interrupteur caché pour la désactiver**
   (dev / power-user / tests).

> ⚠️ Couplage : les volets A et B sont **indissociables**. La lecture au fil de
> l'eau **augmente** le débit d'écriture → sans le batch (B), on aggrave l'usure SD.
> A sans B = régression. À mener ensemble.

---

## 2. État actuel (constat code)

- **`tic-reader/main_uart.py`** : `while True:` (l.313) → `read_frame()` →
  `db.record_measurement(...)` (l.358) → **`sleep(PERIOD_S)`** (l.380). Donc
  **polling**, une trame par période, pas event-driven. Watchdog séparé (l.273) OK.
- **`store/db.py`** :
  - `connect()` : **WAL + `synchronous=NORMAL` déjà actifs** (l.92-93) → durabilité/
    perf déjà bien réglés. ✅ (moitié du volet B acquise)
  - `record_measurement()` : `INSERT` measurements **+ update `level_profile`
    (papp_max au fil de l'eau, pas de `SELECT MAX`) + `commit()`** (l.159-186) →
    **un commit par mesure**.
  - `prune()` : purge par rétention (`RETENTION_DAYS`). OK, à préserver.
  - Schéma `measurements(ts, pdl_index, base, hchc, hchp, papp, iinst, sent[, tariff])`,
    keyé `pdl_index`, index sur `(pdl_index, ts)` et `(pdl_index, papp)`. Bon socle
    pour des `GROUP BY` temporels.
- **`store/local_api.py`** : sert l'API locale (`/live`, etc.). À auditer : quel
  endpoint sert la **courbe**, et renvoie-t-il du **brut** ?
- **`store/levels.py`** : modèle niveau `[talon, plafond]` par PDL (lié à la jauge,
  cf. `isousc-chantier.md`).

---

## 3. Le chantier — 4 volets

### Volet A — Lecture UART au fil de l'eau (event-driven)

**But** : remplacer le `while + sleep(PERIOD_S)` par une **boucle de lecture
continue** : parser **chaque trame à son arrivée** (jusqu'au `\n`/checksum), sans
timer. La cadence d'échantillonnage **devient** celle du compteur (~2 s historique,
~1 s standard).

- Supprimer `sleep(PERIOD_S)` ; `read_frame()` bloque déjà sur le port → la boucle
  se cale naturellement sur le débit TIC.
- **Pas de timer fixe** (évite l'aliasing trame ~1,7 s vs timer 2 s).
- **Doublons de PAPP** (paliers historique) : normaux → on garde tel quel (ou dédup
  optionnelle, mais on échantillonne toujours à la trame).
- **Horodatage** : à la réception en historique (pas de `DATE`), du champ `DATE` en
  standard (cf. `tic-standard-mode.md`).
- Préserver le **watchdog** et le `last_success_time`.

> Sortie de ce volet : on capte **toute** la courbe que le compteur émet, sans perte
> ni jitter d'échantillonnage. C'est le **pendant UART de v0x04** — sans la
> compression delta/keyframe, inutile sur fil (pas de contrainte d'airtime).

### Volet B — Écritures BDD batchées

**But** : ne plus committer par mesure. Accumuler en RAM, **flusher en une
transaction** tous les **N échantillons OU toutes les T secondes** (premier atteint).

- Nouvelle API d'écriture batchée dans `db.py` (`record_measurements_batch()` /
  `executemany` dans **une** transaction) ; `record_measurement` peut rester pour le
  LoRa (cadence ~1/32 s, batch inutile là-bas).
- Batcher **aussi** l'update `level_profile` (papp_max) dans la même transaction.
- WAL + `synchronous=NORMAL` déjà là → garder.
- **Granularité de perte = le batch** (principe v0x04 §17.3) : un crash perd le
  dernier batch non flushé. Garder **batch court (~30–60 s)** → perte négligeable,
  courbe append-only non critique à la seconde.
- Flush **aussi** sur arrêt propre (SIGTERM) pour ne pas perdre le batch courant.
- Paramètres : `BATCH_SIZE` / `BATCH_MAX_AGE_S` dans `settings.py`.

> Dimensionnement : même logique « écriture batchée » que le LoRa documente déjà
> (`LORA_PROTOCOL.md` §usure flash). A + B ensemble : on capte tout **et** on
> ménage la SD.

### Volet C — App plus intelligente (downsampling à la lecture)

**But** : l'API locale ne sert **jamais** la courbe brute sur une plage large. Elle
**agrège par bucket temporel**, à une résolution **adaptée à la fenêtre demandée**.

- Endpoint courbe paramétré : `from`, `to`, et `resolution`/`bucket` (ou dérivé du
  range). Vue mois → 1 pt/heure ; vue heure → pleine résolution.
- **Agrégation préservant les pics** : par bucket, renvoyer **min + max + avg**
  (pas une simple moyenne — la moyenne **tue les pics**, or les pics **sont** le
  signal NILM). Alternative graphe : **LTTB** (Largest Triangle Three Buckets) pour
  une décimation visuellement fidèle.
- **⚠️ Jamais de décimation naïve** (« 1 point sur N ») : perd les transitoires.
- Implémentation Pi (SQLite) : `GROUP BY` sur un bucket calculé (`ts / bucket`),
  `MIN(papp), MAX(papp), AVG(papp)`. Plus tard : petites **tables de rollup**
  (horaire/journalier) si le `GROUP BY` à la volée coûte trop.
- App : demander la résolution **= largeur viewport** (~1–2 k points max), pas plus.
- **Le fin reste TOUJOURS en base** ; on ne sert que du résumé. (Cohérent avec
  « le backend garde le fin » de `learning-period-curve-smoothing.md`.)

> Côté cloud (`ben-api`, TimescaleDB), l'équivalent natif = **continuous
> aggregates**. Sur Pi (Phase 1, SQLite) = `GROUP BY` / rollups maison.

### Volet D — Mode anti-Hawthorne (lissage dégressif, désactivable en douce)

**But** : pendant la **période d'apprentissage** (~4–6 semaines, par PDL), la
**courbe** est lissée par une **fenêtre dégressive** pour **désamorcer
l'attribution** (un pic ne doit pas être rattachable à un geste) — **sans** toucher
à la **jauge/humeur**, qui restent temps réel (non-attribuables). Voir
`learning-period-curve-smoothing.md` pour la justification complète.

- **Fenêtre = fonction de l'âge d'apprentissage du PDL** : ~**2–3 h** au début (fait
  fondre four/lave-linge), resserrée par paliers hebdo jusqu'au réel (~4–6 sem).
- **Où l'appliquer** : dans l'endpoint courbe du **Volet C** (la fenêtre devient un
  paramètre dérivé de l'âge du PDL) → un seul point de vérité, cohérent app + futur
  cloud. **Le stockage reste fin** ; seul l'affichage est lissé.
- **Distinction jauge / courbe** (déjà actée) : jauge = temps réel (spectacle) ;
  courbe = calme, apprend en silence. Ce volet ne concerne **que la courbe**.
- **Curseur** = « à partir de quand un pic devient attribuable », pas « à quel point
  je floute ». Règle : fenêtre **> durée d'un événement appareil**.
- **Toggle caché de désactivation** (`possibilité cachée de le désactiver`) :
  - **Pourquoi caché** : ce n'est pas un réglage grand public (ça réintroduit
    l'attribution prématurément, perd la baseline) ; c'est pour **dev / tests /
    power-user averti**.
  - **Forme** : un flag qui force la résolution **fine** (bypass de la fenêtre
    dégressive), accessible par un **geste discret / écran debug caché** (ex.
    appui long répété sur un élément, ou réglage non listé), **pas** une option
    visible dans les préférences.
  - **Honnêteté** : le lissage est de la **divulgation progressive assumée** (BEN
    affine sa vision les premières semaines), **pas** du masquage à l'insu — la
    confiance est l'actif n°1. Le toggle caché est un **raccourci technique**, le
    discours produit reste transparent.
  - **Portée** : la **fenêtre** (lissage) est **par PDL** (calculée sur l'âge
    d'apprentissage de chaque compteur — sur un boîtier multi-PDL, chacun à son
    stade). Le **toggle de désactivation**, lui, est **GLOBAL** (décidé
    2026-06-11) : un seul flag `anti_hawthorne` dans `settings.json` qui coupe le
    lissage partout — interrupteur dev/power-user caché, pas la peine de le
    granulariser par PDL.

> Piège à surveiller (du doc lissage) : trop lisser = courbe plate et ennuyeuse →
> l'utilisateur décroche. La jauge fait le spectacle, la courbe apprend en silence ;
> trouver le curseur qui tue l'attribution **sans** rendre la vue inutile.

---

## 4. Points d'attention

- **A+B indissociables** (cf. §1) : ne pas livrer A sans B.
- **Concurrence reader/API** : WAL permet lecture API pendant écriture reader. Le
  batch allonge la fenêtre entre commits → vérifier que l'API (jauge `/live` temps
  réel) lit bien la **dernière** valeur, pas seulement le dernier batch flushé →
  exposer la valeur **courante en RAM** (la jauge ne doit pas attendre le flush).
  **C'est le point subtil** : jauge = temps réel (RAM), courbe = batché (BDD).
- **Crash / SIGTERM** : flusher le batch courant à l'arrêt propre.
- **Par PDL** partout (courbe, fenêtre d'apprentissage, rollups) — cohérent avec le
  chantier multi-PDL et `isousc-chantier.md`.
- **Mode TIC** : la finesse de la courbe dépend du mode (historique ~2 s/paliers vs
  standard ~1 s/lisse). Le downsampling et la fenêtre s'adaptent, mais la **matière**
  est plafonnée par le compteur (cf. `tic-standard-mode.md`).
- **Cloud (Phase 2)** : le downsampling Pi préfigure les continuous aggregates
  TimescaleDB côté `ben-api` — garder la même sémantique (min/max/avg par bucket).

---

## 5. Étapes (ordre conseillé, quand on lancera)

**Lot 1 — Pipeline d'acquisition (A + B ensemble)**
1. `main_uart.py` : passer en **lecture au fil de l'eau** (retirer `sleep`,
   event-driven sur la trame). Conserver watchdog.
2. `db.py` : **API d'écriture batchée** (`executemany`, une transaction, N/T) +
   batch de l'update `level_profile`. Flush sur SIGTERM. Params dans `settings.py`.
3. **Exposer la valeur courante en RAM** pour la jauge `/live` (découplée du flush).

**Lot 2 — App intelligente (C)**
4. `local_api.py` : endpoint courbe **agrégé par bucket** (`min/max/avg`, résolution
   = fonction du range). Jamais de brut sur plage large.
5. App : demander la résolution = viewport ; consommer min/max/avg.

**Lot 3 — Anti-Hawthorne (D)**
6. Fenêtre dégressive **paramétrée par l'âge du PDL**, appliquée dans l'endpoint
   courbe (par-dessus C). Stockage reste fin.
7. **Toggle caché** (flag force-fin, geste/écran debug discret), par PDL.

> Lots **séquentiels** : 1 (acquisition saine) → 2 (servir léger) → 3 (lisser
> l'apprentissage). Chaque lot est livrable seul, mais D suppose C (même endpoint).

---

## 6. Fichiers concernés

| Fichier | Volet |
|---|---|
| `src/pi/tic-reader/main_uart.py` | A — lecture au fil de l'eau (retirer `sleep`) |
| `src/pi/store/db.py` | B — écriture batchée (`executemany`, 1 transaction) |
| `src/pi/store/settings.py` | B — `BATCH_SIZE` / `BATCH_MAX_AGE_S` |
| `src/pi/store/local_api.py` | A — valeur courante RAM pour `/live` ; C — endpoint courbe agrégé |
| `src/pi/store/levels.py` | (transverse) modèle niveau par PDL, inchangé sur le principe |
| `ben-app/.../courbe / historique` | C — demander résolution = viewport ; D — toggle caché |

---

## 7. Liens

- `docs/learning-period-curve-smoothing.md` — fondement du volet D (jauge vs courbe,
  fenêtre dégressive, « le backend garde le fin », piège courbe plate).
- `rasperry/LORA_PROTOCOL.md` §17 (v0x04) — **ce chantier est le pendant UART** ;
  pas de delta/keyframe sur fil (pas de contrainte d'airtime), mais même sémantique
  de courbe et même principe « granularité de perte = batch » (§17.3).
- `ben-firmware/docs/tic-standard-mode.md` — cadence/finesse selon le mode, ce qui
  plafonne la matière de la courbe.
- `ben-firmware/docs/isousc-chantier.md` — jauge/humeur par PDL (la jauge temps réel
  que ce chantier ne doit pas perturber).
