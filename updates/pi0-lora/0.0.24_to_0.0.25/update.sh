#!/usr/bin/env bash
# update.sh — pi-0.0.24 → pi-0.0.25
#
# Niveau de consommation 1..4 exposé par l'API /live (visuel app).
#   - nouveau module store/levels.py : percentiles PAPP du foyer (P30/P70/P90
#     sur 7 j glissants) → niveau 1..4, lissage PAPP 2 min + hystérésis.
#     Défaut 2 (« normal ») en cold-start (< 2 j d'historique).
#   - table level_profile (auto-créée par db.connect ; cf. db.py _SCHEMA).
#   - service planifié ben-level-profiler.{service,timer} : recalcul des seuils
#     1×/jour (SoC : seul ce job écrit le profil, l'API reste read-only).
#   - local_api.py : /live ajoute le champ "level" par PDL.
#
# Code déjà sur disque après `git checkout pi-0.0.25`. On installe les 2 units,
# on redémarre l'API (nouveau /live), on arme le timer + un 1er calcul. Pas de
# reboot, pas de restart reader (la table se crée au 1er calcul du profileur).
#
# Tourne en `ben` ; sudo pour les ops systemd. Idempotent.

set -euo pipefail

TR="pi-0.0.24 → pi-0.0.25"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }

REPO="${REPO_PATH:-/opt/ben/repo}"
SVC="$REPO/config/systemd/ben-level-profiler.service"
TMR="$REPO/config/systemd/ben-level-profiler.timer"

# Préflight — le code doit être présent (checkout du tag fait en amont).
[ -f "$REPO/src/pi/store/levels.py" ] || fail "manquant : src/pi/store/levels.py"
[ -f "$SVC" ] || fail "manquant : $SVC"
[ -f "$TMR" ] || fail "manquant : $TMR"

# 1. Install des units du profileur
log "[1/4] install ben-level-profiler.{service,timer}"
sudo install -m 644 -o root -g root "$SVC" \
    /etc/systemd/system/ben-level-profiler.service || fail "install service"
sudo install -m 644 -o root -g root "$TMR" \
    /etc/systemd/system/ben-level-profiler.timer || fail "install timer"
sudo systemctl daemon-reload || fail "daemon-reload"

# 2. Restart de l'API pour charger le nouveau /live (+ import levels)
log "[2/4] restart ben-local-api.service"
sudo systemctl restart ben-local-api.service || fail "restart ben-local-api"

# 3. Armer le timer (recalcul quotidien)
log "[3/4] enable + start ben-level-profiler.timer"
sudo systemctl enable ben-level-profiler.timer || fail "enable timer"
sudo systemctl start  ben-level-profiler.timer || log "  ⚠ start timer (non bloquant)"

# 4. Premier calcul immédiat (crée la table level_profile + profil cold-start)
log "[4/4] premier calcul (ben-level-profiler.service)"
sudo systemctl start ben-level-profiler.service || log "  ⚠ 1er calcul (non bloquant)"

log "✓ update OK — /live expose 'level', profileur quotidien armé"
