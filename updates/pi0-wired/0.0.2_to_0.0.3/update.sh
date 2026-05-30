#!/usr/bin/env bash
# update.sh — pi-0.0.2 → pi-0.0.3
#
# Agent code-only change. No new dependencies. No service add/remove.
# check_update.py has already done: git fetch + git verify-tag pi-0.0.3
# + git checkout pi-0.0.3 → /opt/ben/repo is at the new tag.
#
# All this script needs to do is restart the agent services so they
# load the new code.

set -euo pipefail

# Restart only services actually enabled on this model.
for svc in ben-tic-reader.service ben-lora-receiver.service; do
    if sudo systemctl is-enabled --quiet "$svc" 2>/dev/null; then
        sudo systemctl restart "$svc"
        echo "  restarted: $svc"
    fi
done

echo "Update pi-0.0.2 → pi-0.0.3 done."
