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
import logging
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


class _Journal(logging.Handler):
    """Capte les lignes de `update_lib` : certains défauts de ce chantier sont des NIVEAUX,
    pas des valeurs. Un `WARNING` émis 8 fois × toutes les 10 min noie celui qui compte."""

    def __init__(self):
        super().__init__()
        self.lignes = []

    def emit(self, r):
        self.lignes.append((r.levelno, r.getMessage()))


def journal():
    h = _Journal()
    ul.log.addHandler(h)
    ul.log.setLevel(logging.INFO)
    return h


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


# ── Les DEUX signaux affirmatifs, et EUX SEULS, donnent `main` ───────────────

@cas
def une_route_ABSENTE_404_donne_main_c_est_le_debrayage():
    """C'est l'état d'aujourd'hui — `ben-api#29` n'est pas déployée — et c'est aussi le geste
    de débrayage : retirer la route rend 404, donc rend `main` à tout le parc."""
    assert _avec_cloud(404, "") == ul.REF_DEFAUT


@cas
def aucune_branche_pour_ce_boitier_donne_main_EN_INFO_PAS_EN_WARNING():
    """🚨 `ota_ref` vaut `NULL` pour la quasi-totalité du parc, donc `{"ref": null}` sera la
    réponse NORMALE de 8 boîtiers toutes les 10 minutes. En `WARNING`, ça ferait 1 150 lignes
    d'avertissement par jour pour dire que tout va bien — et ça noierait celui de
    `hors_du_plan`, qui signale un boîtier réellement bloqué. Un avertissement permanent ne se
    lit plus."""
    for corps in ('{"ref": null}', "{}", '{"branche": "canary"}'):
        h = journal()
        try:
            assert _avec_cloud(200, corps) == ul.REF_DEFAUT, corps
            niveaux = [n for n, _ in h.lignes]
            assert niveaux and max(niveaux) <= logging.INFO, (
                f"{corps} doit se dire en INFO, pas en {logging.getLevelName(max(niveaux))} : "
                f"{h.lignes}")
        finally:
            ul.log.removeHandler(h)


# ── TOUT LE RESTE DONNE `main`, EN WARNING ──────────────────────────────────
#
# 🚨 DÉCISION PRISE AVEC XAVIER, NOTÉE DANS PR #43 : `ota_ref` sert à recevoir une version EN
#    AVANCE, et à rien d'autre — il n'existe pas d'usage « retenir un boîtier en arrière », ni
#    dans ben-docs#16 ni dans ben-api#29. Donc `main` est TOUJOURS le choix prudent, et surtout :
#    L'OTA EST LE SEUL CANAL DE RÉPARATION. Un certificat expiré, une CA renouvelée, et un
#    boîtier qui « ne fait rien quand il ne sait pas » sort de l'OTA POUR TOUJOURS.
# ⚖️ Chaque cas vérifie DEUX choses : la ref rendue, ET le niveau du journal. Un `main` silencieux
#    serait aussi faux qu'un gel : ces situations ne sont pas normales.

def _doit_donner_main_en_warning(nom, appel):
    h = journal()
    try:
        assert appel() == ul.REF_DEFAUT, f"{nom} doit donner {ul.REF_DEFAUT}"
        assert any(n >= logging.WARNING for n, _ in h.lignes), (
            f"{nom} doit s'annoncer en WARNING, pas en silence : {h.lignes}")
    finally:
        ul.log.removeHandler(h)


@cas
def une_panne_de_l_API_donne_main_car_l_OTA_EST_LE_SEUL_MOYEN_DE_REPARER():
    """🚨 LE CAS QUI A FAIT RENVERSER LA RÈGLE — DANS CE SENS-CI, APRÈS L'AVOIR RENVERSÉE DANS
    L'AUTRE. Une version antérieure sautait le tick sur une panne, au nom d'une « retenue » que
    personne n'avait demandée. Or un certificat expiré ou une CA renouvelée empêchent le boîtier
    de joindre le cloud : s'il en concluait « je ne sais pas, donc rien », il sortirait de l'OTA
    pour toujours — et l'OTA est précisément ce qui aurait pu le réparer."""
    for statut in (500, 502, 503, 403, 429):
        _doit_donner_main_en_warning(f"HTTP {statut}",
                                     lambda s=statut: _avec_cloud(s, '{"ref": "canary"}'))


