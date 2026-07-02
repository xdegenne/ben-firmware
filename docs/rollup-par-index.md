# Rollup par index — perf courbe/registres + coloration HP/HC

**Statut : DESIGN (prospectif, pas implémenté).** Synthèse de la session 2026-07-02.
Résout d'un coup : perf `/curve` + `/registers`, coloration HP/HC (jauge + courbe), index par tarif.

---

## 1. Le problème

Le boîtier enregistre **~1 mesure/s** → ~2,5 M lignes/mois dans `measurements`. À chaque
demande (courbe 7j/30j, index par tarif), le Pi Zero (mono-cœur) doit **rescanner** ces
millions de lignes (`USE TEMP B-TREE FOR GROUP BY` sur 550 k–2,6 M lignes). Résultat :
`/curve` et `/consumption` 7j/30j = **13–30 s**. Le CPU est le mur (le Mac fait les mêmes
requêtes en ~150–380 ms) → **le volume de lignes EST le problème, pas de raccourci SQL**
(ANALYZE, index dédié, `temp_store=MEMORY` testés sans effet le 02/07).

## 2. La solution : une table de résumé, keyée PAR INDEX

Table pré-agrégée, **une ligne par (tranche de temps, tarif)** :

```sql
CREATE TABLE curve_rollup (
    pdl_index    INTEGER NOT NULL,
    bucket_ts    INTEGER NOT NULL,   -- début aligné de la tranche (ts // n)
    src_standard INTEGER NOT NULL,
    index_id     INTEGER NOT NULL,   -- LE TARIF est DANS LA CLÉ
    ts_start     INTEGER NOT NULL,   -- ts RÉEL du 1er point (bornes exactes des bandes)
    ts_end       INTEGER NOT NULL,   -- ts RÉEL du dernier point
    papp_min     INTEGER,
    papp_max     INTEGER,            -- pics préservés (démarrages)
    papp_sum     INTEGER,            -- + count → moyenne à la LECTURE (incrémental)
    papp_count   INTEGER,
    index_last   INTEGER,            -- dernier index cumulé (Wh) → coût + index par tarif
    PRIMARY KEY (pdl_index, bucket_ts, src_standard, index_id)
);
```

**Pourquoi min/max/moy et pas juste max** : `max`=pics (bouilloire), `min`=talon (conso de
fond, base du dimensionnement), `moy`=niveau général, `index_last`=le compteur. On stocke
`sum`+`count` (pas la moyenne) pour pouvoir mettre à jour **au fil de l'eau**.

