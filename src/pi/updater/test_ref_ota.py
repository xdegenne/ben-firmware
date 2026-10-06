#!/usr/bin/env python3
"""Banc de la REF OTA — « où ce boîtier-ci va-t-il chercher son plan ? »

Chantier `ben-docs#16`, sous-tâche #42. Le choix se fait boîtier par boîtier et il est pris
par le cloud ; le boîtier doit pouvoir s'en passer entièrement.

🚨 CE QUI SE JOUE ICI N'EST PAS LE CHEMIN HEUREUX, c'est le repli. Un boîtier qui cesse de se
   mettre à jour parce qu'une branche a disparu, ou parce que l'API ne répond plus, est un
   boîtier qu'il faut aller chercher à la main — et la règle du dépôt est « branche supprimée
   au merge », donc le geste NORMAL de promotion détruit la ref interrogée.

⚖️ Et les témoins vont dans les DEUX sens : une implémentation qui rendrait toujours `main`
   passerait tous les cas de repli. Ce sont les cas « lu depuis origin/canary » et « canary
   rebasée » qui la font tomber, et ils vérifient le CONTENU lu, pas le fait que ça n'a pas
   levé.

    python3 src/pi/updater/test_ref_ota.py
"""
import json
import pathlib
import subprocess
import sys
import tempfile

R = pathlib.Path(__file__).resolve().parent
sys.path[:0] = [str(R)]

import update_lib as ul  # noqa: E402

CAS = []


def cas(fn):
    CAS.append(fn)
    return fn


