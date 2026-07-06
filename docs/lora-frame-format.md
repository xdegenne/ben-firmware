# Format de trame LoRa — spécification cible

Enveloppe unifiée · champs TLV extensibles · chiffrement authentifié.

**Spécification de référence (2026-07-03).** Format cible de l'émission LoRa Linky → Pi, issu de
la remise à plat complète après les évolutions successives (v0x04→05, ext v1→v2, LTARF, DEMAIN,
Tempo). Remplace le format courbe v0x05 (`rasperry/LORA_PROTOCOL.md` §17) une fois déployé.

**Contexte / opportunité** : profite de la fenêtre **mono-device** (un seul couple LoRa =
ben-0001, qui ne durera pas) pour un **cutover franc**, sans contrainte de rétro-compatibilité.

---

## 1. Objectifs

- **Adaptatif** — extensible sans « flag-day » : quand le parc grossira et qu'on ne pourra plus
  tout reflasher, un nouvel émetteur pourra ajouter un champ qu'un vieux récepteur **ignore
  proprement**.
- **Chiffré** — la confidentialité est une exigence (la courbe de charge = empreinte
  comportementale du foyer ; firmware open source). Cf. `LORA_PROTOCOL.md` §3.1 / §18 /
  mémoire `project_lora_confidentiality_priority`.
- **Propre** — zéro bit mort, zéro champ log-only qui traîne.
- **Zéro perte d'info** — tout ce qui transite aujourd'hui (histo ET standard) est conservé.

## 2. Principes

1. **Enveloppe unique** pour tous les types (boot, courbe…) : `[header clair][corps][MAC]`. Le
   récepteur aiguille sur le type.
2. **TLV** (Type-Length-Value) pour tout champ optionnel/on-change → **forward-compatibilité** (on
   saute un tag inconnu via sa longueur).
3. **Chiffrer par défaut** : en clair UNIQUEMENT ce qu'il faut pour décider/déchiffrer (version +
   nonce). Tout le reste chiffré → aucune décision « est-ce sensible » au cas par cas.
4. **Un frame = un rôle** : type BOOT (identité/contrat), type COURBE (mesures). Même enveloppe.

## 3. Octet version / type (toujours en CLAIR)