@cas
def une_PANNE_RESEAU_donne_main():
    class _Casse(_Conn):
        def request(self, *a, **k):
            raise OSError("réseau injoignable")

    _doit_donner_main_en_warning(
        "panne réseau", lambda: _avec_cloud(200, '{"ref": "canary"}', monkey=_Casse))


@cas
def un_corps_ILLISIBLE_donne_main():
    _doit_donner_main_en_warning("corps illisible", lambda: _avec_cloud(200, "pas du json"))


@cas
def une_ref_PRESENTE_mais_refusee_donne_main_AVEC_un_WARNING():
    """⚖️ Le WARNING est le contre-témoin du cas « aucune branche », qui doit rester en INFO :
    sans lui, un code qui ravalerait tout en `info` passerait, et une valeur qu'un opérateur a
    écrite mais que le boîtier refuse resterait invisible."""
    for mauvaise in ('{"ref": "--upload-pack=id"}', '{"ref": "Hold"}', '{"ref": "hold "}',
                     '{"ref": "a..b"}'):
        _doit_donner_main_en_warning(mauvaise, lambda m=mauvaise: _avec_cloud(200, m))


@cas
def un_certificat_PRESENT_MAIS_ILLISIBLE_donne_main_AVEC_un_WARNING():
    """🚨 `ssl.SSLError` et `PermissionError` héritent tous deux d'`OSError` : la fenêtre de
    `ben_certd.basculer` pendant une rotation tombe donc ici. C'est une anomalie — d'où le
    WARNING — mais un boîtier dont la clé est abîmée a BESOIN de l'OTA."""
    import ssl
    vrai = ssl.create_default_context
    for panne in (PermissionError("device.key"), ssl.SSLError("bad key")):
        ssl.create_default_context = lambda *a, **k: (_ for _ in ()).throw(panne)
        try:
            _doit_donner_main_en_warning(repr(panne), lambda: ul.ref_demandee("ben-0001"))
        finally:
            ssl.create_default_context = vrai


@cas
def un_certificat_ABSENT_donne_main_SANS_warning():
    """⚖️ LE CONTRE-TÉMOIN : un boîtier non provisionné n'a RIEN d'anormal à signaler, et il ne
    peut de toute façon pas être sur une branche. En INFO."""
    import ssl
    vrai = ssl.create_default_context
    ssl.create_default_context = lambda *a, **k: (_ for _ in ()).throw(
        FileNotFoundError("/etc/ben-firmware/certs/root-ca.crt"))
    h = journal()
    try:
        assert ul.ref_demandee("ben-0001") == ul.REF_DEFAUT
        assert h.lignes and max(n for n, _ in h.lignes) <= logging.INFO, h.lignes
    finally:
        ssl.create_default_context = vrai
        ul.log.removeHandler(h)


