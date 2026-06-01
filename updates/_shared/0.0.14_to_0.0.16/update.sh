#!/usr/bin/env bash
# update.sh — pi-0.0.14 → pi-0.0.16
#
# NO-OP : la version 0.0.14 a été publiée avec un fichier .sha256 manquant
# (cf. compatibility.yaml). Les devices qui étaient effectivement passés en
# 0.0.14 (provisioning manuel) doivent être alignés à 0.0.16 — qui contient
# exactement le même code applicatif, juste avec les .sha256 ajoutés.
# Aucune action runtime requise, le git checkout du tag a déjà tout amené.

set -euo pipefail
echo "[update pi-0.0.14 → pi-0.0.16] no-op (alignement version seulement)"
echo "[update pi-0.0.14 → pi-0.0.16] ✓ done"
