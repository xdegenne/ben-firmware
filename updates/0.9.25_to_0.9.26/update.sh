#!/usr/bin/env bash
# update.sh — 0.9.25 → pi-0.9.26 : UN OCTET HORS ALPHABET TIC CONDAMNE SON GROUPE.
#
# ═══ CE QUE LIVRE CETTE VERSION ═══════════════════════════════════════════════════════════════
#
#   `Enedis-NOI-CPT_54E` §6.2.1.2 : le champ « donnée » ne contient que des caractères ASCII
#   IMPRIMABLES, 0x20 à 0x7E — plus `HT (0x09)`, séparateur de champ du mode standard (§5.3.6).
#   L'alphabet légal est donc CONNU D'AVANCE, et tout octet hors de cet ensemble est une erreur
#   PAR DÉFINITION DE LA NORME, pas une heuristique. `read_frame` ne s'en servait pas (issue #10).
#
#   🚨 LE DÉFAUT EST UNE SUBSTITUTION, PAS UNE AMPUTATION. L'octet n'était pas jeté, il était
#      AJOUTÉ : le groupe gardait sa longueur, un de ses caractères était remplacé. Or c'est là
#      que le checksum est aveugle — il vaut (somme & 0x3F) + 0x20, donc modulo 64. Remplacer un
#      caractère par (c - 0x40) retire exactement 64, laisse le checksum IDENTIQUE, et donne un
#      caractère de contrôle :
#
#          'T' = 0x54 -> 0x14      'S' = 0x53 -> 0x13      'I' = 0x49 -> HT
#
#      En historique « PTEC TH.. » devenait « PTEC \x14H.. », ACCEPTÉ — et PTEC donne l'index_id.
#
#   ⚠️ IL FAUT DEUX BITS, PAS UN. Un seul bit retourné casse TOUJOURS la parité, le compteur
#      ayant calculé le bit de parité sur l'octet d'origine. Ce qui atteint ce contrôle est un
#      nombre PAIR de bits dans le même octet — typiquement le bit 6 de la donnée ET le bit de
#      parité. C'est donc une coïncidence RARE qu'on ferme, le même arbitrage que celui déjà
#      écrit dans `tic_parite.octet_valide`.
#
#   ⇒ Trois filtres indépendants, chacun couvrant l'angle mort des deux autres.
#
#   Le relevé gagne `alphabet` (octets) et `groupes_alphabet` (groupes), et `_cause_rejets` nomme
#   la cause — fondre les causes dirait « ça décroche » sans dire OÙ.
#
# ═══ 🚨 CE QUE CE SCRIPT NE TOUCHE PAS ════════════════════════════════════════════════════════
#
#   Cette version ne modifie QUE le lecteur FILAIRE (`tic-reader/`). La chaîne LoRa ne passe pas
#   par `read_frame` : ni `ben-telemetry` ni `ben-radio` ne sont concernées, et `ben-radio` moins
#   que tout — elle est le seul maître du RFM95 et du SPI, et le verrou taint de 0.9.12 est né
#   d'un redémarrage de trop. On ne paie pas ce risque pour un changement qui ne la concerne pas.
#
#   ⇒ Une seule unité possible : `ben-tic-reader.service`, et seulement si le boîtier déclare
#     `tic-uart`. On se sert de `capabilities has` pour DÉCIDER, et on nomme l'unité À LA MAIN.
#
# AUCUNE migration, AUCUNE table, AUCUNE colonne. Le schéma est inchangé.