@cas
def ref_demandee_NE_LEVE_JAMAIS():
    """⚖️ LE TÉMOIN DE LA RÈGLE ELLE-MÊME. Si une seule branche de cette fonction se remettait à
    lever, un boîtier pourrait de nouveau sortir de l'OTA sur une panne de cloud."""
    import ssl
    vrai = ssl.create_default_context
    cas_limites = [(200, "{}"), (404, ""), (500, "x"), (200, "pas du json"),
                   (200, '{"ref": "Hold"}'), (200, '{"ref": null}')]
    for statut, corps in cas_limites:
        assert isinstance(_avec_cloud(statut, corps), str)
    for panne in (PermissionError("x"), ssl.SSLError("y"), FileNotFoundError("z")):
        ssl.create_default_context = lambda *a, **k: (_ for _ in ()).throw(panne)
        try:
            assert isinstance(ul.ref_demandee("ben-0001"), str), panne
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
    · un second argument avait été avancé — « elle passerait outre un ordre légitime de
      retenue » — et il est TOMBÉ depuis : la retenue délibérée n'existe pas (décision de PR #43).
      La première raison suffit : une garde qui ne peut jamais se déclencher est pire qu'absente.

    ⭐ Ce qui la remplace compare le CONTENU des deux plans, donc résiste au squash — voir
       `une_branche_SANS_SUITE_fait_BASCULER_sur_main_apres_l_avoir_crie`. Ce cas-ci fixe le
       comportement voulu quand on ne donne PAS de version à comparer, et il tombe si quelqu'un
       remet une garde d'ancêtre."""
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
        f"sans version à comparer, le boîtier suit la branche : {ref}")
    assert compat == {"plan": "canary"}, compat
    # ⚖️ Et la preuve que le test d'ancêtre ne POUVAIT pas marcher ici :
    r = subprocess.run(["git", "merge-base", "--is-ancestor", "origin/canary", "origin/main"],
                       cwd=w, capture_output=True)
    assert r.returncode != 0, (
        "après un squash, la branche ne doit PAS être ancêtre de main — si elle l'est, ce banc "
        "ne reproduit pas la fusion du dépôt et toute garde d'ancêtre y serait verte à tort")


# ── Le signalement qui remplace la garde ─────────────────────────────────────

@cas
def une_branche_SANS_SUITE_fait_BASCULER_sur_main_apres_l_avoir_crie():
    """🚨 LE PIÈGE DE LA PROMOTION EN SQUASH, et il est silencieux. La branche fusionnée survit,
    `ota_ref` reste posé : le boîtier atteint la dernière version que la branche prévoyait, puis
    affiche paisiblement « Already up to date » pour TOUJOURS, en ratant toutes les releases
    suivantes de `main`.

    ⭐ Décision prise avec Xavier (PR #43) : WARNING **puis bascule**. Comme `ota_ref` ne sert qu'à
       recevoir en avance — il n'existe pas de retenue délibérée à protéger — le boîtier peut se
       réparer lui-même. La détection ne demande aucun état : la branche n'offre plus rien pour sa
       version, `main` offre une transition."""
    d, w = depot({
        "main": ("updates_caps:\n"
                 "  - {from: '0.11.0', to: '0.12.0', tag: 'pi-0.12.0', script: 'x'}\n"),
        "canary": "updates_caps: []\n"})
    h = journal()
    try:
        compat, ref = ul.plan_de_mise_a_jour(str(w), "canary", "0.11.0")
        assert ref == ul.REF_DEFAUT, f"la branche n'a plus rien pour 0.11.0 : {ref}"
        assert compat["updates_caps"][0]["to"] == "0.12.0", compat
        assert any(n >= logging.WARNING for n, _ in h.lignes), (
            f"la bascule doit être CRIÉE, sinon elle est aussi silencieuse que le piège : "
            f"{h.lignes}")
    finally:
        ul.log.removeHandler(h)


