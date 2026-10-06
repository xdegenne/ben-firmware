"""
update_lib.py — helpers for the BEN OTA update agent.

Covers: device.json I/O, la REF où chercher le plan, compatibility.yaml parsing,
git operations, GPG tag verification, SHA256 script verification.
"""

import hashlib
import http.client
import json
import logging
import os
import re
import ssl
import subprocess
import yaml
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


# 🚨 LA LANGUE DE GIT EST ÉPINGLÉE, et ce n'est pas du confort. `git.mo` FRANÇAIS est présent
#    sur l'image du parc — vérifié sur ben-0001 — et `install.sh` ne fixe aucune locale : la
#    langue des messages dépend donc de la façon dont chaque carte SD a été écrite. Un message
#    traduit a deux effets, et le second brûle une version :
#      · les journaux du parc ne se grep plus de la même manière d'un boîtier à l'autre ;
#      · tout contrôle qui LIT ce texte devient faux ailleurs — et le banc livré par le tag est
#        exécuté en PRÉFLIGHT par `update.sh`, donc un contrôle faux fait AVORTER l'update, qui
#        est rejouée toutes les 10 minutes, indéfiniment.
# ⓘ `LC_ALL=C` SUFFIT, mesuré sur la cible : gettext ignore `LANGUAGE` quand la locale est `C`
#    (`LANGUAGE=fr LC_ALL=C` rend bien « couldn't find remote ref »). Inutile de vider LANGUAGE.
# ⓘ On n'épingle QUE les appels de ce chantier. `verify_tag` journalise la sortie de GPG, qui
#    est un autre sujet et que rien ne lit.
# 🚨 CONSTRUIT À CHAQUE APPEL, et ce n'est pas un détail de style. En constante de module,
#    l'environnement était figé à l'IMPORT : un banc qui pose `LANGUAGE=fr` ensuite ne changeait
#    plus rien à ce que git recevait, donc ses deux cas restaient VERTS même en retirant
#    `LC_ALL=C`. La protection était bonne et le banc ne la prouvait plus — c'est-à-dire qu'on
#    ne pouvait plus le voir tomber.
def _env_git() -> dict:
    return {**os.environ, "LC_ALL": "C"}


# ---------------------------------------------------------------------------
# La REF où chercher le plan de mise à jour  (ben-docs#16)
# ---------------------------------------------------------------------------
#
# Le choix se fait boîtier par boîtier, et il est pris par le CLOUD : un champ dans
# `device.json` demanderait d'ouvrir une session sur chaque boîtier, alors que l'API
# a déjà la table `devices` et que le boîtier lui parle en mTLS.
#
# 🚨 ET C'EST L'AGENT QUI DEMANDE, pas le publisher qui relaie. Si une mauvaise
#    version tue le publisher, on doit encore pouvoir piloter ce boîtier — c'est
#    précisément le moment où on en a besoin.
#
# ⭐ DÉBRAYABLE : tout ce qui ne donne pas une ref valide vaut `main`. Pas de
#    réponse, DNS, TLS, 404, 5xx, JSON illisible, champ absent, nom refusé — et le
#    jour où la route est coupée, les boîtiers continuent de se mettre à jour comme
#    avant. Aucune contrainte d'ordre de déploiement : ce code part SEUL.
# 🚨 LA RÈGLE, ET ELLE TIENT EN UNE PHRASE : au moindre doute, ON PREND `main`, et on le DIT
#    en `warning`. Panne de l'API, 5xx, 403, DNS, TLS, certificat illisible, corps illisible, nom
#    de branche refusé : tout cela donne `main`.
#
# ⭐ POURQUOI, ET C'EST UNE DÉCISION PRISE AVEC XAVIER (notée dans PR #43) : `ota_ref` sert à
#    recevoir une version EN AVANCE, et à rien d'autre. Il n'existe pas d'usage « retenir un
#    boîtier en arrière » — ni dans `ben-docs#16`, ni dans `ben-api#29`.
#    ⇒ Donc `main` est TOUJOURS le choix prudent : c'est ce que le reste du parc reçoit de toute
#      façon. Une version d'avance manquée se rattrape au tick suivant ; un boîtier gelé, non.
#
# 🚨 ET C'EST L'ARGUMENT QUI TRANCHE : L'OTA EST LE SEUL CANAL DE RÉPARATION. Un certificat
#    expiré, une CA renouvelée, et le boîtier ne peut plus joindre le cloud. S'il en concluait
#    « je ne sais pas, donc je ne fais rien », il sortirait de l'OTA POUR TOUJOURS — et la seule
#    issue serait d'aller le chercher en SSH. Prendre `main` dans ce cas lui laisse justement la
#    chance de recevoir le correctif.
#    ⚠️ Une version antérieure de cette PR faisait l'inverse, au nom d'un cas d'usage que
#       personne n'avait demandé. C'est le défaut qu'il faut se rappeler ici : la prudence
#       apparente — « ne rien faire quand on ne sait pas » — fermait le seul canal de réparation.
#
# ⓘ LA SEULE EXCEPTION, et elle vient de Xavier : si la branche EXISTE mais que son
#   `compatibility.yaml` est ILLISIBLE ou ABSENT, on saute le tick. Un plan mal formé est
#   typiquement ce qu'une branche d'essai existe pour ATTRAPER ; replier sur `main` ferait
#   disparaître de l'écran le défaut qu'on cherchait à voir. C'est le seul cas qui lève
#   `TickASauter`, et il le fait en `error`.
REF_DEFAUT = "main"
API_HOST = os.environ.get("BEN_API_HOST", "api.benpilote.fr")
API_PORT = int(os.environ.get("BEN_API_PORT", "8443"))
CERT_DIR = os.environ.get("BEN_CERT_DIR", "/etc/ben-firmware/certs")
# ⓘ Court : l'OTA n'est pas pressée, mais un tick qui se traîne tient le verrou
#    `update.lock` et retarde le suivant.
REF_TIMEOUT_S = float(os.environ.get("BEN_OTA_REF_TIMEOUT", "10"))

