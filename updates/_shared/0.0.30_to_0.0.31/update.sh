#!/usr/bin/env bash
# update.sh — pi-0.0.30 → pi-0.0.31
#
# Provisioner BLE (UX vérification couleur) :
#   - code couleur correct → 3 blinks verts rapides (au lieu du vert fixe permanent).
#   - délai de 10s avant d'afficher le code couleur à la connexion (le blink
#     violet/jaune d'attente reste visible pendant ce délai).
#   → code provisioner appliqué au prochain provisioning (service on-demand) :
#     rien à redémarrer.
#
# ben-led-release.service : flash blanc "welcome" rendu ATOMIQUE (les 3 canaux
#   R/G/B commutés en une seule commande pinctrl). Avant, 3 commandes séparées
#   faisaient voir un balayage rouge→jaune→blanc→cyan→bleu au boot.
#   → c'est un fichier .service : il faut le réinstaller dans /etc/systemd/system
#     + daemon-reload (effet visible au prochain boot).
#
# Tourne en `ben` (sudo pour install/systemctl). Idempotent.

set -euo pipefail

TR="pi-0.0.30 → pi-0.0.31"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }

REPO="${REPO_PATH:-/opt/ben/repo}"
MAIN="$REPO/src/pi/provisioner/main.py"
LEDREL="$REPO/config/systemd/ben-led-release.service"

# Garde-fous : le checkout doit bien porter le nouveau code.
grep -q "VERIFY_DISPLAY_DELAY_SEC" "$MAIN" \
    || fail "VERIFY_DISPLAY_DELAY_SEC absent de main.py (checkout incomplet ?)"
grep -q "threading.Timer(VERIFY_DISPLAY_DELAY_SEC" "$MAIN" \
    || fail "délai code couleur absent de main.py (checkout incomplet ?)"
grep -q "set 12,13,16 op dh" "$LEDREL" \
    || fail "flash blanc atomique absent de ben-led-release.service (checkout incomplet ?)"

log "[1/2] réinstallation de ben-led-release.service (flash blanc atomique)"
sudo install -m 644 "$LEDREL" /etc/systemd/system/ben-led-release.service

log "[2/2] daemon-reload"
sudo systemctl daemon-reload

log "✓ update OK — UX vérif couleur (3 blinks verts + délai 10s) + flash boot atomique (effet au prochain boot/provisioning)"
