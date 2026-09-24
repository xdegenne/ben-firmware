"""Ce que CHAQUE route répond sur CHACUN des deux ports.

═══ POURQUOI CE BANC EXISTE ═══════════════════════════════════════════════════

Le 24/09, `test_access.py` était VERT À 40/40 pendant que `/access`,
`/invitations` et `/tokens/integration` étaient ouvertes SANS AUCUN JETON sur le
port clair. Mesuré sur ben-0001 : `/access` rendait l'uid ET le prénom de
chacun, `/tokens/integration` un jeton `member` permanent en clair,
`/access/revoke` coupait le propriétaire sans recours.

🚨 Le banc n'était pas faux, il testait la COUCHE D'EN DESSOUS — le magasin, pas
   le routage. Une batterie verte sur une autre couche ne prouve rien, et c'est
   pire que pas de banc du tout : elle rassure.

═══ CE QUI REND CELUI-CI DIFFÉRENT ════════════════════════════════════════════

⭐ Il n'a PAS sa propre liste de routes. Il les extrait du code de `do_GET` et
   `do_POST` (lecture de l'arbre syntaxique), puis exige qu'une décision soit
   écrite pour chacune. Ajouter une route sans la classer fait ROUGIR le banc.

   Un banc qui itère sur sa propre liste a rigoureusement le défaut du code
   qu'il surveille : on ajoute une route, on oublie les deux listes, et tout
   reste vert.

⚠️ Il éprouve le RÉGIME (`server.chiffre`), pas TLS lui-même : les deux serveurs
   du banc écoutent en clair, seul le drapeau diffère. C'est exactement ce que
   les gardes consultent — mais ça ne dit donc rien du chiffrement, qui se
   vérifie ailleurs.
"""
from __future__ import annotations

import ast
import json
import pathlib
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

# 🚨 IMPORTER COMME `local_api` IMPORTE. Il fait `import access` NU ; un
#    `from store import access` créerait un SECOND objet-module, avec son propre
#    singleton `_shared` — le banc poserait sa base jetable dans l'un et le
#    handler ouvrirait la vraie dans l'autre. Constaté : `_role()` dégradait
#    silencieusement en `None` et TOUT répondait 401, y compris avec un jeton
#    valide. Un banc qui se trompe de module teste un autre programme.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import access  # noqa: E402
import local_api  # noqa: E402

SOURCE = pathlib.Path(local_api.__file__)

# ── Les trois verdicts possibles sur le port CLAIR ───────────────────────────
SERVIE = "servie"      # route historique : le port clair la sert, comme avant
ABSENTE = "absente"    # 404 : elle n'existe que sur le canal chiffré
UPGRADE = "upgrade"    # 426 : elle existe et DIT qu'il faut le canal chiffré

# 🚨 LA TABLE DE DÉCISION. Une route du répartiteur qui n'est pas ici fait
#    échouer le banc — c'est le seul mécanisme qui survit à l'oubli.
ATTENDU: dict[str, tuple[str, str]] = {
    # route                 méthode   sur :8087
    "/ping":               ("GET",   SERVIE),
    "/health":             ("GET",   SERVIE),
    "/pdls":               ("GET",   SERVIE),
    "/live":               ("GET",   SERVIE),
    "/measurements":       ("GET",   SERVIE),
    "/curve":              ("GET",   SERVIE),
    "/chart":              ("GET",   SERVIE),
    "/consumption":        ("GET",   SERVIE),
    "/registers":          ("GET",   SERVIE),
    "/lora-link":          ("GET",   SERVIE),
    "/events":             ("GET",   SERVIE),
    "/settings":           ("GET",   SERVIE),
    # ⭐ `/claim` répond 426 elle-même : l'app a besoin de l'indication pour
    #    basculer sur le canal chiffré. C'est la seule qui le fasse.
    "/claim":              ("POST",  UPGRADE),
    # 🚨 Les routes d'administration. C'est LEUR classement qui a manqué.
    "/access":             ("GET",   ABSENTE),
    "/invitations":        ("POST",  ABSENTE),
    "/access/revoke":      ("POST",  ABSENTE),
    "/access/rename":      ("POST",  ABSENTE),
    "/tokens/revoke":      ("POST",  ABSENTE),
    "/tokens/integration": ("POST",  ABSENTE),
    # ⚠️ ÉTEINT LE BOÎTIER. Classée, jamais appelée — voir `DANGEREUSES`.
    "/unprovision":        ("POST",  SERVIE),
}

