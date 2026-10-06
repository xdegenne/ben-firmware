#!/usr/bin/env bash
# update.sh — 0.9.28 → pi-0.10.0 : LA VERSION INSTALLÉE ATTEINT ENFIN LE CLOUD.
#
# ═══ LE DÉFAUT QUE CETTE VERSION FERME ════════════════════════════════════════════════════════
#
#   Chantier `ben-docs#15`, sous-tâche #38. Mesuré le 2026-10-04, après la publication de
#   pi-0.9.28 : `devices.sw_version` annonçait **0.9.27 pour les 8 boîtiers** alors que ben-0001
#   et ben-0003 tournaient bel et bien en 0.9.28 (`device.json` bumpé, dépôt sur le tag,
#   « ✓ update OK » au journal) et battaient normalement.
#
#   `devices.sw_version` est la SEULE source de la vue parc — le panneau « Versions firmware » du
#   tableau `ben-parc` et la colonne « version » de son détail. Un tableau de bord qui annonce
#   une version périmée rend le parc IMPOSSIBLE À PILOTER : on ne sait plus qui a pris quoi, ce
#   qui est exactement ce que l'ingestion cloud existe pour dire.
#
#   LA CAUSE, en une ligne : cette colonne n'est écrite que par la route `/hello` — la
#   DÉCLARATION — et par rien d'autre. Le battement, lui, va sur `/ping`, qui écrit `last_seen`
#   et la santé et ne touche JAMAIS la version. Or la déclaration post-OTA reposait sur un
#   DRAPEAU que chaque `update.sh` devait penser à poser. Un seul des 37 scripts du dépôt l'a
#   posé — `0.9.26_to_0.9.27`, celui qui a introduit le mécanisme — et il a été OUBLIÉ dès la
#   transition suivante, une release plus tard.
#
# ═══ CE QUE LIVRE CETTE VERSION — UNE CONDITION, PLUS UN ÉVÉNEMENT ════════════════════════════
#
#   `ben_publisher.py` mémorise LOCALEMENT la version que le cloud a ACCEPTÉE (après un 2xx) et
#   la compare, à chaque tour, à celle de `device.json` qu'il relit déjà. Différentes ⇒ déclarer.
#
#     /var/lib/ben-firmware/version-declaree.json   { "version": "0.10.0", "ts": … }
#
#   ⭐ LE DÉPÔT PORTAIT DÉJÀ L'ARGUMENT, et c'est ce qui rend ce correctif évident après coup.
#      `ben_publisher.py`, sur le déclencheur « un pdl sans ref » : « Réconcilier un état est
#      plus solide que rattraper un événement — un événement raté est définitif, une condition se
#      re-vérifie au tour suivant. » Le drapeau était un événement. Et `check_update.py`, sur le
#      redémarrage du publisher : « Le laisser à la charge de chaque `update.sh` reviendrait à
#      l'oublier un jour. » La déclaration n'avait pas eu droit au même traitement.
#
#   ⭐ MÉMOIRE ABSENTE = JAMAIS DÉCLARÉE, et c'est ce qui recale TOUT LE PARC : aucun boîtier ne
#      porte ce fichier aujourd'hui, donc chacun déclare UNE fois au premier battement après son
#      OTA. Le trou du 04/10 se ferme sans qu'on touche à un boîtier, et sans rien de manuel.
#
#   Ce que la condition couvre, et que le drapeau ne couvrait pas : une OTA · un RETOUR ARRIÈRE ·
#   un `device.json` édité à la main · une déclaration REFUSÉE (4xx/5xx, réseau), re-tentée au
#   tour suivant sans mémoire d'événement à conserver · un publisher redémarré au mauvais moment.
#   ⓘ Ce qu'elle ne couvre PAS : une restauration de la base CLOUD à un état antérieur — le
#     boîtier croirait avoir déjà déclaré. Seule une variante où le cloud rend sa version dans la
#     réponse du `/ping` fermerait ce cas. Hors périmètre, noté dans `ben-docs#15`.
#
#   LE DRAPEAU EST RETIRÉ, pose ET consommation. Le publisher SUPPRIME sans jamais le LIRE celui
#   qu'un boîtier porterait encore : l'interpréter reviendrait à garder les deux mécanismes, donc
#   à garder celui qu'on retire.
#
# ═══ 🚨 CE QUE CE SCRIPT NE PEUT PAS CONTRÔLER, ET POURQUOI IL N'ESSAIE PAS ════════════════════
#
#   L'effet de cette version — une déclaration qui part — n'est observable QU'APRÈS la sortie de
#   ce script. L'ordre de `check_update.py` est ⑧ ce script · ⑨ le bump de `device.json` · ⑩ le
#   redémarrage de `ben-publisher`. La déclaration a donc lieu à ⑩+60 s au plus tard, quand nous
#   n'existons plus.
#
#   ⇒ On ne l'EXIGE PAS. Un garde-fou impossible à tenir brûle une version aussi sûrement qu'un
#     vrai défaut : pi-0.9.12 a été brûlée pour avoir interrogé `/info`, une route inexistante,
#     et l'update s'est rejouée toutes les 10 minutes. Ce qu'on vérifie ici, ce sont les
#     PRÉCONDITIONS (le code est là, il compile, son banc passe, il est BRANCHÉ) ; ce qu'on
#     attend ensuite est écrit en fin de journal, nommément, pour que personne n'aille le
#     chercher.
#
# ═══ AUCUN REDÉMARRAGE ICI, ET AUCUNE MIGRATION ═══════════════════════════════════════════════
#
#   Un seul service porte ce changement — `ben-publisher` — et c'est l'AGENT qui le redémarre, à
#   l'étape ⑩, APRÈS le bump. Le redémarrer ici le relancerait sur un `device.json` encore en
#   0.9.28 : il déclarerait 0.9.28, mémoriserait 0.9.28, et dépenserait une déclaration pour
#   apprendre au cloud une version que le boîtier est en train de quitter.
#   ⓘ Ce serait SANS DOMMAGE DURABLE — et c'est une propriété de la condition, pas une chance :
#     au redémarrage de ⑩ la mémoire (0.9.28) différerait de l'installée (0.10.0), donc le
#     boîtier redéclarerait. Là où le drapeau avait besoin d'une garde d'ordre explicite pour
#     survivre à cette course, la condition s'en passe. On ne redémarre pas pour autant.
#
#   AUCUNE table, AUCUNE colonne, AUCUN DDL : la mémoire est un FICHIER. Une table aurait
#   demandé un DDL ici même, et `open_db()` du publisher ouvre en écriture SANS rejouer le schéma
#   (délibéré) tandis que l'API locale est en lecture seule — donc une table oubliée aurait fait
#   lever `no such table` à chaque tour, et le boîtier aurait cessé de publier EN SILENCE. Un
#   fichier absent ou illisible, lui, vaut « jamais déclarée » : une déclaration de trop, bornée
#   par le plancher. Le retour arrière vers 0.9.28 ne demande donc de restaurer RIEN.
#
#   ⓘ POURQUOI 0.10.0 ET PAS 0.9.29 : le mécanisme de la déclaration change de nature et un état
#     local apparaît. ⚠️ Attention à l'ordre LEXICOGRAPHIQUE : « 0.10.0 » < « 0.9.28 » comme
#     chaînes. Sans conséquence sur l'OTA — `find_next_transition` compare `from` par ÉGALITÉ,
#     jamais par ordre — mais le panneau « Versions firmware » de `ben-parc` trie en texte, donc
#     0.10.0 s'affichera SOUS 0.9.28.