# 🚨 LE NOM VIENT DU RÉSEAU ET FINIT DANS UNE LIGNE DE COMMANDE `git`. L'alphabet
#    est donc FERMÉ, et volontairement plus étroit que ce que git accepte : les
#    branches de ce dépôt sont en kebab-case sans accent (`feat/42-...`), donc
#    refuser les majuscules et tout le reste ne coûte rien et retire des cas à
#    raisonner. ⇒ Le premier caractère est alphanumérique, ce qui règle `-…` et
#    `--upload-pack=…` — qui serait une EXÉCUTION DE COMMANDE.
# ⚠️ La signature GPG du tag reste le verrou qui décide quel CODE s'exécute, mais
#    elle intervient APRÈS : elle ne couvre pas ça.
_REF_ALPHABET = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,99}$")


class TickASauter(Exception):
    """Le plan de la branche d'essai est INUTILISABLE ⇒ on ne fait rien, et ça doit se VOIR.

    🚨 UN SEUL CAS LA LÈVE : `compatibility.yaml` illisible ou absent sur une branche qui, elle,
       existe. Tout le reste prend `main` (voir le commentaire de `REF_DEFAUT`). Si vous la voyez
       levée ailleurs, c'est que la règle a été reperdue.

    ⓘ L'agent sort en 0 : rien n'a été tenté, `device.json` n'est pas touché. Mais il le
      journalise en `error`, pas en `info` — un gel silencieux est le défaut qu'on ferme ici.
    """


def ref_valide(ref) -> bool:
    """La ref est-elle employable dans un `git fetch` et un `git show` ?

    ⚠️ On refuse plus que l'alphabet : `..` est un intervalle de révisions, `refs/`
       permettrait de viser un espace de noms qui n'est pas celui des branches, et
       un `/` ou un `.lock` final ne sont pas des noms de branche valides.
    """
    if not isinstance(ref, str) or not _REF_ALPHABET.match(ref):
        return False
    return not (".." in ref or ref.startswith("refs/")
                or ref.endswith("/") or ref.endswith(".lock"))


