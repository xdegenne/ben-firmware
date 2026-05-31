#!/usr/bin/env bash
# update.sh — pi0-lora transition 0.0.9 → 0.0.10
#
# Code change : heartbeat LED encore plus discret (2 flashs courts de 0.1 s
# × intensité 5/255 = ~2%, séparés de 1 s, toutes les 60 s).
# Aucune migration de données / config — juste restart du service pour
# que le nouveau main.py prenne effet.

set -euo pipefail

echo "[update 0.0.9→0.0.10] restart ben-lora-receiver pour charger le nouveau heartbeat"
# check_update.py tourne en user `ben` (cf. ben-update.service User=ben) — pas root.
# `ben` a NOPASSWD sudo (install.sh step 4), on doit donc préfixer.
sudo systemctl restart ben-lora-receiver.service

echo "[update 0.0.9→0.0.10] done"
