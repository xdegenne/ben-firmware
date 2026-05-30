#!/usr/bin/env bash
# update.sh — pi-0.0.2 → pi-0.0.3
#
# Agent code-only change + bump ben-update.timer cadence (24h → 10min for dev).
# check_update.py has already done: git fetch + git verify-tag pi-0.0.3
# + git checkout pi-0.0.3 → /opt/ben/repo is at the new tag.

set -euo pipefail

REPO_PATH="/opt/ben/repo"

# 1. Réappliquer l'unit timer (cadence 10 min) — la modif vit dans la golden
#    image au provisioning, donc l'OTA doit re-copier les units modifiés.
sudo cp "$REPO_PATH/config/systemd/ben-update.timer" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart ben-update.timer
echo "  ben-update.timer: cadence 10min appliquée"

# 2. Restart only services actually enabled on this model.
for svc in ben-tic-reader.service ben-lora-receiver.service; do
    if sudo systemctl is-enabled --quiet "$svc" 2>/dev/null; then
        sudo systemctl restart "$svc"
        echo "  restarted: $svc"
    fi
done

echo "Update pi-0.0.2 → pi-0.0.3 done."
