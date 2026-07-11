#!/usr/bin/env bash
# update.sh — pi-0.7.0 → pi-0.8.0   (MONO-FLUX capabilities) : relabel model → commercial
#
# Petite migration : renomme device.json.model en libellé COMMERCIAL affiché par l'app
# (pi0-wired→Filaire, pi0-lora→Radio, pi0-lora-wired→Mixte). Concerne TOUS les devices (PAS
# capability-gated) → teste l'OTA capabilities sur tout le parc avec une vraie action. `model`
# n'est PAS branché au runtime (juste exposé/affiché firmware+app) → relabel sûr.
#
# NB (leçon pi-0.6.0) : l'écriture device.json se fait SANS sudo → l'agent tourne en `ben`, le dir
# /etc/ben-firmware est ben-owned → device.json reste ben-owned (l'agent peut bumper ensuite).
set -euo pipefail
TR="pi-0.7.0 → pi-0.8.0"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"
DJ="/etc/ben-firmware/device.json"

log "relabel device.json.model → commercial"
python3 - "$REPO" "$DJ" <<'PY'
import json, sys, os
repo, p = sys.argv[1], sys.argv[2]
sys.path.insert(0, os.path.join(repo, "src/pi"))
from capabilities import label_for_model     # SOURCE DE VÉRITÉ model technique → label
d = json.load(open(p))
label = label_for_model(d.get("model"))
if label is None:
    print(f"  model '{d.get('model')}' déjà relabellé ou inconnu → no-op"); raise SystemExit(0)
print(f"  {d['model']} → {label}")
d["model"] = label
tmp = p + ".tmp"                              # écriture atomique, SANS sudo → reste ben-owned
json.dump(d, open(tmp, "w"), indent=2)
json.load(open(tmp))
os.replace(tmp, p)
PY
[ $? -eq 0 ] || fail "relabel échoué — device.json inchangé"

log "restart ben-local-api (sert la nouvelle valeur model à l'app)"
sudo systemctl restart ben-local-api.service || fail "restart ben-local-api"
log "✓ update OK — model relabellé"