```
bit 7      : chiffré ? (1 = corps chiffré, 0 = clair)
bits 0-6   : type de trame (128 possibles)
   0x01 = BOOT / IDENTITÉ
   0x05 = COURBE / DATA
   (0x03 = config receiver→émetteur, cf. LORA_PROTOCOL.md §16 — à porter si conservé)
```
Seul discriminant chiffré/clair : toujours lisible **avant** tout déchiffrement (sinon problème de
l'œuf et la poule). Ne JAMAIS mettre l'indicateur « chiffré » dans les flags (qui sont chiffrés).

## 4. Header clair (authentifié par le MAC, non chiffré)

```
[0]     version/type
[1-3]   boot_count  (uint24 LE) — compteur EEPROM, +1 à chaque BOOT (survit brownout)
[4-6]   msg_count   (uint24 LE) — compteur RAM, +1 à chaque TRAME émise (boot OU courbe), reset boot
```
7 octets. `boot_count`+`msg_count` = **nonce** (§6) : écriture EEPROM seulement au boot (pas
d'usure), `(boot_count, msg_count)` jamais deux fois le même, même après reboot.

## 5. Corps (chiffrable) — selon le type

**Type COURBE (0x05) :**
```
core :
  [flags:1]     bit0 src_standard, bit1 ts_valid, bits2-7 réservés
  [batch_seq:2] séquence courbe (reset 0 au reboot ; détection reboot + ordre)
  [index_id:1]  registre actif (rang PTEC histo / NTARF standard)
  [index_value:4]
  [papp_ref:3]  PAPP du keyframe — int24 signé si src_standard (±8,4 M VA : couvre 36 kVA+
                et la plage compteur 99 999 ; corrige l'overflow latent de l'int16 v0x05)
  [N:1]         nb de points
  [period_ds:1] période réelle (1/10 s)
  [ts:7]        saison(1) + YYMMDDhhmmss(6)  — présent si ts_valid
points :
  (N-1) × [dt varint][delta_papp zigzag varint]
TLV :
  [tag][len][value] …   (voir §7)
```
**Type BOOT (0x01) :** pas de core courbe — juste des TLV d'identité (`ADCO`, `ISOUSC`, `PREF`,
`CONTRAT`). Émise au boot + on-change (abonnement/contrat).
**Gain sécurité** : la trame boot actuelle est NON signée (ADCO spoofable) → ici signée + chiffrée.

## 6. Chiffrement (ChaCha20, encrypt-then-MAC)

- **Cipher** : ChaCha20 (lib Crypto rweather, AVR-friendly). Flot → ciphertext = même taille,
  chiffrable **in-place** sur le buffer.
- **Clés** dérivées **une fois au boot** de la clé par-device `K` (32 o, déjà provisionnée) :
  - `K_enc = HMAC-SHA256(K, "ben-lora-enc")[:32]`
  - `K_mac = HMAC-SHA256(K, "ben-lora-mac")[:32]`
- **Nonce** (96 bits IETF) = `boot_count(3o LE) ‖ msg_count(3o LE) ‖ 0×6`. Unicité garantie
  (EEPROM monotone + par-trame) → jamais de réutilisation de keystream, même après brownout.
  Monotone = **anti-rejeu** gratuit côté récepteur.
- **MAC** = `HMAC-SHA256(K_mac, header_clair ‖ ciphertext)[:8]`, en fin de trame.
- **Ordre** = **encrypt-then-MAC** : chiffrer le corps → MAC sur (clair ‖ chiffré). Récepteur :
  **vérifier le MAC d'abord** (rejet si KO), puis déchiffrer.
- **Indicateur** : bit7 de l'octet version.

## 7. Format et table des tags TLV

Un champ = `[tag:1][len:1][valeur:len]`. Tag = 1 octet (256 types, ~13 utilisés). Len = 1 octet
(valeur ≤ 255 ; les nôtres ≤ 32). Un récepteur qui ne connaît pas `tag` **saute `len` octets**.

| Tag | Nom | Len | Mode | Sens |
|-----|-----|-----|------|------|
| 0x01 | ADCO     | 12   | tous | ID compteur (ADSC/ADCO) |
| 0x02 | ISOUSC   | 1    | histo | intensité souscrite (A) |
| 0x03 | PREF     | 1    | std  | puissance de réf. (kVA) |
| 0x04 | CONTRAT  | ≤16  | tous | NGTF (std) / OPTARIF (histo) |
| 0x10 | EAIT     | 4    | std  | énergie active injectée (Wh, producteur) |
| 0x11 | LTARF    | ≤16  | std  | libellé tarif en cours |
| 0x20 | DEMAIN   | 1    | histo | couleur du lendemain : 0=BLEU 1=BLANC 2=ROUGE |
| 0x21 | NJOURF   | 1    | std  | n° profil jour courant (0-9) |
| 0x22 | NJOURF1  | 1    | std  | n° profil prochain jour (0-9) = NJOURF+1 |
| 0x30 | ADPS     | 0/1  | histo | dépassement puissance souscrite (présence ; valeur=I opt.) |
| 0x31 | PEJP     | 0/1  | histo | préavis EJP (présence) |
| 0x40 | MSG1     | ≤32  | std  | message court |
| 0x41 | MSG2     | ≤16  | std  | message ultra-court |

Plages réservées : `0x00-0x0F` identité/contrat · `0x10-0x1F` énergie/tarif · `0x20-0x2F`
calendrier/Tempo · `0x30-0x3F` alertes · `0x40-0x4F` messages · `0x50+` futur.

## 8. Trame complète (récap)

```
CLAIR :   [ver/type:1][boot_count:3][msg_count:3]
CHIFFRÉ : [ core (selon type) ][ points (courbe) ][ TLV… ]
MAC :     [8]  = HMAC(K_mac, clair ‖ chiffré)
```

## 9. Inventaire « zéro perte » (ancien → nouveau)

| Info | Aujourd'hui | Nouveau |
|---|---|---|
| ADCO / ISOUSC / PREF / CONTRAT | trame boot v0x01 (**non signée**) | trame BOOT, TLV (signée+chiffrée) |
| batch_seq | courbe o.2-3 | core courbe (chiffré) — le nonce utilise `msg_count`, séparé |
| index_id / index_value / PAPP / period / horodate / src_standard / ts_valid | courbe | core courbe (chiffré) |
| EAIT | ext v1/v2 | TLV 0x10 |
| LTARF | ext v2 | TLV 0x11 |
| DEMAIN (histo) | flags bits 0-1 (**log-only**) | TLV 0x20 (enfin stocké/exposé) |
| ADPS / PEJP (histo) | flags bits 2-3 (**log-only**) | TLV 0x30 / 0x31 |
| DIAG | ext v2 | **SUPPRIMÉ** (debug, cause index=0 comprise) |
| NJOURF / NJOURF+1 (std Tempo) | — | **AJOUT** TLV 0x21 / 0x22 |
| MSG1 / MSG2 (std) | — | **AJOUT** TLV 0x40 / 0x41 |
| boot_count | — | **AJOUT** (nonce) |

Supprimés : `has_ext`, `ext_v2`, `DIAG`, tous les bits de flags mono-usage. **Rien d'utile perdu.**

## 10. Migration (cutover mono-device)

1. Écrire/valider le **décodeur récepteur** (round-trip au banc `tic-gen.py` + `test_v05_roundtrip`).
2. **Mesurer le coût RAM ChaCha** sur l'Arduino (compile ben-ops) — **SEUL vrai risque** (Pro Mini
   2 Ko, déjà ~74 %). Libérer de la RAM si besoin (trim struct, `CURVE_BUF_LEN`).
3. Bump récepteur (OTA) + reflash Arduino **dans la même session** (mono-device → pas de flag-day).
4. Bench + on-device ben-0001 avant de figer.

## 11. Points ouverts (à trancher à l'implémentation)

- Valeur exacte d'`ADPS`/`PEJP` (présence seule vs intensité) — présence pour commencer.
- Conserver le type config `0x03` (LORA_PROTOCOL.md §16) dans l'enveloppe ou le retirer.
- **Livraison possible en 2 temps** : format **TLV clair** d'abord (RAM-neutre, dérisque), puis
  **chiffrement** une fois le coût RAM ChaCha mesuré/dégagé.

---

## Rattachements
- Historique / menace / crypto d'origine : `rasperry/LORA_PROTOCOL.md` (§3 menace, §17 courbe
  v0x05, §18 chiffrement).
- Confidentialité = exigence : mémoire `project_lora_confidentiality_priority`.
- Banc de test : `project_tic_9600_test_rig` (`ben-ops/scripts/tic-gen.py`).
- Tempo / calendrier : `project_tempo_demain_spec` + `Enedis-NOI-CPT_54E.pdf`.
