#!/usr/bin/env bash
# update.sh — 0.10.0 → pi-0.11.0 : CHAQUE BOÎTIER DEMANDE AU CLOUD OÙ CHERCHER SES MISES À JOUR.
#
# ═══ CE QUE LIVRE CETTE VERSION ═══════════════════════════════════════════════════════════════
#
#   Chantier `ben-docs#16`, sous-tâche #42. But : valider une OTA sur une branche, sur un ou deux
#   boîtiers seulement ; quand c'est bon, fusionner la branche sur `main` et tout le monde reçoit.
#
#   Aujourd'hui `main` est écrit EN DUR dans l'agent, deux fois — donc un tag publié part sur les
#   8 boîtiers au tick suivant, c'est-à-dire dans les dix minutes. Désormais :
#
#     ① avant chaque tentative d'OTA : GET /api/devices/<id>/update, en mTLS
#     ② le cloud répond { "ref": "canary" }
#     ③ pas de réponse, réponse illisible, nom refusé, branche absente ⇒ `main`
#     ④ l'agent fetch cette ref et y lit compatibility.yaml
#     ⑤ le reste ne change pas : GPG du tag, SHA256 du script, UNE transition par tick
#
#   🚨 C'EST L'AGENT QUI DEMANDE, PAS LE PUBLISHER QUI RELAIE. Si une mauvaise version tue le
#      publisher, on doit encore pouvoir piloter ce boîtier — c'est précisément le moment où on en
#      a besoin. Le prix est une trentaine de lignes de client mTLS dans l'agent, entièrement sous
#      `try/except` : il répare tous les autres services, il ne doit pas gagner une dépendance
#      capable de le tuer.
#
#   ⭐ DÉBRAYABLE, ET SANS CONTRAINTE D'ORDRE DE DÉPLOIEMENT. La route n'existe pas encore
#      (`ben-api#29`) : tout boîtier prendra donc `main` et se comportera exactement comme avant.
#      C'est voulu — ce volet part SEUL, et le jour où la route serait coupée, rien ne s'arrête.
#
# ═══ 🚨 CE QU'IL FAUT ATTENDRE, ET QUI N'EST PAS UNE PANNE ════════════════════════════════════
#
#   LE PREMIER APPEL AU CLOUD N'AURA PAS LIEU PENDANT CETTE UPDATE, MAIS AU TICK SUIVANT.
#
#   L'agent est un PROCESSUS NEUF à chaque tick : celui qui exécute ce script a chargé son code au
#   démarrage, donc AVANT le `git checkout` de l'étape ⑥. Le code neuf est sur le disque, il ne
#   s'exécutera qu'au prochain réveil du `ben-update.timer` (~10 min).
#
#   ⇒ Attendu : « Fetching origin (main) » dès le tick suivant, et « ref OTA indisponible (…) — on
#     prend main » tant que `ben-api#29` n'est pas déployée. Les deux sont l'état NORMAL.
#   ⓘ C'est exactement cette asymétrie qui a fait fermer `ben-firmware#37` sans la faire : un
#     correctif dans l'agent n'est jamais immédiat. Un correctif dans le publisher, si — l'agent le
#     redémarre à l'étape ⑩.
#
# ═══ CE QUE CE SCRIPT NE FAIT PAS ═════════════════════════════════════════════════════════════
#
#   Aucun redémarrage : l'agent est un `Type=oneshot` lancé par timer, il n'y a rien à relancer, et
#   les autres services ne sont pas touchés. Aucune migration, aucune table, aucune colonne, aucun
#   état nouveau sur le disque — la ref n'est pas mémorisée, elle est redemandée à chaque tick.
#   ⇒ Le retour arrière vers 0.10.0 ne demande de restaurer RIEN.
#
#   ⓘ Modifier `update_lib.py` SOUS un agent en marche est sans risque : Python a déjà importé le
#     module, le fichier sur le disque ne change pas le processus courant.

