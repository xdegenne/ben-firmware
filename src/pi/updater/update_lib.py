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
# 🚨 LA RÈGLE, ET ELLE TIENT EN UNE PHRASE : on ne suit `main` que sur un signal AFFIRMATIF —
#    le cloud a dit « aucune branche », ou la branche demandée N'EXISTE DÉMONTRABLEMENT PLUS.
#    Tout le reste — 5xx, délai dépassé, DNS, TLS, corps illisible, fetch qui échoue sans qu'on
#    sache pourquoi — fait SAUTER LE TICK.
#
# ⚠️ C'EST UN RENVERSEMENT, et il faut savoir pourquoi. La première version repliait sur `main`
#    « à la moindre erreur », au nom du débrayage. Mais `main` n'est le choix prudent QUE si le
#    boîtier est sur une branche pour recevoir quelque chose EN AVANCE. Si on l'y a mis pour le
#    RETENIR avant une release risquée, alors `main` est précisément le danger : un 503 d'un seul
#    tick suffisait à lui livrer la release qu'on voulait éviter — et `device.json` étant bumpé,
#    ce n'est PAS rattrapable.
#    ⇒ Le boîtier ne peut pas distinguer les deux intentions. Mais il n'a pas à les distinguer :
#      une ABSENCE DE RÉPONSE N'EST PAS UNE RÉPONSE. Sans instruction, on ne fait RIEN. Une OTA
#      n'est jamais urgente — le timer repasse dans dix minutes — alors qu'une release appliquée
#      par erreur ne se retire pas.
#
# ⚠️ CE QUE ÇA COÛTE, ET IL FAUT L'ASSUMER : couper le SERVEUR fige l'OTA du parc le temps de la
#    panne (le boîtier saute ses ticks). Débrayer le mécanisme ne se fait donc pas en éteignant
#    l'API, mais par un signal affirmatif : RETIRER LA ROUTE (404 ⇒ `main`), ou mettre `ota_ref`
#    à NULL. Les deux sont immédiats et explicites.
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
    """On ne sait pas quel plan suivre ⇒ on ne fait RIEN, et on réessaie au tick suivant.

    🚨 CE N'EST PAS UN ÉCHEC, et l'agent sort en 0 : rien n'a été tenté, `device.json` n'est pas
       touché, et le timer repasse dans dix minutes. Un échec (code 1) voudrait dire qu'une
       update a été tentée et a raté ; ici on s'est abstenu, ce qui est le résultat voulu.
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
    """Demande au cloud où chercher le plan. Rend une ref, ou LÈVE `TickASauter`.

    On ne rend `main` que sur un signal AFFIRMATIF :

      · **404** — la route n'existe pas, donc le mécanisme n'est pas déployé ou a été retiré.
        C'est l'état d'aujourd'hui, et c'est aussi le geste de débrayage ;
      · **2xx sans `ref`** (absente ou `null`) — le cloud dit « ce boîtier n'est retenu sur
        aucune branche ». ⚠️ EN `INFO`, PAS EN `WARNING` : `ota_ref` vaut `NULL` pour la quasi-
        totalité du parc, donc ce sera la réponse NORMALE de 8 boîtiers toutes les 10 minutes.
        Un avertissement permanent ne se lit plus, et il noierait celui de `hors_du_plan` — qui
        signale, lui, un boîtier réellement bloqué ;
      · **2xx avec une ref refusée** par la validation — là c'est un `WARNING` : quelqu'un a
        écrit une valeur que le boîtier ne peut pas employer, donc une intention qui ne
        s'applique pas et qu'on mettrait une heure à comprendre sans cette ligne. On prend
        `main` plutôt que de figer : la valeur est une erreur humaine, et le boîtier n'avance
        plus tant qu'elle est là.

    🚨 TOUT LE RESTE LÈVE : 5xx, 403, délai dépassé, DNS, TLS, connexion refusée, corps
       illisible. Voir le commentaire de `REF_DEFAUT` : un 503 d'un seul tick ne doit pas livrer
       à un boîtier RETENU la release qu'on lui épargnait.
    """
    try:
        ctx = ssl.create_default_context(
            ssl.Purpose.SERVER_AUTH, cafile=f"{CERT_DIR}/root-ca.crt")
        ctx.load_cert_chain(f"{CERT_DIR}/device.crt", f"{CERT_DIR}/device.key")
        # 🚨 On ne touche NI `check_hostname` NI `verify_mode` : les désactiver annulerait la
        #    moitié de l'authentification. Si le serveur est rejeté, on corrige le certificat.
    except OSError as e:
        # ⓘ Pas de certificat = boîtier non provisionné ou désappairé : il ne peut PAS parler au
        #   cloud, donc il ne peut pas être retenu sur une branche. `main` est sans risque, et
        #   sauter le tick figerait l'OTA d'un boîtier qui ne pourra jamais demander.
        log.info("pas de certificat (%s) — ce boîtier ne peut être retenu sur aucune branche, "
                 "on prend %s", e, REF_DEFAUT)
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
        raise TickASauter(f"cloud injoignable pour la ref ({e})") from e

    if statut == 404:
        log.info("route de ref absente (404) — mécanisme non déployé, on prend %s", REF_DEFAUT)
        return REF_DEFAUT
    if not (200 <= statut < 300):
        raise TickASauter(f"HTTP {statut} sur la ref — on ne sait pas quel plan suivre")
    try:
        ref = json.loads(corps).get("ref")
    except Exception as e:  # noqa: BLE001
        raise TickASauter(f"réponse de ref illisible ({e})") from e

    if ref is None:
        log.info("aucune branche pour ce boîtier — on suit %s", REF_DEFAUT)
        return REF_DEFAUT
    if not ref_valide(ref):
        log.warning("ref OTA refusée (%r) — on prend %s", ref, REF_DEFAUT)
        return REF_DEFAUT
    if ref != REF_DEFAUT:
        log.info("ref OTA : %s (dite par le cloud)", ref)
    return ref


def ref_existe_sur_origin(repo_path: str, ref: str):
    """`True` existe · `False` n'existe pas · `None` on ne sait pas.

    🚨 ON LIT UN CODE DE SORTIE, JAMAIS UN MESSAGE. `ls-remote --exit-code` rend 0 si la ref
       existe, **2** si aucune ne correspond, et 128 sur une panne — trois codes distincts,
       mesurés. Lire « couldn't find remote ref » aurait rouvert le défaut de la locale, qui a
       déjà failli brûler cette version.
    """
    r = subprocess.run(
        ["git", "-C", repo_path, "ls-remote", "--exit-code", "--heads", "origin", "--", ref],
        capture_output=True, text=True, env=_env_git(),
    )
    if r.returncode == 0:
        return True
    if r.returncode == 2:
        return False
    log.warning("impossible de savoir si %s existe sur origin (code %d) : %s",
                ref, r.returncode, (r.stderr or "").strip()[:200])
    return None


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
       · elle PASSAIT OUTRE un ordre légitime : épingler un boîtier sur une branche tirée d'un
         vieux commit pour le RETENIR avant une release risquée. Cette branche étant ancêtre de
         `main`, la garde ramenait le boîtier sur `main` et lui appliquait la release.

    ⭐ LES DEUX DISENT LA MÊME CHOSE : le boîtier ne peut pas distinguer « branche morte, je
       suis coincé » de « on me retient exprès ». Il ne doit donc pas DÉCIDER — c'est le cloud
       qui choisit la ref, et une seconde décision locale recréerait deux vérités. Il obéit, et
       il CRIE : un boîtier bruyant se répare, un boîtier silencieusement figé ne se voit pas.

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
                        ref: str = REF_DEFAUT) -> tuple:
    """Rend `(compatibility, ref_employée)`, ou LÈVE `TickASauter`.

    🚨 MÊME RÈGLE QUE `ref_demandee` : on ne rend la main à `main` que si la branche
       **n'existe démontrablement plus** sur `origin` — le cas normal après une promotion,
       puisque la règle du dépôt est « branche supprimée au merge ». Tout autre échec fait
       sauter le tick : un réseau qui tombe pendant qu'on suit une branche de RETENUE ne doit
       pas livrer la release de `main`.

    🚨 UN `compatibility.yaml` ILLISIBLE SUR LA BRANCHE FAIT SAUTER LE TICK, et c'est la raison
       la plus forte de tout ce raisonnement : **un plan mal formé est précisément ce que la
       branche d'essai existe pour attraper.** Replier sur `main` ferait disparaître de l'écran
       le défaut qu'on cherchait à voir — le boîtier se mettrait à jour normalement, et le plan
       cassé ne se manifesterait qu'au moment où il atteindrait tout le parc. Sauter le tick le
       laisse visible, sans rien appliquer : l'erreur se corrige en poussant sur la branche, et
       le boîtier repart au tick suivant.

    🚨 Il ne cherche PAS à deviner qu'une branche a été promue : le boîtier obéit à la ref que
       le cloud lui donne, et `hors_du_plan` le fait crier s'il s'y trouve figé. Voir le
       commentaire de `hors_du_plan` pour les deux raisons qui ont fait retirer la garde
       d'ascendance.
    """
    if ref == REF_DEFAUT:
        fetch_origin(repo_path, REF_DEFAUT)
        return load_compatibility_from_remote(repo_path, REF_DEFAUT), REF_DEFAUT
    try:
        fetch_origin(repo_path, ref)
    except Exception as e:  # noqa: BLE001
        motif = (getattr(e, "stderr", None) or str(e)).strip()[:200]
        if ref_existe_sur_origin(repo_path, ref) is False:
            log.warning("la branche %s n'existe plus sur origin (%s) — repli sur %s DANS CE "
                        "TICK ; vérifier `ota_ref` côté cloud", ref, motif, REF_DEFAUT)
            fetch_origin(repo_path, REF_DEFAUT)
            return load_compatibility_from_remote(repo_path, REF_DEFAUT), REF_DEFAUT
        raise TickASauter(f"fetch de {ref} impossible et la branche existe peut-être "
                          f"encore ({motif})") from e
    try:
        return load_compatibility_from_remote(repo_path, ref), ref
    except Exception as e:  # noqa: BLE001
        raise TickASauter(f"plan de {ref} illisible ({e}) — on n'applique RIEN de {REF_DEFAUT} "
                          f"à sa place") from e


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
         "--", f"+{ref}:refs/remotes/origin/{ref}"],
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