set -euo pipefail
TR="→ pi-0.9.26"
log()  { echo "[update $TR] $*"; }
warn() { echo "[update $TR] ⚠ $*" >&2; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"
API="http://127.0.0.1:8087/health"
TIC="$REPO/src/pi/tic-reader"

# ═══ PRÉFLIGHT ════════════════════════════════════════════════════════════════════════════════
for f in "$TIC/main_uart.py" "$TIC/tic_parite.py"; do
    [ -f "$f" ] || fail "absent du dépôt : $f (checkout pi-0.9.26 incomplet ?)"
done

# ⚠️ `ast.parse`, JAMAIS `py_compile` : celui-ci écrit dans __pycache__, qui appartient à root
#    sur les boîtiers du parc alors que l'OTA tourne en `ben`.
python3 - "$TIC/main_uart.py" "$TIC/tic_parite.py" <<'PYEOF' || fail "un fichier livré ne compile pas"
import ast, pathlib, sys
for a in sys.argv[1:]:
    p = pathlib.Path(a); ast.parse(p.read_text(encoding="utf-8"), filename=p.name)
PYEOF

# 🚨 LE CORRECTIF EST-IL BRANCHÉ ? Un contrôle livré mais jamais appelé serait une update qui
#    ne change RIEN, en silence — et elle passerait tous les contrôles ci-dessous.
python3 - "$TIC/main_uart.py" <<'PYEOF' || fail "le contrôle d'alphabet n'est pas branché dans read_frame"
import pathlib, sys
src = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
ko = []
if "ALPHABET_HISTO" not in src or "ALPHABET_STD" not in src:
    ko.append("les tables ne sont pas importées depuis tic_parite")
if "alphabet[b]" not in src:
    ko.append("read_frame ne consulte aucune table d'alphabet")
if "alphabet=ALPHABET_HISTO" not in src or "alphabet=ALPHABET_STD" not in src:
    ko.append("MODES n'injecte pas les tables par mode")
if "groupes_alphabet" not in src:
    ko.append("le relevé ne porte pas la cause alphabet")
if ko:
    print("\n".join("  " + k for k in ko), file=sys.stderr); sys.exit(1)
PYEOF

# 🚨 LE CONTRÔLE QUI COMPTE : un garde FAUX brûle une version aussi sûrement qu'un vrai défaut.
#    On ÉPROUVE les tables sur la cible, AVEC LEUR TÉMOIN — sans le témoin, une table qui
#    refuserait TOUT passerait tous les cas de refus ci-dessous, et le boîtier cesserait
#    d'enregistrer la moindre mesure en silence, journaux calmes et service « active ».
python3 - "$TIC" <<'PYEOF' || fail "les tables d'alphabet ne se comportent pas comme attendu"
import sys
sys.path.insert(0, sys.argv[1])
import tic_parite as t
ko = []

# ① la forme : indexées par l'octet DÉJÀ masqué à 7 bits
if len(t.ALPHABET_HISTO) != 128 or len(t.ALPHABET_STD) != 128:
    ko.append("les tables ne font pas 128 entrées — read_frame indexe l'octet masqué")

# ② les refus exigés par l'issue #10
for octet, nom in ((0x00, "NUL"), (0x04, "EOT"), (0x1F, "US"), (0x7F, "DEL"), (0x80, "0x80")):
    if t.ALPHABET_HISTO[octet & 0x7F] or t.ALPHABET_STD[octet & 0x7F]:
        ko.append(f"{nom} ({octet:#04x}) est accepté")

# ③ HT : légal en standard, interdit en historique. Les DEUX sens comptent — l'accepter en
#    historique rouvrirait le trou ('I' - HT = 64 tout rond, donc checksum inchangé), le
#    refuser en standard condamnerait TOUS les groupes et rendrait le lecteur muet.
if not t.ALPHABET_STD[0x09]:
    ko.append("HT refusé en standard — tous les groupes seraient condamnés, lecteur MUET")
if t.ALPHABET_HISTO[0x09]:
    ko.append("HT accepté en historique — le trou du modulo 64 est rouvert")

# ④ ⚖️ LE TÉMOIN, sans lequel rien de ce qui précède ne prouve quoi que ce soit
for octet in range(0x20, 0x7F):
    if not t.ALPHABET_HISTO[octet] or not t.ALPHABET_STD[octet]:
        ko.append(f"{octet:#04x} ({chr(octet)!r}) REFUSÉ alors qu'il est imprimable")
        break

# ⑤ table et prédicat ne doivent pas pouvoir dire deux choses différentes
for i in range(128):
    if bool(t.ALPHABET_HISTO[i]) != t.octet_dans_alphabet(i) \
       or bool(t.ALPHABET_STD[i]) != t.octet_dans_alphabet_std(i):
        ko.append(f"table et prédicat divergent en {i:#04x}")
        break

if ko:
    print("\n".join("  " + k for k in ko), file=sys.stderr); sys.exit(1)
PYEOF
log "préflight OK (fichiers présents, compilent, contrôle branché, tables éprouvées avec leur témoin)"

# ═══ QUELLE UNITÉ CE BOÎTIER DOIT-IL REDÉMARRER ? ═════════════════════════════════════════════
#
# ⭐ On interroge la MÊME source de vérité que le boot : `capabilities.py`, qui lit le
#    `device.json`. Pas `device.json.model`, qui porte un LABEL commercial (« Radio »,
#    « Filaire ») et dont se fier a déjà été supprimé en 0.9.12.
CAPS="$REPO/src/pi/capabilities.py"
[ -f "$CAPS" ] || fail "capabilities.py absent — on ne touche à AUCUN service dans le doute"

if ! python3 "$CAPS" has tic-uart >/dev/null 2>&1; then
    log "ce boîtier ne déclare pas tic-uart — code livré, aucun service à redémarrer"
    log "✓ update OK"
    exit 0
fi
UNITE="ben-tic-reader.service"
log "unité concernée : $UNITE   (ben-telemetry et ben-radio VOLONTAIREMENT exclues)"

# ═══ ÉTAT AVANT, pour que le contrôle d'effet ait un point de comparaison ═════════════════════
LISAIT=0; TS_AVANT=0
if systemctl is-active --quiet ben-local-api.service; then
    AVANT="$(python3 - "$API" <<'PYEOF' || true
import json, sys, urllib.request
try:
    d = json.loads(urllib.request.urlopen(sys.argv[1], timeout=15).read())
except Exception:
    sys.exit(0)
ts, now = d.get("last_tic_ts") or 0, d.get("now") or 0
print(f"{ts} {1 if ts and (now - ts) < 300 else 0}")
PYEOF
)"
    [ -n "${AVANT:-}" ] && { TS_AVANT=${AVANT%% *}; LISAIT=${AVANT##* }; }
fi
# ⚠️ Si le boîtier NE LISAIT PAS avant, on n'exige pas de lecture après : un câble TIC débranché
#    ferait échouer l'update, donc la ferait REJOUER toutes les 10 min pour toujours. C'est la
#    mécanique exacte qui a brûlé pi-0.9.12.
[ "$LISAIT" = "1" ] \
    && log "avant : le boîtier lisait (last_tic_ts=$TS_AVANT) — une lecture sera EXIGÉE après" \
    || warn "avant : aucune lecture récente — la lecture ne sera pas exigée (câble ? compteur ?)"

# ═══ REDÉMARRAGE ══════════════════════════════════════════════════════════════════════════════
# 🚨 `is-active --quiet` NE SUFFIT PAS : il rend non-zéro pour `activating`, donc il rate
#    précisément un service en boucle de plantage. On lit l'ÉTAT COMPLET.
ETAT=$(systemctl is-active "$UNITE" 2>/dev/null || true)
case "$ETAT" in
    active|activating|reloading|deactivating) TOURNE=oui ;;
    *)                                        TOURNE=non ;;