@cas
def une_branche_QUI_A_ENCORE_QUELQUE_CHOSE_est_suivie():
    """⚖️ LE CONTRE-TÉMOIN, et sans lui le chantier n'aurait plus d'objet : un code qui
    basculerait TOUJOURS sur `main` passerait le cas précédent — et plus aucun boîtier ne
    recevrait jamais une version en avance."""
    d, w = depot({
        "main": "updates_caps: []\n",
        "canary": ("updates_caps:\n"
                   "  - {from: '0.11.0', to: '0.12.0-rc1', tag: 'pi-0.12.0-rc1', script: 'x'}\n")})
    compat, ref = ul.plan_de_mise_a_jour(str(w), "canary", "0.11.0")
    assert ref == "canary", ref
    assert compat["updates_caps"][0]["to"] == "0.12.0-rc1", compat


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
def un_compatibility_yaml_ILLISIBLE_sur_la_branche_fait_SAUTER_LE_TICK():
    """Un YAML mal formé sur la branche lève une `yaml.YAMLError`, pas une
    `CalledProcessError` : avec l'`except` d'origine l'erreur traversait et le tick échouait
    toutes les 10 minutes.

    🚨 MAIS LE REPLI N'EST PAS LA BONNE RÉPONSE NON PLUS, et c'est la raison la plus forte du
    chantier : **un plan mal formé est exactement ce que la branche d'essai existe pour
    attraper.** Replier sur `main` ferait disparaître de l'écran le défaut qu'on cherchait à
    voir — le boîtier se mettrait à jour normalement et le plan cassé ne se manifesterait qu'en
    atteignant tout le parc. Sauter le tick le laisse visible, sans rien appliquer."""
    d, w = depot({"main": "plan: main\n", "canary": "plan: [ceci n'est pas\n  du yaml: :\n"})
    h = journal()
    try:
        ul.plan_de_mise_a_jour(str(w), "canary")
    except ul.TickASauter:
        # ⚖️ ET LE NIVEAU COMPTE : un tick sauté en `info` est un gel SILENCIEUX, qui peut durer
        #    des semaines sans qu'on le voie. C'est le seul cas qui gèle, il doit crier.
        assert any(n >= logging.ERROR for n, _ in h.lignes), (
            f"un plan inutilisable doit se dire en ERROR : {h.lignes}")
        return
    finally:
        ul.log.removeHandler(h)
    raise AssertionError("un plan illisible doit faire sauter le tick, pas replier sur main")


@cas
def un_compatibility_yaml_ABSENT_de_la_branche_fait_SAUTER_LE_TICK():
    """Même famille, et c'est le cas le plus facile à produire : on pousse une branche d'essai
    sans le fichier. `git show origin/<ref>:compatibility.yaml` échoue alors — et la branche, elle,
    existe bel et bien, donc on ne replie pas."""
    d, w = depot({"main": "plan: main\n"})
    git("checkout", "-q", "-B", "sans-plan", "main", cwd=w)
    (w / "compatibility.yaml").unlink()
    git("commit", "-qam", "sans plan", cwd=w)
    git("push", "-q", "origin", "sans-plan", cwd=w)
    h = journal()
    try:
        ul.plan_de_mise_a_jour(str(w), "sans-plan")
    except ul.TickASauter:
        assert any(n >= logging.ERROR for n, _ in h.lignes), h.lignes
        return
    finally:
        ul.log.removeHandler(h)
    raise AssertionError("un plan ABSENT doit faire sauter le tick")


@cas
def un_fetch_qui_echoue_donne_main_et_NOMME_la_branche():
    """« Je n'ai pas pu fetcher » ne veut pas dire « la branche n'existe plus » — mais les deux
    donnent `main`. Ce que `ls-remote` apporte n'est plus une décision, c'est la bonne CAUSE dans
    le journal : « la branche n'existe plus » (promotion) ou « fetch impossible » (panne). Une
    cause fausse envoie chercher au mauvais endroit."""
    d, w = depot({"main": "plan: main\n", "canary": "plan: canary\n"})
    # `main` est déjà connu localement ; seul `origin` devient injoignable.
    subprocess.run(["git", "remote", "set-url", "origin", str(d / "disparu.git")],
                   cwd=w, check=True, capture_output=True)
    h = journal()
    try:
        ul.plan_de_mise_a_jour(str(w), "canary")
    except Exception as e:  # noqa: BLE001
        # ⓘ `main` étant injoignable aussi dans ce montage, la lecture finit par lever — ce qui
        #   est le comportement voulu pour `main` (une panne de `main` est une panne réelle). Ce
        #   que ce cas prouve est qu'on n'a PAS sauté le tick au motif de la branche.
        assert not isinstance(e, ul.TickASauter), (
            "un fetch impossible ne doit pas faire sauter le tick : on prend main")
    # ⚠️ ASSERTION SPÉCIFIQUE, et la première ne l'était pas : `ref_existe_sur_origin` émet elle
    #    aussi un WARNING contenant « canary », donc exiger « un WARNING qui nomme la branche »
    #    était satisfait SANS la ligne du repli — vérifié par mutation, le cas restait vert quand
    #    on faisait passer le repli en `info`. On exige donc la ligne du REPLI, qui est la seule à
    #    dire « on prend ».
    assert any(n >= logging.WARNING and "canary" in m and "on prend" in m
               for n, m in h.lignes), (
        f"le repli sur main doit se dire en WARNING et nommer la branche : {h.lignes}")
    ul.log.removeHandler(h)


