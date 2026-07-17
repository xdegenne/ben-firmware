# Front-end TIC filaire — modulation & démodulation

Comment le signal télé-info (TIC) du Linky devient un flux série UART lisible par le Pi.
Montage de référence : `pcb/TIC-Reader-wired/` (schéma `TIC-Reader-wired.net`).

## 1. Ce que sort le Linky : OOK sur porteuse 50 kHz

La TIC n'est **pas** un UART en bande de base. C'est un **flux série UART asynchrone**
(format **7E1**, débit **1200 bps** en historique / **9600 bps** en standard) **modulé en
OOK / ASK** (*On-Off Keying*, tout-ou-rien) sur une **porteuse ~50 kHz** :

- bit d'un état → **bouffée de sinus 50 kHz** présente,
- bit de l'autre état → **silence** (pas de porteuse).

C'est de la modulation d'amplitude, pas de la FSK. La donnée utile est **l'enveloppe**
de ces bouffées.

## 2. La chaîne (montage filaire)

![Schéma du décodeur TIC filaire (Linky → opto → BS170 → GPIO15)](images/schematic-tic-reader.png)

Tracé équivalent :

```
J1 (Linky I1) ──[R1 750Ω]──► U1 pin1 (LED)
J1 (Linky I2) ─────────────► U1 pin2 (LED)      ◄── DOMAINE PORTEUSE 50 kHz (isolé du Pi)
                                 │  U1 = opto LTV-814 (AC/DC)
        +5V ──► U1 pin4 (collecteur)
                U1 pin3 (émetteur) ──► ● Net-(Q1-G) ──[R2 3.2k]──► GND
                                        │  BS170 grille
  +3V3 ──[R3 10k]──► Q1 drain ●─────────► J2 (GPIO15 = RXD du Pi)   ◄── UART BINAIRE 3,3 V
                     Q1 source ──► GND
```

| Réf | Valeur | Rôle |
|---|---|---|
| U1 | LTV-814 (opto AC/DC) | **isolation galvanique** + capteur + **détecteur d'enveloppe** |
| R1 | 750 Ω | limite le courant dans la LED de l'opto (couplage porteuse) |
| Q1 | BS170 (NMOS) | mise en forme + **inversion** → logique 3,3 V propre |
| R2 | 3.2 kΩ | charge de grille (décharge Cgs) : lissage résiduel + seuil |
| R3 | 10 kΩ | pull-up de drain vers +3,3 V (sortie UART) |

## 3. Où passe-t-on de la porteuse au binaire : DANS l'opto

**Le sinus 50 kHz ne traverse pas l'opto.** Le phototransistor du LTV-814 est **trop lent**
(ton/toff ~dizaine de µs, bande passante chargée **< 50 kHz**) : pendant une bouffée
« porteuse présente », la LED pulse à 50 kHz mais le phototransistor ne suit pas → il voit une
**lumière moyenne** et **conduit à un niveau ~continu**. Porteuse absente → il se bloque.

→ **La sortie de l'opto suit déjà l'ENVELOPPE** (les bits UART). C'est **la lenteur de l'opto
qui fait l'essentiel de la démodulation** (détection d'enveloppe **asynchrone / non cohérente**,
purement passive). Le `R2 // Cgrille` et le seuil du BS170 ne font que **finir de lisser le
ripple résiduel et mettre en forme**.

Le **BS170** en source commune (drain tiré à 3,3 V par R3) **inverse** et fournit un UART
logique 3,3 V propre sur GPIO15 (RXD, `ttyAMA0`).

### Vu à l'oscilloscope

**La porteuse 50 kHz** au point d'entrée (TIC / LED opto), capturée en base rapide (50 MSa/s) :

![Porteuse 50 kHz réelle au point TIC, période 20 µs](images/tic-carrier-real.png)

