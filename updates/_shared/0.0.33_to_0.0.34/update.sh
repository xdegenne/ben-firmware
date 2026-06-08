#!/usr/bin/env bash
# update.sh — pi-0.0.33 → pi-0.0.34
#
# Modèle de niveau « course [talon, plafond] » (remplace les percentiles
# P30/P70/P90 qui dégénéraient sur les foyers à faible base : un frigo seul
# était sur-noté niveau 3).
#   - db.py    : colonnes level_profile.talon + .papp_max_alltime (high-water
#                mark MONOTONE de la PAPP, maintenu par record_measurement, jamais
#                décrémenté → vrai plafond du foyer) + index idx_meas_pdl_papp.
#                Migrations ALTER + CREATE INDEX auto au prochain connect.
#   - levels.py: ratio = (PAPP−talon)/(plafond−talon), bandes 0,10/0,40/0,70 ;
#                talon = P15 calculé EN SQL ; cold-start (ou plafond<=talon) →
#                niveau 2.
#   → niveau enfin juste sur une maison vide (frigo = niveau 1).
#
# Backfill one-shot du plafond depuis l'historique (sinon papp_max_alltime NULL
# → niveau 2 jusqu'au 1er pic). Puis restart API + reader (charge le code) et
# recalcul immédiat du talon. Pas de reboot. Tourne en `ben`. Idempotent.

set -euo pipefail

TR="pi-0.0.33 → pi-0.0.34"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }

REPO="${REPO_PATH:-/opt/ben/repo}"
DEVICE_JSON="/etc/ben-firmware/device.json"
STORE="$REPO/src/pi/store"

[ -f "$STORE/levels.py" ] || fail "manquant : src/pi/store/levels.py"
grep -q "papp_max_alltime" "$STORE/db.py" \
    || fail "db.py pas à jour (checkout incomplet ?)"

MODEL=$(python3 -c "import json;print(json.load(open('$DEVICE_JSON'))['model'])") \
    || fail "lecture du model depuis device.json"
log "[1/4] modèle = $MODEL"

# Migration (ALTER/index via db.connect) + backfill du plafond depuis l'historique.
log "[2/4] migration colonnes + backfill papp_max_alltime depuis l'historique"
sudo -u ben python3 - "$STORE" <<'PYBF'
import sys
sys.path.insert(0, sys.argv[1])
import db
conn = db.connect()  # applique les ALTER + CREATE INDEX
rows = conn.execute(
    "SELECT pdl_index, MAX(papp) FROM measurements "
    "WHERE papp IS NOT NULL GROUP BY pdl_index").fetchall()
for pdl, mx in rows:
    conn.execute(
        "INSERT INTO level_profile (pdl_index, computed_ts, papp_max_alltime) "
        "VALUES (?, 0, ?) "
        "ON CONFLICT(pdl_index) DO UPDATE SET "
        "papp_max_alltime = MAX(COALESCE(papp_max_alltime, 0), excluded.papp_max_alltime)",
        (pdl, mx))
conn.commit()
conn.close()
print(f"  backfill plafond : {len(rows)} pdl(s)")
PYBF

log "[3/4] restart ben-local-api + reader (charge le nouveau code)"
sudo systemctl restart ben-local-api.service || fail "restart ben-local-api"
case "$MODEL" in
    pi0-wired)
        sudo systemctl restart ben-tic-reader.service || fail "restart tic-reader" ;;
    pi0-lora)
        sudo systemctl restart ben-lora-receiver.service || fail "restart lora-receiver" ;;
    pi0-lora-wired)
        sudo systemctl restart ben-tic-reader.service ben-lora-receiver.service \
            || fail "restart readers" ;;
    *)
        log "modèle sans reader connu — skip restart reader" ;;
esac

log "[4/4] recalcul immédiat du talon (sinon attente du timer ~1x/jour)"
sudo systemctl start ben-level-profiler.service \
    || log "profiler non démarré maintenant — le timer le fera"

log "✓ update OK — niveau = course [talon, plafond]"