def ref_demandee(device_id: str) -> str:
    """Demande au cloud où chercher le plan. Rend TOUJOURS une ref — elle ne lève jamais.

    Au moindre doute, `main`. Ce qui change d'un cas à l'autre est le NIVEAU du journal :

      · **404** — la route n'existe pas : mécanisme non déployé, ou retiré. C'est l'état
        d'aujourd'hui, et c'est aussi le geste de débrayage. En `info` ;
      · **2xx sans `ref`** (absente ou `null`) — « aucune branche pour ce boîtier ».
        ⚠️ EN `INFO`, PAS EN `WARNING` : `ota_ref` vaut `NULL` pour la quasi-totalité du parc,
        donc ce sera la réponse NORMALE de 8 boîtiers toutes les 10 minutes. Un avertissement
        permanent ne se lit plus, et il noierait celui de `hors_du_plan` — qui signale, lui, un
        boîtier réellement bloqué ;
      · **tout le reste** — 5xx, 403, délai dépassé, DNS, TLS, connexion refusée, corps
        illisible, certificat présent mais illisible, ref fournie mais refusée — donne `main` en
        **`warning`**. Ces cas ne sont pas normaux et doivent se voir, mais aucun ne justifie de
        fermer le seul canal de réparation du boîtier.

    🚨 CETTE FONCTION NE LÈVE JAMAIS. C'est le point qui a été repris deux fois : une version
       antérieure sautait le tick sur une panne, au nom d'une « retenue » que personne n'avait
       demandée — et un certificat expiré suffisait alors à sortir un boîtier de l'OTA pour
       toujours.
    """
    try:
        ctx = ssl.create_default_context(
            ssl.Purpose.SERVER_AUTH, cafile=f"{CERT_DIR}/root-ca.crt")
        ctx.load_cert_chain(f"{CERT_DIR}/device.crt", f"{CERT_DIR}/device.key")
        # 🚨 On ne touche NI `check_hostname` NI `verify_mode` : les désactiver annulerait la
        #    moitié de l'authentification. Si le serveur est rejeté, on corrige le certificat.
    except FileNotFoundError as e:
        # ⓘ AUCUN certificat = boîtier non provisionné ou désappairé : il ne peut PAS parler au
        #   cloud, donc il ne peut recevoir AUCUN ordre de branche. `main` est le seul plan
        #   qu'il puisse suivre, et c'est aussi celui dont il a besoin : l'OTA est ce qui pourrait
        #   lui rendre un certificat.
        log.info("pas de certificat (%s) — aucun ordre de branche possible, on prend %s",
                 e, REF_DEFAUT)
        return REF_DEFAUT
    except OSError as e:
        # ⚠️ DISTINCT de `FileNotFoundError`, et pas pour rien : `OSError` englobe `ssl.SSLError`
        #    ET `PermissionError` (les deux en héritent, vérifié). Un certificat PRÉSENT mais
        #    momentanément illisible — la fenêtre de `ben_certd.basculer` pendant une rotation —
        #    n'est pas « ce boîtier n'a pas de certificat » : c'est une anomalie, donc `warning`.
        #    Mais on prend `main` quand même : un boîtier dont la clé est abîmée a BESOIN de
        #    l'OTA.
        log.warning("certificat présent mais inutilisable (%s) — on prend %s", e, REF_DEFAUT)
        return REF_DEFAUT
    try:
        conn = http.client.HTTPSConnection(
            API_HOST, API_PORT, context=ctx, timeout=REF_TIMEOUT_S)
        try:
            conn.request("GET", f"/api/devices/{device_id}/update")
            rep = conn.getresponse()
            # ⓘ Borné : on lit un objet d'une ligne, pas un corps de taille inconnue.
            statut, corps = rep.status, rep.read(4096)
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        log.warning("cloud injoignable pour la ref (%s) — on prend %s", e, REF_DEFAUT)
        return REF_DEFAUT

    if statut == 404:
        log.info("route de ref absente (404) — mécanisme non déployé, on prend %s", REF_DEFAUT)
        return REF_DEFAUT
    if not (200 <= statut < 300):
        log.warning("HTTP %d sur la ref — on prend %s", statut, REF_DEFAUT)
        return REF_DEFAUT
    try:
        ref = json.loads(corps).get("ref")
    except Exception as e:  # noqa: BLE001
        log.warning("réponse de ref illisible (%s) — on prend %s", e, REF_DEFAUT)
        return REF_DEFAUT

    if ref is None:
        log.info("aucune branche pour ce boîtier — on suit %s", REF_DEFAUT)
        return REF_DEFAUT
    if not ref_valide(ref):
        # ⚠️ `warning` : quelqu'un a écrit une valeur que le boîtier ne peut pas employer, donc
        #    une intention qui ne s'applique pas. Mais on prend `main` — figer le boîtier pour
        #    une faute de frappe fermerait son canal de réparation.
        log.warning("ref OTA refusée (%r) — on prend %s ; corriger `ota_ref` côté cloud",
                    ref, REF_DEFAUT)
        return REF_DEFAUT
    if ref != REF_DEFAUT:
        log.info("ref OTA : %s (dite par le cloud)", ref)
    return ref


