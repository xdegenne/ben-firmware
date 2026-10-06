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
import atexit
import json
import os
import pathlib
import shutil
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


# 🚨 LES DÉPÔTS JETABLES SE NETTOIENT. Ce banc est exécuté en PRÉFLIGHT par `update.sh` : une
#    update qui échoue est rejouée TOUTES LES 10 MINUTES, donc chaque passage laisserait ses
#    dépôts derrière lui et ils s'empileraient sur la carte SD d'un Pi Zero. `atexit` et non un
#    `finally` par cas : il nettoie aussi quand un cas lève.
_A_NETTOYER: list = []


@atexit.register
def _nettoyer():
    for d in _A_NETTOYER:
        shutil.rmtree(d, ignore_errors=True)


def depot(branches: dict):
    """Un `origin` nu + un clone, avec un `compatibility.yaml` par branche.

    ⭐ Un VRAI dépôt, et c'est le fond du banc : `plan_de_mise_a_jour` ne se prouve pas en
       simulant `subprocess`. Ce qu'on veut savoir est si le fichier lu vient bien de la ref
       demandée — question à laquelle seul git répond.
    """
    d = pathlib.Path(tempfile.mkdtemp())
    _A_NETTOYER.append(d)
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
def une_branche_FUSIONNEE_EN_SQUASH_reste_SUIVIE_le_boitier_obeit():
    """🚨 LE CAS QUI A FAIT RETIRER UNE GARDE. On avait ajouté un test d'ancêtre pour détecter
    une branche promue dont le boîtier n'aurait pas été détaché. Deux raisons l'ont tué :

    · **les dépôts BEN ne fusionnent QU'EN SQUASH** — vérifié sur l'API GitHub. Un squash crée
      un commit NEUF, donc les commits de la branche ne sont JAMAIS ancêtres de `main` et le
      test répondait toujours « non » : la garde ne pouvait pas se déclencher. Le banc la
      croyait bonne parce qu'il fusionnait en `--ff-only`, forme qui n'arrive jamais ici ;
    · elle PASSAIT OUTRE un ordre légitime — retenir un boîtier sur une vieille branche avant
      une release risquée.

    ⭐ Le boîtier ne peut pas distinguer les deux situations : il OBÉIT, et c'est
       `hors_du_plan` qui le fait crier. Ce cas fixe donc le comportement VOULU, et il tombe si
       quelqu'un remet une garde d'ancêtre."""
    d = pathlib.Path(tempfile.mkdtemp()); _A_NETTOYER.append(d)
    git("init", "-q", "--bare", str(d / "origin.git"), cwd=d)
    w = d / "w"
    git("clone", "-q", str(d / "origin.git"), str(w), cwd=d)
    git("config", "user.email", "banc@ben", cwd=w)
    git("config", "user.name", "banc", cwd=w)
    # ⚠️ `-B main` explicite : le git du boîtier nomme sa branche par défaut `master`.
    git("checkout", "-q", "-B", "main", cwd=w)
    (w / "compatibility.yaml").write_text("plan: main-v1\n")
    git("add", "-A", cwd=w); git("commit", "-qm", "v1", cwd=w)
    git("push", "-q", "origin", "main", cwd=w)
    git("checkout", "-q", "-B", "canary", cwd=w)
    (w / "compatibility.yaml").write_text("plan: canary\n")
    git("commit", "-qam", "essai", cwd=w); git("push", "-q", "origin", "canary", cwd=w)
    # LE SQUASH, tel que GitHub le fait : un commit NEUF sur main, sans parent dans canary.
    git("checkout", "-q", "main", cwd=w)
    git("merge", "-q", "--squash", "canary", cwd=w)
    git("commit", "-qm", "squash de canary", cwd=w)
    git("push", "-q", "origin", "main", cwd=w)

    compat, ref = ul.plan_de_mise_a_jour(str(w), "canary")
    assert ref == "canary", (
        f"le boîtier doit OBÉIR au cloud, pas deviner la promotion : {ref}")
    assert compat == {"plan": "canary"}, compat
    # ⚖️ Et la preuve que le test d'ancêtre ne POUVAIT pas marcher ici :
    r = subprocess.run(["git", "merge-base", "--is-ancestor", "origin/canary", "origin/main"],
                       cwd=w, capture_output=True)
    assert r.returncode != 0, (
        "après un squash, la branche ne doit PAS être ancêtre de main — si elle l'est, ce banc "
        "ne reproduit pas la fusion du dépôt et toute garde d'ancêtre y serait verte à tort")


# ── Le signalement qui remplace la garde ─────────────────────────────────────

