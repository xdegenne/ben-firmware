#!/usr/bin/env bash
# update.sh — → pi-0.9.7   : couleur Tempo en champ explicite + événements sans action.
#
# (1) COULEUR TEMPO RÉSOLUE PAR LE BOÎTIER. `/live` et `/registers` portent désormais
#     `tempo_color` = bleu / blanc / rouge (absent hors Tempo). Sans ce champ, l'app
#     devait chercher « rouge »/« blanc »/« bleu » dans un libellé FRANÇAIS, et connaître
#     les DEUX conventions : historique « Heures Pleines Jours Rouges », standard
#     « HP  ROUGE ». C'est au serveur de résoudre, comme il le fait déjà pour `tariff_label`.
#
#     ⚠️ AUCUNE colonne ajoutée, aucune migration : la couleur est une INTERPRÉTATION
#     d'`index_id`, pas une donnée. En historique elle est déterministe (5-6 bleu,
#     7-8 blanc, 9-10 rouge) ; en standard elle est lue une fois par registre dans le
#     `LTARF` déjà conservé par `tariff_labels`. La stocker sur `measurements`
#     dupliquerait des millions de fois ce que la colonne voisine porte déjà — et le
#     rollup, keyé sur `index_id`, ventile DÉJÀ la conso par couleur depuis toujours.
#
#     Sur `/registers`, la couleur rend les index exploitables : « heures pleines —
#     340 kWh » ne veut rien dire en Tempo, où trois registres HP coexistent du simple
#     au quintuple.
#
# (2) ÉVÉNEMENTS SANS ACTION. `record_ngtf()` n'émet plus de bouton. Deux destinations
#     essayées puis écartées : « Voir les tarifs » → /dashboard (qui ne montre ni tarif
#     ni contrat) puis « Voir ma formule » → /settings (l'événement DIT DÉJÀ « passé de
#     X à Y »). Surtout, une route est une notion de l'APP : le firmware n'a pas à
#     connaître sa navigation, et l'action étant figée à la naissance de l'événement,
#     réorganiser les écrans casserait les anciennes lignes. Le champ reste au contrat
#     pour un futur backend. Détail : ../../CHANGELOG.md (0.9.7).
#
# Pur code (`store/db.py`, `store/local_api.py`) : aucune dépendance, aucune migration,
# aucun changement d'unit. UNIVERSEL (LoRa ET filaire) → pas de gate capability.
# Code déjà sur disque après `git checkout pi-0.9.7`. Tourne en `ben` + sudo.

set -euo pipefail
TR="→ pi-0.9.7"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"

# ── Préflight : le code patché doit être présent (checkout du tag en amont) ────────────────────
grep -q 'def resolve_tempo_color' "$REPO/src/pi/store/db.py" \
    || fail "resolve_tempo_color absent (checkout pi-0.9.7 incomplet ?)"
grep -q 'TEMPO_COLOR_HISTO' "$REPO/src/pi/store/db.py" || fail "table couleurs historique absente"
grep -q 'tempo_color' "$REPO/src/pi/store/local_api.py" || fail "/live non patché"

# ── Seule l'API sert ces champs ; les lecteurs ne sont pas concernés ───────────────────────────
# `resolve_tempo_color` n'est appelée que par local_api (au rendu). Redémarrer les lecteurs
# coûterait un trou de mesure pour rien.
if systemctl list-unit-files ben-local-api.service >/dev/null 2>&1; then
    log "restart ben-local-api (/live.tempo_color, /registers[].tempo_color)"
    sudo systemctl restart ben-local-api.service || fail "restart ben-local-api"
    sleep 2
    systemctl is-active ben-local-api.service >/dev/null || fail "ben-local-api inactif après restart"
else
    log "ben-local-api absent → rien à redémarrer (code en place, effet au prochain démarrage)"
fi

log "✓ tempo_color sur /live et /registers (bleu/blanc/rouge, résolu côté boîtier)"
log "✓ événements sans action (la navigation appartient à l'app)"
log "✓ update OK"