def ref_existe_sur_origin(repo_path: str, ref: str):
    """`True` existe · `False` n'existe pas · `None` on ne sait pas.

    🚨 ON LIT UN CODE DE SORTIE, JAMAIS UN MESSAGE — `ls-remote` sort en 128 sur une panne, et
       lire « couldn't find remote ref » aurait rouvert le défaut de la locale qui a déjà failli
       brûler cette version.

    🚨 ET ON COMPARE LE NOM COMPLET, parce que le MOTIF de `ls-remote` est apparié SUR LA QUEUE
       du nom de ref. Vérifié sur git 2.54.0 : avec `canary` SUPPRIMÉE mais `foo/canary` encore
       présente, `ls-remote --heads origin -- canary` rend le code 0 et affiche
       `refs/heads/foo/canary`. Le boîtier aurait conclu « la branche existe encore », donc
       sauté CHAQUE tick, sans fin, et ne serait JAMAIS revenu sur `main` — un boîtier perdu pour
       une homonymie en sous-dossier.
    """
    cible = f"refs/heads/{ref}"
    r = subprocess.run(
        ["git", "-C", repo_path, "ls-remote", "--heads", "origin", "--", cible],
        capture_output=True, text=True, env=_env_git(),
    )
    # ⓘ Sans `--exit-code`, le code 0 vaut « la commande a abouti » et non « trouvé » : une
    #   sortie VIDE avec un code 0 est donc la preuve de l'absence. C'est ce qu'on veut, le code
    #   servant à distinguer la panne (128) de la réponse.
    if r.returncode != 0:
        log.warning("impossible de savoir si %s existe sur origin (code %d) : %s",
                    ref, r.returncode, (r.stderr or "").strip()[:200])
        return None
    return any(ligne.split("\t")[-1].strip() == cible
               for ligne in r.stdout.splitlines() if ligne.strip())


# ---------------------------------------------------------------------------
# device.json
# ---------------------------------------------------------------------------

def load_device_json(path: str = "/etc/ben-firmware/device.json") -> dict:
    with open(path) as f:
        return json.load(f)


def save_device_json(data: dict, path: str = "/etc/ben-firmware/device.json") -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


# ---------------------------------------------------------------------------
# compatibility.yaml
# ---------------------------------------------------------------------------

def load_compatibility_from_remote(repo_path: str = "/opt/ben/repo",
                                   ref: str = REF_DEFAUT) -> dict:
    """Read compatibility.yaml from origin/<ref> without touching the working tree."""
    result = subprocess.run(
        ["git", "-C", repo_path, "show", f"origin/{ref}:compatibility.yaml"],
        check=True,
        capture_output=True,
        text=True,
        env=_env_git(),
    )
    return yaml.safe_load(result.stdout)


def hors_du_plan(compat: dict, version: str) -> bool:
    """Le plan retenu ignore-t-il complètement la version installée ?

    🚨 CE QUI REMPLACE LA GARDE « BRANCHE DÉJÀ FUSIONNÉE », ET POURQUOI ELLE A ÉTÉ RETIRÉE.
       Cette garde comparait l'historique (`merge-base --is-ancestor`) pour détecter une
       branche promue dont le boîtier n'aurait pas été détaché. Elle était fausse deux fois :

       · **les dépôts BEN ne fusionnent QU'EN SQUASH** (vérifié : `allow_merge_commit` et
         `allow_rebase_merge` sont faux). Un squash crée un commit NEUF : les commits de la
         branche ne sont donc JAMAIS des ancêtres de `main`, et le test répondait toujours
         « non ». La garde ne pouvait pas se déclencher. ⚠️ Et son banc la croyait bonne parce
         qu'il fusionnait en `--ff-only` — une forme qui n'arrive jamais ici ;
       · un second argument avait été avancé — « elle passerait outre un ordre légitime de
         retenue » — et il est TOMBÉ depuis : la retenue délibérée n'existe pas (décision de
         PR #43). Il ne reste donc que la première raison, mais elle suffit : une garde qui ne
         peut jamais se déclencher est pire qu'absente, puisqu'on la croit active.

    ⭐ CE QUI REMPLACE LA GARDE est plus simple et se vérifie : on compare ce que la branche
       OFFRE à ce que `main` offre, pour la version du boîtier (`plan_de_mise_a_jour`). Le
       contenu, pas l'historique — donc immune au squash. Et quand la branche n'a plus rien,
       le boîtier CRIE puis BASCULE : un boîtier bruyant se répare, un boîtier silencieusement
       figé ne se voit pas.

    ⇒ Vrai quand la version installée n'est NI le `from` d'une transition, NI le `to` d'une :
      le plan n'a jamais entendu parler d'elle, donc ce boîtier n'avancera plus jamais.
      ⓘ Le `to` compte : un boîtier à la dernière version est à jour, ce n'est pas une anomalie.
      ⓘ Ça ne vaut pas que pour une branche : ben-0005 annonce `0.9.29`, une version qui n'est
        dans aucune transition de `main`, et il est hors du parc OTA depuis des semaines sans
        qu'aucune ligne ne le dise. Ceci l'aurait dit.
    """
    caps = compat.get("updates_caps") or []
    if not caps:
        return True
    return (version not in {t.get("from") for t in caps}
            and version not in {t.get("to") for t in caps})


