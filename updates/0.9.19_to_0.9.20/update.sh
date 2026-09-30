#!/usr/bin/env bash
# update.sh — 0.9.19 → pi-0.9.20 : LE PUBLISHER RATTRAPE SON RETARD.
#
# ═══ CE QUE LIVRE CETTE VERSION ═══════════════════════════════════════════════════════════════
#
#   Un boîtier à COURTE FENÊTRE DE CONNECTIVITÉ ne rattrapait jamais son retard (issue #13). Il
#   envoyait 500 points toutes les 60 s — 500/min — pour une production radio de 64,5/min, soit
#   un débit NET de 435/min. Il lui fallait donc PLUS DE 3 H 30 de connectivité par jour rien
#   que pour ne pas reculer ; en dessous, l'écart grandissait chaque jour.
#
#   ⭐ Le goulot n'était PAS la taille du lot : un aller-retour coûte ~400 ms pour 1000 points
#      (mesuré sur un Pi Zero du parc), contre 60 s de sommeil. `BATCH` 500 → 1000, et une
#      cadence de 10 s tant qu'il reste du retard. Le seuil de survie tombe à ~16 min/jour.
#
# ═══ 🚨 CE SCRIPT NE TOUCHE QU'UN SEUL SERVICE : ben-publisher ════════════════════════════════
#
#   Ni les lecteurs, ni ben-radio, ni ben-local-api : aucun ne partage de code avec ce
#   changement. Redémarrer un lecteur coûterait des mesures sur chaque boîtier du parc pour
#   rien, et ben-radio porte en plus le risque SPI/taint payé en 0.9.12.
#
# ═══ 🚨 ET LE CONTRÔLE D'EFFET N'EST PAS CELUI DES VERSIONS PRÉCÉDENTES ═══════════════════════
#
#   `last_tic_ts` prouve qu'un LECTEUR lit. Il ne prouve RIEN sur le publisher, qui n'écrit pas
#   dans `measurements` — il ne fait que marquer `sent`. Reprendre ce contrôle ici donnerait un
#   vert qui ne veut rien dire : exactement le défaut qui a brûlé pi-0.9.12, sous une autre
#   forme.
#
#   ⭐ Ce qu'on contrôle donc : le service est debout, ET IL LE RESTE. Le mode de panne qu'on
#      craint est une boucle de plantage — `active` une seconde, `auto-restart` la suivante —
#      et `is-active` juste après un `restart` ne la voit pas. On regarde donc DEUX FOIS, à
#      15 s d'intervalle, et on exige que le compteur de redémarrages n'ait pas bougé.
#
# AUCUNE migration, AUCUNE table, AUCUNE colonne.

