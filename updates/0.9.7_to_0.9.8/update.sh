#!/usr/bin/env bash
# update.sh — → pi-0.9.8   : bandes HC/HP coloriées en mode STANDARD.
#
# BUG. `_band_kind()` classe un libellé tarifaire en hc / hp / base — c'est lui qui donne
# leur couleur aux bandes de la courbe ET au badge live. Il ne reconnaissait que les
# libellés HISTORIQUES, en toutes lettres : « Heures Creuses Jours Bleus » → « creus »,
# « Heures Pleines Jours Rouges » → « plein ».
#
# Or en STANDARD, `LTARF` est ABRÉGÉ : « HP  BLEU », « HC  BLANC », « HP  ROUGE ». Aucun
# des deux mots recherchés → tout retombait sur `base` → **courbe entièrement grise**, et
# pas de badge HC/HP. Le commentaire au-dessus de HISTO_LABELS énonçait pourtant la règle
# (« le mot Creuses/Pleines pilote _band_kind ») : exacte en historique, fausse en standard.
#
# ⚠️ Ça ne touchait PAS que Tempo : tout contrat HC/HP en mode standard était concerné.
# Révélé par le passage en Tempo de ben-0001 le 13/08.
#
# FIX : on accepte aussi le préfixe « HC »/« HP » du LTARF.
#
# RÉTROACTIF SANS BACKFILL : les bandes ne sont pas stockées, elles sont recalculées à la
# lecture depuis `curve_rollup` (qui porte index_id). Tout l'historique se colore donc dès
# le redémarrage de l'API — rien à réécrire en base.
#
# Aurait dû partir dans 0.9.7 ; défaut trouvé après publication du tag, et un tag publié ne
# se réécrit jamais. Détail : ../../CHANGELOG.md (0.9.8).
#
# (2) BORNES DE CONTRAT (`contract_epoch`). En STANDARD, `index_id` vaut NTARF — une POSITION
#     dans le calendrier du contrat, pas un sens absolu. Sur ben-0001 la nuit du 12 au 13/08 :
#     23:59:46 index_id=1 = « BASE » ; 00:00:07 trame d'identité, CONTRAT='TEMPO' ; 00:01:11
#     le MÊME index_id=1 désigne les heures creuses bleues. L'index ne saute même pas
#     (15 409 379 → 15 409 381) : rien ne trahit la rupture dans les valeurs.
#     Sans borne, dès la capture du LTARF « HC BLEU », TOUTES les bandes d'index_id=1 auraient
#     été étiquetées heures creuses — 2,7 M de points de l'ère BASE compris, toute la courbe
#     passée en indigo du jour au lendemain, sans signal.
#     → table `contract_epoch`, écrite par record_ngtf() au moment du constat ; bornes
#     existantes reconstituées depuis les événements `changement_offre`. Table SÉPARÉE des
#     événements : un événement est un message (masquable, supprimable), une borne est un fait.
#     Aucune colonne sur `measurements` : le contrat est une PÉRIODE, pas une propriété du point.
#
# (3) REPLI DE LIBELLÉ RESTREINT. `resolve_label` retombait sur « le libellé le plus récent tous
#     contrats confondus » → un index_id devenu heures creuses s'affichait encore « BASE ». Ne
#     vaut plus que si le contrat est INCONNU (NGTF pas encore capté au démarrage), cas visé à
#     l'origine. Mieux vaut aucun libellé qu'un libellé d'une autre offre.
#
# Pur code (`store/db.py`) : aucune dépendance, migration ADDITIVE (une table), pas d'unit.
# UNIVERSEL (LoRa ET filaire) → pas de gate capability.
# Code déjà sur disque après `git checkout pi-0.9.8`. Tourne en `ben` + sudo.

set -euo pipefail
TR="→ pi-0.9.8"
log()  { echo "[update $TR] $*"; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"

# ── Préflight : le code patché doit être présent (checkout du tag en amont) ────────────────────
grep -q 'startswith("hc")' "$REPO/src/pi/store/db.py" \
    || fail "_band_kind non corrigé (checkout pi-0.9.8 incomplet ?)"
grep -q 'contract_epoch' "$REPO/src/pi/store/db.py" || fail "bornes de contrat absentes"
grep -q 'def contract_of' "$REPO/src/pi/store/db.py" || fail "résolution en mémoire absente"

# ── LE LECTEUR D'ABORD : lui seul peut créer la table et la remplir ────────────────────────────
# `contract_epoch` est créée par `db.connect()` EN ÉCRITURE, et c'est la même ouverture qui
# reconstitue les bornes depuis les événements. L'API locale ouvre en LECTURE SEULE : elle ne
# peut ni créer la table ni l'amorcer. Sans ce redémarrage, les bornes n'existeraient qu'au
# prochain reboot du boîtier. (Même piège qu'en 0.9.6 — l'ordre compte.)
restarted=0
for svc in ben-telemetry ben-tic-reader; do
    if systemctl is-active "$svc.service" >/dev/null 2>&1; then
        log "restart $svc (crée contract_epoch + reconstitue les bornes depuis les événements)"
        sudo systemctl restart "$svc.service" || fail "restart $svc"
        restarted=1
    fi
done
[ "$restarted" = 1 ] || log "aucun lecteur actif — bornes créées au prochain démarrage"
sleep 5

# ── Puis l'API, qui calcule les bandes (tariff_bands, au rendu de /chart) ──────────────────────
if systemctl list-unit-files ben-local-api.service >/dev/null 2>&1; then
    log "restart ben-local-api (bandes HC/HP de la courbe + badge en mode standard)"
    sudo systemctl restart ben-local-api.service || fail "restart ben-local-api"
    sleep 2
    systemctl is-active ben-local-api.service >/dev/null || fail "ben-local-api inactif après restart"
else
    log "ben-local-api absent → rien à redémarrer (code en place, effet au prochain démarrage)"
fi

log "✓ bandes HC/HP coloriées en STANDARD (LTARF abrégé désormais reconnu)"
log "✓ rétroactif : tout l'historique se colore, aucun backfill"
log "✓ bornes de contrat posées — un registre est désormais lu sous SON époque"
log "✓ update OK"
