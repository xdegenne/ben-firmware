#!/usr/bin/env bash
# update.sh — → pi-0.9.9   : registres bornés au contrat en cours + contrat refusé sur trame tronquée.
#
# (1) `/registers` NE MONTRE QUE LES REGISTRES DU CONTRAT EN COURS.
#     `db.registers()` agrégeait TOUT le rollup sans borne. Un registre d'une offre révolue
#     restait donc affiché à vie. Constaté sur ben-0001 le 13/08 : le registre BASE de l'ère
#     historique — plus rien renvoyé depuis sept semaines — trônait à côté des registres Tempo.
#     En standard `index_id` vaut NTARF, une POSITION dans le calendrier du contrat : le même
#     numéro désigne un registre PHYSIQUE DIFFÉRENT d'une offre à l'autre, donc agréger
#     par-dessus une bascule mélange deux compteurs. On borne au début de l'époque courante
#     (`contract_epoch`, posée en 0.9.8).
#     `ts_start = 0` — boîtier n'ayant jamais vu de changement d'offre, cas de ben-0010 — donne
#     un filtre NEUTRE : tout reste affiché. Vérifié sans régression sur ben-0003 (histo HP/HC)
#     et ben-0010 (histo Tempo), dont les bases antérieures n'ont pas la table.
#
#     La valeur affichée reste BRUTE, celle qui figure sur la facture. Ne pas la ramener à un
#     cumul « depuis le début du contrat » : le Linky ne remet PAS ses registres EASF à zéro en
#     changeant d'offre, donc un registre neuf peut traîner l'énergie d'une offre précédente.
#     C'est la vérité du compteur, pas une anomalie (documenté dans la docstring).
#
# (2) UNE TRAME TIC TRONQUÉE N'ANNONCE PLUS DE CONTRAT.
#     À chaque ré-enregistrement de l'émetteur, ben-0001 recevait `CONTRAT='00'` puis, 7 s plus
#     tard, la vraie valeur. Sans conséquence en 0.9.4 ; depuis la 0.9.8 et `contract_epoch`,
#     chaque redémarrage ouvrirait une époque tarifaire BIDON, émettrait DEUX `changement_offre`
#     (aller puis retour) et — avec le point (1) ci-dessus — VIDERAIT `/registers`, qui se borne
#     désormais à l'époque courante : un registre de nuit comme HC BLEU disparaîtrait pour une
#     journée entière.
#     Cause : `readAndParseTIC()` (émetteur) sort de sa boucle STX→ETX sur TIMEOUT comme sur ETX
#     et rend `kept > 0` dans les deux cas. Une trame coupée après ses premières lignes livre un
#     ADCO juste (ADSC/ADCO est en tête) et un contrat qui ne vaut rien.
#     Côté RÉCEPTEUR (ce qui part ici) : un boot dépourvu d'ISOUSC ET de PREF vient d'un émetteur
#     qui n'a pas lu la TIC — son contrat est refusé. Garde volontairement redondante avec celle
#     de l'émetteur : le récepteur ne doit jamais faire confiance à ce qui arrive par radio, et
#     lui part par OTA quand les émetteurs déjà posés garderont leur firmware des mois.
#
# ⚠️ La correction ÉMETTEUR (tic-reader 0.1.7, `v.complete`) N'EST PAS dans cet OTA : pas d'OTA
#    sur AVR. Elle attend la prochaine fournée de reflash. Les deux gardes sont indépendantes.
#
# Pur code (`store/db.py`, `ben-telemetry/ben_telemetry.py`, `lora-receiver/main.py`) :
# AUCUNE migration, aucune table, aucune colonne — `contract_epoch` existe depuis 0.9.8.
# UNIVERSEL (LoRa ET filaire) → pas de gate capability.
# Code déjà sur disque après `git checkout pi-0.9.9`. Tourne en `ben` + sudo.

set -euo pipefail
TR="→ pi-0.9.9"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"

# ── Préflight : le code patché doit être présent (checkout du tag en amont) ────────────────────
grep -q 'depuis = epochs\[-1\]\[0\] if epochs else 0' "$REPO/src/pi/store/db.py" \
    || fail "registers() non borné au contrat (checkout pi-0.9.9 incomplet ?)"
grep -q 'CONTRAT ignoré' "$REPO/src/pi/ben-telemetry/ben_telemetry.py" \
    || fail "garde contrat absente du récepteur"

# ── LE LECTEUR D'ABORD (convention 0.9.6/0.9.8 : l'ordre compte) ───────────────────────────────
# Ici aucune table n'est à créer, mais c'est le lecteur qui porte la garde (2) : tant qu'il n'a
# pas redémarré, un ré-enregistrement d'émetteur peut encore poser une époque bidon — que l'API,
# elle, prendrait immédiatement en compte via le point (1). On remet donc la source d'abord.
restarted=0
for svc in ben-telemetry ben-tic-reader; do
    if systemctl is-active "$svc.service" >/dev/null 2>&1; then
        log "restart $svc (refus du contrat sur trame de boot incomplète)"
        sudo systemctl restart "$svc.service" || fail "restart $svc"
        restarted=1
    fi
done
[ "$restarted" = 1 ] || log "aucun lecteur actif — garde effective au prochain démarrage"
sleep 5

# ── Puis l'API, qui sert /registers ────────────────────────────────────────────────────────────
if systemctl list-unit-files ben-local-api.service >/dev/null 2>&1; then
    log "restart ben-local-api (/registers borné au contrat en cours)"
    sudo systemctl restart ben-local-api.service || fail "restart ben-local-api"
    sleep 2
    systemctl is-active ben-local-api.service >/dev/null || fail "ben-local-api inactif après restart"
else
    log "ben-local-api absent → rien à redémarrer (code en place, effet au prochain démarrage)"
fi

log "✓ /registers ne montre que les registres du contrat en cours (valeur BRUTE, celle de la facture)"
log "✓ un boot sans ISOUSC ni PREF n'ouvre plus d'époque tarifaire"
log "✓ update OK"