def plan_de_mise_a_jour(repo_path: str = "/opt/ben/repo",
                        ref: str = REF_DEFAUT,
                        version: str = "") -> tuple:
    """Rend `(compatibility, ref_employée)`. Lève `TickASauter` dans UN seul cas.

    🚨 TOUT ÉCHEC DE FETCH DONNE `main`, avec un `warning` qui en NOMME la cause — branche
       disparue (le cas normal après une promotion, « branche supprimée au merge ») ou panne qu'on
       n'a pas pu qualifier. `main` est toujours le choix prudent : c'est ce que le reste du parc
       reçoit, et `ota_ref` ne sert qu'à recevoir EN AVANCE (décision notée dans PR #43).

    ⓘ `ref_existe_sur_origin` ne DÉCIDE donc plus rien : elle sert à écrire la bonne cause dans le
      journal. C'est peu, et c'est assez — une cause fausse envoie chercher au mauvais endroit.

    🚨 LA SEULE EXCEPTION : la branche existe mais son `compatibility.yaml` est ILLISIBLE ou
       ABSENT ⇒ `TickASauter`, en `error`. Un plan mal formé est typiquement ce qu'une branche
       d'essai existe pour ATTRAPER ; replier sur `main` ferait disparaître de l'écran le défaut
       qu'on cherchait à voir, et il ne se manifesterait qu'en atteignant tout le parc.

    ⭐ ET LA BRANCHE SANS SUITE : si elle n'offre plus rien pour `version` alors que `main` offre
       une transition, le boîtier BASCULE sur `main` après l'avoir crié. C'est le cas d'une
       branche fusionnée EN SQUASH qui survit avec son `ota_ref` encore posé — la branche ne
       bougera plus, et sans ça le boîtier raterait toutes les releases suivantes en affichant
       paisiblement « Already up to date ». ⓘ Décision prise avec Xavier (PR #43) : comme il
       n'existe pas de retenue délibérée à protéger, le boîtier peut se réparer lui-même.
    """
    if ref == REF_DEFAUT:
        fetch_origin(repo_path, REF_DEFAUT)
        return load_compatibility_from_remote(repo_path, REF_DEFAUT), REF_DEFAUT

    def _main():
        fetch_origin(repo_path, REF_DEFAUT)
        return load_compatibility_from_remote(repo_path, REF_DEFAUT), REF_DEFAUT

    try:
        fetch_origin(repo_path, ref)
    except Exception as e:  # noqa: BLE001
        motif = (getattr(e, "stderr", None) or str(e)).strip()[:200]
        if ref_existe_sur_origin(repo_path, ref) is False:
            log.warning("la branche %s n'existe plus sur origin — on prend %s ; remettre "
                        "`ota_ref` à NULL côté cloud", ref, REF_DEFAUT)
        else:
            log.warning("fetch de %s impossible (%s) — on prend %s", ref, motif, REF_DEFAUT)
        return _main()
    try:
        plan = load_compatibility_from_remote(repo_path, ref)
        # ⚠️ UN FICHIER VIDE N'EST PAS UNE ERREUR POUR `yaml.safe_load` : il rend `None`, et
        #    `find_next_transition` lèverait alors une `AttributeError` plus loin — le défaut
        #    resterait visible, mais en « Update failed » et code 1, pas par le chemin décrit.
        #    Un plan vide EST un plan inutilisable : il appartient ici.
        if not isinstance(plan, dict):
            raise ValueError(f"plan vide ou non structuré ({type(plan).__name__})")
    except Exception as e:  # noqa: BLE001
        # 🚨 `error`, et on NE PREND PAS `main`. Voir la docstring : c'est le seul cas.
        log.error("le plan de la branche %s est inutilisable (%s) — tick sauté, et on n'applique "
                  "RIEN de %s à sa place : c'est le défaut que cette branche sert à attraper",
                  ref, e, REF_DEFAUT)
        raise TickASauter(f"plan de {ref} inutilisable ({e})") from e

    if version:
        try:
            plan_main = _main()[0]
        except Exception as e:  # noqa: BLE001
            # ⓘ On ne peut pas comparer : on suit la branche, qui est l'ordre du cloud.
            log.warning("plan de %s illisible pour comparaison (%s) — on suit %s",
                        REF_DEFAUT, e, ref)
            return plan, ref
        dev = {"softwareVersion": version}
        if (find_next_transition(plan, dev) is None
                and find_next_transition(plan_main, dev) is not None):
            log.warning("la branche %s n'offre plus rien depuis %s alors que %s offre une "
                        "transition — branche probablement fusionnée avec `ota_ref` resté posé "
                        "⇒ ON BASCULE sur %s pour ne pas rater les releases suivantes",
                        ref, version, REF_DEFAUT, REF_DEFAUT)
            return plan_main, REF_DEFAUT
    return plan, ref