Le vrai 50 kHz (période **20 µs**). La forme n'est **pas une sinusoïde pure** : elle est écrêtée /
mise en forme par le front-end (LED de l'opto). C'est la porteuse **présente** (une bouffée « ON »).

**Toute la chaîne** sur 320 ms (TIC → opto → BS) :

![Démodulation le long de la chaîne : TIC → sortie opto → drain BS](images/tic-chain-demod.png)

- **① TIC** : ⚠️ les cycles visibles sont un **artefact d'aliasing** (ce panneau est échantillonné
  à 25 kSa/s ≪ 50 kHz → fausse onde lente) → **ignorer leur forme**, regarder l'**enveloppe** :
  bouffées pleines = porteuse **ON** (un bit), creux effondrés = porteuse **OFF** (l'autre) = les données.
- **② sortie opto** (grille BS) : **enveloppe démodulée, 0/3 V, plus de porteuse**.
- **③ drain BS** (→ GPIO15) : **logique 0/3,3 V, inversée**.

**Zoom simultané opto vs BS** (2 voies, alignées dans le temps, 200 µs/div) :

<p align="center"><img alt="CH1 jaune = sortie opto, CH2 bleu = drain BS" src="images/tic-opto-bs-2ch.png" width="440"></p>

**CH1 (jaune) = sortie opto** (enveloppe démodulée, fronts arrondis par le RC) ; **CH2 (bleu) =
drain BS** (logique **inversée**). Capturées **ensemble** → on voit l'inversion du BS170 en direct :
jaune haut ⇄ bleu bas.

## 4. Le rôle de l'opto (récap, par ordre d'importance)

1. **Isolation galvanique** (rôle fondamental) — seule la lumière traverse la barrière : pas de
   masse commune avec le Linky/secteur. Sécurité + conformité TIC (la sortie télé-info se lit en
   isolé). Un pépin d'un côté ne remonte pas de l'autre (Vce 35 V, isolation kV).
2. **Capteur** — le Linky pousse un **courant** dans la LED (via R1) → lumière → phototransistor
   côté isolé.
3. **AC/DC** — le LTV-814 encaisse la porteuse **alternative** 50 kHz **sans pont redresseur**
   en entrée (une LED d'opto standard ne conduirait qu'une polarité).
4. **Détecteur d'enveloppe** — par sa bande passante finie (cf. §3).

## 5. Le rôle du BS170

Le BS170 (N-MOS, montage **source commune**) n'est pas qu'un inverseur — il fait **4** choses :

1. **Mise en forme (seuillage)** — la sortie de l'opto est une enveloppe **molle** (fronts
   arrondis par le RC, ripple résiduel de porteuse). Le BS170 la **remet au carré** : au-dessus
   de son seuil de grille (Vgs(th) ≈ 2 V) il conduit à fond (drain → 0 V), en dessous il bloque
   (drain → 3,3 V). C'est le **comparateur** du montage → créneaux nets, fronts raides (cf. la
   voie bleue vs jaune de la capture 2-voies ci-dessus).
2. **Adaptation 3,3 V + protection du GPIO** — le côté opto est référencé **+5 V** (collecteur
   sur +5 V), mais le drain est tiré à **+3,3 V** par R3. La sortie vers le Pi ne dépasse donc
   **jamais 3,3 V** → compatible et **sûre** pour GPIO15 (les entrées du Pi ne sont **pas**
   5 V-tolérantes). Sans le BS, on enverrait du ~5 V dans le Pi = casse.
3. **Inversion** — source commune → grille haute = drain bas. Le sens final (idle UART = niveau
   haut) tombe juste avec la convention TIC + le sens de la modulation.
4. **Buffer haute impédance** — la grille MOS ne tire aucun courant → elle **ne charge pas** le
   détecteur d'enveloppe (R2 // Cgrille) → ne perturbe pas la démodulation en amont.

## 6. Le vrai enjeu de dimensionnement

La bande passante effective de l'opto doit tomber dans une **fenêtre étroite** :

- **assez rapide** pour reproduire l'enveloppe UART → passer ~9,6 kHz en standard (bit = 104 µs),
- **assez lente** pour **tuer** la porteuse 50 kHz.

Soit un facteur **~5** seulement entre les deux → front-end **sensible**. `R1` fixe le courant
LED (point de fonctionnement / CTR de l'opto), la charge de sortie fixe la bande passante. Trop
rapide → du 50 kHz bave en sortie ; trop lent → l'enveloppe 9600 bps est écrasée aussi. C'est la
cause racine des réglages histo↔standard (cf. `docs/tic-standard-mode.md`,
`MissTIC-wired-rev01`, et les valeurs de ratio R_LED/R_gate du front-end émetteur).

> Image du board : `docs/TIC-Reader-wired-rev01.png`.