set -euo pipefail
TR="→ pi-0.11.0"
log()  { echo "[update $TR] $*"; }
warn() { echo "[update $TR] ⚠ $*" >&2; }
fail() { echo "[update $TR] ✗ ERREUR : $*" >&2; exit 1; }
REPO="${REPO_PATH:-/opt/ben/repo}"
UPD="$REPO/src/pi/updater"
API="http://127.0.0.1:8087/health"

# ═══ PRÉFLIGHT ① — les fichiers livrés sont là et compilent ═══════════════════════════════════
FICHIERS=("$UPD/update_lib.py" "$UPD/check_update.py" "$UPD/test_ref_ota.py")
for f in "${FICHIERS[@]}"; do
    [ -f "$f" ] || fail "absent du dépôt : $f (checkout pi-0.11.0 incomplet ?)"
done
# ⚠️ `ast.parse`, JAMAIS `py_compile` : celui-ci écrit dans __pycache__, qui appartient à root sur
#    les boîtiers du parc alors que l'OTA tourne en `ben`.
python3 - "${FICHIERS[@]}" <<'PYEOF' || fail "un fichier livré ne compile pas"
import ast, pathlib, sys
for a in sys.argv[1:]:
    p = pathlib.Path(a); ast.parse(p.read_text(encoding="utf-8"), filename=p.name)
PYEOF
log "préflight ① OK (3 fichiers présents et compilables)"

# ═══ PRÉFLIGHT ② — LE BANC LIVRÉ PAR LE TAG, SUR LE PYTHON ET LE GIT DU BOÎTIER ══════════════
#
#   15 cas, et ce banc-là a besoin du `git` de la cible : quatre de ses cas montent un VRAI dépôt
#   jetable à deux branches pour vérifier d'où le plan a été lu. C'est le seul moyen de prouver
#   « lu depuis origin/canary » — et de prouver qu'une branche REBASÉE est relue à jour, ce qui
#   dépend d'une refspec forcée.
#
# ⚖️ Les témoins vont dans les deux sens : une implémentation qui rendrait toujours `main`
#    passerait tous les cas de repli. 9 mutations vérifiées ROUGES avant livraison — dont une qui
#    est restée VERTE et a fait corriger un commentaire faux plutôt que garder une garde
#    invérifiable.
# ⚠️ `TMPDIR=/var/tmp` et pas /tmp : /tmp peut être un tmpfs étroit sur un Pi Zero, et ce banc y
#    crée des dépôts git.
TMPDIR=/var/tmp python3 "$UPD/test_ref_ota.py" \
    || fail "le banc de la ref OTA échoue — NE PAS déployer en l'état"
log "préflight ② OK (banc livré : 15 cas, dont 4 sur un vrai dépôt git)"

# ═══ PRÉFLIGHT ③ — 🚨 LE CORRECTIF EST BRANCHÉ, PROUVÉ SUR L'ARBRE ════════════════════════════
#
#   Un code livré mais jamais APPELÉ serait une update qui ne change rien — et « rien » est ici
#   INDISCERNABLE de l'état d'avant, puisque l'état d'avant est « on prend main ». C'est le
#   contrôle de cette version.
#
# 🚨 SUR L'ARBRE SYNTAXIQUE, JAMAIS UN `grep` : ces deux fichiers NOMMENT `ref_demandee`,
#    `plan_de_mise_a_jour` et `REF_DEFAUT` dans leurs commentaires, longuement. Un grep serait VERT
#    sur un fichier qui ne les appelle jamais.
python3 - "$UPD/update_lib.py" "$UPD/check_update.py" <<'PYEOF' || fail "le correctif est livré mais PAS branché"
import ast, pathlib, sys
lib, agent = (ast.parse(pathlib.Path(a).read_text(encoding="utf-8")) for a in sys.argv[1:3])

def defs(arbre):
    return {n.name: n for n in ast.walk(arbre) if isinstance(n, ast.FunctionDef)}

def appels(arbre):
    return {n.func.id for n in ast.walk(arbre)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)} | \
           {n.func.attr for n in ast.walk(arbre)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}

