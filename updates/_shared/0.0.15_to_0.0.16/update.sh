#!/usr/bin/env bash
# update.sh — pi-0.0.15 → pi-0.0.16
#
# NO-OP : 0.0.16 corrige le bug d'apt redirect dans la transition
# 0.0.13 → 0.0.15 (cf. note compatibility.yaml). Les devices déjà à
# 0.0.15 n'ont rien à appliquer — juste alignement version.

set -euo pipefail
echo "[update pi-0.0.15 → pi-0.0.16] no-op (alignement version)"
echo "[update pi-0.0.15 → pi-0.0.16] ✓ done"