esac
log "$UNITE : état systemd « $ETAT » → tourne : $TOURNE"

# ⭐ ON NE DÉMARRE PAS ce qui ne tournait pas. Ce qui doit tourner est une décision de
#    `check_network` à partir des capabilities ; une update livre du code, elle ne décide pas de
#    la composition du boîtier. (Leçon de 0.9.17, qui a lancé un lecteur filaire sur des boîtiers
#    radio et lui a fait perdre 2 538 fois la course au GPIO de la LED.)
if [ "$TOURNE" != "oui" ]; then
    warn "$UNITE ne tourne pas — on ne la démarre PAS ici, le code neuf est en place"
    log "✓ update OK"
    exit 0
fi

sudo systemctl restart "$UNITE"
sleep 8
systemctl is-active --quiet "$UNITE" \
    || fail "$UNITE ne démarre plus — voir « journalctl -u ben-tic-reader -n 40 »"
log "✓ $UNITE debout"

# ═══ CONTRÔLE D'EFFET ═════════════════════════════════════════════════════════════════════════
#
# 🚨 Sur /health, JAMAIS /info — cette route n'existe pas, et l'avoir interrogée a brûlé
#    pi-0.9.12. /health prouve EN PLUS que la base est lisible (`db: true`), là où un simple
#    code 200 masquerait la panne.
#
# ⭐ Et c'est le contrôle qui attrape le pire cas de CETTE version : un contrôle d'alphabet trop
#    sévère condamnerait tous les groupes, laisserait le service « active » et les journaux
#    calmes, et n'enregistrerait plus une seule mesure. Le préflight le teste sur les tables,
#    celui-ci le vérifie sur de VRAIES trames du compteur.
#
# ⓘ 150 s d'attente : le mode se redétecte au démarrage (sondage des deux débits, ~10 s observé
#   sur un boîtier historique), puis l'écriture est groupée — un last_tic_ts peut mettre une
#   quinzaine de secondes à bouger même quand tout va bien.
if [ "$LISAIT" = "1" ]; then
    python3 - "$API" "$TS_AVANT" <<'PYEOF' || fail "le boîtier ne lit plus après le redémarrage"
import json, sys, time, urllib.request
api, avant = sys.argv[1], int(sys.argv[2])
fin, dernier = time.time() + 150, None
while time.time() < fin:
    try:
        d = json.loads(urllib.request.urlopen(api, timeout=10).read())
    except Exception as e:
        dernier = f"/health injoignable : {e}"; time.sleep(5); continue
    if not d.get("db"):
        print(f"  /health répond mais db=false : {d}", file=sys.stderr); sys.exit(1)
    ts = d.get("last_tic_ts") or 0
    if ts > avant:
        print(f"  mesure enregistrée après le redémarrage (last_tic_ts {avant} → {ts})"); sys.exit(0)
    dernier = f"last_tic_ts toujours à {ts}, inchangé depuis {avant}"
    time.sleep(5)
print(f"  {dernier}", file=sys.stderr); sys.exit(1)
PYEOF
    log "✓ le boîtier enregistre toujours, et la base est lisible"
else
    warn "lecture non exigée — contrôle d'effet limité au démarrage de l'unité"
fi

log "✓ update OK"