# Routes dont l'effet est irréversible : on vérifie leur CLASSEMENT, on
# n'émet pas la requête. Le classement est ce qui porte la propriété de
# sécurité ; l'appeler ne prouverait rien de plus et éteindrait la machine.
DANGEREUSES = {"/unprovision"}

# Routes joignables sans jeton même sur le canal chiffré.
SANS_JETON_EN_CHIFFRE = {"/ping", "/claim"}


def routes_du_repartiteur() -> set[str]:
    """Les chemins que `do_GET`/`do_POST` comparent réellement à `path`.

    ⭐ Lu dans le CODE, jamais recopié : c'est ce qui rend l'oubli impossible.
    Reconnaît `path == "/x"` et `path in ("/x", "/y")`.
    """
    arbre = ast.parse(SOURCE.read_text(encoding="utf-8"))
    trouvees: set[str] = set()
    for noeud in ast.walk(arbre):
        if not isinstance(noeud, ast.FunctionDef) or noeud.name not in (
                "do_GET", "do_POST"):
            continue
        for n in ast.walk(noeud):
            if not isinstance(n, ast.Compare) or not isinstance(n.left, ast.Name):
                continue
            if n.left.id != "path":
                continue
            for op, comp in zip(n.ops, n.comparators):
                if isinstance(op, ast.Eq) and isinstance(comp, ast.Constant):
                    trouvees.add(comp.value)
                elif isinstance(op, ast.In) and isinstance(comp, ast.Tuple):
                    for e in comp.elts:
                        if isinstance(e, ast.Constant):
                            trouvees.add(e.value)
    return trouvees


def _serveur(chiffre: bool) -> tuple[ThreadingHTTPServer, str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), local_api.Handler)
    srv.chiffre = chiffre
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _appel(base: str, route: str, methode: str, jeton: str | None = None) -> int:
    corps = json.dumps({}).encode() if methode == "POST" else None
    req = urllib.request.Request(base + route, data=corps, method=methode)
    if corps is not None:
        req.add_header("Content-Type", "application/json")
    if jeton:
        req.add_header("Authorization", f"Bearer {jeton}")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:  # noqa: BLE001
        # Une route qui explose (base absente au banc) a bien été SERVIE :
        # la garde l'a laissée passer, et c'est la seule chose qu'on mesure.
        return 500


def _erreur(base: str, route: str, methode: str) -> str | None:
    """Le champ `error` de la réponse, ou None. Deux refus peuvent partager un
    code et ne rien dire de la même chose."""
    corps = json.dumps({}).encode() if methode == "POST" else None
    req = urllib.request.Request(base + route, data=corps, method=methode)
    if corps is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            charge = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            charge = json.loads(e.read() or b"{}")
        except Exception:  # noqa: BLE001
            return None
    except Exception:  # noqa: BLE001
        return None
    return charge.get("error") if isinstance(charge, dict) else None


CAS = []


def cas(fn):
    CAS.append(fn)
    return fn


@cas
def aucune_route_du_repartiteur_n_echappe_a_la_table():
    """🚨 LE CAS QUI SURVIT À L'OUBLI. Tout le reste du banc en dépend : sans
    lui, ajouter une route sans la classer laisserait la batterie verte."""
    du_code = routes_du_repartiteur()
    de_la_table = set(ATTENDU)
    oubliees = du_code - de_la_table
    fantomes = de_la_table - du_code
    assert not oubliees, (
        f"routes servies par le code mais NON CLASSÉES : {sorted(oubliees)} — "
        f"décidez ce qu'elles valent sur :8087 avant de les livrer")
    assert not fantomes, (
        f"routes classées mais absentes du code : {sorted(fantomes)}")


