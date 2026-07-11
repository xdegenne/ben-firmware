#!/usr/bin/env bash
# update.sh — pi-0.8.0 → pi-0.8.1   (MONO-FLUX) : fix agent OTA (clobber device.json)
#
# CONTEXTE : la 0.8.0 relabellait device.json.model dans update.sh, mais l'agent OTA réécrivait
# ENSUITE une copie mémoire de device.json prise AVANT update.sh (save_device_json écrase tout le
# fichier) → le relabel était annulé (model resté technique, softwareVersion=0.8.0). Même classe de
# bug pour TOUT update.sh qui modifie device.json (les capabilities n'avaient survécu que par chance,
# via le pré-write du bug 0.6.0). Le relabel, lui, n'avait pas cette chance → perdu.
#
# FIX : entièrement dans le CODE de l'agent (check_update.py), mis à jour par le checkout du tag :
#   1. re-lecture de device.json depuis le DISQUE juste avant le bump softwareVersion
#      → les edits d'update.sh survivent désormais ;
#   2. self-heal au démarrage : normalise model → libellé commercial (Filaire/Radio/Mixte),
#      idempotent, à CHAQUE tick → rétro-corrige les devices déjà à 0.8.0 sans 2e release.
#
# → RIEN à faire ici : le nouveau code agit au tick suivant (l'agent courant est encore l'ancien).
#   local_api lit device.json PAR REQUÊTE → aucun restart. No-op volontaire.
set -euo pipefail
TR="pi-0.8.0 → pi-0.8.1"
log() { echo "[update $TR] $*"; }

# Garde-fou : vérifier que le nouvel agent (avec le fix) est bien checké out, sinon le fix
# n'agira pas au prochain tick (checkout incomplet).
REPO="${REPO_PATH:-/opt/ben/repo}"
grep -q 'Self-heal' "$REPO/src/pi/updater/check_update.py" \
    || { echo "[update $TR] ✗ ERREUR : check_update.py pas à jour (fix absent — checkout incomplet ?)" >&2; exit 1; }

log "fix agent OTA (self-heal relabel + re-read avant bump) — effet au prochain tick"
log "✓ update OK"
