#!/usr/bin/env bash
# update.sh — pi0-lora transition 0.0.10 → 0.0.11
#
# Code change : SNR_MAX_PLAUSIBLE bumpé de 12 à 20 pour ne plus déclencher
# le warning "RSSI/SNR aberrants" en close-range (l'émetteur est à
# quelques dizaines de mètres → SNR ≈ 12-15 dB est tout à fait valide).
# Aucune migration de données / config — juste restart du service pour
# que le nouveau main.py prenne effet.

set -euo pipefail

echo "[update 0.0.10→0.0.11] restart ben-lora-receiver pour charger le nouveau SNR_MAX_PLAUSIBLE"
sudo systemctl restart ben-lora-receiver.service

echo "[update 0.0.10→0.0.11] done"
