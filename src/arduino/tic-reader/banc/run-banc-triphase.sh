#!/usr/bin/env bash
# Extrait le texte réel du .ino, compile le harnais, et le lance.
set -euo pipefail
cd "$(dirname "$0")"   # …/tic-reader/banc
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
python3 extrait_reel.py "$TMP/reel.h"
c++ -std=c++17 -I"$TMP" -o "$TMP/banc" test_triphase_emetteur.cpp
"$TMP/banc"
