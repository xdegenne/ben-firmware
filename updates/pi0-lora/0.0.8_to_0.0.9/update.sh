#!/usr/bin/env bash
# update.sh — pi0-lora transition 0.0.8 → 0.0.9
#
# Code change : heartbeat LED revue pour une présence "calme" (flash 0.4 s
# au lieu de 0.05 s × intensité 10/255 = ~4% au lieu de 30/255 = ~12%).
# Aucune migration de données / config — juste restart du service pour
# que le nouveau main.py prenne effet.

set -euo pipefail

echo "[update 0.0.8→0.0.9] restart ben-lora-receiver pour charger le nouveau heartbeat"
# check_update.py tourne en user `ben` (cf. ben-update.service User=ben) — pas root.
# `ben` a NOPASSWD sudo (install.sh step 4), on doit donc préfixer.
sudo systemctl restart ben-lora-receiver.service

echo "[update 0.0.8→0.0.9] done"