@cas
def une_HOMONYME_EN_SOUS_DOSSIER_ne_fait_PAS_passer_la_branche_pour_presente():
    """🚨 LE MOTIF DE `ls-remote` EST APPARIÉ SUR LA QUEUE DU NOM DE REF. Vérifié sur git 2.54.0 :
    avec `canary` supprimée mais `foo/canary` encore là, `ls-remote --heads origin -- canary`
    rend le code 0 et affiche `refs/heads/foo/canary`. Le boîtier aurait conclu « la branche
    existe encore », donc sauté CHAQUE tick, sans fin, et ne serait JAMAIS revenu sur `main` —
    perdu pour une homonymie en sous-dossier."""
    d, w = depot({"main": "plan: main\n", "foo/canary": "plan: homonyme\n"})
    assert ul.ref_existe_sur_origin(str(w), "canary") is False, (
        "`canary` n'existe pas : seule `foo/canary` existe, et le motif ne doit pas la confondre")
    assert ul.ref_existe_sur_origin(str(w), "foo/canary") is True
    # ⇒ et le repli doit donc bien avoir lieu
    compat, ref = ul.plan_de_mise_a_jour(str(w), "canary")
    assert ref == ul.REF_DEFAUT and compat == {"plan": "main"}, (compat, ref)


@cas
def une_branche_dont_l_ABSENCE_EST_PROUVEE_retombe_sur_main():
    """⚖️ LE CONTRE-TÉMOIN du cas précédent : sans lui, un code qui sauterait TOUJOURS le tick
    passerait — et le geste normal de promotion (« branche supprimée au merge ») figerait les
    boîtiers d'essai pour toujours. `ls-remote` répond ici 2 : l'absence est DÉMONTRÉE."""
    d, w = depot({"main": "plan: main\n"})
    compat, ref = ul.plan_de_mise_a_jour(str(w), "promue-puis-supprimee")
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


@cas
def le_repli_PAR_MODELE_de_find_next_transition_ne_leve_pas():
    """🚨 DÉFAUT LATENT, trouvé en revue : `_rev_num` avait été supprimée par accident dans un
    remplacement de bloc, alors que `find_next_transition` l'appelle encore dans son repli PAR
    MODÈLE. Un `NameError` y serait levé à CHAQUE tick. Rien ne le déclenche aujourd'hui —
    `compatibility.yaml` n'a plus de section `updates:` depuis le ménage du 2026-07-22 — mais un
    défaut qui dort dans l'agent d'OTA est précisément celui qu'on ne veut pas laisser dormir, et
    rien ne l'aurait rattrapé avant qu'il morde.

    ⚖️ Ce cas emprunte donc le CHEMIN MORT exprès, avec son gating matériel, pour que la
       suppression de `_rev_num` se voie."""
    plan = {"updates": {"pi0-wired": [
        {"from": "0.1.0", "to": "0.2.0", "tag": "pi-0.2.0",
         "script": "x", "requires": {"hardwareRevision": {"minimum": "rev03"}}}]}}
    # matériel TROP ANCIEN ⇒ transition écartée, et c'est `_rev_num` qui le décide
    assert ul.find_next_transition(
        plan, {"softwareVersion": "0.1.0", "model": "pi0-wired",
               "hardwareRevision": "rev01"}) is None
    # matériel suffisant ⇒ transition rendue
    t = ul.find_next_transition(
        plan, {"softwareVersion": "0.1.0", "model": "pi0-wired",
               "hardwareRevision": "rev03"})
    assert t and t["tag"] == "pi-0.2.0", t


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
