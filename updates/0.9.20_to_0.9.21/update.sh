#!/usr/bin/env bash
# update.sh — 0.9.20 → pi-0.9.21 : ÉRADIQUER LES FANTÔMES DÉJÀ ENTRÉS (issue #7).
#
# ═══ CE QUE LIVRE CETTE VERSION ═══════════════════════════════════════════════════════════════
#
#   0.9.19 a fermé la porte : plus aucun PDL ne naît d'un ADCO difforme. Cette version-ci
#   répare ce qui était DÉJÀ entré — et c'est une MIGRATION DE DONNÉES, pas une livraison de
#   code. Aucun `.py` de service n'est modifié.
#
#   ⭐ UNE SEULE CAUSE POUR DEUX SYMPTÔMES : un caractère de la trame TIC dont le BIT 6 s'est
#      mis à 1 (`0`→`p`, `1`→`q`, `.`→`n`). 0x40 vaut 64, et le checksum TIC est
#      `(somme & 0x3F) + 0x20` — AVEUGLE à tout multiple de 64. `HC..` et `HCn.` ont le même
#      checksum, 0x47. Relevé sur un boîtier du parc : trois ADCO fantômes et quatre époques
#      tarifaires abîmées. Le contrôle de parité de 0.9.18 ferme cette porte-là (un seul bit
#      retourné rend la parité fausse) ; ici on nettoie derrière.
#
# ═══ 🚨 CE QUI REND CETTE UPDATE DIFFÉRENTE DE TOUTES LES PRÉCÉDENTES ═════════════════════════
#
#   Les autres livraient du code : le risque était « le service ne repart pas », visible tout
#   de suite. Ici le risque est « on a supprimé la mauvaise ligne » — IRRÉVERSIBLE, sur des
#   bases qui NE SONT PAS SAUVEGARDÉES. D'où :
#
#   ① SAUVEGARDE OBLIGATOIRE des lignes visées dans /var/tmp AVANT d'écrire, et refus d'agir
#      si elle n'a pas pu être écrite. C'est la seule trace qui survivra à notre aveuglement —
#      le boîtier concerné est INJOIGNABLE (fenêtres de connectivité courtes, pas de SSH).
#   ② MARCHE À BLANC d'abord, journalisée. Le journal est la seule chose qu'on relira.
#   ③ ⭐ RIEN À FAIRE ⇒ ON NE TOUCHE À AUCUN SERVICE. Six boîtiers du parc sont déjà propres ;
#      leur redémarrer un lecteur coûterait des mesures pour rien (leçon de 0.9.17).
#
# ═══ 🚨 LE CONTRÔLE D'EFFET EST UN INVARIANT, PAS UN COMPTE ═══════════════════════════════════
#
#   Le succès n'est PAS « j'ai supprimé quelque chose » : sur un boîtier sain il n'y a rien à
#   faire, et c'est un succès. Exiger un effet ferait échouer l'update partout ailleurs,
#   `device.json` ne serait pas bumpé, et elle REJOUERAIT toutes les 10 min — la mécanique
#   exacte qui a brûlé pi-0.9.12. On vérifie donc « la base est conforme », vrai AVANT comme
#   APRÈS sur un boîtier propre.
#
# AUCUNE migration de schéma, aucune table, aucune colonne.

