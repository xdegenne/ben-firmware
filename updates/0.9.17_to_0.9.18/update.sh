#!/usr/bin/env bash
# update.sh — 0.9.17 → pi-0.9.18 : ALIGNEMENT DE VERSION, rien d'autre.
#
# ═══ POURQUOI UNE TRANSITION QUI NE FAIT RIEN ═════════════════════════════════════════════════
#
#   pi-0.9.17 a livré le bon CODE et un mauvais SCRIPT : sa garde ne jouait pas, et il démarrait
#   sur les boîtiers radio un lecteur filaire qui n'a rien à y faire (détail dans
#   updates/0.9.16_to_0.9.18/update.sh).
#
#   ⭐ Les boîtiers FILAIRES qui ont pris 0.9.17 n'ont, eux, rien subi : le lecteur y tournait
#      déjà, donc `restart` l'a bien redémarré, et le contrôle d'effet a été vert. Leur code est
#      DÉJÀ celui de 0.9.18 — les deux tags portent le même arbre pour le lecteur TIC.
#
#   ⇒ Il ne reste qu'à aligner le numéro de version, pour que le parc soit sur un seul palier et
#     que la prochaine transition n'ait qu'un `from` à gérer.
#
# ═══ 🚨 CE QU'UNE TRANSITION VIDE NE DOIT SURTOUT PAS FAIRE ═══════════════════════════════════
#
#   NE PAS REDÉMARRER LE LECTEUR. Il tourne, il lit, son code est déjà le bon. Le redémarrer
#   coûterait une poignée de secondes de mesures sur chaque boîtier du parc, pour rien — et
#   rouvrirait la fenêtre de course au GPIO de la LED sur un `pi0-lora-wired`.
#
#   ⚠️ « Ça ne coûte rien de redémarrer par précaution » est faux deux fois : ça coûte des
#      mesures, et ça expose à un mode de panne qu'on vient justement de payer.
#
# ═══ CE QU'ELLE FAIT QUAND MÊME, ET POURQUOI ══════════════════════════════════════════════════
#
#   Elle VÉRIFIE le checkout. Le tag est neuf, et un tag est une chose qu'on peut rater : un
#   fichier manquant ou un fichier qui ne compile pas doit être vu MAINTENANT, tant que rien n'a
#   bougé, plutôt qu'au prochain redémarrage du lecteur — c'est-à-dire au prochain reboot, des
#   semaines plus tard, sans que personne ne relie la panne au tag.
#
#   ⭐ Le coût est nul et le bénéfice est un échec PROPRE : `device.json` n'est pas bumpé, rien
#      n'est touché, l'update retentera. Un boîtier en 0.9.17 avec un lecteur qui tourne est un
#      état parfaitement sain — il n'y a aucune urgence à en sortir.
#
# AUCUN service touché. AUCUNE migration. Tourne en `ben`, et n'utilise même pas sudo.

set -euo pipefail
TR="→ pi-0.9.18"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"
TICDIR="$REPO/src/pi/tic-reader"

for f in "$TICDIR/tic_parite.py" "$TICDIR/main_uart.py"; do
    [ -f "$f" ] || fail "absent du dépôt : $f (checkout pi-0.9.18 incomplet ?)"
done

# ⚠️ `ast.parse`, jamais `py_compile` : celui-ci écrit dans __pycache__, qui appartient à root
# sur les boîtiers du parc alors que l'OTA tourne en `ben`.
python3 - "$TICDIR/tic_parite.py" "$TICDIR/main_uart.py" <<'PYEOF' || fail "un fichier Python livré ne compile pas"
import ast, pathlib, sys
for a in sys.argv[1:]:
    p = pathlib.Path(a)
    ast.parse(p.read_text(encoding="utf-8"), filename=p.name)
PYEOF

grep -q "from tic_parite import octet_valide" "$TICDIR/main_uart.py" \
    || fail "main_uart.py n'importe pas tic_parite — le checkout n'est pas celui attendu"

log "✓ checkout vérifié (fichiers présents, compilent, import branché)"
log "✓ aucun service touché — le lecteur tourne déjà avec ce code"
log "✓ update OK (alignement de version)"
