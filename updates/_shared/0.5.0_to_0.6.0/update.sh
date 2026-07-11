#!/usr/bin/env bash
# update.sh — pi-0.5.0 → pi-0.6.0   (_shared, per-model : MIGRATION model → capabilities)
#
# Dernière release en mode PER-MODEL (l'ancien agent la trouve dans updates.pi0-wired /
# updates.pi0-lora). Elle bascule le device vers le modèle CAPABILITIES :
#   1. écrit `capabilities` dans device.json, DÉRIVÉ du `model` connu (pas de sonde) ;
#   2. le nouvel agent + le module capabilities + l'orchestrateur sont déjà sur disque (checkout) ;
#   3. redémarre les readers via les capabilities ;
#   4. NE redémarre PAS ben-update.service (l'agent) → le nouvel agent prend effet au prochain tick.
# Idempotent + GARDÉ : n'écrit capabilities QUE si device.json reste valide (increvable pour le
# parc dans la nature). `model` est CONSERVÉ dans device.json (sécurité rollback vers l'ancien agent).
set -euo pipefail
TR="pi-0.5.0 → pi-0.6.0 (migration capabilities)"
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

log "daemon-reload + démarrage des readers via capabilities"
sudo systemctl daemon-reload || fail "daemon-reload"
# les readers via le module (source de vérité capa→service). Ne touche PAS ben-update.service.
sudo python3 "$REPO/src/pi/capabilities.py" boot || log "  ⚠ start readers (non bloquant)"
log "✓ migration OK — device en capabilities (nouvel agent actif au prochain tick OTA)"