def _rev_num(rev: str) -> int:
    """`"rev03"` → 3.

    ⓘ RENDUE À SON APPELANT après l'avoir supprimée par accident dans un remplacement de bloc :
      `find_next_transition` l'appelle encore dans son repli PAR MODÈLE, et un `NameError` y
      serait levé à chaque tick. Rien ne le déclenche aujourd'hui — `compatibility.yaml` n'a
      plus de section `updates:` depuis le ménage du 2026-07-22 — mais un défaut latent dans
      l'agent d'OTA est précisément celui qu'on ne veut pas laisser dormir.
    """
    return int(rev.lstrip("rev"))


def find_next_transition(compat: dict, device: dict) -> Optional[dict]:
    """
    Return the first applicable transition for this device, or None.

    Deux flux, dans l'ordre (backward-compatible pendant la bascule capabilities) :
      1. `updates_caps` — MONO-FLUX keyé sur softwareVersion (device migré : plus de model).
      2. `updates.<model>` — ANCIEN flux per-model (device pas encore migré : device.json a
         encore `model` + `hardwareRevision`).
    Le device migré est en `updates_caps` (qui démarre à la version de migration) → il ne matche
    jamais le fallback. Le device pas migré n'a pas de match `updates_caps` → il prend le fallback.
    """
    current = device["softwareVersion"]

    # 1. Flux capabilities (mono-flux). Pas de model, pas de gating HW ici (l'update.sh est
    #    capability-aware et gère lui-même les cas HW via has_cap/cap_hw).
    for t in (compat.get("updates_caps") or []):
        if t.get("from") == current:
            return t

    # 2. Fallback per-model (device pas encore migré).
    model = device.get("model")
    if model:
        hw_rev = device.get("hardwareRevision", "")
        for t in (compat.get("updates", {}).get(model) or []):
            if t.get("from") != current:
                continue
            min_rev = t.get("requires", {}).get("hardwareRevision", {}).get("minimum")
            if min_rev and hw_rev and _rev_num(hw_rev) < _rev_num(min_rev):
                log.warning(
                    "Transition %s→%s skipped: hardware %s below required %s",
                    t["from"], t["to"], hw_rev, min_rev,
                )
                continue
            return t
    return None


# ---------------------------------------------------------------------------
# Git operations
# ---------------------------------------------------------------------------

