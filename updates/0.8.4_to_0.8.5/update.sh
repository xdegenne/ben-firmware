#!/usr/bin/env bash
# update.sh — pi-0.8.4 → pi-0.8.5   (MONO-FLUX) : échelle de jauge résolue CÔTÉ BOÎTIER
#
# CONTEXTE : `/live` renvoyait le `plafond` OBSERVÉ (high-water mark de la PAPP) sans condition.
# Juste après l'unboxing, ce max ≈ la conso courante (une poignée de mesures) → l'app scalait la
# jauge dessus → une conso FAIBLE apparaissait ROUGE. L'app faisait aussi l'arbitrage
# (`plafond ?? maxVa ?? 9000`) — de la logique métier qui n'a rien à faire côté app.
#
# FIX (local_api.py + levels.py) : le boîtier résout l'échelle et l'expose autoritative, comme
# `level` / `tariff_label`. `plafond` = high-water mark OBSERVÉ **uniquement si le foyer est
# CONNU** (même gate que le niveau : `levels.is_known` = ≥ MIN_SAMPLES, ≥ 2 j, dynamique) ; sinon
# = l'ABONNEMENT (maxVa PREF/ISOUSC, sinon DEFAULT_MAXVA=9000). L'app affiche `plafond` tel quel.
#
# → concerne TOUS les modèles : `ben-local-api` (qui sert `/live`) tourne partout. On le
#   redémarre pour charger le nouveau code. Reader/receiver non touchés.
set -euo pipefail
TR="pi-0.8.4 → pi-0.8.5"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"

# Garde-fous : le fix doit être dans le checkout.
grep -q 'def is_known' "$REPO/src/pi/store/levels.py" \
    || fail "levels.is_known absent (checkout incomplet ?)"
grep -q 'DEFAULT_MAXVA' "$REPO/src/pi/store/local_api.py" \
    || fail "résolution d'échelle absente de local_api (checkout incomplet ?)"

if systemctl is-active --quiet ben-local-api.service; then
    log "restart ben-local-api (charge l'échelle résolue côté boîtier)"
    sudo systemctl restart ben-local-api.service || fail "restart ben-local-api"
    log "✓ /live sert désormais l'échelle résolue (plus de faux rouge à l'unboxing)"
else
    log "ben-local-api inactif (device non provisionné ?) — rien à redémarrer"
fi
log "✓ update OK"