**Frontière de tranche** = **toutes les n minutes OU changement de tarif** — mais le
changement de tarif **coupe tout seul** parce que `index_id` est dans la clé (à une bascule
HC→HP au milieu d'une tranche, la tranche a **2 lignes**, une par tarif). Seule règle
explicite à coder : la tranche de n min. `n` ≈ **1–5 min** (compromis finesse/volume).

## 3. Alimentation : à l'ingestion (pas de job séparé)

Quand une mesure arrive (ou un paquet LoRa) : **un seul UPSERT** de la ligne de sa
tranche+tarif — `min=min(...)`, `max=max(...)`, `sum+=papp`, `count+=1`, `index_last=...`,
`ts_end=ts`. La tranche se remplit, puis se fige quand l'heure avance.

- **Wired** (~1/s) : 1 upsert/mesure.
- **LoRa** (~1 paquet/min de 58 pts) : **résumer le paquet d'abord** (min/max/sum sur les 58)
  → 1 upsert par tranche touchée. Encore moins d'écritures.

Philosophie : **écrire un peu plus maintenant pour lire beaucoup moins après** — chez nous
c'est la LECTURE qui rame, pas l'écriture. Purge du rollup alignée sur la rétention.

### 3bis. Rétro-alimentation (backfill) — le morceau délicat

À l'introduction du rollup, il faut **agréger tout l'historique existant** (millions de lignes)
en une passe. Or c'est **précisément le scan lourd** qu'on cherche à éviter → à faire **une
seule fois**, avec soin :

- **Chunké** : par plage de temps (jour par jour, ou par plage de `bucket_ts`) — pas un
  `GROUP BY` unique sur 2,6 M lignes qui bloquerait le Pi des minutes.
- **REPRENABLE** ⚠️ : mémoriser le dernier `bucket_ts` traité (curseur persistant) → si le Pi
  **reboote au milieu** (ben-0001 = brownout), on reprend où on en était, on ne recommence pas
  de zéro. **Indispensable** ici.
- **En tâche de fond / maintenance**, jamais sur le chemin live (la lecture courante continue
  de servir depuis le brut tant que le rollup n'est pas complet — bascule à la fin).
- **Idempotent** : re-passer sur une plage déjà faite = même résultat (UPSERT).

Une fois le backfill terminé, l'alimentation incrémentale (§3) suffit à le tenir à jour.

**Mécanisme progressif (par batch, tranquille) :**

- **Sens = de MAINTENANT vers le passé** (newest-first). L'incrémental (§3) couvre déjà
  `[déploiement → now]`. Le backfill remonte `[déploiement → plus vieux]`, un morceau à la
  fois → les **vues récentes** (24h/7j, les plus consultées) accélèrent **en premier** ; le
  vieux (rarement regardé) arrive après.
- **Curseur « watermark » persistant** : le rollup couvre `[watermark, now]`. À chaque batch,
  le watermark **recule**. `/curve` utilise le **rollup si `fenêtre.début ≥ watermark`**, sinon
  le **brut** → couverture qui s'étend sans jamais bloquer la lecture (§8, bandes toujours
  servies). Curseur stocké en base (survit brownout) → **reprise** exacte après reboot.
- **Batch = une tranche de temps bornée** (ex. **1 jour** ≈ 86 k lignes) : son `GROUP BY` est
  tractable, à l'opposé du scan 2,6 M qui rame. C'est le découpage qui rend chaque pas rapide.
- **Cadence douce** : **1 batch par tick** (ex. toutes les 30–60 s), avec pause entre →
  jamais de monopolisation du mono-cœur ; réception LoRa + API restent prioritaires. Rythme
  **tunable** (ralentir si contention). Peut se **greffer sur le cycle de maintenance existant**
  (`db.prune()` périodique) : un batch par passage.
- **Idempotent** (UPSERT) : re-passer un batch = même résultat → aucun risque à réessayer.
- **Fin** : le curseur atteint la plus vieille donnée → backfill complet → `/curve` sur rollup
  partout. Rien de spécial à faire, ça se termine tout seul.

En clair : le rollup se remplit **en arrière-plan, jour par jour, doucement**, en commençant
par le récent ; pendant ce temps le boîtier sert normalement (rollup là où c'est prêt, brut
ailleurs). Zéro fenêtre de blocage.

## 4. Ce que ça débloque

| Besoin | Avant | Avec le rollup |
|---|---|---|
| `/curve` large | scan `measurements` (lent) | agrège le rollup (rapide) |
| Bandes HP/HC | détecter les runs sur les points | lignes rollup **déjà rangées par tarif** |
| `/consumption` par tarif | recalcul | `Σ` deltas sur le rollup |
| `/registers` (index par tarif) | `GROUP BY` sur `measurements` (lourd) | `index_last` par `index_id`, direct |

## 5. Coloration HP/HC — les zones sont des PLAGES, jamais un parcours de points

**Principe : on ne parcourt JAMAIS les points pour dessiner les zones.** On découple :

| | Source |
|---|---|
| **La ligne** (courbe) | brut (zoom serré) ou résumé (vue large) |
| **Les zones** (bandes HP/HC) | **toujours le résumé** |

Le résumé est déjà découpé par tarif, avec `ts_start`/`ts_end` **exacts** → les bandes = ces
tranches, bornes précises, **une poignée de lignes** pour toute la fenêtre. Même en zoom serré
(ligne = brut), les bandes sortent du résumé.

**Classification `index_id → hc/hp/base`** : depuis `tariff_labels` (table minuscule, 1–6
lignes) — on classe le libellé (`LTARF`/convention) : contient « creuse » → `hc`, « pleine »
→ `hp`, sinon `base`/∅. Fait **1–6 fois** (par registre), jamais par point. NB : `tariff`
numérique est **NULL en standard** (seul `index_id`=NTARF est rempli) et NTARF=1 est **ambigu**
(BASE *ou* HC) → **c'est le LABEL qui tranche**, d'où la classif via `tariff_labels`.

Réponse `/curve` :
```json
{
  "points": [ {ts, papp}, ... ],                 // la LIGNE (index_id inutile ici)
  "tariff_bands": [                              // les ZONES (~2/jour), depuis le résumé
    { "from": ..., "to": ..., "kind": "hc" },
    { "from": ..., "to": ..., "kind": "hp" }
  ]
}
```
Côté app : `_tariffBands` (qui parcourait les points) **disparaît** → l'app dessine ~14
rectangles depuis `tariff_bands`. `tariff_kinds` peut même être caché côté app (change jamais).

## 6. L'API arbitre brut vs résumé (l'app reste bête)

L'app exprime son **intention** : `/curve?from&to&buckets=<largeur écran>`. Le **serveur**
choisit la stratégie :
1. largeur de tranche demandée = `(to-from)/buckets` ;
2. ≥ finesse du résumé → sert le **résumé** ;
3. plus fin (zoom serré) → sert le **brut** ;
4. **même forme de réponse** dans les 2 cas (`points` + `tariff_bands`).

→ l'app ignore l'existence du résumé ; on peut changer la stratégie serveur (finesse, seuil)
**sans toucher l'app**. Suite logique de « l'app pilote la résolution via `buckets`, le
firmware agrège » — on ajoute juste le résumé comme **source rapide** derrière le même endpoint.

## 7. Colonne commune = `index_id`

La coloration marche brut OU résumé parce que le tarif (`index_id`) est stocké sur **chaque
mesure brute** ET dans chaque ligne de résumé. Une seule logique de bandes, branchée sur les
deux sources. Le zoom qui retombe sur le brut garde ses couleurs gratuitement (mais les
bandes, elles, viennent toujours du résumé).

## 8. Transition / déploiement smooth

App (Shorebird/store) et serveur (OTA) roulent **indépendamment** → chaque combinaison de
versions doit marcher, sans coordination ni régression visuelle.

- **Champs serveur ADDITIFS** : `tariff_bands`, `tariff_kinds`, `tariff_label`, `contract`
  sont **ajoutés**, jamais renommés/retirés. Un **vieux app** les ignore (garde sa convention
  front) → continue de marcher tel quel.
- **App = détection de feature, pas de version** : la nouvelle app regarde « la réponse
  contient-elle `tariff_bands` ? ». Oui → bandes serveur. Non (vieux serveur) → **repli** sur
  le comportement actuel (convention front / pas de bandes en standard). Même logique que le
  repli `/curve`→`/measurements` déjà en place → la nouvelle app marche sur **n'importe quel**
  serveur.
- **Le serveur fournit TOUJOURS les bandes** (jamais de trou visuel) : pendant le backfill du
  rollup, `/curve` sert le brut **et** calcule les bandes depuis le brut (parcours temporaire,
  toléré le temps du backfill) ; rollup prêt → bandes depuis le résumé. L'app voit des bandes
  **en continu**, la bascule brut→résumé est **invisible**.
- **Ordre de rollout indifférent** (les 2 sens dégradent proprement) ; naturel = **serveur
  d'abord** (ajoute les champs), **app ensuite** (les consomme).
- **Changement visuel = additif/positif** : aujourd'hui le standard n'a **aucune** bande (c'est
  le bug) ; après, les bandes correctes **apparaissent**. BASE (ben-0001) reste sans bande
  (correct). On ne **perd rien** à l'écran — ça ne fait que se corriger. Pas de flicker, pas de
  régression. → transition « smooth » par construction.

---

## Rattachements
- Reprend/affine le chantier perf `/curve` (rollup) de `CHANTIERS.md`.
- Recoupe `project_chantier_courbe_temps_reel` (volet C).
- S'appuie sur la résolution de label unifiée (chantier labels, DONE 02/07) : `resolve_label`,
  `tariff_labels` (keyé `(pdl,src,index_id,ngtf)`), `/live.tariff_label`, `/registers`.
