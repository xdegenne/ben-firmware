#!/usr/bin/env bash
# update.sh — → pi-0.9.4   : coloration HC/HP en HISTO Tempo/BBR (registres index 5-10).
#
# BUG : `db.HISTO_LABELS` (convention de libellé tarifaire en historique) s'arrêtait à l'index 4
# (Base / HC / HP / EJP). Les registres BBR/Tempo (index 5-10 : BBRHCJB…BBRHPJR) tombaient dans
# le trou → `resolve_label(histo, 5..10)` renvoyait None → `_band_kind(None)="base"` → l'app ne
# coloriait AUCUNE bande de courbe et n'affichait PAS le badge HC/HP ; `/live.tariff_label=null`.
# Révélé par ben-0010, 1er device Tempo (les histo précédents = Base/HC-HP, index 0-2, couverts).
#
# FIX : on complète HISTO_LABELS avec les 6 registres BBR. Le mot « Creuses »/« Pleines » du
# libellé pilote `_band_kind` → couleur HC (indigo) / HP (orange) de la courbe ET le badge live,
# et `/live.tariff_label` porte le libellé complet (« Heures Creuses Jours Bleus »…). Le jour
# Tempo (Bleu/Blanc/Rouge) reste dans le TEXTE seul : le tricolore n'est pas rendu par l'app
# (chantier couleur Tempo séparé). Détail : ../../CHANGELOG.md (0.9.4).
#
# Pur code (`store/db.py`) : aucune dépendance, aucune migration, aucun changement d'unit.
# UNIVERSEL (tout device qui sert /live+/chart : LoRa ET filaire — un filaire Tempo a le même
# trou) → PAS de gate capability. Restart ben-local-api (lit HISTO_LABELS via resolve_label).
# Code déjà sur disque après `git checkout pi-0.9.4`. Tourne en `ben` + sudo.

set -euo pipefail
TR="→ pi-0.9.4"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"

# ── Préflight : le code patché doit être présent (checkout du tag en amont) ────────────────────
grep -q 'BBRHCJB' "$REPO/src/pi/store/db.py" || fail "registres BBR absents de HISTO_LABELS (checkout pi-0.9.4 incomplet ?)"

# ── Restart du service qui sert /live + /chart (lecture des libellés) ───────────────────────────
if systemctl list-unit-files ben-local-api.service >/dev/null 2>&1; then
    log "restart ben-local-api (HISTO_LABELS Tempo → coloration HC/HP courbe + badge + /live.tariff_label)"
    sudo systemctl restart ben-local-api.service || fail "restart ben-local-api"
    sleep 2
    systemctl is-active ben-local-api.service >/dev/null || fail "ben-local-api inactif après restart"
else
    log "ben-local-api absent → rien à redémarrer (code en place, effet au prochain démarrage)"
fi

log "✓ HISTO_LABELS Tempo/BBR (index 5-10) · coloration HC/HP courbe + badge + libellé /live"
log "✓ update OK"