dl, da = defs(lib), defs(agent)
al, aa = appels(lib), appels(agent)
ko = []
for f in ("ref_valide", "ref_demandee", "plan_de_mise_a_jour"):
    if f not in dl:
        ko.append(f"{f}() ABSENTE de update_lib")
# ⚖️ Chacune doit être appelée, et PAS par n'importe qui : la décision vit dans l'agent.
if "ref_demandee" not in aa:
    ko.append("l'agent n'appelle pas ref_demandee() — il ne demanderait jamais la ref")
if "plan_de_mise_a_jour" not in aa:
    ko.append("l'agent n'appelle pas plan_de_mise_a_jour() — pas de repli dans le tick")
if "ref_valide" not in al:
    ko.append("ref_valide() n'est jamais appelée — le nom du réseau irait tel quel dans git")
# 🚨 Les deux lectures de git doivent accepter une ref ; sans le paramètre, `main` serait encore
#    en dur et tout le reste serait décoratif.
for f in ("fetch_origin", "load_compatibility_from_remote"):
    if f in dl and "ref" not in [a.arg for a in dl[f].args.args]:
        ko.append(f"{f}() n'a pas de paramètre `ref` — `main` est encore en dur")
# ⚖️ LE TÉMOIN INVERSE : l'agent ne doit plus lire le plan en direct, sinon le repli est contourné.
if "load_compatibility_from_remote" in aa:
    ko.append("l'agent lit encore le plan en direct — il saute le repli de plan_de_mise_a_jour()")
if ko:
    print("\n".join("  " + k for k in ko), file=sys.stderr); sys.exit(1)
print(f"  update_lib : {len(dl)} fonctions · agent : les 2 appels en place · ref paramétrée")
PYEOF
log "préflight ③ OK (demande, repli et validation tous branchés ; plus de lecture directe)"

# ═══ ÉTAT AVANT — ce que l'agent connaît comme refs distantes ═════════════════════════════════
log "refs distantes connues de $REPO :"
git -C "$REPO" for-each-ref refs/remotes --format='   %(refname:short)' || true

# ═══ CONTRÔLE D'EFFET — /health, et rien d'autre ══════════════════════════════════════════════
#
# 🚨 Sur /health, JAMAIS /info — cette route n'existe pas et l'avoir interrogée a brûlé pi-0.9.12.
#    /health prouve EN PLUS que la base est LISIBLE (`db: true`).
# ⭐ CE QU'IL PROUVE, ET PAS PLUS : que ce script n'a rien cassé. L'appel au cloud n'a pas encore
#    eu lieu (voir l'en-tête), et aucun service n'a été redémarré — il n'y a donc rien non plus à
#    prouver debout. On n'exige RIEN de la lecture TIC : aucun lecteur n'est touché.
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
    print(f"  /health OK — db lisible, version locale {d.get('softwareVersion')}")
    sys.exit(0)
print(f"  {dernier}", file=sys.stderr); sys.exit(1)
PYEOF
    log "✓ /health répond et la base est lisible"
else
    warn "ben-local-api ne tourne pas — contrôle d'effet sauté, et ce script ne l'a pas touchée"
fi

# ═══ CE QU'IL RESTE À REGARDER ════════════════════════════════════════════════════════════════
log "── à vérifier APRÈS cette update (hors de portée de ce script) ──"
log "   🚨 RIEN NE CHANGE AU TICK COURANT : l'agent en mémoire est l'ANCIEN. Le premier appel"
log "      au cloud a lieu au réveil suivant du ben-update.timer, dans ~10 min."
log "   journalctl -u ben-update -n 30 --no-pager  → attendu, au tick SUIVANT :"
log "     « ref OTA indisponible (…) — on prend main »   (ben-api#29 pas encore déployée)"
log "     « Fetching origin (main) »"
log "   ⇒ les deux sont l'état NORMAL, pas une panne. Le mécanisme est installé et inerte."
log "   Quand ben-api#29 sera là : poser ota_ref='canary' sur UN boîtier, et attendre"
log "     « ref OTA : canary (dite par le cloud) » puis « plan lu depuis origin/canary »."

log "✓ update OK"
