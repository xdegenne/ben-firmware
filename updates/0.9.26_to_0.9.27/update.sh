#!/usr/bin/env bash
# update.sh — 0.9.26 → pi-0.9.27 : LE BOÎTIER PUBLIE SOUS LA RÉFÉRENCE QUE LE CLOUD LUI REND.
#
# ═══ CE QUE LIVRE CETTE VERSION ═══════════════════════════════════════════════════════════════
#
#   Bascule de la CLÉ D'ARCHIVE des mesures (chantier ben-docs#5, sous-tâche #32). Le boîtier
#   cesse d'envoyer son `pdl_index` — un entier LOCAL, qui ne veut rien dire ailleurs — et envoie
#   la référence OPAQUE que le cloud lui a rendue à la déclaration.
#
#       POST …/hello          RARE, événementiel
#          ↑ sw · fw · model · pdl: [ {index, adco} ]
#          ↓ 200  refs: [ {pdl, ref} ]        (ou {pdl, motif} si l'ADS n'est pas conforme)
#          déclenché : ① à l'init du boîtier  ② tant qu'un pdl est SANS ref  ③ APRÈS une OTA
#          🚨 PAS au démarrage du publisher — un service qui redémarre n'est pas un événement.
#
#       POST …/ping           FRÉQUENT — c'est l'ancien /hello renommé
#          ↑ instantané de santé · contract_epoch · tariff_labels · meter_profile, clavés par ref
#          ↓ 204, rien
#
#       POST …/measurements
#          ↑ { lots: [ {ref, points: [...]} ] }     groupé par compteur, SANS pdl
#
#   Côté boîtier : `pdl.ref` (colonne ajoutée, migration idempotente) + `refs_connues`,
#   `pdls_sans_ref`, `poser_refs` dans `store/db.py` ; `fetch_batch` rend `(rowids, lots)`
#   groupés par `ref` dans `publisher/ben_publisher.py`.
#
# ═══ ⚠️ LE PARC EST MUET ENTRE LA BASCULE DE L'API ET CETTE OTA ═══════════════════════════════
#
#   L'API refuse l'ancien format. Donc, AVANT cette update, un boîtier qui ne publie pas n'est
#   PAS une anomalie — c'est l'état attendu, et il devient une anomalie APRÈS. Rien n'est perdu :
#   la rétention locale est de 180 jours et `sent = 1` n'est posé, par `rowid`, qu'après un 2xx.
#
#   ⇒ Ce script RELÈVE le retard de l'outbox avant d'agir et l'écrit au journal, pour que le
#     « après » ait un point de comparaison. Il n'EXIGE rien de la publication : il ne peut pas
#     (voir le séquencement ci-dessous), et un contrôle qu'on ne peut pas tenir est un contrôle
#     qui brûle une version — c'est exactement ce qui est arrivé à pi-0.9.12 avec `/info`.
#
# ═══ 🚨 LE SÉQUENCEMENT RÉEL DE L'AGENT, ET CE QU'IL PERMET ═══════════════════════════════════
#
#   Lu dans `src/pi/updater/check_update.py`, et il n'est pas celui qu'on aimerait :
#
#       …
#       ⑥ git checkout <tag>                   le code neuf est sur le disque
#       ⑧ run_update_script(update.sh)         ← NOUS SOMMES ICI
#       ⑨ device.json.softwareVersion = 0.9.27 ← LE BUMP, APRÈS NOUS
#       ⑩ systemctl restart ben-publisher      ← APRÈS LE BUMP, et c'est l'agent qui le fait
#
#   ⇒ Ce script NE PEUT PAS déclencher la déclaration lui-même : il tourne AVANT le bump, donc
#     le cloud apprendrait l'ANCIENNE version. On ne prétend pas résoudre ce que le séquencement
#     ne permet pas.
#
#   ⇒ La forme qui marche est un DRAPEAU que le publisher consomme à son tour :
#
#       /var/lib/ben-firmware/declaration-requise.json
#       { "motif": "ota", "version_attendue": "0.9.27", "pose_ts": …, "pose_par": … }
#
#     🚨 LE CONTRAT, et le `version_attendue` n'est pas décoratif : le publisher ne consomme le
#        drapeau que si `device.json.softwareVersion` LUI EST ÉGAL, et ne l'EFFACE qu'après un
#        2xx. Sans cette garde il resterait une fenêtre de quelques secondes — entre notre pose
#        (⑧) et le bump (⑨) — pendant laquelle un tick du publisher déclarerait 0.9.26.
#
#     ⓘ Pour CETTE transition la fenêtre est fermée par construction : le publisher vivant
#       exécute encore le code 0.9.26, qui ignore tout du drapeau. La garde est donc inutile
#       aujourd'hui et porteuse dès 0.9.28 — c'est précisément pour ça qu'on l'écrit maintenant.
#
#   ⭐ ET LE DRAPEAU EST UNE CEINTURE, PAS LE MÉCANISME : après cette update AUCUN pdl n'a de
#      `ref` (la colonne vient de naître, tout est NULL), donc le déclencheur ② — « tant qu'un
#      pdl est SANS ref », une CONDITION re-vérifiée à chaque tour — suffit à lui seul. Un
#      déclencheur raté est définitif, une condition se re-vérifie. On pose quand même le
#      drapeau, parce que c'est la version où il naît et parce qu'il sera, lui, le seul
#      mécanisme des OTA suivantes, quand les refs seront déjà connues.
#
# ═══ 🚨 LA SEULE CHOSE QUI PEUT VRAIMENT CASSER ICI : LA COLONNE ABSENTE ══════════════════════
#
#   `SELECT_BATCH` joint `pdl` et lit `p.ref`. Or le publisher ouvre la base EN ÉCRITURE MAIS
#   SANS REJOUER LE SCHÉMA (`open_db`, délibérément) : il ne peut donc PAS créer la colonne.
#   Et `ben-local-api` ouvre en LECTURE SEULE : encore moins.
#
#   ⇒ Si personne n'avait ouvert la base avec `db.connect()` après le checkout, `fetch_batch`
#     lèverait `OperationalError: no such column: p.ref` à chaque tour, et le boîtier cesserait
#     de publier en silence — service « active », journaux presque calmes.
#
#   ⇒ On ne PARIE donc pas sur un redémarrage de lecteur pour appliquer la migration : CE SCRIPT
#     L'APPLIQUE LUI-MÊME, en ouvrant la base en écriture, et il VÉRIFIE la colonne ensuite.
#     Déterministe, et ça couvre le boîtier dont le lecteur ne tourne pas (câble TIC débranché,
#     émetteur muet) — qui est justement celui qu'un pari aurait laissé muet pour toujours.
#
#   ⓘ Ouvrir en écriture rejoue TOUTES les migrations idempotentes de `db.py`. Sur le parc elles
#     sont déjà appliquées (`user_version = 1`, `tariff_labels` déjà segmenté par `ngtf`) : la
#     seule qui MORDE ici est `ALTER TABLE pdl ADD COLUMN ref TEXT`.
#
# ═══ CE QUE CE SCRIPT NE REDÉMARRE PAS, ET POURQUOI ═══════════════════════════════════════════
#
#   🚨 PAS ben-publisher. Pas par oubli : l'agent le redémarre lui-même à l'étape ⑩, APRÈS le
#      bump. Le redémarrer ici le relancerait AVANT, donc sur un `device.json` encore à 0.9.26 —
#      on ferait de nos propres mains le défaut que ce chantier cherche à éviter.
#
#   🚨 PAS les LECTEURS — ni ben-tic-reader, ni ben-telemetry. Le changement de `db.py` est
#      strictement ADDITIF (une colonne, trois fonctions), aucun lecteur n'appelle ces fonctions,
#      et les 4 accès à `pdl` du dépôt nomment tous leurs colonnes (`INSERT INTO pdl(pdl_index,
#      adco, first_seen, last_seen)`, pas de `SELECT *`) : un lecteur qui continue de tourner sur
#      l'ancien `db.py` au-dessus de la base migrée se comporte à l'identique. Un redémarrage de
#      lecteur COÛTE des mesures ; on ne le paie pas pour rien (leçons de 0.9.17 et 0.9.21).
#
#   🚨 PAS ben-radio, moins que tout. `capabilities.py` mappe `lora-tic-receiver` sur DEUX
#      services (ben-radio ET ben-telemetry) : passer par le helper générique
#      `capabilities.py restart lora-tic-receiver` redémarrerait la façade radio, seule maîtresse
#      du RFM95 et du SPI, dont le verrou taint de 0.9.12 est né d'un redémarrage de trop. Elle
#      n'importe même pas `db.py`. On se sert de `capabilities has` pour DÉCIDER, et on nomme les
#      unités À LA MAIN.
#
#   ✓ SEULE unité redémarrée : `ben-local-api`, et seulement si elle tournait. Son redémarrage ne
#     coûte ni mesure, ni GPIO, ni radio, et c'est le dernier consommateur de `db.py` du boîtier :
#     la laisser sur un `db.py` plus vieux que le schéma de sa base est la configuration dont
#     `_meta()` porte déjà la cicatrice. ⚠️ APRÈS la migration, jamais avant : elle ouvre en
#     lecture seule et ne peut rien créer.
#
# MIGRATION : OUI — une colonne, AJOUT SEUL. L'ancien code tourne sur la nouvelle base, donc le
#             retour arrière reste possible sans restaurer quoi que ce soit.