set -euo pipefail
TR="→ pi-0.10.0"
log()  { echo "[update $TR] $*"; }
warn() { echo "[update $TR] ⚠ $*" >&2; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"
SRC="$REPO/src/pi"
PUB="$SRC/publisher"
API="http://127.0.0.1:8087/health"
MEMOIRE="/var/lib/ben-firmware/version-declaree.json"
DRAPEAU="/var/lib/ben-firmware/declaration-requise.json"

# ═══ PRÉFLIGHT ① — le fichier livré est là et compile ═════════════════════════════════════════
FICHIERS=("$PUB/ben_publisher.py" "$PUB/test_declaration_version.py")
for f in "${FICHIERS[@]}"; do
    [ -f "$f" ] || fail "absent du dépôt : $f (checkout pi-0.10.0 incomplet ?)"
done

# ⚠️ `ast.parse`, JAMAIS `py_compile` : celui-ci écrit dans __pycache__, qui appartient à root
#    sur les boîtiers du parc alors que l'OTA tourne en `ben`.
python3 - "${FICHIERS[@]}" <<'PYEOF' || fail "un fichier livré ne compile pas"
import ast, pathlib, sys
for a in sys.argv[1:]:
    p = pathlib.Path(a); ast.parse(p.read_text(encoding="utf-8"), filename=p.name)
PYEOF
log "préflight ① OK (2 fichiers présents et compilables)"

# ═══ PRÉFLIGHT ② — LE BANC QUE LE TAG LIVRE, SUR LE PYTHON DU BOÎTIER ════════════════════════
#
#   15 cas, aucun matériel, aucune base : la condition dans ses quatre états (mémoire absente ⇒
#   UNE déclaration · égale ⇒ AUCUNE · différente ⇒ UNE · refus du cloud ⇒ re-tentée au tour
#   suivant SANS rafale), le retour arrière, la version vide, les deux planchers, et la mémoire
#   sur disque (aller-retour, atomicité, fichier illisible, répertoire non inscriptible).
#
# ⚖️ LES TÉMOINS NÉGATIFS SONT LA MOITIÉ DU BANC, et c'est ce qui le rend utile ici : une
#    condition qui serait TOUJOURS vraie fermerait le défaut d'origine et passerait tous les cas
#    « il faut déclarer » — en faisant redéclarer sept boîtiers toutes les 60 s pour toujours.
#    Les 10 mutations du correctif ont été vérifiées ROUGE avant livraison.
# ⓘ Précédent : `0.9.10_to_0.9.11`, `0.9.11_to_0.9.13` et `0.9.27_to_0.9.28` exécutent un banc livré.
# ⚠️ `TMPDIR=/var/tmp` et pas /tmp : /tmp peut être un tmpfs étroit sur un Pi Zero.
BANC="$PUB/test_declaration_version.py"
TMPDIR=/var/tmp python3 "$BANC" \
    || fail "le banc de la déclaration échoue — NE PAS déployer en l'état"
log "préflight ② OK (banc livré : 15 cas, les quatre états de la mémoire + les deux planchers)"

# ═══ PRÉFLIGHT ③ — 🚨 LE CORRECTIF EST BRANCHÉ, PROUVÉ SUR L'ARBRE ════════════════════════════
#
#   Un code livré mais jamais APPELÉ serait une update qui ne change rien, en silence — et
#   « rien » est ici indiscernable de l'état d'avant, puisque l'état d'avant est précisément
#   « aucune déclaration ne part ». C'est le contrôle de cette version.
#
# 🚨 SUR L'ARBRE SYNTAXIQUE, JAMAIS UN `grep` — et ce n'est pas du raffinement. `ben_publisher.py`
#    NOMME `memoriser_version_declaree`, `DECLARER_FLAG` et le chemin du drapeau DANS SES
#    COMMENTAIRES, longuement : un `grep` serait VERT sur un fichier qui ne les appelle jamais, et
#    ROUGE sur la suppression du drapeau qu'on vient de faire. La prose cite toujours la clause
#    qu'elle explique ; seul l'arbre dit ce que le code FAIT.
python3 - "$PUB/ben_publisher.py" <<'PYEOF' || fail "le correctif est livré mais PAS branché"
import ast, pathlib, sys
arbre = ast.parse(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
definies = {n.name for n in ast.walk(arbre) if isinstance(n, ast.FunctionDef)}
appelees = {n.func.id for n in ast.walk(arbre)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
noms = {n.id for n in ast.walk(arbre) if isinstance(n, ast.Name)}
# ⚠️ Les constantes se cherchent dans les AFFECTATIONS de module, pas dans les noms employés :
#    un `os.unlink(DRAPEAU_LEGUE)` sans affectation passerait le second contrôle tout en levant
#    un NameError au premier tour — et ce tour-là est appelé HORS du `try` d'init, donc le
#    publisher ne démarrerait plus du tout.
affectees = {t.id for n in arbre.body if isinstance(n, ast.Assign)
             for t in n.targets if isinstance(t, ast.Name)}
# Le retrait du drapeau est un APPEL, et on exige de le voir porter CE nom-là.
retrait = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and n.func.attr in ("unlink", "remove")
              and any(isinstance(a, ast.Name) and a.id == "DRAPEAU_LEGUE" for a in n.args)
              for n in ast.walk(arbre))
ko = []
for f in ("version_declaree", "memoriser_version_declaree", "motif_declaration"):
    if f not in definies:
        ko.append(f"fonction ABSENTE : {f}()")
    elif f not in appelees:
        ko.append(f"{f}() est définie mais JAMAIS APPELÉE — update inerte, et en silence")
for c in ("VERSION_DECLAREE", "DRAPEAU_LEGUE"):
    if c not in affectees:
        ko.append(f"{c} n'est pas défini au niveau du module")
# ⚖️ LE TÉMOIN INVERSE : la pose et la lecture du drapeau doivent avoir DISPARU du code.
#    Sans ce contrôle, un correctif ajouté À CÔTÉ de l'ancien mécanisme passerait les lignes du
#    dessus, et on garderait les deux — donc celui qu'on retire.
if "DECLARER_FLAG" in noms:
    ko.append("DECLARER_FLAG vit encore dans le CODE : les deux mécanismes ont été gardés")
if not retrait:
    ko.append("aucun unlink(DRAPEAU_LEGUE) : un drapeau légué resterait sur le disque")
if ko:
    print("\n".join("  " + k for k in ko), file=sys.stderr)
    sys.exit(1)
print(f"  {len(definies)} fonctions, les 3 du chantier définies ET appelées, drapeau retiré")
PYEOF
log "préflight ③ OK (les trois fonctions sont appelées, l'ancien drapeau ne l'est plus)"

# ═══ ÉTAT AVANT — ce qu'on relèvera après, et le drapeau qu'on NE touche pas ══════════════════
#
# ⚠️ On ne SUPPRIME pas le drapeau ici : c'est le publisher qui le fera, à son premier tour, et
#    le laisser à deux endroits recréerait exactement la dispersion qu'on corrige.
if [ -f "$MEMOIRE" ]; then
    warn "mémoire de version DÉJÀ présente ($MEMOIRE) — inattendu avant cette version :"
    warn "  $(cat "$MEMOIRE" 2>/dev/null | head -c 200)"
    warn "  sans gravité : si elle diffère de la version installée, le boîtier redéclarera."
else
    log "aucune mémoire de version ($MEMOIRE) = l'état attendu ⇒ ce boîtier déclarera UNE fois"
fi
[ -f "$DRAPEAU" ] \
    && log "ⓘ un ancien drapeau traîne ($DRAPEAU) — le publisher le retirera, sans le lire" \
    || log "ⓘ aucun ancien drapeau sur ce boîtier"

# ═══ CONTRÔLE D'EFFET — /health, et RIEN d'autre ══════════════════════════════════════════════
#
# 🚨 Sur /health, JAMAIS /info — cette route n'existe pas et l'avoir interrogée a brûlé
#    pi-0.9.12. /health prouve EN PLUS que la base est LISIBLE (`db: true`), là où un simple
#    code 200 masquerait la panne.
#
# ⭐ CE QU'IL PROUVE, ET IL N'EN PROUVE PAS PLUS : que ce script n'a rien cassé. Il ne peut pas
#    prouver la déclaration, qui part après ⑩ (voir l'en-tête) — et on ne redémarre aucun
#    service, donc il n'y a rien non plus à prouver debout. On n'exige RIEN de la lecture TIC
#    pour la même raison qu'en 0.9.28 : un câble débranché ferait échouer puis rejouer l'update
#    toutes les 10 minutes.
if systemctl is-active --quiet ben-local-api.service; then
    python3 - "$API" <<'PYEOF' || fail "/health ne répond pas, ou la base n'est plus lisible"
import json, sys, time, urllib.request
api, fin, dernier = sys.argv[1], time.time() + 60, None
while time.time() < fin:
    try:
        d = json.loads(urllib.request.urlopen(api, timeout=10).read())
    except Exception as e:
        dernier = f"/health injoignable : {e}"; time.sleep(5); continue
    if not d.get("db"):
        print(f"  /health répond mais db=false : {d}", file=sys.stderr); sys.exit(1)
    print(f"  /health OK — db lisible, version annoncée localement : {d.get('softwareVersion')}")
    sys.exit(0)
print(f"  {dernier}", file=sys.stderr); sys.exit(1)
PYEOF
    log "✓ /health répond et la base est lisible"
else
    warn "ben-local-api ne tourne pas — contrôle d'effet sauté, et ce script ne l'a pas touchée"
fi

# ═══ CE QU'IL RESTE À REGARDER, ET QUE CE SCRIPT NE PEUT PAS VÉRIFIER ═════════════════════════
log "── à vérifier APRÈS cette update (hors de portée de ce script) ──"
log "   La déclaration part au redémarrage de ben-publisher (étape ⑩), donc dans la minute."
log "   journalctl -u ben-publisher -n 40 --no-pager   → attendu, DANS CET ORDRE :"
log "     « déclaration : version installée 0.10.0, déclarée jamais »"
log "     « déclaration OK — N compteur(s) déclaré(s) … »"
log "     et, sur un boîtier qui en portait un : « ancien drapeau … retiré »"
log "   puis : cat $MEMOIRE   → { \"version\": \"0.10.0\", … }"
log "   côté cloud : devices.sw_version passe à 0.10.0, et le panneau « Versions firmware »"
log "     du tableau ben-parc cesse d'annoncer 0.9.27 pour les boîtiers qui ont pris l'OTA."
log "   🚨 SI RIEN NE PART : la condition est re-vérifiée à CHAQUE tour, donc un refus du cloud"
log "      ou une coupure n'est pas définitif — le plancher est de 300 s. C'est toute la"
log "      différence avec le drapeau, qui se perdait."

log "✓ update OK"
