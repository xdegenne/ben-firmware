#!/usr/bin/env bash
# update.sh — pi-0.0.3 → pi-0.0.4
#
# Agent code-only change. No new deps, no service add/remove, no timer change.
# check_update.py has already done: git fetch + verify-tag + checkout pi-0.0.4.
# Just restart the running agents to load the new code.

set -euo pipefail

for svc in ben-tic-reader.service ben-lora-receiver.service; do
    if sudo systemctl is-enabled --quiet "$svc" 2>/dev/null; then
        sudo systemctl restart "$svc"
        echo "  restarted: $svc"
    fi
done

echo "Update pi-0.0.3 → pi-0.0.4 done."
