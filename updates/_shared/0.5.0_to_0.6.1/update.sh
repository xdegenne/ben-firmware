#!/usr/bin/env bash
# update.sh — MIGRATION model → capabilities   (_shared, per-model → pi-0.6.1)
#
# Utilisé par 0.5.0→0.6.1 (migration) ET 0.6.0→0.6.1 (récup d'un device migré). pi-0.6.1 = BUG FIX
# de pi-0.6.0 : cette version écrivait device.json en sudo → root-owned → l'agent (ben) ne pouvait
# plus bumper softwareVersion (PermissionError, retry en boucle → device cassé). FIX = chown ben:ben
# INCONDITIONNEL en fin de script → restaure l'ownership, y compris sur le chemin « déjà migré »
# (AUTO-RÉPARE un device cassé par 0.6.0). Sinon inchangé : écrit capabilities (dérivées du model,
# model CONSERVÉ), idempotent, redémarre les readers, NE restart PAS l'agent (nouvel agent au tick suivant).
set -euo pipefail
TR="→ pi-0.6.1 (migration/fix capabilities)"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"
DJ="/etc/ben-firmware/device.json"

grep -q 'updates_caps\|find_next_transition' "$REPO/src/pi/updater/update_lib.py" \
    || fail "update_lib.py pas à jour (nouvel agent absent — checkout incomplet ?)"
[ -f "$REPO/src/pi/capabilities.py" ] || fail "capabilities.py absent — checkout incomplet ?"

log "écriture capabilities dans device.json (dérivé du model)"
sudo python3 - "$REPO" "$DJ" <<'PY'
import json, sys, os
repo, p = sys.argv[1], sys.argv[2]
sys.path.insert(0, os.path.join(repo, "src/pi"))
from capabilities import caps_for_model     # SOURCE DE VÉRITÉ UNIQUE model→caps
d = json.load(open(p))
if "capabilities" in d:
    print("  déjà migré (capabilities présentes) → no-op"); raise SystemExit(0)
caps = caps_for_model(d.get("model"), d.get("hardwareRevision", "rev01"))
if caps is None:
    print(f"  ✗ model inconnu: {d.get('model')!r}", file=sys.stderr); raise SystemExit(1)
d["capabilities"] = caps                    # `model` CONSERVÉ (rollback safety)
# écriture atomique + validation (increvable)
tmp = p + ".tmp"
json.dump(d, open(tmp, "w"), indent=2)
json.load(open(tmp))                         # re-parse = garde-fou
os.replace(tmp, p)
print("  capabilities:", list(caps))
PY
[ $? -eq 0 ] || fail "écriture capabilities échouée — device.json inchangé"

# CRUCIAL (fix pi-0.6.1) : l'écriture sudo ci-dessus rootifie device.json → sans ça l'agent (ben)
# ne peut plus bumper softwareVersion (PermissionError). chown INCONDITIONNEL → répare aussi un
# device déjà cassé par 0.6.0 (chemin « déjà migré » : le python no-op, ce chown restaure l'owner).
sudo chown ben:ben "$DJ" || fail "chown device.json"

log "daemon-reload + démarrage des readers via capabilities"
sudo systemctl daemon-reload || fail "daemon-reload"
# les readers via le module (source de vérité capa→service). Ne touche PAS ben-update.service.
sudo python3 "$REPO/src/pi/capabilities.py" boot || log "  ⚠ start readers (non bloquant)"
log "✓ migration OK — device en capabilities (nouvel agent actif au prochain tick OTA)"