set -euo pipefail
TR="→ pi-0.9.21"
log()  { echo "[update $TR] $*"; }
warn() { echo "[update $TR] ⚠ $*" >&2; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"
STORE="$REPO/src/pi/store"
MENAGE="$STORE/menage_fantomes.py"

# ═══ PRÉFLIGHT ════════════════════════════════════════════════════════════════════════════════
[ -f "$MENAGE" ] || fail "absent du dépôt : $MENAGE (checkout pi-0.9.21 incomplet ?)"

# ⚠️ `ast.parse`, JAMAIS `py_compile` : celui-ci écrit dans __pycache__, qui appartient à root
#    sur les boîtiers du parc alors que l'OTA tourne en `ben`.
python3 - "$MENAGE" <<'PYEOF' || fail "menage_fantomes.py ne compile pas"
import ast, pathlib, sys
p = pathlib.Path(sys.argv[1]); ast.parse(p.read_text(encoding="utf-8"), filename=p.name)
PYEOF

# 🚨 ON ÉPROUVE LA RÈGLE SUR LA CIBLE, AVEC SES TÉMOINS. Une règle qui supprimerait TOUT
#    passerait sans cela tous les cas de suppression — et effacerait le passage en Tempo d'un
#    boîtier du parc, donc tout calcul de coût antérieur. C'est une base EN MÉMOIRE : elle ne
#    touche pas à celle du boîtier.
python3 - "$STORE" <<'PYEOF' || fail "la règle de ménage ne se comporte pas comme attendu"
import sys
sys.path[:0] = [sys.argv[1]]
import db, menage_fantomes as m
ko = []
c = db.connect(":memory:")
c.execute("INSERT INTO pdl VALUES(0,'061961403012',1,9)")
c.execute("INSERT INTO pdl VALUES(1,'061961403p12',1,1)")          # bit 6 sur un ADCO
for ts, ngtf in ((0, "HC.."), (10, "HCn."), (12, "HC..")):          # bit 6 sur un contrat
    c.execute("INSERT INTO contract_epoch VALUES(0,?,?)", (ts, ngtf))
c.commit()
if m.pdls_fantomes(c) != [1]:                    ko.append("l'ADCO a bit 6 n'est pas vu")
if len(m.epoques_bidon(c, 0)) != 2:              ko.append("les epoques abimees ne sont pas vues")
m.menage(c, a_blanc=False)
if [tuple(r) for r in c.execute("SELECT ts_start,ngtf FROM contract_epoch")] != [(0, "HC..")]:
    ko.append("le menage ne laisse pas la seule epoque juste")
if m.conforme(c):                                ko.append("l'invariant reste faux apres menage")

t = db.connect(":memory:")                       # ⚖️ LE TÉMOIN : une VRAIE bascule survit
t.execute("INSERT INTO pdl VALUES(0,'031864467282',1,9)")
for ts, ngtf in ((0, "BASE"), (500, "TEMPO")):
    t.execute("INSERT INTO contract_epoch VALUES(0,?,?)", (ts, ngtf))
t.commit()
m.menage(t, a_blanc=False)
if [tuple(r) for r in t.execute("SELECT ts_start,ngtf FROM contract_epoch")] != [(0, "BASE"), (500, "TEMPO")]:
    ko.append("TEMOIN : une vraie bascule de contrat a ete effacee")
if ko:
    print("\n".join("  " + k for k in ko), file=sys.stderr); sys.exit(1)
PYEOF
log "préflight OK (compile, règle éprouvée avec son témoin)"

# ═══ MARCHE À BLANC — c'est elle qu'on relira ═════════════════════════════════════════════════
log "── marche à blanc ──"
python3 "$MENAGE" --a-blanc 2>&1 | sed "s/^/[update $TR]   /" || fail "la marche à blanc a échoué"

# 🚨 LA PORTE DE SORTIE INTERROGE L'INVARIANT, PAS UN COMPTE DE FANTÔMES. Compter seulement
#    les fantômes et les époques laisserait passer « base déjà conforme ✓ » sur un boîtier
#    portant une ORPHELINE sans rapport — un succès affiché sans que rien n'ait été vérifié.
A_FAIRE=$(python3 - "$STORE" <<'PYEOF'
import sqlite3, sys
sys.path[:0] = [sys.argv[1]]
import db, menage_fantomes as m
with sqlite3.connect(f"file:{db.DB_PATH}?mode=ro", uri=True) as c:
    c.row_factory = sqlite3.Row
    print(len(m.conforme(c)))
PYEOF
) || fail "base illisible — on ne touche à rien"

# ⭐ RIEN À FAIRE ⇒ AUCUN SERVICE TOUCHÉ. C'est le cas de la plupart du parc.
if [ "$A_FAIRE" = "0" ]; then
    log "✓ base déjà conforme — aucun service touché"
    log "✓ update OK"
    exit 0
fi
log "$A_FAIRE anomalie(s) à traiter"

# ═══ SAUVEGARDE — rien ne se supprime avant validation ════════════════════════════════════════
SAUV="/var/tmp/ben-menage-fantomes-$(date -u +%Y%m%dT%H%M%SZ).sql"
python3 - "$STORE" "$SAUV" <<'PYEOF' || fail "sauvegarde impossible — ON N'ÉCRIT RIEN"
import sqlite3, sys
sys.path[:0] = [sys.argv[1]]
import db, menage_fantomes as m
with sqlite3.connect(f"file:{db.DB_PATH}?mode=ro", uri=True) as c:
    c.row_factory = sqlite3.Row
    n = m.sauvegarde(c, sys.argv[2])
print(n)
PYEOF
[ -s "$SAUV" ] || fail "sauvegarde vide : $SAUV — ON N'ÉCRIT RIEN"
log "sauvegarde : $SAUV ($(wc -l < "$SAUV") lignes) — À CONSERVER jusqu'à validation"

# ═══ QUELS ÉCRIVAINS ARRÊTER ? ════════════════════════════════════════════════════════════════
#
# ⭐ On interroge la MÊME source de vérité que le boot : `capabilities.py`. Pas
#    `device.json.model`, qui porte un LABEL commercial et dont se fier a été supprimé en 0.9.12.
# 🚨 ben-radio n'est JAMAIS touchée : elle n'importe même pas db.py, et son redémarrage porte
#    le risque SPI/taint payé en 0.9.12. On se sert de `capabilities has` pour DÉCIDER, on
#    nomme les unités à la main.
CAPS="$REPO/src/pi/capabilities.py"
[ -f "$CAPS" ] || fail "capabilities.py absent — on ne touche à AUCUN service dans le doute"

ECRIVAINS=""
python3 "$CAPS" has tic-uart          >/dev/null 2>&1 && ECRIVAINS="$ECRIVAINS ben-tic-reader.service"
python3 "$CAPS" has lora-tic-receiver >/dev/null 2>&1 && ECRIVAINS="$ECRIVAINS ben-telemetry.service"

# 🚨 ET LE PUBLISHER, qui n'écrit pourtant aucune mesure. Il lit un lot de `sent=0`, le
#    POSTE, puis marque `sent=1` PAR ROWID. Un lot parti sous le pdl_index FANTÔME juste
#    avant le ménage verrait ses lignes déplacées sous le vrai PDL, puis marquées envoyées
#    par des rowid qui n'ont pas bougé — le cloud ne les aurait JAMAIS reçues sous le bon
#    compteur, et plus rien ne les lui enverrait. Fenêtre étroite, perte définitive.
#    ⓘ Il est sans état : le redémarrer ne coûte rien, il reprend au premier point non envoyé.
systemctl list-unit-files ben-publisher.service >/dev/null 2>&1 \
    && ECRIVAINS="$ECRIVAINS ben-publisher.service"
ECRIVAINS="${ECRIVAINS# }"

# ⚠️ On n'arrête QUE ce qui tourne, et on ne redémarrera QUE ça (leçon de 0.9.17 : `restart`
#    sur un service arrêté le DÉMARRE, et un lecteur filaire lancé sur un boîtier radio perd
#    la course au GPIO de la LED contre ben-radio — 2 538 plantages en neuf heures).
TOURNAIENT=""
for U in $ECRIVAINS; do
    systemctl is-active --quiet "$U" && TOURNAIENT="$TOURNAIENT $U"
done
TOURNAIENT="${TOURNAIENT# }"
log "écrivains en cours : ${TOURNAIENT:-aucun}   (ben-radio VOLONTAIREMENT exclue)"

# 🚨 LE GESTE QUI DÉCIDE : ben-telemetry garde `_pdl_par_emetteur` EN RAM. Sans arrêt, il
#    continuerait d'écrire sous le fantôme quelle que soit la requête SQL — constaté sur un
#    boîtier du parc le 29/09.
for U in $TOURNAIENT; do sudo systemctl stop "$U"; done
[ -n "$TOURNAIENT" ] && log "écrivains arrêtés"

# ═══ LE MÉNAGE ════════════════════════════════════════════════════════════════════════════════
RC=0
python3 "$MENAGE" 2>&1 | sed "s/^/[update $TR]   /" || RC=$?

# On redémarre AVANT de juger : même si le ménage a échoué, un boîtier sans lecteur est pire
# que tout. L'échec sera rapporté juste après.
for U in $TOURNAIENT; do sudo systemctl start "$U"; done
[ -n "$TOURNAIENT" ] && log "écrivains redémarrés"

# ⓘ « base intacte » n'est pas une formule : `menage()` ne fait son `commit()` qu'à la toute
#   fin, et la connexion annule tout sur exception. Un échec ici n'a donc rien écrit.
[ "$RC" = "0" ] || fail "le ménage a PLANTÉ (code $RC) — rien n'a été commité, sauvegarde en $SAUV"

# ═══ CONTRÔLE D'EFFET : L'INVARIANT ═══════════════════════════════════════════════════════════
# 🚨 ON RAPPORTE, ON N'ÉCHOUE PAS. Une anomalie que ce ménage-ci ne sait pas réparer est un
#    état de la DONNÉE : la base n'est ni pire qu'avant, ni urgente, et la sauvegarde existe.
#    Échouer laisserait `device.json` non bumpé, donc l'update REJOUERAIT toutes les 10 min —
#    et ce serait DÉFINITIF, puisque aucune version ultérieure ne pourrait plus atteindre ce
#    boîtier. Seuls le préflight (code cassé) et « un service arrêté n'est pas revenu »
#    (dégât réel) ont le droit de faire échouer cette update.
if python3 - "$STORE" <<'PYEOF'
import sqlite3, sys
sys.path[:0] = [sys.argv[1]]
import db, menage_fantomes as m
with sqlite3.connect(f"file:{db.DB_PATH}?mode=ro", uri=True) as c:
    c.row_factory = sqlite3.Row
    ko = m.conforme(c)
if ko:
    print("\n".join("  " + k for k in ko), file=sys.stderr); sys.exit(1)
PYEOF
then
    log "invariant vérifié : aucun PDL difforme, aucune époque abîmée, aucune orpheline"
else
    warn "invariant ENCORE FAUX après ménage — sauvegarde en $SAUV, à instruire À LA MAIN"
    warn "(on ne fait PAS échouer l'update : elle rejouerait toutes les 10 min pour toujours)"
fi

# ⭐ Et les écrivains sont revenus dans l'état où ils étaient — ni plus, ni moins.
for U in $TOURNAIENT; do
    systemctl is-active --quiet "$U" || fail "$U ne redémarre plus — « journalctl -u $U -n 40 »"
done

# Le schéma se crée à l'ouverture en ÉCRITURE ; l'API locale ouvre en LECTURE SEULE. On la
# contrôle sur /health (jamais /info, qui n'existe pas — c'est ce 404 qui a brûlé pi-0.9.12),
# qui prouve en plus que la base reste lisible.
if systemctl is-active --quiet ben-local-api.service; then
    curl -fsS --max-time 10 http://127.0.0.1:8087/health 2>/dev/null | grep -q '"db":[[:space:]]*true' \
        || warn "/health ne confirme pas db:true — à regarder"
fi

log "✓ update OK (sauvegarde conservée : $SAUV)"