@cas
def sur_le_port_clair_chaque_route_tient_son_classement():
    srv, base = _serveur(chiffre=False)
    try:
        for route, (methode, verdict) in sorted(ATTENDU.items()):
            if route in DANGEREUSES:
                continue
            code = _appel(base, route, methode)
            if verdict is ABSENTE:
                assert code == 404, f"{route} sur :8087 → {code}, attendu 404"
            elif verdict is UPGRADE:
                assert code == 426, f"{route} sur :8087 → {code}, attendu 426"
            else:
                assert code != 404, (
                    f"{route} sur :8087 → 404 : une route historique a disparu")
    finally:
        srv.shutdown()


@cas
def sur_le_canal_chiffre_tout_exige_un_jeton_sauf_ping_et_claim():
    """⚖️ LE TÉMOIN. Sans lui, un code qui refuserait TOUT partout satisferait
    le cas précédent — et le banc serait vert sur une API morte."""
    srv, base = _serveur(chiffre=True)
    try:
        for route, (methode, _) in sorted(ATTENDU.items()):
            if route in DANGEREUSES:
                continue
            code = _appel(base, route, methode)
            if route in SANS_JETON_EN_CHIFFRE:
                # ⚠️ NE PAS se contenter du code. `/claim` répond bien 401 sans
                #    en-tête — mais c'est le jeton FIREBASE qui lui manque, pas
                #    le jeton BEN. Ce qu'on vérifie, c'est que la GARDE l'a
                #    laissée passer : `token_required` ne doit pas apparaître.
                erreur = _erreur(base, route, methode)
                assert erreur != "token_required", (
                    f"{route} est bloquée par la garde alors qu'elle doit rester "
                    f"ouverte — sans elle, on ne peut ni détecter le canal ni "
                    f"en obtenir un jeton")
            else:
                assert code == 401, f"{route} sur :8088 sans jeton → {code}, attendu 401"
    finally:
        srv.shutdown()


@cas
def les_routes_d_administration_ne_sont_servies_QUE_chiffrees():
    """⚖️ Le recoupement des deux précédents, sur les seules routes qui
    comptent : absentes en clair, présentes (et gardées) en chiffré."""
    admin = [r for r, (_, v) in ATTENDU.items() if v is ABSENTE]
    assert admin, "plus aucune route d'administration : la table a été vidée ?"
    clair, base_clair = _serveur(chiffre=False)
    chiffre, base_chiffre = _serveur(chiffre=True)
    try:
        for route in sorted(admin):
            methode = ATTENDU[route][0]
            assert _appel(base_clair, route, methode) == 404
            assert _appel(base_chiffre, route, methode) == 401
    finally:
        clair.shutdown()
        chiffre.shutdown()


@cas
def un_jeton_de_membre_n_ouvre_aucune_route_d_owner():
    """Le second étage de la garde : le jeton est présent et valide, le RÔLE ne
    suffit pas. Sans ce cas, `_exige_owner` pourrait être mort."""
    srv, base = _serveur(chiffre=True)
    try:
        with access.session() as c:
            membre = access.mint(c, uid="uid_banc_membre", label="banc",
                                 role=access.ROLE_MEMBER)
        for route, (methode, verdict) in sorted(ATTENDU.items()):
            if verdict is not ABSENTE or route in DANGEREUSES:
                continue
            code = _appel(base, route, methode, jeton=membre)
            assert code == 403, f"{route} avec un jeton member → {code}, attendu 403"
    finally:
        srv.shutdown()


if __name__ == "__main__":
    # Base d'accès jetable : `session()` mémorise la première connexion ouverte.
    access._shared = access.connect(
        str(pathlib.Path(tempfile.mkdtemp()) / "access.db"))
    ko = 0
    for fn in CAS:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            ko += 1
            print(f"  KO   {fn.__name__} : {e}")
    print(f"\n{len(CAS) - ko}/{len(CAS)}")
    sys.exit(1 if ko else 0)
