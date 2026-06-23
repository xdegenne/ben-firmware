# Point ouvert — TIC « mode standard » vs « historique »

> Statut : **✅ VALIDÉ SUR VRAI LINKY STANDARD (2026-06-23, ben-0004) + RELEASE pi-0.0.43.**
> Le compteur de Xavier est passé standard (~00:10), l'auto-détection a basculé seule
> (diag : 38 groupes, checksum 100%). Wired + DB + API validés live. Release **pi-0.0.43**
> (wired+lora) + **arduino 0.0.4** prête : décodage standard **bout-en-bout** —
> Arduino (auto-détection + parseur + v0x04 papp signé/ts/EAIT), récepteur LoRa,
> stockage générique `(src_standard, index_id, index_value)` + `meter_ts`, `/live`
> expose `tic_mode`. Reste : test e2e Arduino-flashé → récepteur, et chantier
> ISOUSC-standard (PREF→jauge) + prix par tarif (différés).
>
> Le front-end **LTV814 + BS170** (le montage histo existant,
> en 5V) décode **histo 1200 ET standard 9600 à 100%** au banc (soak 1782/1782, 0 erreur). Pas de
> nouveau hardware. La fausse piste "LTV814 trop lent → 6N137" venait d'un régulateur monté par
> erreur à la place du BS170. Voir `docs/tic-frame-generator-bench.md` et mémoire
> `project_tic_standard_mode`.
>
> **Firmware (fait, à tester sur ben-0004) :** `src/pi/tic-reader/main_uart.py` est désormais
> **bi-mode** — auto-détection du mode au boot (sonde 1200/histo puis 9600/standard via checksum,
> mode persisté testé en premier dans `tic-state.json`) + parseur standard conforme à
> Enedis-NOI-CPT_54E §5.3.6/§6.2 (séparateur HT, checksum HT-de-queue-inclus, horodate déduite du
> nombre de champs). En standard on stocke `papp`←SINSTS + `iinst`←IRMS1 par le chemin existant
> (courbe + jauge OK, zéro changement de schéma). Les index d'énergie standard (EAST/EASFxx/NTARF)
> et l'injection (SINSTI/EAIT) sont **parsés + loggés** mais pas stockés → chantier index bi-mode
> (`docs/chantier-index-energie-bimode.md`). PREF (kVA) ≠ ISOUSC (A) → non mappé dans la jauge.
> Déploiement ben-0004 = **à la main** (labo, hors OTA).
>
> **Décision (2026-06-22) : détection au boot UNIQUEMENT — accepté en prod.** La détection ne
> tourne qu'au démarrage du process ; si Enedis rebascule le mode en cours de route, le reader ne
> décode plus rien jusqu'à ce que le **watchdog** (10 min sans trame valide) relance le process et
> re-sonde les deux débits. Ce **trou de ~10 min max** est jugé acceptable, y compris en prod : un
> device reprogrammé à distance se recale tout seul, sans humain. **Pas de re-détection à chaud à
> implémenter.** (En labo, recalage immédiat = `systemctl restart ben-tic-reader`.)
>
> **Reste à faire :** (1) **valider le décodage sur un vrai Linky réellement en mode standard**
> (ben-0004 demain — la 1re trame est loggée en INFO exprès) ; banc OK mais ~85% de confiance
> (porteuse carrée du banc ≠ sinus du vrai compteur) ; (2) brancher le stockage des index
> d'énergie standard (chantier bi-mode) ; (3) calibrer la jauge depuis PREF (chantier ISOUSC).

## Le sujet

Le Linky expose sa TIC dans **deux modes**, mutuellement exclusifs :

| | **Historique** (ce qu'on lit aujourd'hui) | **Standard** |
|---|---|---|
| Débit | **1200 bauds** | **9600 bauds** |
| Format | 7E1 | 7E1 |
| Étiquettes | courtes (`ADCO`, `BASE`, `PAPP`, `IINST`, `ISOUSC`, `PTEC`…) | longues + nombreuses, **horodatées**, soutiré **et** injecté |
| Trame / checksum | format classique | format différent (séparateurs, checksum) |
| Par défaut | la plupart des compteurs | activé pour autoconso/production, ou selon config Enedis |

