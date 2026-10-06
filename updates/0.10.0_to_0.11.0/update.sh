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
#     ③ AU MOINDRE DOUTE, `main`, en `warning` : panne de l'API, 5xx, 403, DNS, TLS, certificat
#       illisible, corps illisible, nom de branche refusé, branche disparue
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
#      (`ben-api#29`) : le cloud rend 404, tout boîtier prend donc `main` et se comporte
#      exactement comme avant. Ce volet part SEUL.
#
#   🚨 ET « AU MOINDRE DOUTE, `main` » EST UNE DÉCISION PRISE AVEC XAVIER, notée dans PR #43.
#      Elle a été renversée deux fois en revue avant d'être tranchée, et voici ce qui tranche :
#
#      · `ota_ref` sert à recevoir une version EN AVANCE, et à RIEN d'autre. L'usage « retenir un
#        boîtier en arrière » n'existe pas — ni dans `ben-docs#16`, ni dans `ben-api#29` ;
#      · donc `main` est TOUJOURS le choix prudent : c'est ce que le reste du parc reçoit de toute
#        façon, et une version d'avance manquée se rattrape au tick suivant ;
#      · 🚨 SURTOUT, L'OTA EST LE SEUL CANAL DE RÉPARATION. Un certificat expiré, une CA
#        renouvelée, et le boîtier ne joint plus le cloud. S'il en concluait « je ne sais pas,
#        donc je ne fais rien », il sortirait de l'OTA POUR TOUJOURS, et il faudrait aller le
#        chercher en SSH. La prudence apparente fermait le seul canal de réparation.
#
#   ⓘ UNE SEULE EXCEPTION, et elle vient de Xavier : si la branche EXISTE mais que son
#     `compatibility.yaml` est ILLISIBLE ou ABSENT, on saute le tick, en `error`. Un plan mal
#     formé est TYPIQUEMENT ce qu'une branche d'essai existe pour ATTRAPER — replier sur `main`
#     ferait disparaître de l'écran le défaut qu'on cherchait à voir, et il ne se manifesterait
#     qu'en atteignant tout le parc.
#
#   ⭐ ET LA BRANCHE SANS SUITE : si la branche n'offre plus rien pour la version du boîtier alors
#     que `main` offre une transition — c'est une branche fusionnée EN SQUASH qui survit avec son
#     `ota_ref` encore posé — le boîtier CRIE puis BASCULE sur `main`. Sans ça il atteindrait la
#     dernière version prévue par la branche, puis afficherait « Already up to date » pour
#     toujours en ratant toutes les releases suivantes.
#
# ═══ 🚨 CE QU'IL FAUT ATTENDRE, ET QUI N'EST PAS UNE PANNE ════════════════════════════════════
#
#   LE PREMIER APPEL AU CLOUD N'AURA PAS LIEU PENDANT CETTE UPDATE, MAIS AU TICK SUIVANT.
#
#   L'agent est un PROCESSUS NEUF à chaque tick : celui qui exécute ce script a chargé son code au
#   démarrage, donc AVANT le `git checkout` de l'étape ⑥. Le code neuf est sur le disque, il ne
#   s'exécutera qu'au prochain réveil du `ben-update.timer` (~10 min).
#
#   ⇒ Attendu : « route de ref absente (404) — mécanisme non déployé, on prend main » puis
#     « Fetching origin (main) », dès le tick suivant et tant que `ben-api#29` n'est pas
#     déployée. Les deux sont l'état NORMAL. ⚠️ Ces libellés sont ceux que l'agent écrit VRAIMENT,
#     relevés sur ben-0001 : un script figé par son SHA256 qui annonce une ligne inexistante
#     envoie l'opérateur chercher ce qui n'est pas là.
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
#   33 cas, et ce banc-là a besoin du `git` de la cible : QUINZE de ses cas montent un VRAI dépôt
#   jetable pour vérifier d'où le plan a été lu. C'est le seul moyen de prouver « lu depuis
#   origin/canary », qu'une branche REBASÉE est relue à jour (refspec forcée), et qu'une branche
#   SANS SUITE fait BASCULER sur `main` après l'avoir crié. Deux cas éprouvent des NIVEAUX de
#   journal et non des valeurs — « aucune branche » doit se dire en INFO (ce sera la réponse de 8
#   boîtiers toutes les 10 min) et une ref refusée en WARNING. Et deux cas prouvent que NI un TAG
#   NI une TÊTE DE PULL REQUEST ne peuvent servir de plan : le dépôt est PUBLIC, donc un
#   `ota_ref = "pull/N/head"` ferait sinon lire un plan écrit par un inconnu.
#
# 🚨 ET IL NE LIT PLUS UN MESSAGE DE GIT TRADUISIBLE — c'est le défaut le plus grave trouvé en
#    revue, et il brûlait cette version. Deux cas lisent le texte d'une erreur de git ; `git.mo`
#    FRANÇAIS est présent sur l'image du parc (vérifié sur ben-0001) et `install.sh` ne fixe
#    aucune locale. Sur un boîtier en français, git dit « impossible de trouver la référence
#    distante » : le banc tombait, CE préflight échouait, l'update avortait et se rejouait toutes
#    les 10 minutes sur un boîtier parfaitement SAIN. `fetch_origin` épingle désormais `LC_ALL=C`
#    et les deux cas tournent SOUS `LANGUAGE=fr` pour le prouver. Reproduit sur ben-0001 avant
#    correction, vert après.
#
# ⚖️ Les témoins vont dans les deux sens : une implémentation qui rendrait toujours `main`
#    passerait tous les cas de repli. 42 mutations vérifiées ROUGES avant livraison — et UNE est
#    restée VERTE, ce qui a fait corriger le commentaire plutôt que garder une garde invérifiable
#    (le `--` avant la refspec : c'est le `+` qui fait barrière).
# ⚠️ `TMPDIR=/var/tmp` et pas /tmp : /tmp peut être un tmpfs étroit sur un Pi Zero, et ce banc y
#    crée des dépôts git.
TMPDIR=/var/tmp python3 "$UPD/test_ref_ota.py" \
    || fail "le banc de la ref OTA échoue — NE PAS déployer en l'état"