set -euo pipefail
TR="→ pi-0.9.27"
log()  { echo "[update $TR] $*"; }
warn() { echo "[update $TR] ⚠ $*" >&2; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"
SRC="$REPO/src/pi"
STORE="$SRC/store"
PUB="$SRC/publisher"
API="http://127.0.0.1:8087/health"
DRAPEAU="/var/lib/ben-firmware/declaration-requise.json"

# ═══ PRÉFLIGHT ① — les fichiers livrés sont là et compilent ═══════════════════════════════════
for f in "$STORE/db.py" "$PUB/ben_publisher.py"; do
    [ -f "$f" ] || fail "absent du dépôt : $f (checkout pi-0.9.27 incomplet ?)"
done

# ⚠️ `ast.parse`, JAMAIS `py_compile` : celui-ci écrit dans __pycache__, qui appartient à root
#    sur les boîtiers du parc alors que l'OTA tourne en `ben`.
python3 - "$STORE/db.py" "$PUB/ben_publisher.py" <<'PYEOF' || fail "un fichier livré ne compile pas"
import ast, pathlib, sys
for a in sys.argv[1:]:
    p = pathlib.Path(a); ast.parse(p.read_text(encoding="utf-8"), filename=p.name)
PYEOF

# ═══ PRÉFLIGHT ② — 🚨 LA MIGRATION ET LES FONCTIONS, ÉPROUVÉES AVEC LEUR TÉMOIN ═══════════════
#
#   C'est LE contrôle de cette version, et il se tient sur une base JETABLE de /var/tmp, jamais
#   sur celle du boîtier.
#
#   ⚠️ POURQUOI PAS `db.connect(":memory:")`, comme en 0.9.21 : une base neuve naît avec `ref`
#      déjà dans son `CREATE TABLE`. Elle ne prouverait donc RIEN du chemin qui nous intéresse —
#      l'`ALTER TABLE` sur une table qui EXISTE DÉJÀ SANS la colonne, qui est exactement l'état
#      des 7 boîtiers du parc. On fabrique donc une base à l'ANCIENNE FORME, puis on l'ouvre.
#
#   ⚖️ ET SURTOUT LE TÉMOIN POSITIF. Sans lui, un `refs_connues` qui rendrait TOUJOURS VIDE et un
#      `fetch_batch` qui ne rendrait JAMAIS de lot passeraient tous les contrôles de refus
#      ci-dessous — et le boîtier cesserait de publier EN SILENCE, ce qui est précisément le mode
#      de défaillance que ce chantier introduit. C'est la leçon du préflight de 0.9.19 (un
#      `adco_valide` qui refuse tout refuse bien tous les mauvais ADCO) et de 0.9.26 (une table
#      d'alphabet vide refuse bien tous les octets illégaux).
python3 - "$STORE" "$PUB" "$SRC" <<'PYEOF' || fail "la migration ou les fonctions de ref ne se comportent pas comme attendu"
import os, shutil, sqlite3, sys, tempfile
store, pub, src = sys.argv[1], sys.argv[2], sys.argv[3]
# `ben_publisher` insère lui-même src/pi dans sys.path à l'import ; on met les deux, l'ordre
# important étant que `publisher/` précède pour que `ben_publisher` et `health` soient trouvés.
sys.path[:0] = [pub, src]
import ben_publisher as bp
from store import db

ko = []
# ⚠️ /var/tmp et pas /tmp : /tmp peut être un tmpfs étroit sur un Pi Zero, et la base de test
#    n'a aucune raison de disputer de la RAM au lecteur.
tmpdir = tempfile.mkdtemp(prefix="ben-0.9.27-preflight-", dir="/var/tmp")
base = os.path.join(tmpdir, "ancienne.db")
try:
    # ── ① On fabrique la base À L'ANCIENNE FORME : `pdl` SANS `ref`, telle qu'elle est sur
    #    les 7 boîtiers. C'est le seul état qui éprouve l'ALTER.
    v = sqlite3.connect(base)
    v.execute("CREATE TABLE pdl (pdl_index INTEGER PRIMARY KEY, adco TEXT NOT NULL UNIQUE, "
              "first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL)")
    v.execute("INSERT INTO pdl(pdl_index, adco, first_seen, last_seen) "
              "VALUES(0,'061947000000',1,2)")
    v.execute("INSERT INTO pdl(pdl_index, adco, first_seen, last_seen) "
              "VALUES(1,'031864467282',1,2)")
    v.commit(); v.close()
    if "ref" in [r[1] for r in sqlite3.connect(base).execute("PRAGMA table_info(pdl)")]:
        ko.append("la base de test naît DÉJÀ avec `ref` — le préflight ne prouverait rien")

    # ── ② L'ouverture en ÉCRITURE doit AJOUTER la colonne. C'est toute la migration.
    c = db.connect(base)
    if "ref" not in [r[1] for r in c.execute("PRAGMA table_info(pdl)")]:
        ko.append("db.connect() en écriture n'ajoute PAS la colonne pdl.ref")
        print("\n".join("  " + k for k in ko), file=sys.stderr); sys.exit(1)

    # ── ③ IDEMPOTENCE : deuxième ouverture, aucune erreur, colonne toujours là, données intactes.
    c.close()
    c = db.connect(base)
    if [r[0] for r in c.execute("SELECT pdl_index FROM pdl ORDER BY pdl_index")] != [0, 1]:
        ko.append("la seconde ouverture a perdu des lignes de `pdl`")

    # ── ④ L'ÉTAT DE DÉPART, qui est le déclencheur ② : tout le monde est sans ref.
    if db.pdls_sans_ref(c) != [0, 1]:
        ko.append(f"pdls_sans_ref ne voit pas les 2 compteurs neufs : {db.pdls_sans_ref(c)}")
    if db.refs_connues(c) != {}:
        ko.append("refs_connues rend quelque chose sur une base qui n'a aucune ref")

    # ── ⑤ ⚖️ LE TÉMOIN POSITIF : poser puis relire doit rendre LA VALEUR POSÉE.
    if db.poser_refs(c, {0: "cpt-7f3a9e"}) != 1:
        ko.append("poser_refs ne pose pas la ref d'un compteur sans ref")
    if db.refs_connues(c) != {0: "cpt-7f3a9e"}:
        ko.append(f"TÉMOIN : refs_connues ne rend pas la ref posée → {db.refs_connues(c)}")
    if db.pdls_sans_ref(c) != [1]:
        ko.append("pdls_sans_ref continue de réclamer un compteur qui a sa ref")

    # ── ⑥ IDEMPOTENT ET NON DESTRUCTIF. Un refus du cloud (motif au lieu de ref) ne doit PAS
    #    effacer une ref valide : sinon un refus transitoire ferait cesser de publier un
    #    compteur parfaitement légitime, et c'est irrattrapable sans intervention.
    if db.poser_refs(c, {0: "cpt-7f3a9e"}) != 0:
        ko.append("re-poser la MÊME ref n'est pas un no-op (UPDATE inutile à chaque tour)")
    db.poser_refs(c, {0: None, 1: ""})
    if db.refs_connues(c) != {0: "cpt-7f3a9e"}:
        ko.append("une entrée SANS ref a effacé une ref déjà connue")

    # ── ⑦ 🚨 LA JOINTURE EST LE FILTRE, ET C'EST L'INVARIANT QU'ON NE POURRAIT PAS RATTRAPER.
    #    Un `rowid` de compteur SANS ref qui entrerait dans la liste rendue serait passé à
    #    `sent = 1` après le 2xx du lot des AUTRES compteurs : mesure perdue POUR TOUJOURS,
    #    sans trace. On l'éprouve sur de vraies lignes.
    for rid, (pdl, ts) in enumerate(((0, 100), (0, 160), (1, 120), (1, 180)), start=1):
        c.execute("INSERT INTO measurements(ts, pdl_index, papp, sent) VALUES(?,?,?,0)",
                  (ts, pdl, 1000 + rid))
    c.commit()
    rowids, lots = bp.fetch_batch(c, 100)
    if sorted(rowids) != [1, 2]:
        ko.append(f"fetch_batch rend des rowid de compteur SANS ref (ou en oublie) : {rowids}")
    if len(lots) != 1 or lots[0].get("ref") != "cpt-7f3a9e" or len(lots[0]["points"]) != 2:
        ko.append(f"le lot du compteur à ref n'est pas celui attendu : {lots}")
    # 🚨 `pdl_index` NE SORT PAS d'ici — c'est littéralement l'énoncé du chantier.
    for lot in lots:
        if "pdl" in lot or "pdl_index" in lot:
            ko.append("un lot porte encore un pdl_index")
        for p in lot["points"]:
            if "pdl" in p or "pdl_index" in p:
                ko.append("un point porte encore un pdl_index")
                break

    # ── ⑧ ⚖️ LE SECOND TÉMOIN : la ref arrive, les points retenus REPARTENT. Sans lui, un
    #    `fetch_batch` qui ne rendrait jamais rien passerait ⑦ les doigts dans le nez.
    db.poser_refs(c, {1: "cpt-b21c04"})
    rowids, lots = bp.fetch_batch(c, 100)
    if sorted(rowids) != [1, 2, 3, 4]:
        ko.append(f"TÉMOIN : les points du 2e compteur ne repartent pas après sa ref : {rowids}")
    if sorted(l["ref"] for l in lots) != ["cpt-7f3a9e", "cpt-b21c04"]:
        ko.append(f"TÉMOIN : les lots ne sont pas groupés par compteur : {lots}")
    c.close()
finally:
    shutil.rmtree(tmpdir, ignore_errors=True)

if ko:
    print("\n".join("  " + k for k in ko), file=sys.stderr); sys.exit(1)
PYEOF
log "préflight OK (ALTER éprouvé sur une base à l'ANCIENNE forme, refs et lots avec leurs 2 témoins)"

# ═══ LA MIGRATION SUR LA CIBLE, ET L'ÉTAT D'AVANT ═════════════════════════════════════════════
#
# ⚠️ C'est un ALTER TABLE sur une base qu'un lecteur écrit en continu. `db.connect()` a un
#    `timeout=5` ; un lecteur qui groupe ses écritures peut tenir le verrou plus longtemps. On
#    réessaie, et on ÉCHOUE FRANCHEMENT si la base reste inaccessible : l'update sera rejouée au
#    tick suivant (device.json non bumpé), ce qui est exactement le bon comportement — bien
#    mieux qu'improviser.
#
# ⓘ Le retard de l'outbox est encadré PAR LES ROWID, jamais par `count(*) WHERE sent = 0` : celui
#   -là balaie `idx_meas_sent` et prend 37 SECONDES sur un Pi Zero (mesuré le 2026-09-19). C'est
#   la forme exacte de `pending_approx()`.
ETAT_AVANT="$(python3 - "$SRC" <<'PYEOF'
import sqlite3, sys, time
sys.path[:0] = [sys.argv[1]]
from store import db

derniere = None
conn = None
for essai in range(6):
    try:
        conn = db.connect()          # ÉCRITURE → rejoue les migrations idempotentes
        break
    except sqlite3.Error as e:
        derniere = e
        time.sleep(5)
if conn is None:
    print(f"base inouvrable en écriture après 6 essais sur 30 s : {derniere}", file=sys.stderr)
    sys.exit(1)

if "ref" not in [r[1] for r in conn.execute("PRAGMA table_info(pdl)")]:
    print("  pdl.ref ABSENTE après l'ouverture en écriture — le publisher ne pourrait pas "
          "lire p.ref et cesserait de publier en silence", file=sys.stderr)
    sys.exit(1)

refs = db.refs_connues(conn)
sans = db.pdls_sans_ref(conn)
row = conn.execute("SELECT (SELECT max(rowid) FROM measurements), "
                   "       (SELECT min(rowid) FROM measurements WHERE sent = 0)").fetchone()
retard = 0 if not row or row[0] is None or row[1] is None else max(0, row[0] - row[1] + 1)
conn.close()
print(f"{len(refs)} {len(sans)} {retard}")
PYEOF
)" || fail "la migration de schéma n'a pas pu être appliquée ni vérifiée sur la cible"
REFS=${ETAT_AVANT%% *}; RESTE=${ETAT_AVANT#* }; SANS=${RESTE%% *}; RETARD=${RESTE##* }
log "✓ pdl.ref présente sur la base du boîtier — $REFS compteur(s) déjà référencé(s), $SANS sans ref"
# ⚠️ Un retard énorme AVANT l'update est NORMAL et attendu : l'API refuse l'ancien format depuis
#    sa bascule. Ce nombre est là pour être COMPARÉ après, pas pour décider quoi que ce soit.
log "retard de l'outbox AVANT : ~$RETARD point(s) non envoyé(s) — normal, le parc est muet"
[ "$SANS" = "0" ] && [ "$REFS" = "0" ] \
    && warn "aucun compteur du tout dans la table pdl — boîtier qui n'a jamais lu de trame ?" || true

# ═══ COMPOSITION DU BOÎTIER — on INTERROGE, et la décision est de NE PAS TOUCHER ══════════════
#
# ⭐ Même source de vérité que le boot : `capabilities.py`, qui lit `device.json`. Jamais
#    `device.json.model`, qui porte un LABEL commercial (« Radio », « Filaire ») et dont se fier
#    a déjà été supprimé en 0.9.12.
#
#    Ici la capability ne sert pas à choisir QUI redémarrer — on ne redémarre aucun lecteur, voir
#    l'en-tête — mais à deux décisions bien réelles : écrire au journal quel lecteur est
#    DÉLIBÉRÉMENT laissé en marche (l'audit se relit dans six mois), et savoir si une lecture a
#    le droit d'être EXIGÉE au contrôle d'effet.
CAPS="$SRC/capabilities.py"
[ -f "$CAPS" ] || fail "capabilities.py absent — on ne touche à AUCUN service dans le doute"

LIT=0
if python3 "$CAPS" has tic-uart >/dev/null 2>&1; then
    LIT=1; log "capability tic-uart → ben-tic-reader tourne et RESTE EN MARCHE (change rien pour elle)"
fi
if python3 "$CAPS" has lora-tic-receiver >/dev/null 2>&1; then
    LIT=1; log "capability lora-tic-receiver → ben-telemetry RESTE EN MARCHE, et ben-radio n'est même pas regardée"
fi
[ "$LIT" = "1" ] || warn "aucune capability de lecture déclarée — aucune lecture ne sera exigée"

# ═══ ÉTAT AVANT côté /health, pour que le contrôle d'effet ait un point de comparaison ════════
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
    # ⚠️ `|| true` OBLIGATOIRE, et il manque dans 0.9.19 comme dans 0.9.26 : sous `set -e`, un
    #    `[ … ] && { … }` qui est la DERNIÈRE commande d'un bloc `then` et qui rend non-zéro
    #    abat le script. Ça mord quand ben-local-api est « active » mais /health injoignable —
    #    l'update échouerait AVANT même d'avoir rien tenté, et sans message explicatif.
    [ -n "${AVANT:-}" ] && { TS_AVANT=${AVANT%% *}; LISAIT=${AVANT##* }; } || true
fi
# ⚠️ Si le boîtier NE LISAIT PAS avant, on n'exige pas de lecture après : un câble TIC débranché
#    ferait échouer l'update, donc la ferait REJOUER toutes les 10 min pour toujours. C'est la
#    mécanique exacte qui a brûlé pi-0.9.12.
[ "$LISAIT" = "1" ] && [ "$LIT" = "1" ] \
    && log "avant : le boîtier lisait (last_tic_ts=$TS_AVANT) — une lecture sera EXIGÉE après" \
    || { LISAIT=0; warn "avant : aucune lecture récente — la lecture ne sera pas exigée (câble ? émetteur ?)"; }

# ═══ REDÉMARRAGE — ben-local-api SEULE, et APRÈS la migration ═════════════════════════════════
#
# ⚠️ L'ordre n'est pas négociable : l'API ouvre la base en LECTURE SEULE et ne peut rien créer.
#    La migration est déjà faite ci-dessus, donc elle démarrera sur un schéma complet.
UNITE="ben-local-api.service"

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
if [ "$TOURNE" = "oui" ]; then
    sudo systemctl restart "$UNITE"
    sleep 8
    systemctl is-active --quiet "$UNITE" \
        || fail "$UNITE ne démarre plus — voir « journalctl -u ben-local-api -n 40 »"
    log "✓ $UNITE debout"
else
    warn "$UNITE ne tourne pas — on ne la démarre PAS ici, le code neuf est en place"
fi

# ═══ CONTRÔLE D'EFFET ═════════════════════════════════════════════════════════════════════════
#
# 🚨 Sur /health, JAMAIS /info — cette route n'existe pas, et l'avoir interrogée a brûlé
#    pi-0.9.12. /health prouve EN PLUS que la base est LISIBLE (`db: true`), là où un simple code
#    200 masquerait la panne.
#
# ⭐ Et c'est le contrôle qui attrape le pire cas de CETTE version, qui n'est pas du code mais un
#    ALTER TABLE exécuté SOUS un lecteur en marche : si la base était restée verrouillée, abîmée
#    ou illisible, `db: true` tomberait et `last_tic_ts` se figerait, service « active » et
#    journaux calmes. C'est la seule chose que cette update fait au boîtier avant que l'agent ne
#    redémarre le publisher — donc c'est elle, et elle seule, qu'il faut prouver inoffensive.
#
# ⓘ 150 s : l'écriture du lecteur est groupée, un `last_tic_ts` met une quinzaine de secondes à
#   bouger même quand tout va bien.
if [ "$LISAIT" = "1" ]; then
    python3 - "$API" "$TS_AVANT" <<'PYEOF' || fail "le boîtier ne lit plus / la base n'est plus lisible"
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
        print(f"  mesure enregistrée après la migration (last_tic_ts {avant} → {ts})"); sys.exit(0)
    dernier = f"last_tic_ts toujours à {ts}, inchangé depuis {avant}"
    time.sleep(5)
print(f"  {dernier}", file=sys.stderr); sys.exit(1)
PYEOF
    log "✓ le boîtier enregistre toujours, et la base est lisible après l'ALTER"
elif [ "$TOURNE" = "oui" ]; then
    # Pas de lecture à exiger, mais /health doit au moins répondre avec `db: true` : c'est
    # gratuit, et ça prouve que l'ALTER n'a pas rendu la base illisible.
    python3 - "$API" <<'PYEOF' || fail "/health ne confirme pas que la base est lisible"
import json, sys, time, urllib.request
fin = time.time() + 60
dernier = "jamais joint"
while time.time() < fin:
    try:
        d = json.loads(urllib.request.urlopen(sys.argv[1], timeout=10).read())
    except Exception as e:
        dernier = f"/health injoignable : {e}"; time.sleep(5); continue
    if d.get("db"):
        print("  /health répond, db=true"); sys.exit(0)
    dernier = f"db=false : {d}"
    time.sleep(5)
print(f"  {dernier}", file=sys.stderr); sys.exit(1)
PYEOF
    log "✓ base lisible (db=true) — lecture non exigée sur ce boîtier"
else
    warn "ben-local-api à l'arrêt — contrôle d'effet impossible, la migration reste VÉRIFIÉE plus haut"
fi

# ═══ LE DRAPEAU « IL FAUT SE REDÉCLARER » ═════════════════════════════════════════════════════
#
# 🚨 Posé EN DERNIER, et c'est volontaire : une update qui échoue ne laisse rien derrière elle.
#    ⓘ Le poser plus tôt serait d'ailleurs sans danger — `version_attendue` interdit au publisher
#      de le consommer tant que `device.json` n'est pas bumpé, donc tant que l'update n'a pas
#      réussi. La garde rend l'ordre indifférent ; on choisit le plus propre à relire.
#
# ⚠️ Posé INCONDITIONNELLEMENT, même si `ben-publisher` est à l'arrêt ou absent : c'est un état
#    sur le disque, pas un signal à quelqu'un d'écoutant. Il attendra le prochain publisher.
#
# ⚠️ Écrit en `ben` dans /var/lib/ben-firmware, qui lui appartient (c'est déjà là que vivent la
#    base, update.lock, lora-state.json). Écriture ATOMIQUE (tmp + os.replace) : un publisher qui
#    lirait au mauvais moment ne doit jamais tomber sur un JSON tronqué.
#
# 🚨 ET UN ÉCHEC ICI N'EST QU'UN `warn`, JAMAIS UN `fail`. Faire échouer l'update ferait rejouer
#    le script toutes les 10 minutes POUR TOUJOURS (device.json non bumpé) — la mécanique exacte
#    qui a brûlé pi-0.9.12 — et pour une ceinture dont la bretelle tient : le déclencheur « un
#    pdl sans ref » déclarera ce boîtier de toute façon. On ne brûle pas une version pour un
#    fichier d'appoint.
SI_PAS_DE_DRAPEAU="drapeau non posé — la déclaration partira par « un pdl sans ref », voie normale ici"
python3 - "$DRAPEAU" <<'PYEOF' || { warn "$SI_PAS_DE_DRAPEAU"; DRAPEAU=""; }
import json, os, pathlib, sys, time
p = pathlib.Path(sys.argv[1])
p.parent.mkdir(parents=True, exist_ok=True)
t = p.with_suffix(".json.tmp")
with open(t, "w", encoding="utf-8") as f:
    json.dump({"motif": "ota",
               "version_attendue": "0.9.27",
               "pose_ts": int(time.time()),
               "pose_par": "updates/0.9.26_to_0.9.27/update.sh"}, f)
    f.flush(); os.fsync(f.fileno())
os.replace(t, p)
PYEOF
if [ -n "$DRAPEAU" ]; then
    log "✓ drapeau posé : $DRAPEAU (version_attendue=0.9.27)"
    log "  ⓘ le publisher ne le consommera qu'une fois device.json bumpé par l'agent, et ne"
    log "    l'effacera qu'après un 2xx. Et de toute façon AUCUN pdl n'a de ref : le déclencheur"
    log "    « un pdl sans ref » suffirait à lui seul pour cette transition."
fi

# ═══ CE QU'IL RESTE À REGARDER, ET QUE CE SCRIPT NE PEUT PAS VÉRIFIER ═════════════════════════
#
# L'agent va bumper device.json PUIS redémarrer ben-publisher — donc après notre sortie. La
# déclaration, le retour des refs et la reprise des envois ne sont observables qu'ensuite.
log "── à vérifier APRÈS cette update (hors de portée de ce script) ──"
log "   journalctl -u ben-publisher -n 60 --no-pager   → déclaration, refs reçues, lots partis"
log "   retard de l'outbox AVANT = ~$RETARD point(s) : il doit DÉCROÎTRE. Un boîtier qui ne"
log "   publiait pas n'était PAS une anomalie avant cette update ; après, ça en est une."

log "✓ update OK"