def git(*a, cwd):
    return subprocess.run(["git", *a], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


def depot(branches: dict):
    """Un `origin` nu + un clone, avec un `compatibility.yaml` par branche.

    ⭐ Un VRAI dépôt, et c'est le fond du banc : `plan_de_mise_a_jour` ne se prouve pas en
       simulant `subprocess`. Ce qu'on veut savoir est si le fichier lu vient bien de la ref
       demandée — question à laquelle seul git répond.
    """
    d = pathlib.Path(tempfile.mkdtemp())
    git("init", "-q", "--bare", str(d / "origin.git"), cwd=d)
    w = d / "w"
    git("clone", "-q", str(d / "origin.git"), str(w), cwd=d)
    git("config", "user.email", "banc@ben", cwd=w)
    git("config", "user.name", "banc", cwd=w)
    premier = True
    for nom, contenu in branches.items():
        if premier:
            git("checkout", "-q", "-B", nom, cwd=w)
            premier = False
        else:
            git("checkout", "-q", "-B", nom, "main", cwd=w)
        (w / "compatibility.yaml").write_text(contenu)
        git("add", "-A", cwd=w)
        git("commit", "-qm", f"plan {nom}", cwd=w)
        git("push", "-q", "origin", nom, cwd=w)
    # 🚨 Le clone part SANS aucune ref distante connue : c'est l'état d'un boîtier qui
    #    n'a jamais entendu parler de la branche d'essai.
    git("remote", "set-branches", "origin", "main", cwd=w)
    for ref in list(branches) + ["HEAD"]:
        subprocess.run(["git", "update-ref", "-d", f"refs/remotes/origin/{ref}"],
                       cwd=w, capture_output=True)
    return d, w


# ── La validation du nom, qui finit dans une ligne de commande git ────────────

@cas
def les_noms_de_branche_legitimes_passent():
    for bon in ("main", "canary", "feat/42-ref-ota-par-boitier", "v0.10.x", "a"):
        assert ul.ref_valide(bon), bon


@cas
def un_nom_qui_est_une_OPTION_est_refuse():
    """🚨 `--upload-pack=…` dans un nom de branche est une EXÉCUTION DE COMMANDE. Le premier
    caractère imposé alphanumérique règle toute cette famille d'un coup."""
    for mauvais in ("--upload-pack=/bin/sh", "-x", "--exec=id"):
        assert not ul.ref_valide(mauvais), mauvais


@cas
def les_autres_formes_refusees_et_POURQUOI_chacune():
    refus = {
        "a..b": "intervalle de révisions",
        "refs/heads/main": "espace de noms qui n'est pas celui des branches",
        "canary/": "un nom de branche ne finit pas par /",
        "canary.lock": "git s'en sert pour ses verrous",
        "MAIN": "hors alphabet — les branches du dépôt sont en kebab-case",
        "canar y": "espace",
        "canary@{1}": "révision relative",
        "": "vide",
        "a" * 101: "trop long",
    }
    for mauvais, pourquoi in refus.items():
        assert not ul.ref_valide(mauvais), f"{mauvais!r} doit être refusé ({pourquoi})"
    for pas_une_chaine in (None, 42, ["canary"], {"ref": "canary"}):
        assert not ul.ref_valide(pas_une_chaine), repr(pas_une_chaine)


@cas
def main_est_valide_et_c_est_le_temoin():
    """⚖️ Sans ce cas, une `ref_valide` qui refuserait TOUT passerait tous les refus
    ci-dessus — et le boîtier retomberait sur `main`… en le journalisant comme un refus, à
    chaque tick, pour toujours."""
    assert ul.ref_valide(ul.REF_DEFAUT)


# ── Ce que le cloud répond, et tout ce qui vaut `main` ────────────────────────

class _Rep:
    def __init__(self, status, corps):
        self.status, self._c = status, corps.encode()

    def read(self, _n=None):
        return self._c


class _Conn:
    """Fausse connexion : on éprouve la DÉCISION, pas la pile TLS."""
    def __init__(self, *a, **k):
        pass

    def request(self, *a, **k):
        pass

    def getresponse(self):
        return _Rep(*_Conn.reponse)

    def close(self):
        pass


def _avec_cloud(status, corps, monkey=None):
    import http.client
    import ssl
    vrai_conn, vrai_ctx = http.client.HTTPSConnection, ssl.create_default_context
    _Conn.reponse = (status, corps)
    http.client.HTTPSConnection = monkey or _Conn
    ssl.create_default_context = lambda *a, **k: _Ctx()
    try:
        return ul.ref_demandee("ben-0001")
    finally:
        http.client.HTTPSConnection, ssl.create_default_context = vrai_conn, vrai_ctx


class _Ctx:
    def load_cert_chain(self, *a, **k):
        pass


@cas
def une_ref_dite_par_le_cloud_est_employee():
    """⚖️ LE TÉMOIN POSITIF. Sans lui, une implémentation qui rend toujours `main` passe tout
    le reste du banc."""
    assert _avec_cloud(200, '{"ref": "canary"}') == "canary"


@cas
def une_ref_refusee_par_la_validation_vaut_main():
    assert _avec_cloud(200, '{"ref": "--upload-pack=id"}') == ul.REF_DEFAUT


@cas
def tout_ce_qui_ne_donne_pas_une_ref_vaut_main():
    for status, corps in ((404, ""), (500, "boom"), (200, "pas du json"),
                          (200, "{}"), (200, '{"ref": null}'), (200, '{"branche": "canary"}')):
        assert _avec_cloud(status, corps) == ul.REF_DEFAUT, (status, corps)


@cas
def un_5xx_AU_CORPS_VALIDE_vaut_main_aussi():
    """🚨 Le cas qui exige de regarder le CODE et pas seulement le corps. Un proxy ou une page
    d'erreur peut rendre un JSON parfaitement formé ; sans la garde sur le statut, le boîtier
    suivrait une branche dite par une réponse d'ERREUR. Les autres cas de refus passent tous
    sans cette garde, parce que leur corps est illisible — celui-ci est le seul qui la vise."""
    for status in (403, 404, 500, 502, 503):
        assert _avec_cloud(status, '{"ref": "canary"}') == ul.REF_DEFAUT, status


@cas
def une_PANNE_RESEAU_ne_leve_jamais_et_vaut_main():
    """🚨 L'agent répare tous les autres services : il ne doit pas gagner une dépendance
    capable de le tuer. Un `raise` ici, et plus aucune OTA sur ce boîtier."""
    class _Casse(_Conn):
        def request(self, *a, **k):
            raise OSError("réseau injoignable")

    assert _avec_cloud(200, '{"ref": "canary"}', monkey=_Casse) == ul.REF_DEFAUT


@cas
def un_certificat_absent_vaut_main_aussi():
    """Le cas du boîtier désappairé, ou d'un certificat pas encore posé."""
    import ssl
    vrai = ssl.create_default_context
    ssl.create_default_context = lambda *a, **k: (_ for _ in ()).throw(
        FileNotFoundError("/etc/ben-firmware/certs/root-ca.crt"))
    try:
        assert ul.ref_demandee("ben-0001") == ul.REF_DEFAUT
    finally:
        ssl.create_default_context = vrai


# ── Le plan lui-même, sur un VRAI dépôt ──────────────────────────────────────

@cas
def le_plan_est_lu_depuis_la_ref_demandee():
    """⭐ LE CAS QUI PORTE TOUT LE CHANTIER, et il vérifie le CONTENU : sans ça, « ça n'a pas
    levé » serait satisfait par une implémentation qui lit toujours `main`."""
    d, w = depot({"main": "plan: main\n", "canary": "plan: canary\n"})
    compat, ref = ul.plan_de_mise_a_jour(str(w), "canary")
    assert ref == "canary", ref
    assert compat == {"plan": "canary"}, compat


@cas
def une_branche_ABSENTE_retombe_sur_main_DANS_LE_MEME_TICK():
    """🚨 Le cas normal après une promotion : « branche supprimée au merge ». Sans ce repli,
    le geste de promotion arrête les mises à jour des boîtiers d'essai, en silence."""
    d, w = depot({"main": "plan: main\n"})
    compat, ref = ul.plan_de_mise_a_jour(str(w), "jamais-existe")
    assert ref == ul.REF_DEFAUT, ref
    assert compat == {"plan": "main"}, compat


@cas
def une_branche_REBASEE_est_relue_a_jour():
    """🚨 LE CAS DE LA REFSPEC FORCÉE, et c'est l'usage PRÉVU : une branche d'essai se rebase
    depuis `main`, donc son historique est RÉÉCRIT. Sans le `+`, le fetch refuse la mise à
    jour non fast-forward et le boîtier continue de lire l'ANCIEN plan — sans rien dire."""
    d, w = depot({"main": "plan: main\n", "canary": "plan: canary-v1\n"})
    _, ref = ul.plan_de_mise_a_jour(str(w), "canary")
    assert ref == "canary"
    # On réécrit l'histoire de canary, comme le ferait un rebase, et on force la poussée.
    git("checkout", "-q", "--orphan", "neuf", cwd=w)
    (w / "compatibility.yaml").write_text("plan: canary-v2\n")
    git("add", "-A", cwd=w)
    git("commit", "-qm", "rebase", cwd=w)
    git("push", "-q", "--force", "origin", "neuf:canary", cwd=w)
    compat, ref = ul.plan_de_mise_a_jour(str(w), "canary")
    assert ref == "canary"
    assert compat == {"plan": "canary-v2"}, (
        f"plan périmé relu : {compat} — la refspec n'est pas forcée")


@cas
def main_sans_ref_particuliere_marche_comme_avant():
    """⚖️ LE CONTRE-TÉMOIN DU CHANTIER : on AJOUTE un étage, on n'en casse pas un."""
    d, w = depot({"main": "plan: main\n"})
    compat, ref = ul.plan_de_mise_a_jour(str(w))
    assert ref == ul.REF_DEFAUT and compat == {"plan": "main"}


@cas
def une_option_passee_en_ref_echoue_comme_une_REF_introuvable():
    """🚨 LA SECONDE BARRIÈRE — `ref_valide` refuse déjà ces noms, mais si quelqu'un appelait
    `fetch_origin` sans valider, git doit refuser une ref introuvable, jamais exécuter une
    option.

    ⚠️ ET CE CAS NE PROUVE PAS CE QU'ON CROYAIT : il tient grâce au `+` de la refspec, qui la
       rend non-interprétable comme une option — vérifié par mutation, retirer le `--` laisse
       ce cas VERT. Ne pas le relire comme un banc du `--`."""
    d, w = depot({"main": "plan: main\n"})
    try:
        ul.fetch_origin(str(w), "--upload-pack=/bin/false")
    except subprocess.CalledProcessError as e:
        assert "remote ref" in (e.stderr or "") or "refspec" in (e.stderr or ""), e.stderr
        return
    raise AssertionError("une option passée en ref doit faire ÉCHOUER le fetch")


if __name__ == "__main__":
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
