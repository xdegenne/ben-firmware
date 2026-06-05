# Vérification couleur au provisioning BLE

Confirmation visuelle, au moment de l'association BLE, que l'app se connecte bien
**au boîtier que l'utilisateur a sous les yeux** — et pas à celui du voisin (copro,
immeuble) ni à un faux boîtier.

## Principe (version simple retenue)

```
1. À la connexion BLE, le device génère un code de 3 couleurs (ex. Bleu-Jaune-Blanc)
2. Il l'affiche en boucle sur sa LED RGB (un noir long délimite chaque répétition)
3. L'app montre 4 boutons couleur ; l'utilisateur tape la séquence qu'il VOIT
4. L'app envoie le code au device (GATT VERIFY)
5. Le device compare au code affiché → verified | wrong | locked
6. Tant que ce n'est pas `verified`, le device REFUSE la config WiFi
```

Avant connexion (aucun téléphone), la LED garde l'indicateur générique « à configurer »
(violet/jaune). Le code couleur n'apparaît qu'**à la connexion BLE** → un code frais par
session.

Ce que ça garantit :

- **Bon boîtier** : connecté au mauvais device → son code ≠ celui qu'on lit sur SON
  boîtier → rejeté. Force « le boîtier que je vois == celui auquel je suis connecté ».
- **Preuve de présence** : impossible de finir sans *voir* la LED.
- **Garde-fou côté device** : le refus de `WIFI_CONFIG` avant `verified` est appliqué par
  le firmware, pas par l'app → on ne peut pas court-circuiter l'étape.

## Palette (lisible daltonien)

La LED n'offre que la **teinte** comme information. Règle d'or : **jamais rouge ET vert
ensemble** (confusion deutan/protan, ~8 % des hommes). On supprime donc le **vert**.

| Token | Couleur | Duty RGB (0..100, à calibrer device) |
|---|---|---|
| `B` | Bleu | `(0, 0, 100)` |
| `Y` | Jaune | `(80, 70, 0)` |
| `W` | Blanc | `(70, 70, 80)` |
| `R` | Rouge | `(100, 0, 0)` (poussé : les protans le voient sombre) |

Redondances côté app (la LED reste teinte-only, on sécurise le *matching*) : chaque
bouton = pastille **+ libellé texte** **+ icône de forme distincte**. Un daltonien ne se
trompe pas de bouton même s'il hésite sur la teinte.

> Calibration : les duties sont des valeurs de départ. La LED rev01 est à cathode
> commune, le canal vert est perceptuellement plus lumineux — ajuster `BLANC`/`JAUNE`
> sur device réel pour qu'ils ne virent pas verdâtre.

## Code & robustesse

- **Longueur 3**, palette 4 → **64 combinaisons**. L'entropie modeste est compensée par le
  rate-limit + cooldown ; la protection dominante reste « il faut VOIR le boîtier », pas la
  résistance au brute-force.
- Affichage en boucle : chaque couleur ~0,7 s, séparées par un **noir court ~0,3 s** ; un
  **noir long ~1,5 s** délimite le début de chaque répétition (sinon on ne sait pas où
  commence le code). Le noir court sert aussi à distinguer deux couleurs identiques qui se
  suivent. Ne jamais utiliser un flash blanc comme marqueur — le blanc est une couleur du
  code.
- **Rate-limit** : 5 tentatives. Le **même** code reste affiché sur les essais (une faute
  de frappe honnête → on retape, pas besoin de relire). Au 5e échec → `locked`, flash
  rouge, **cooldown 30 s**, puis **nouveau code** régénéré + `pending`.
- Génération via `secrets.choice` (pas `random`).

## Contrat GATT (ajouts)

Service `b3e7e511-0001-4bea-9b15-000000000000` (inchangé), deux caractéristiques en plus :

| Char | UUID (suffixe) | Flags | Payload |
|---|---|---|---|
| `VERIFY` | `…0005` | write | code deviné, 3 lettres ASCII parmi `BYWR` |
| `VERIFY_STATUS` | `…0006` | read · notify | `pending \| verified \| wrong \| locked` |

`WIFI_CONFIG` (`…0001`) renvoie désormais `failed:not_verified` via `STATUS` si écrit
avant `verified`.

## Place dans le flux app

`scan → connexion BLE → **vérification couleur** → WiFi → progression`

L'écran de vérification s'insère entre la connexion et le formulaire WiFi. Tant que
`verified` n'est pas reçu, on ne propose pas la saisie WiFi.

Déclenchement côté firmware : hook **`on_connect`** (bluezero, symétrique de
`on_disconnect`). Au connect → génère un code frais + lance la séquence. Au disconnect sans
succès → le restart systemd existant ramène l'indicateur générique.

## États de la LED

| Situation | LED |
|---|---|
| Advertising, aucun téléphone | violet/jaune (= « à configurer ») |
| Téléphone connecté | séquence couleur du code (noir long 1,5 s · couleur 0,7 s · noir 0,3 s · …) |
| Vérifié | vert fixe |
| WiFi OK | 3 flashs verts → reboot |
| Échec WiFi | rouge → violet/jaune |
| Déconnexion sans succès | exit → restart → retour violet/jaune |

## Limite connue & upgrade possible (anti-MITM)

Dans cette version, **le code transite sur le canal** (app → device). Un relais BLE actif
(rare : attaquant à portée ~10 m pendant la fenêtre de provisioning) pourrait le repasser
au vrai device → non détecté.

Garde-fous qui comptent plus à ce stade :
1. **Chiffrer le lien BLE** (pairing) — pour que le mot de passe WiFi ne soit pas en
   clair, indépendamment du code. (Aujourd'hui : `write` simple, `encrypt-write` reporté
   au passage app Flutter — cf. docstring de `provisioner/main.py`.)
2. Rate-limit + cooldown (ci-dessus).

**Upgrade « version forte » (plus tard, par OTA si besoin)** : ne plus transmettre le
code, mais le **dériver des clés de session** des deux côtés (numeric comparison / SAS).
L'humain compare device vs app ; un MITM négocie des clés différentes → les deux codes
diffèrent → abandon. C'est *comparer au lieu de transmettre* qui tue le MITM.

## À synchroniser

- `ben-firmware/src/pi/provisioner/main.py` (contrat GATT + logique) ↔
  `ben-app/lib/core/config/ble_constants.dart` (UUIDs) — doivent rester alignés.
- Le simulateur central (`central_sim.py`, s'il est utilisé en CI) doit faire l'étape
  VERIFY avant `WIFI_CONFIG`, sinon il reçoit `failed:not_verified`.