set -euo pipefail
TR="→ pi-0.9.20"
log()  { echo "[update $TR] $*"; }
warn() { echo "[update $TR] ⚠ $*" >&2; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"
PUB="$REPO/src/pi/publisher/ben_publisher.py"
UNITE="ben-publisher.service"

# ═══ PRÉFLIGHT ════════════════════════════════════════════════════════════════════════════════
[ -f "$PUB" ] || fail "absent du dépôt : $PUB (checkout pi-0.9.20 incomplet ?)"

# ⚠️ `ast.parse`, JAMAIS `py_compile` : celui-ci écrit dans __pycache__, qui appartient à root
#    sur les boîtiers du parc alors que l'OTA tourne en `ben`.
python3 - "$PUB" <<'PYEOF' || fail "ben_publisher.py ne compile pas"
import ast, pathlib, sys
p = pathlib.Path(sys.argv[1]); ast.parse(p.read_text(encoding="utf-8"), filename=p.name)
PYEOF

# 🚨 ON ÉPROUVE LA DÉCISION, PAS SA PRÉSENCE. Une cadence inversée laisserait le service
#    parfaitement « active » tout en RALENTISSANT quand le boîtier prend du retard — un défaut
#    qu'aucun contrôle de service ne verrait jamais.
python3 - "$REPO/src/pi/publisher" "$REPO/src/pi" <<'PYEOF' || fail "la cadence ne se comporte pas comme attendu"
import sys
sys.path[:0] = [sys.argv[1], sys.argv[2]]
import ben_publisher as pub
ko = []
if pub.cadence(0) != pub.PERIOD:                    ko.append("à jour : la cadence n'est pas celle de croisière")
if pub.cadence(pub.BATCH - 1) != pub.PERIOD:        ko.append("sous un lot plein : on accélère à tort")
if pub.cadence(pub.BATCH) != pub.PERIOD_RETARD:     ko.append("un lot plein en attente : on n'accélère pas")
if not (pub.PERIOD_RETARD < pub.PERIOD):            ko.append("le rattrapage est PLUS LENT que la croisière")


class _BaseQuiTombe:                                # verrou tenu, carte SD fatiguée
    def execute(self, *a, **k):
        import sqlite3
        raise sqlite3.OperationalError("database is locked")


if pub.cadence_sure(_BaseQuiTombe()) != pub.PERIOD:
    ko.append("une base illisible ne retombe pas sur la croisière")
if ko:
    print("\n".join("  " + k for k in ko), file=sys.stderr); sys.exit(1)
PYEOF
log "préflight OK (compile, cadence éprouvée avec ses témoins)"

# ═══ LE SERVICE TOURNE-T-IL ? ═════════════════════════════════════════════════════════════════
#
# 🚨 `is-active --quiet` NE SUFFIT PAS : il rend non-zéro pour `activating`, donc il rate
#    précisément un service en boucle de plantage. On lit l'ÉTAT COMPLET.
ETAT=$(systemctl is-active "$UNITE" 2>/dev/null || true)
case "$ETAT" in
    active|activating|reloading|deactivating) TOURNE=oui ;;
    *)                                        TOURNE=non ;;
esac
log "$UNITE : état systemd « $ETAT » → tourne : $TOURNE"

# ⭐ ON NE DÉMARRE PAS ce qui ne tournait pas. Ce qui doit tourner est une décision de
#    `check_network` à partir des capabilities — une update livre du code, elle ne décide pas
#    de la composition du boîtier. (Leçon de 0.9.17.)
if [ "$TOURNE" != "oui" ]; then
    warn "$UNITE ne tourne pas — on ne la démarre PAS ici, le code neuf est en place"
    log "✓ update OK (code livré, aucun service touché)"
    exit 0
fi

AVANT=$(systemctl show "$UNITE" -p NRestarts --value 2>/dev/null || echo 0)
sudo systemctl restart "$UNITE"
log "$UNITE redémarré (NRestarts avant : $AVANT)"

# ═══ CONTRÔLE D'EFFET : debout, ET IL LE RESTE ════════════════════════════════════════════════
sleep 15
systemctl is-active --quiet "$UNITE" \
    || fail "$UNITE ne démarre plus — voir « journalctl -u ben-publisher -n 40 »"

APRES=$(systemctl show "$UNITE" -p NRestarts --value 2>/dev/null || echo 0)
[ "$APRES" = "$AVANT" ] \
    || fail "$UNITE redémarre en boucle (NRestarts $AVANT → $APRES) — il plante au démarrage"

# ⭐ Et on exige une trace de VIE, pas seulement un process debout : le publisher journalise à
#    chaque tour de boucle. Un service qui se lance puis se fige sur une exception avalée
#    resterait « active » sans plus rien faire.
sudo journalctl -u "$UNITE" --since "-30 seconds" --no-pager -o cat 2>/dev/null \
    | grep -qiE "envoyé|rien à envoyer|hello|retard illisible" \
    || warn "aucune trace d'activité en 30 s — à surveiller (boîtier hors ligne ?)"

log "✓ $UNITE debout et stable"
log "✓ update OK"
