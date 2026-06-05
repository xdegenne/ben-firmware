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

Si une étape échoue → `device.json` **non modifié**, le device reste sur sa
version et re-tente au prochain tick. Donc une release cassée **bloque** la
montée de version (sans danger : le device reste sur l'ancienne).

Sécurité = **GPG (tag signé) + SHA256 (script)**. Les deux sont obligatoires.

## Checklist de release (version N → N+1)

Soit `M` le modèle (`pi0-wired`, `pi0-lora`, …) et la transition `A_to_B`
(ex. `0.0.26_to_0.0.28`).

1. **Code** — modifier `src/pi/...`.

2. **Script de transition** — `updates/<M>/<A>_to_<B>/update.sh`
   (+ miroir `updates/_shared/<A>_to_<B>/update.sh`, contenu identique) :
   - shebang `#!/usr/bin/env bash`, `set -euo pipefail`
   - **idempotent** (rejouable sans casse)
   - tourne en `ben` ; `sudo` pour les `systemctl`
   - le code est déjà sur disque après le `git checkout` → le script ne fait que
     migrations/redémarrages/installs nécessaires
   - `chmod +x` les deux fichiers

3. **⚠️ SHA256 (à NE PAS oublier)** — générer le checksum adjacent, depuis la
   racine du repo, pour **chaque** `update.sh` :
   ```bash
   for d in <M> _shared; do
     f="updates/$d/<A>_to_<B>/update.sh"
     shasum -a 256 "$f" > "$f.sha256"
   done
   # vérif :
   shasum -a 256 -c updates/<M>/<A>_to_<B>/update.sh.sha256   # -> OK
   ```
   Format du fichier : `<digest hex>  <chemin relatif>` (le device ne lit que le
   digest, le chemin est cosmétique).

4. **compatibility.yaml** — 3 endroits :
   - `devices: <M>: rev01: pi: latest:` → `"<B>"`
   - ajouter une entrée `history:` (version `<B>`, `released:`, `notes:`)
   - `updates: <M>:` → ajouter
     ```yaml
     - from: "<A>"
       to: "<B>"
       tag: "pi-<B>"
       script: "updates/<M>/<A>_to_<B>/update.sh"
     ```

5. **Commit** (Claude) — message clair, **sans `Co-Authored-By`** (convention BEN).
   Ne PAS committer d'images/artefacts non liés.

6. **Tag GPG + push** (Xavier — `pinentry-tty`, terminal interactif requis) :
   ```bash
   git push origin main
   git tag -s pi-<B> -m "pi-<B> — <résumé>"
   git push origin pi-<B>
   ```

7. **Les devices se mettent à jour seuls** au prochain tick (~10 min). Rien à
   faire à la main sur un device. Suivre : `journalctl -u ben-update.service -f`.

## Vérifs avant de pousser le tag

- [ ] `update.sh` **et** `update.sh.sha256` présents (pi0-wired **et** _shared)
- [ ] `shasum -a 256 -c .../update.sh.sha256` → `OK`
- [ ] `bash -n update.sh` (syntaxe), `chmod +x` ok
- [ ] `compatibility.yaml` : `latest`, `history`, transition `updates:` cohérents
- [ ] le `tag:` de la transition == le tag qu'on va signer
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
