# Publier une release firmware (OTA)

Process complet pour livrer une nouvelle version aux devices via OTA. À suivre
**dans l'ordre** — l'oubli d'une étape (typiquement le `.sha256`) casse l'OTA.

## Comment l'OTA fonctionne (côté device)

`ben-update.timer` réveille `ben-update.service` (→ `check_update.py`) **toutes
les ~10 min**. À chaque tick, **une seule** transition de version est appliquée
(un device en retard de plusieurs versions rattrape tick par tick) :

1. lit `device.json` (version courante)
2. `git fetch` origin (tags + main)
3. lit `compatibility.yaml` depuis `origin/main`, cherche la transition
   `from == version courante`
4. **vérifie la signature GPG** du tag cible (`git verify-tag`) — clé publique
   embarquée sur le device
5. `git checkout <tag>`
6. **vérifie le SHA256** de l'`update.sh` (`update.sh.sha256` adjacent)
7. exécute `update.sh`
8. **seulement si tout réussit** : écrit la nouvelle version dans `device.json`
9. redémarre **`ben-publisher`** — systématiquement, après **chaque** update, et
   **sans que `update.sh` ait à le demander** (voir la section suivante)

Si une étape échoue → `device.json` **non modifié**, le device reste sur sa
version et re-tente au prochain tick. Donc une release cassée **bloque** la
montée de version (sans danger : le device reste sur l'ancienne).

Sécurité = **GPG (tag signé) + SHA256 (script)**. Les deux sont obligatoires.

## 🚨 Ce dont `update.sh` n'a PAS à s'occuper

Deux opérations se font **dans l'agent ou dans le service concerné**, jamais dans le
script de transition. Les y remettre, c'est les redonner à 37 scripts — donc les faire
oublier un jour.

| opération | qui s'en charge | depuis |
|---|---|---|
| redémarrer `ben-publisher` | `check_update.py`, étape 9 ci-dessus | 0.9.15 |
| faire connaître la version installée au cloud | `ben_publisher.py`, **à chaque battement** | 0.10.0 |

**Le redémarrage du publisher.** Le service exécute le code de `/opt/ben/repo` : après le
`git checkout` de l'étape 5, le processus vivant tourne encore sur l'**ancien** code. L'agent
le redémarre donc lui-même, avec cet argument écrit dans son propre code — *« le laisser à la
charge de chaque `update.sh` reviendrait à l'oublier un jour »*.

**La déclaration de version n'est plus un geste du tout.** Jusqu'à 0.9.28 elle reposait sur un
drapeau `/var/lib/ben-firmware/declaration-requise.json` que chaque `update.sh` devait poser —
`devices.sw_version` n'étant écrite que par la route `/hello`, et jamais par le battement.
**Un seul des 37 scripts du dépôt l'a posé**, et la vue parc est restée figée sur 0.9.27
pendant deux releases : parc impilotable, on ne savait plus qui avait pris quoi
([`ben-docs#15`](https://github.com/xdegenne/ben-docs/issues/15)). Depuis 0.10.0 le publisher
mémorise la version que le cloud a **acceptée** et la compare à `device.json` à chaque tour.
C'est une **condition**, pas un événement : elle n'a pas d'instant, donc elle ne peut pas être
ratée, et elle rattrape aussi le retour arrière, le refus du cloud et le `device.json` édité à
la main.

⇒ **Un `update.sh` neuf n'écrit RIEN dans `/var/lib/ben-firmware/`** à ce sujet et ne redémarre
**pas** `ben-publisher`. Le drapeau est mort : le publisher le supprime sans même le lire.

⭐ **Le modèle à copier est le DERNIER script, jamais un ancien.**
`updates/0.9.26_to_0.9.27/update.sh` documente le drapeau sur une trentaine de lignes : c'est
de l'**histoire**, pas une consigne. Un script de transition ne se relit que pour comprendre ce
qui a été fait ce jour-là.

ⓘ Ce qui reste bien à la charge de `update.sh` : les **migrations de schéma** (le publisher
ouvre en écriture sans rejouer le schéma, l'API locale est en lecture seule — aucun des deux ne
peut créer une table), les **redémarrages de lecteurs** (eux seuls savent si leur correctif
l'exige, et un restart coûte des mesures), les **préflights**, et le **contrôle d'effet**.

## Checklist de release (version N → N+1)

Soit la transition `A_to_B` (ex. `0.9.28_to_0.10.0`).

🚨 **MONO-FLUX depuis le ménage du 2026-07-22.** Plus de répertoire par modèle, plus de
miroir `updates/_shared/` : **un seul** script par transition, et c'est `update.sh`
lui-même qui interroge `capabilities.py` pour savoir sur quel modèle il tourne. Les
répertoires `updates/pi0-wired/`, `updates/pi0-lora/` et `updates/_shared/` sont de
l'**histoire** — rien de neuf n'y va.

1. **Code** — modifier `src/pi/...`, **et livrer son banc** (`test_*.py` à côté) : il sera
   exécuté en préflight par `update.sh`, sur le Python du boîtier.

2. **Script de transition** — `updates/<A>_to_<B>/update.sh`, **un seul fichier** :
   - shebang `#!/usr/bin/env bash`, `set -euo pipefail`
   - **idempotent** (rejouable sans casse)
   - tourne en `ben` ; `sudo` pour les `systemctl`
   - le code est déjà sur disque après le `git checkout` → le script ne fait que
     migrations/redémarrages/installs nécessaires
   - `chmod +x` les deux fichiers

3. **⚠️ SHA256 (à NE PAS oublier)** — générer le checksum adjacent, depuis la
   racine du repo :
   ```bash
   f="updates/<A>_to_<B>/update.sh"
   shasum -a 256 "$f" | awk '{print $1"  update.sh"}' > "$f.sha256"
   shasum -a 256 -c "$f.sha256"   # -> OK
   ```
   🚨 L'agent valide la signature GPG, fait le checkout, **puis refuse d'exécuter un
   script dont il ne peut pas vérifier l'empreinte**. Oubli constaté le 2026-08-12 →
   **pi-0.9.5 brûlée**, republiée en 0.9.6.
   Format du fichier : `<digest hex>  <chemin relatif>` (le device ne lit que le
   digest, le chemin est cosmétique).

4. **compatibility.yaml** — **un seul endroit**, `updates_caps:`, en fin de liste :
   ```yaml
   - from: "<A>"
     to:   "<B>"
     tag:  "pi-<B>"
     script: "updates/<A>_to_<B>/update.sh"   # <le raisonnement, les mesures, les pièges>
   ```
   🚨 **L'agent ne lit QUE `updates_caps`** — jamais `history:`, jamais `latest:`, jamais
   `updates: <modèle>:`. Ces clés sont la matrice d'AVANT le ménage, conservée pour
   l'histoire : y écrire une transition ne déclenche **rien**.
   ⓘ Il lit `updates_caps` depuis **`origin/main`**, alors que `update.sh` vient **du tag**.
   Donc corriger une transition ne demande pas de tag ; corriger un script, si.
   ⚠️ **`install.sh` → `INITIAL_TAG`** doit valoir le tag de cette transition, sinon tout
   provisioning neuf part d'une version périmée et plante.

5. **CHANGELOG.md** — une entrée `[<B>]`. Les notes vivent là, pas dans
   `compatibility.yaml`.

6. **Commit** (Claude) — message clair, **sans `Co-Authored-By`** (convention BEN).
   Ne PAS committer d'images/artefacts non liés.

7. **Tag GPG + push** (Xavier — `pinentry-tty`, terminal interactif requis) :
   ```bash
   export GPG_TTY=$(tty)   # sans ça la signature échoue par « Opération annulée »
   git push origin main
   git tag -s pi-<B> -F <fichier>   # sujet + corps : l'effet, pas le geste
   git tag -v pi-<B> | head -3      # avant de pousser
   git push origin pi-<B>
   ```
   ⚠️ **Pousser `main` AVANT le tag ouvre une fenêtre** : `updates_caps` vient
   d'`origin/main`, le tag n'existe pas encore, donc tout boîtier qui tique entre les deux
   échoue sur `tag not found`. Sans danger — `device.json` non bumpé, rejeu au tick suivant,
   et c'est arrivé le 2026-10-06 — mais ne pas s'en inquiéter en lisant le journal.

8. **Les devices se mettent à jour seuls** au prochain tick (~10 min). Rien à
   faire à la main sur un device. Suivre : `journalctl -u ben-update.service -f`.

## Vérifs avant de pousser le tag

🚨 **La commande obligatoire est dans `~/work/ben/CLAUDE.md`** (« Publier une release OTA —
checklist OBLIGATOIRE ») : elle vérifie les sommes de **tous** les `update.sh`, la dernière
transition de `updates_caps`, l'`INITIAL_TAG` d'`install.sh` et l'entrée de CHANGELOG. **Exiger
`✅ TOUT OK` avant de proposer un tag.** Un tag publié ne se réécrit jamais : une release ratée
est une version **brûlée**.

Et ce qu'elle ne peut pas vérifier :

- [ ] `bash -n update.sh` (syntaxe), `chmod +x` ok
- [ ] les bancs du dépôt verts, et le banc neuf **vu tomber** (sabotages vérifiés rouges)
- [ ] 🚨 le **contrôle d'effet** exécuté **tel quel sur un boîtier**, en `ssh`, AVANT de faire
      signer le tag — un garde-fou faux brûle une version aussi sûrement qu'un vrai défaut
      (pi-0.9.12 : le contrôle interrogeait `/info`, route inexistante). Contrôler sur
      `/health`, qui prouve en plus que la base est lisible (`db: true`)
- [ ] le dépôt des boîtiers **testés à la main** nettoyé : l'agent fait un `git checkout` **nu**
- [ ] commit fait, working tree propre

## Règle d'or : immutabilité des tags

**Ne jamais ré-écrire/déplacer un tag déjà poussé.** Un device qui a fait
`git fetch --tags` garde sa réf locale ; déplacer le tag ne la met pas à jour →
OTA bloquée sur ce device.

**Si un tag publié est cassé** (ex. `.sha256` oublié) : ne pas le re-tagger.
Publier une **nouvelle version** avec une **transition directe** depuis la
dernière version saine, et laisser le tag cassé devenir orphelin (plus
référencé dans `compatibility.yaml`). Exemple réel : `pi-0.0.27` taggé sans
`.sha256` → on a publié `pi-0.0.28` (transition `0.0.26 → 0.0.28` directe).

## Changement cassant ?

Si la MAJ nécessite une version d'app spécifique (ex. la vérif couleur au
provisioning exige l'app avec l'étape VERIFY), le signaler dans `notes:` et
**coordonner** la sortie firmware ↔ app. Une transition n'impactant que le
provisioning BLE n'affecte pas un device déjà en service en mode normal.