log "préflight ② OK (banc livré : 33 cas, dont 15 sur un vrai dépôt git, et 2 sous LANGUAGE=fr)"

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
for f in ("ref_valide", "ref_demandee", "plan_de_mise_a_jour", "hors_du_plan",
          "ref_existe_sur_origin"):
    if f not in dl:
        ko.append(f"{f}() ABSENTE de update_lib")
# ⚖️ Chacune doit être appelée, et PAS par n'importe qui : la décision vit dans l'agent.
if "ref_demandee" not in aa:
    ko.append("l'agent n'appelle pas ref_demandee() — il ne demanderait jamais la ref")
if "plan_de_mise_a_jour" not in aa:
    ko.append("l'agent n'appelle pas plan_de_mise_a_jour() — pas de repli dans le tick")
# 🚨 Sans cet appel, un boîtier sorti du parc OTA journaliserait « Already up to date » pour
#    toujours — c'est l'état de ben-0005, invisible depuis des semaines.
if "hors_du_plan" not in aa:
    ko.append("l'agent n'appelle pas hors_du_plan() — un boîtier figé resterait silencieux")
# 🚨 LE SEUL CAS QUI DOIT GELER. Sans `TickASauter` définie ET levée ET rattrapée par l'agent, un
#    plan de branche inutilisable repasserait en silence sur `main` — et le défaut que la branche
#    d'essai servait à attraper disparaîtrait de l'écran.
if not any(isinstance(n, ast.ClassDef) and n.name == "TickASauter" for n in ast.walk(lib)):
    ko.append("TickASauter absente — un plan de branche inutilisable ne gèlerait plus")
leves = [n for n in ast.walk(lib) if isinstance(n, ast.Raise) and "TickASauter" in ast.dump(n)]
if not leves:
    ko.append("TickASauter n'est JAMAIS levée — le plan de branche inutilisable ne gèlerait plus")
# 🚨 ET UN SEUL ENDROIT DOIT LA LEVER. Si `ref_demandee` se remettait à lever, une panne de cloud
#    sortirait un boîtier de l'OTA — alors que l'OTA est son seul canal de réparation. C'est la
#    décision de PR #43, et elle est encodée ici parce qu'elle a déjà été renversée deux fois.
if len(leves) != 1:
    ko.append(f"TickASauter est levée {len(leves)} fois : un seul cas doit geler (plan de branche "
              f"inutilisable), tout le reste prend main")
for n in ast.walk(lib):
    if isinstance(n, ast.FunctionDef) and n.name == "ref_demandee":
        if any(isinstance(x, ast.Raise) and "TickASauter" in ast.dump(x) for x in ast.walk(n)):
            ko.append("ref_demandee lève TickASauter — une panne de cloud gèlerait le boîtier")
if "TickASauter" not in {h.type.attr for n in ast.walk(agent)
                         if isinstance(n, ast.Try) for h in n.handlers
                         if isinstance(h.type, ast.Attribute)}:
    ko.append("l'agent ne rattrape pas TickASauter — un tick sauté deviendrait un ÉCHEC")
# ⚖️ Et le témoin inverse : `ls-remote` doit être interrogé, sinon « la branche n'existe plus »
#    serait deviné — ou pire, lu dans un message traduit.
if "ref_existe_sur_origin" not in al:
    ko.append("ref_existe_sur_origin() n'est pas appelée — l'absence serait devinée")
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
log "     « route de ref absente (404) — mécanisme non déployé, on prend main »"
log "       (ben-api#29 pas encore déployée — c'est l'état NORMAL)"
log "     « Fetching origin (main) »"
log "   ⇒ les deux sont l'état NORMAL, pas une panne. Le mécanisme est installé et inerte."
log "   Quand ben-api#29 sera là : poser ota_ref='canary' sur UN boîtier, et attendre"
log "     « ref OTA : canary (dite par le cloud) » puis « plan lu depuis origin/canary »."

log "✓ update OK"
