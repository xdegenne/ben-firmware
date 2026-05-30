#!/usr/bin/env bash
# update.sh — pi-0.0.4 → pi-0.0.5
# Agent code-only change. Just restart the running agents.

set -euo pipefail

for svc in ben-tic-reader.service ben-lora-receiver.service; do
    if sudo systemctl is-enabled --quiet "$svc" 2>/dev/null; then
        sudo systemctl restart "$svc"
        echo "  restarted: $svc"
    fi
done

echo "Update pi-0.0.4 → pi-0.0.5 done."
