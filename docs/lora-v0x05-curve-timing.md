# Trame courbe LoRa v0x05 — horodatage par point (dt) + chaîne bi-mode

> **État : déployé et validé le 2026-06-23 sur ben-0001.**
> Arduino `tic-reader` **0.0.6**, récepteur `curve_codec.py` **v0x05**.
> Spec de référence : `rasperry/LORA_PROTOCOL.md` §17. Ce document explique le **pourquoi**
> du design temporel (la partie qui « commence à être sérieuse ») et la chaîne complète.

---

## 1. Le problème qu'on a résolu

La courbe de charge (PAPP ~1 Hz) part de l'Arduino émetteur en **batches** d'~1 min
(une trame LoRa toutes les ~60 s, sinon on explose le duty cycle 868 MHz — cf. §17.1).
Chaque batch contient N points. **Question centrale : comment le récepteur sait-il à
quel instant placer chaque point ?**

### Ce qui cassait les courbes (v0x04)

La v0x04 envoyait **une seule période** pour tout le batch (`period_ds`, octet 12), et
le récepteur supposait un **espacement uniforme** : `t[i] = t_rx − (N−1−i)·period`.

Deux défauts cumulés :

1. **`period_ds` était le nominal théorique** (1,0 s codé en dur en standard), alors que
   la cadence réelle était ~2 s (on ratait une trame sur deux à cause du
   `Serial.begin()/end()` par trame — cf. §5). Le récepteur tassait donc 30 points réels
   sur 60 s dans une fenêtre de 30 s → **courbe compressée, méconnaissable**.
2. **Espacement supposé uniforme** : même avec la bonne moyenne, un trou ponctuel (trame
   ratée → 2 s au lieu de 1) déplaçait tous les points suivants de travers.

### La correction (v0x05) : un dt par point

Chaque point porte désormais son **propre intervalle** `dt` (temps écoulé depuis le point
précédent), en **secondes**, varint. Le récepteur reconstruit le temps par **cumul** :
`t[i] = t0 + Σ dt[0..i]`. Plus aucune hypothèse d'uniformité → **courbe fidèle quelle que
soit la cadence**, trames ratées incluses.

---

## 2. D'où vient le temps, exactement (les trois unités)

C'est le point qui prête à confusion. Il y a **trois horloges/unités** distinctes :

| Étage | Horloge | Unité | Rôle |
|---|---|---|---|
| Mesure (Arduino) | `millis()` (compteur monotone, pas de RTC) **OU** horodate Linky | — | mesurer l'écart entre deux trames |
| Fil (trame LoRa) | — | **secondes** (varint) | transporter le `dt` de chaque point |
| Stockage (Pi) | epoch UTC | **secondes entières** | `ts` (système) + `meter_ts` (compteur) |

### En mode STANDARD : le dt vient de l'horodate du compteur

La TIC standard émet un champ `DATE` (`SAAMMJJhhmmss`) dans **chaque** trame. C'est
l'instant **réel de la mesure**, donné par l'horloge du Linky. Donc :

```
dt = time_of_day(horodate[i]) − time_of_day(horodate[i-1])     (+ 86400 si minuit)
```

C'est **le plus juste possible** : autoritaire (horloge compteur, pas le résonateur
8 MHz de l'Arduino qui dérive), et **sans dérive cumulative**.

### En mode HISTORIQUE : le dt vient de millis()