@cas
def un_boitier_dont_la_version_est_INCONNUE_DU_PLAN_est_signale():
    """🚨 « Rien à faire » et « je ne peux plus rien faire » ne se disent pas pareil. Une version
    que le plan ignore sort le boîtier du parc OTA : il journaliserait « Already up to date » à
    chaque tick, pour toujours, et personne ne le verrait.

    ⓘ Ce n'est pas une hypothèse : ben-0005 annonce `0.9.29`, version absente de toute
       transition, et il est hors du parc depuis des semaines sans qu'une ligne le dise."""
    plan = {"updates_caps": [{"from": "0.10.0", "to": "0.11.0", "tag": "pi-0.11.0"}]}
    assert ul.hors_du_plan(plan, "0.9.29"), "une version inconnue du plan doit être signalée"
    assert ul.hors_du_plan(plan, "0.12.0"), "une version EN AVANCE sur le plan aussi"


@cas
def une_version_CONNUE_du_plan_ne_declenche_aucun_signalement():
    """⚖️ LE TÉMOIN, et sans lui un `hors_du_plan` qui rendrait toujours True ferait crier les
    8 boîtiers à chaque tick — et un avertissement permanent ne se lit plus."""
    plan = {"updates_caps": [{"from": "0.10.0", "to": "0.11.0", "tag": "pi-0.11.0"},
                             {"from": "0.11.0", "to": "0.12.0", "tag": "pi-0.12.0"}]}
    for connue in ("0.10.0", "0.11.0", "0.12.0"):
        assert not ul.hors_du_plan(plan, connue), (
            f"{connue} est dans le plan — en `from` ou en `to` — rien à signaler")


@cas
def un_plan_VIDE_est_signale_aussi():
    """Un `compatibility.yaml` sans aucune transition ne peut faire avancer personne."""
    assert ul.hors_du_plan({}, "0.11.0")
    assert ul.hors_du_plan({"updates_caps": []}, "0.11.0")


@cas
def un_compatibility_yaml_ILLISIBLE_sur_la_branche_retombe_sur_main():
    """🚨 Le repli ne couvrait QUE l'échec de git. Un YAML mal formé sur la branche d'essai
    lève une `yaml.YAMLError`, pas une `CalledProcessError` : l'erreur traversait, le tick
    échouait, et il échouait de nouveau toutes les 10 minutes. Or une branche d'essai est
    précisément l'endroit où un YAML se casse."""
    d, w = depot({"main": "plan: main\n", "canary": "plan: [ceci n'est pas\n  du yaml: :\n"})
    compat, ref = ul.plan_de_mise_a_jour(str(w), "canary")
    assert ref == ul.REF_DEFAUT, ref
    assert compat == {"plan": "main"}, compat


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
       ce cas VERT. Ne pas le relire comme un banc du `--`.

    🚨 ET IL TOURNE SOUS `LANGUAGE=fr`, DÉLIBÉRÉMENT. Ce banc est exécuté en PRÉFLIGHT par
       `update.sh` : s'il échoue, l'update avorte, est rejouée toutes les 10 min, et la version
       est BRÛLÉE. Or il lit un message de git — qui est TRADUIT. `git.mo` français est présent
       sur l'image du parc (vérifié sur ben-0001), donc un boîtier dont la locale est française
       dirait « impossible de trouver la référence distante » et ce cas tomberait, sur un
       boîtier parfaitement sain. ⇒ `fetch_origin` épingle `LC_ALL=C`, et c'est CE cas qui le
       prouve : sans l'épinglage il est rouge sur une cible traduite."""
    d, w = depot({"main": "plan: main\n"})
    avant = os.environ.get("LANGUAGE")
    os.environ["LANGUAGE"] = "fr"
    try:
        ul.fetch_origin(str(w), "--upload-pack=/bin/false")
    except subprocess.CalledProcessError as e:
        assert "remote ref" in (e.stderr or "") or "refspec" in (e.stderr or ""), (
            f"message de git non épinglé en C — il parle la langue du boîtier : {e.stderr!r}")
        return
    finally:
        if avant is None:
            os.environ.pop("LANGUAGE", None)
        else:
            os.environ["LANGUAGE"] = avant
    raise AssertionError("une option passée en ref doit faire ÉCHOUER le fetch")


@cas
def le_message_de_git_est_en_C_meme_si_le_boitier_parle_francais():
    """⚖️ LE TÉMOIN DIRECT de l'épinglage, sur le chemin NORMAL d'erreur — celui que
    `plan_de_mise_a_jour` journalise. Un journal qui change de langue d'un boîtier à l'autre
    ne se grep pas, et surtout : tout contrôle qui lirait ce texte deviendrait faux ailleurs."""
    d, w = depot({"main": "plan: main\n"})
    avant = os.environ.get("LANGUAGE")
    os.environ["LANGUAGE"] = "fr"
    try:
        ul.fetch_origin(str(w), "nexistepas")
    except subprocess.CalledProcessError as e:
        assert "couldn't find remote ref" in (e.stderr or ""), (
            f"git n'est pas épinglé en C : {e.stderr!r}")
        return
    finally:
        if avant is None:
            os.environ.pop("LANGUAGE", None)
        else:
            os.environ["LANGUAGE"] = avant
    raise AssertionError("une ref absente doit faire échouer le fetch")


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