**BEN lit les deux modes** (auto-détection, cf. statut ci-dessus) — reste à **valider le
standard sur un vrai compteur** (jusqu'ici : seulement banc + spec).

## Pourquoi c'est à la fois hardware ET firmware

- **Front-end (hardware)** : le montage de lecture (opto + BS170, calibré pour la
  TIC) doit passer **9600 bauds** proprement (fronts 8× plus rapides). À vérifier
  sur signal réel — c'est le volet « développement hardware » à finir.
- **Lecture (firmware)** : débit UART (1200 → 9600), **parseur de trame** différent
  (étiquettes, séparateurs, checksum), idéalement **auto-détection** du mode.

## À faire

1. Se procurer / configurer un Linky en **mode standard** (ou un béta-testeur qui
   l'a) et **caractériser** : est-ce que le front-end capte le signal ? quelle tête
   ont les trames ?
2. Décider : **auto-détecter** le mode au boot (essayer 9600 puis 1200, ou inverse)
   vs config par device.
3. Écrire le **parseur standard** (en plus de l'historique).
4. Vérifier que les champs dont on a besoin (PAPP, IINST, index, ISOUSC) ont bien
   leur équivalent en standard (les noms changent).

## Lien

- C'est une des **inconnues** qui justifient le **1ᵉʳ test béta = blinder le
  hardware** (cf. `docs/presentations/ben-big-picture.md` §8 / slides).
- Touche `src/pi/tic-reader/main_uart.py` (filaire) et l'Arduino émetteur (LoRa)
  s'il faut lire la TIC différemment côté émetteur aussi.

---

## Annexe — Foyers solaires : ce que SINSTS/SINSTI apportent (et leur angle mort)

> Ajouté 2026-06-11. Question récurrente : un foyer en **autoconsommation** a-t-il
> intérêt à lire SINSTS/SINSTI (standard) plutôt que PAPP (historique) ? Réponse
> nuancée — touche le NILM (`ben-api/RECOGNITION.md`), la jauge et v0x04
> (`rasperry/LORA_PROTOCOL.md` §17.9, champ SINSTI).

### Le point physique fondamental — le compteur ne voit que la frontière réseau

Le Linky mesure **uniquement l'échange net au PDL**, jamais la production PV ni la conso
réelle de la maison. Il ne voit que ce qui **traverse** le compteur :

- **SINSTS** (soutiré) = puissance tirée **du réseau** (réseau → maison)
- **SINSTI** (injecté) = puissance renvoyée **au réseau** (maison → réseau)

À un instant donné, **un seul des deux est non nul**. Et surtout :

> **Le solaire autoconsommé est INVISIBLE au compteur.** Le courant panneau → frigo ne
> traverse jamais le Linky. Donc :
> `conso réelle = SINSTS + (solaire autoconsommé, non vu)`
> → **impossible de reconstruire la vraie conso à partir du seul Linky.** Angle mort
> structurel : pendant les heures ensoleillées, les signatures NILM sont **masquées**
> (un appareil qui démarre peut ne pas bouger SINSTS si le panneau l'absorbe).

### L'intérêt dépend du montage exact — deux cas

« Autoconso sans revente » recouvre deux installations physiquement différentes :

| Montage | SINSTI | Intérêt de lire SINSTS/SINSTI |
|---|---|---|
| **Cas A** — anti-injection (onduleur bridé « zéro export », surplus écrêté) | **≡ 0** en permanence | SINSTI inutile ; SINSTS = résidu réseau, angle mort PV demeure → **peu de valeur ajoutée vs PAPP** |
| **Cas B** — surplus injecté mais non vendu (« donné » au réseau, pas de dispositif anti-retour) | **> 0** pendant surplus | ✅✅ **oui** — SINSTI = signal surplus, que l'historique **ne peut structurellement pas** donner |

### Pourquoi SINSTI vaut de l'or dans le cas B

En **historique, PAPP ne voit que le soutiré → aveugle à l'injection** : pendant un
surplus, PAPP ≈ 0 et on ignore tout du surplus. SINSTI, lui, dit « tu produis plus que
tu ne consommes, de tant ». Usages produit :

- **Pilotage de charge (load shifting)** : « surplus dispo → lance le lave-vaisselle /
  chauffe-eau **maintenant**, c'est gratuit ». C'est *le* conseil clé de l'autoconso
  sans revente (surplus injecté = perdu → autant le consommer).
- **Humeur / jauge BEN** : BEN content en surplus, signal « profites-en ».

### Le mur, dans tous les cas

Même avec SINSTS **et** SINSTI, on n'a que la **frontière réseau**, jamais la
**production** ni la **conso totale**. Pour un NILM honnête sur foyer solaire, il faut
une **mesure PV complémentaire** : API onduleur, ou **pince ampèremétrique (CT clamp)**
sur la ligne PV. Le Linky seul ne suffira jamais.

### Conséquences pour BEN

- **Jauge / humeur** : une jauge basée sur SINSTS est **distordue aux heures de soleil**
  (conso masquée par le PV) → prévoir de signaler le surplus plutôt que de mentir sur la
  conso.
- **NILM** : matière dégradée en journée sur foyer solaire → l'apprentissage doit en
  tenir compte (cf. `docs/learning-period-curve-smoothing.md`).
- **v0x04 §17.9** : câbler le bit SINSTI dans le bloc extension n'a d'intérêt que pour le
  **cas B** ; sur cas A, c'est zéro tout le temps. À trancher selon la cible.
- **À déterminer** : dans quel cas (A ou B) sont les foyers solaires ciblés → décide si
  SINSTI mérite la v1.