La trame historique **n'a pas d'horodate**. Pas le choix : on mesure l'écart avec
`millis()`. Pour éviter la dérive de troncature (arrondir chaque écart vers le bas
s'accumule sur 50 points), on calcule le `dt` comme **différence d'offsets cumulés
arrondis** depuis le début du batch → `Σ dt` = durée totale arrondie, erreur bornée.

### On parle en secondes, pas en 1/10 s

Le Linky horodate **à la seconde** (`hh mm ss`, pas de sous-seconde) et émet ~1 trame/s.
La seconde **est** sa vraie résolution → encoder en 1/10 s serait sur-ingénierie (et
doublerait l'octet `dt` : un écart de 1-2 s = 10-20 en 1/10 s tient sur 1 octet, mais en
ms = 1000-2000 → 2 octets). **Donc : dt en secondes, 1 octet/point.**

---

## 3. t0 — l'ancre absolue du batch

Le `dt` donne l'**espacement** ; il faut une **ancre** pour l'instant absolu du 1ᵉʳ point.

| Mode | t0 | Précision absolue |
|---|---|---|
| **Standard** | horodate compteur du keyframe (`meter_ts`) | **exacte** — immunisée au délai LoRa (l'heure voyage dans la trame) |
| **Historique** | `t_rx` (réception Pi) − Σ dt | décalée de l'**airtime LoRa** (~1-3 s + retries) |

> **Pourquoi l'historique est décalé** : l'émetteur flushe le batch **dès** le dernier
> point capté (pas d'attente), mais l'**airtime** d'émettre ~150 o en SF9 + l'attente
> d'ACK (`sendtoWait`) font que le Pi reçoit la trame quelques secondes après. En
> ancrant sur `t_rx`, tout le batch glisse de cet airtime. **La forme reste exacte** (les
> dt sont internes au batch), seul l'absolu glisse. Et **ça ne concerne que le
> LoRa-historique** : en standard l'horodate compteur immunise ; en filaire (wired) le Pi
> lit chaque trame en direct, pas de radio.

### Preuve mesurée (ben-0001, 2026-06-23)

```
ts          dts   meter_ts     dmt   papp
1782229514    0   1782229507     0    130
1782229515    1   1782229508     1    131
1782229516    1   1782229509     1    131
1782229518    2   1782229511     2    130     ← trame ratée : dt=2 capturé fidèlement
1782229519    1   1782229512     1    131
...
```

- `dts` (espacement système) suit le **vrai rythme** (1,1,2,1…) — pas figé.
- `dmt` = `dts` → confirme que le dt vient de l'**horodate compteur** en standard.
- `ts − meter_ts = 7 s` **constant** → l'airtime LoRa. `meter_ts` est en avance =
  l'instant **réel** de mesure. C'est lui le timestamp fiable pour le standard.

---

## 4. Stockage : deux timestamps, provenance séparée

`measurements` garde **les deux** (colonnes `ts` et `meter_ts`) :

- **`ts`** = horloge système du Pi (NTP) au moment de l'ancrage. Toujours présent.
  Approx en LoRa (décalé de l'airtime).
- **`meter_ts`** = horodate compteur (epoch UTC), **NULL si indisponible** (historique,
  ou mode dégradé saison minuscule). En standard, c'est le timestamp **autoritaire**.

Le `meter_ts` se calcule via `meter_epoch()` : `ts_season` ('E'=été UTC+2 / 'H'=hiver
UTC+1) → conversion locale→UTC **sans base de fuseaux** (le compteur dit la saison).

---

## 5. Le fix débit — UART ouvert en continu (begin-once)

Cause du ~0,5/s : l'Arduino faisait `Serial.begin(7E1)` pour lire la TIC, puis
`Serial.end()` + `Serial.begin(8N1)` pour imprimer du debug, **à chaque trame**. Ce
va-et-vient créait une **fenêtre aveugle** où l'UART ratait la trame suivante.

Corrigé : **l'UART TIC reste ouvert en continu** (`ticSerialBegin()` ne reconfigure que
sur changement de mode), **tout le debug par trame est retiré**. Résultat mesuré :
**n=58 points/batch** (~1/s) au lieu de ~30. Combiné au `period_ds` réel + dt par point,
les courbes sont recalées.

---

## 6. Format de trame v0x05 (résumé)

Keyframe **inchangé à 20 octets** (offsets identiques à v0x04 → moins de risque de bug) ;
seule la **section points** change : paires `[dt, delta_papp]` au lieu de `[delta_papp]`.

```
0     version    = 0x05
1     flags      bit4 ts_valid · bit5 src_standard · bit6 has_ext · bit0-3 DEMAIN/ADPS/PEJP
2-3   batch_seq  uint16 LE
4     index_id   histo: PTEC 0x00..0x0A ; standard: NTARF 1..10
5-8   index_value uint32 LE (Wh)
9-10  papp_ref   int16 (signé si src_standard, − = injection) / uint16 (histo)
11    N          uint8
12    period_ds  uint8 — MOYENNE/hint du batch en 1/10 s (le timing vient des dt, pas d'ici)
13    ts_season  'E'/'H' / 0x00
14-19 ts         YY MM DD hh mm ss (champ DATE standard ; 0 si histo)
20..  points     (N-1) × [ dt (varint, secondes) , delta_papp (varint zig-zag signé) ]
[ext] EAIT       uint32 LE (Wh) si has_ext (producteur standard)
len-8 HMAC       HMAC-SHA256(clé, 0..len-9) tronqué 8 o
```

**Coût** : ~+1 octet/point vs v0x04. Un batch d'1 min ≈ 175 o (< buffer 160 → auto-flush
vers ~50 points). `period_ds` est conservé comme **synthèse** (moyenne du batch) — pas de
décalage d'offsets, et un champ de sanity gratuit.

> **⚠️ Collision de numéro de version** : `LORA_PROTOCOL.md` §12 mentionnait jadis un
> « v0x05 » pour une *trame de mesure* (lignée prototype `lora_v6.py`, **jamais
> construite** — le flag `src_standard` a suffi). L'octet version **0x05 désigne
> désormais la trame COURBE**. Il n'y a pas de conflit déployé (pas de trame de mesure
> 0x05 en service), mais garder ça en tête en lisant l'ancien §12.

---

## 7. Carry-forward + LED (robustesse standard)

- **Carry-forward NTARF + EASF** : l'index tarifaire est cumulatif/quasi-statique. Si la
  ligne NTARF ou EASF saute (front-end marginal au 9600), on **reporte la dernière valeur
  connue** au lieu de jeter le point de courbe (PAPP, lui, est toujours là). La garde
  standard n'exige donc que **PAPP + une horodate valide**.
- **Plus de rouge sur drop de ligne** : en standard, une trame partielle est **sautée
  silencieusement** (pas de `blinkErr`). Le rouge est réservé au **Vcc bas (1×)** et au
  **pas-de-TIC-du-tout (2×)**.
- **Langage LED émetteur** : 🔵 bleu bref = trame TIC lue · ⚪ blanc bref = batch émis en
  LoRa · 🟢 vert = ACK reçu du récepteur · 🔴 rouge = erreur (1× Vcc, 2× pas de TIC).

---

## 8. Fichiers

| Rôle | Fichier |
|---|---|
| Émetteur | `src/arduino/tic-reader/tic-reader.ino` (FW 0.0.6, `curveAdd`/`curveFlush`/`todSeconds`) |
| Décodeur | `src/pi/lora-receiver/curve_codec.py` (`decode_v04`, `sample_dt_s`, `anchor_timestamps`, `meter_timestamps`) |
| Récepteur | `src/pi/lora-receiver/main.py` (`on_recv_curve` — dispatch sur `PROTOCOL_VERSION_CURVE`) |
| Tests | `src/pi/lora-receiver/test_v05_roundtrip.py`, `test_v05_store.py` |
| Spec | `rasperry/LORA_PROTOCOL.md` §17 |
