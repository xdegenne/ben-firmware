#!/usr/bin/env bash
# update.sh — pi-0.2.0 → pi-0.3.0   (pi0-wired)
#
# ROLLUP PAR INDEX — Phase 3 (côté LECTURE). Exploite le rollup (rempli en 0.2.0) :
#  - NOUVEL endpoint GET /chart : courbe RICHE {points, tariff_bands, source}. Le serveur arbitre
#    la source des points (rollup rapide sur vue large / brut au zoom, ou raw=1 pour forcer le brut) ;
#    `tariff_bands` = zones tarifaires AUTO-DESCRIPTIVES (from/to/kind/index_id/src_standard/label) —
#    tous tarifs distingués (HC/HP, Tempo, EJP), histo ET standard, jamais un parcours de points.
#    /curve reste INTACT (brut) → app courante inchangée.
#  - /consumption et /registers ACCÉLÉRÉS via le rollup (index_last) avec fallback brut : mesuré sur
#    ben-0003 ×85 (/consumption, 3574→42 ms) et ×550 (/registers, 28 s→52 ms), résultat EXACT au Wh.
# STRICTEMENT ADDITIF : /curve/consumption/registers gardent leur forme (bascule INTERNE
# transparente) ; /chart est nouveau. Aucune migration BDD. Cf. docs/rollup-par-index.md §5/§6.
# Code déjà sur disque après checkout. Tourne en `ben`.
set -euo pipefail
TR="pi-0.2.0 → pi-0.3.0"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"
grep -q 'def tariff_bands' "$REPO/src/pi/store/db.py" \
    || fail "db.py pas à jour (tariff_bands absent — checkout incomplet ?)"
grep -q '"/chart"' "$REPO/src/pi/store/local_api.py" \
    || fail "local_api.py pas à jour (/chart absent — checkout incomplet ?)"
log "restart ben-tic-reader + ben-local-api (endpoint /chart + lectures rollup)"
sudo systemctl restart ben-tic-reader.service || fail "restart ben-tic-reader"
sudo systemctl restart ben-local-api.service  || fail "restart ben-local-api"
log "✓ update OK — /chart (courbe + bandes HP/HC) + /consumption & /registers accélérés (rollup)"