def fetch_origin(repo_path: str = "/opt/ben/repo", ref: str = REF_DEFAUT) -> None:
    """Rapatrie les tags et la ref demandée.

    🚨 CÔTÉ SOURCE : `refs/heads/<ref>`, ET C'EST UNE BARRIÈRE, PAS UNE PRÉCISION. Avec un simple
       `<ref>`, git résout le nom à SA façon et ne cherche pas que dans les branches — vérifié sur
       git 2.54.0, `+pi-0.11.0:…` rapatrie le TAG, et `+pull/43/head:…` la TÊTE D'UNE PULL REQUEST.
       Or ce dépôt est PUBLIC : n'importe qui peut ouvrir une PR depuis un fork, donc un
       `ota_ref = "pull/N/head"` ferait lire un plan ÉCRIT PAR UN INCONNU. La signature GPG protège
       toujours le CODE exécuté — `update.sh` vient du tag — mais un plan étranger peut faire
       rejouer un vieux `update.sh` signé sur une version qui n'est pas la sienne.
       ⓘ Et c'était incohérent avec `ref_existe_sur_origin`, qui ne regarde que `refs/heads/` : le
         fetch pouvait réussir là où la vérification d'existence aurait dit non.

    🚨 REFSPEC EXPLICITE ET FORCÉE (`+`), et les deux moitiés comptent.
       · EXPLICITE : sans elle, la mise à jour de `refs/remotes/origin/<ref>` dépend
         du `remote.origin.fetch` du dépôt (`+refs/heads/*:…` sur les boîtiers du
         parc, posé par `install.sh`). Un boîtier provisionné autrement lirait un
         plan PÉRIMÉ sans que rien ne le dise.
       · FORCÉE : une branche d'essai se REBASE depuis `main` — c'est même l'usage
         prévu — donc son historique est réécrit. Sans le `+`, le fetch refuserait
         la mise à jour non fast-forward et le boîtier continuerait de lire
         l'ANCIEN plan.
    🚨 ET C'EST LE `+` QUI FAIT AUSSI BARRIÈRE AUX OPTIONS, pas le `--` — mesuré, pas
       supposé : une refspec préfixée par `+` ne peut pas être lue comme une option,
       donc `--upload-pack=…` arrive à git comme un NOM DE REF et échoue sur
       « couldn't find remote ref ». Le retrait du `--` a été éprouvé : il ne change
       rien tant que la refspec garde cette forme. On le garde quand même — il ne
       coûte rien et il protège un changement de forme futur — mais il ne faut pas
       le créditer d'une protection qu'on ne peut pas voir tomber.
    ⇒ La VRAIE première barrière est `ref_valide`, qui impose un premier caractère
      alphanumérique.
    """
    subprocess.run(
        ["git", "-C", repo_path, "fetch", "--tags", "origin",
         "--", f"+refs/heads/{ref}:refs/remotes/origin/{ref}"],
        check=True,
        capture_output=True,
        text=True,
        env=_env_git(),
    )


def verify_tag(tag: str, repo_path: str = "/opt/ben/repo") -> None:
    """
    Verify the GPG signature of a git tag.

    Requires the BEN release public key to be imported in the system GPG keyring.
    The key is installed at provisioning time by install.sh from
    /etc/ben-firmware/gpg/ben-releases.pub.

    Raises subprocess.CalledProcessError on verification failure.
    """
    result = subprocess.run(
        ["git", "-C", repo_path, "verify-tag", tag],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        log.error("GPG verification failed for tag %s:\n%s", tag, result.stderr)
        raise subprocess.CalledProcessError(result.returncode, result.args, result.stderr)
    log.debug("GPG output: %s", result.stderr.strip())


def checkout_tag(tag: str, repo_path: str = "/opt/ben/repo") -> None:
    subprocess.run(
        ["git", "-C", repo_path, "checkout", tag],
        check=True,
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# Script verification and execution
# ---------------------------------------------------------------------------

def verify_sha256(script_path: str) -> None:
    """
    Verify SHA256 of update.sh against the adjacent .sha256 file.

    The .sha256 file contains the hex digest (optionally followed by a filename).
    Raises ValueError on mismatch, FileNotFoundError if the checksum file is absent.
    """
    checksum_path = script_path + ".sha256"
    expected = Path(checksum_path).read_text().split()[0].strip()
    actual = hashlib.sha256(Path(script_path).read_bytes()).hexdigest()
    if actual != expected:
        raise ValueError(
            f"SHA256 mismatch for {script_path}: expected {expected}, got {actual}"
        )


def run_update_script(script_path: str) -> None:
    """Execute update.sh with bash. Raises subprocess.CalledProcessError on failure."""
    subprocess.run(["bash", script_path], check=True)
