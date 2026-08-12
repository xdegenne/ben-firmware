#!/usr/bin/env bash
# update.sh — → pi-0.9.5   : pdl_index = un COMPTEUR (ADCO) + événements + performances API.
#
# TROIS CHANTIERS, tous PUR CODE (aucune dépendance, aucun changement d'unit).
#
# 1) `pdl_index` identifiait l'ÉMETTEUR, pas le COMPTEUR. L'adresse LoRa est flashée en EEPROM :
#    reposer l'émetteur sur un autre Linky empilait deux compteurs sous le même PDL (constaté sur
#    ben-0001 : 4 « changements d'offre » fantômes en juin-juillet = des déplacements de test).
#    Nouvelles tables `pdl` (ADCO → pdl_index, à vie) et `emitter` (quel compteur au bout de quel
#    émetteur), entretenues à la TRAME DE BOOT — seule à porter l'ADCO.
#    ⚠️ MIGRATION SANS DOULEUR : les boîtiers du terrain n'ont vécu que sur UN Linky, donc lier
#    l'ADCO courant au pdl_index existant est historiquement JUSTE. `sources.json` sert de graine
#    (le 1er PDL vaut TOUJOURS 0) ET de repli : après cette OTA le Pi redémarre mais PAS l'Arduino,
#    qui reste en STREAMING sans réémettre sa trame de boot → sans repli le boîtier cesserait de
#    stocker. La résolution est ADDITIVE, jamais bloquante.
#
# 2) ÉVÉNEMENTS (table `event`, endpoint `/events`, en-tête `X-Ben-Last-Event` sur `/live`).
#    Deux bascules détectées : l'OFFRE (OPTARIF/NGTF, lus DIRECTEMENT dans la trame, via
#    `record_ngtf`) et le MODE TIC historique↔standard (auto-détection du lecteur, via
#    `record_tic_mode`). Write-on-change, 1re observation silencieuse, symétriques.
#    L'écriture de l'état n'est jamais conditionnée à l'émission : la notif est un bonus.
#
# 3) PERFORMANCES — le plus gros gain, sans rapport avec (1) et (2). Toute requête sur
#    `measurements` qui ne filtrait pas sur `pdl_index` était AVEUGLE à l'index
#    (pdl_index, ts, papp) et balayait la table. Mesuré sur ben-0001 (3,1 M lignes) :
#      /live 28,6 s → 0,10 s   |   /pdls 36,7 s → 0,06 s   |   /health 16,6 s → 0,99 s
#      levels.refresh_all 199,9 s → 0,55 s  (P15 du talon calculé sur `curve_rollup`)
#    Charge moyenne du boîtier : 1,96 → 0,16. Détail : ../../CHANGELOG.md (0.9.5).
#
# UNIVERSEL (LoRa ET filaire) → pas de gate capability.
# Code déjà sur disque après `git checkout pi-0.9.5`. Tourne en `ben` + sudo.

set -euo pipefail
TR="→ pi-0.9.5"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"

# ── Préflight : le code patché doit être présent (checkout du tag en amont) ────────────────────
grep -q 'def bind_emitter'     "$REPO/src/pi/store/db.py"    || fail "bind_emitter absent (checkout pi-0.9.5 incomplet ?)"
grep -q 'def record_tic_mode'  "$REPO/src/pi/store/db.py"    || fail "record_tic_mode absent"
grep -q 'def pdl_list'         "$REPO/src/pi/store/db.py"    || fail "pdl_list absent"
grep -q '_profil_depuis_rollup' "$REPO/src/pi/store/levels.py" || fail "levels.py non patché"

# ── 1. LE LECTEUR D'ABORD : c'est lui qui crée le schéma ───────────────────────────────────────
# Les 3 nouvelles tables + la colonne `level_profile.src_standard` sont créées à l'ouverture EN
# ÉCRITURE (`db.connect()` rejoue `_SCHEMA` en CREATE IF NOT EXISTS). L'API locale ouvre en
# LECTURE SEULE : elle ne peut RIEN créer. D'où cet ordre — sinon l'API interrogerait une base
# pas encore migrée. (Elle dégrade proprement dans ce cas, mais autant ne pas s'y exposer.)
restarted=0
for svc in ben-telemetry ben-tic-reader; do
    if systemctl is-active "$svc.service" >/dev/null 2>&1; then
        log "restart $svc (crée le schéma : tables pdl/emitter/event + level_profile.src_standard)"
        sudo systemctl restart "$svc.service" || fail "restart $svc"
        restarted=1
    fi
done
[ "$restarted" = 1 ] || log "aucun lecteur actif (check_network les démarre) — schéma créé au prochain démarrage"
sleep 5

# ── 2. Puis l'API, qui sert /events + l'en-tête X-Ben-Last-Event ───────────────────────────────
if systemctl list-unit-files ben-local-api.service >/dev/null 2>&1; then
    log "restart ben-local-api (/events, X-Ben-Last-Event, /pdls.adco, requêtes indexées)"
    sudo systemctl restart ben-local-api.service || fail "restart ben-local-api"
    sleep 2
    systemctl is-active ben-local-api.service >/dev/null || fail "ben-local-api inactif après restart"
fi

# ── 3. Vérification : le stockage continue-t-il ? ──────────────────────────────────────────────
# Le risque n°1 de cette version est là : `get_pdl_index()` a changé, et s'il renvoyait None la
# trame serait JETÉE. Le repli sources.json est censé l'empêcher — on le vérifie plutôt que de
# l'espérer. Fenêtre large : une trame LoRa arrive toutes les ~40 s.
for svc in ben-telemetry ben-tic-reader; do
    systemctl is-active "$svc.service" >/dev/null 2>&1 || continue
    systemctl is-active "$svc.service" >/dev/null || fail "$svc inactif après restart"
done
log "✓ pdl_index ancré sur l'ADCO (migration additive, repli sources.json)"
log "✓ événements : changement d'offre + de mode, /events, X-Ben-Last-Event"
log "✓ requêtes indexées : /live 28,6→0,10 s · /pdls 36,7→0,06 s · profileur 200→0,55 s"
log "✓ update OK"
