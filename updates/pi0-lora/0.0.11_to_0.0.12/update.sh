#!/usr/bin/env bash
# update.sh — pi0-lora transition 0.0.11 → 0.0.12
#
# Code change : heartbeat UX-driven — flashs OK (vert) très courts (50 ms),
# flashs erreur (violet/orange) plus longs (300 ms) et un poil plus intenses
# (8/255) pour que l'œil attrape les alertes en passant.
# Aucune migration de données / config — juste restart du service.

set -euo pipefail

echo "[update 0.0.11→0.0.12] restart ben-lora-receiver pour charger heartbeat UX-driven"
sudo systemctl restart ben-lora-receiver.service

echo "[update 0.0.11→0.0.12] done"
