#!/usr/bin/env bash
# update.sh — pi0-lora transition 0.0.12 → 0.0.13
#
# Code changes :
# - Trame reçue (RF, avant HMAC) : bleu 30/255 200ms → jaune 5/255 50ms espacé 400ms
# - Trame validée (HMAC OK) : vert 30/255 200ms → vert 5/255 50ms
# - Heartbeat : 2 flashs séparés de 1.0s → 0.5s (rapprochés)
# Aucune migration de données / config — juste restart du service.

set -euo pipefail

echo "[update 0.0.12→0.0.13] restart ben-lora-receiver pour charger flashs RX jaune/vert + heartbeat rapproché"
sudo systemctl restart ben-lora-receiver.service

echo "[update 0.0.12→0.0.13] done"
