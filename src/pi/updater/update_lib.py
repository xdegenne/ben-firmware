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
_ENV_GIT = {**os.environ, "LC_ALL": "C"}


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
    """Demande au cloud où chercher le plan. Rend TOUJOURS une ref employable.

    🚨 NE LÈVE JAMAIS. C'est l'agent qui répare tous les autres services : il ne
       doit pas gagner une dépendance capable de le tuer. Même doctrine que l'import
       défensif de `label_for_model`, et que le `hello()` du publisher — un échec de
       métadonnée n'empêche pas le travail de se faire.
    """
    try:
        ctx = ssl.create_default_context(
            ssl.Purpose.SERVER_AUTH, cafile=f"{CERT_DIR}/root-ca.crt")
        ctx.load_cert_chain(f"{CERT_DIR}/device.crt", f"{CERT_DIR}/device.key")
        # 🚨 On ne touche NI `check_hostname` NI `verify_mode` : les désactiver
        #    annulerait la moitié de l'authentification. Si le serveur est rejeté,
        #    on corrige le certificat.
        conn = http.client.HTTPSConnection(
            API_HOST, API_PORT, context=ctx, timeout=REF_TIMEOUT_S)
        try:
            conn.request("GET", f"/api/devices/{device_id}/update")
            rep = conn.getresponse()
            # ⓘ Borné : on lit un objet d'une ligne, pas un corps de taille inconnue.
            corps = rep.read(4096)
            if not (200 <= rep.status < 300):
                log.info("ref OTA : HTTP %d — on prend %s", rep.status, REF_DEFAUT)
                return REF_DEFAUT
        finally:
            conn.close()
        ref = json.loads(corps).get("ref")
    except Exception as e:  # noqa: BLE001
        log.info("ref OTA indisponible (%s) — on prend %s", e, REF_DEFAUT)
        return REF_DEFAUT
    if not ref_valide(ref):
        # ⚠️ `warning` et non `info` : contrairement à « pas de réponse », celui-ci
        #    veut dire que quelqu'un a écrit une valeur que le boîtier refuse — donc
        #    une intention qui ne s'applique pas, et qu'on mettrait une heure à
        #    comprendre sans cette ligne.
        log.warning("ref OTA refusée (%r) — on prend %s", ref, REF_DEFAUT)
        return REF_DEFAUT
    if ref != REF_DEFAUT:
        log.info("ref OTA : %s (dite par le cloud)", ref)
    return ref


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
        env=_ENV_GIT,
    )
    return yaml.safe_load(result.stdout)


def deja_fusionnee(repo_path: str, ref: str) -> bool:
    """`origin/<ref>` est-elle entièrement contenue dans `origin/main` ?

    ⭐ C'est la parade au PIÈGE DE LA PROMOTION, et elle ne demande AUCUN état : si la
       branche est un ancêtre de `main`, alors `main` contient déjà tout ce qu'elle
       contient, PLUS la suite. La suivre encore ne peut que faire rater des releases.

    ⚠️ Un code de retour autre que 0 ou 1 (donc une vraie erreur de git) vaut « pas
       fusionnée » : on garde le comportement nominal plutôt que de dévier sur un
       doute. On n'arrive ici qu'après deux `fetch` réussis, donc les deux refs
       existent.
    """
    r = subprocess.run(
        ["git", "-C", repo_path, "merge-base", "--is-ancestor",
         f"origin/{ref}", f"origin/{REF_DEFAUT}"],
        capture_output=True, text=True, env=_ENV_GIT,
    )
    return r.returncode == 0


def plan_de_mise_a_jour(repo_path: str = "/opt/ben/repo",
                        ref: str = REF_DEFAUT) -> tuple:
    """Rend `(compatibility, ref_employée)`. Retombe sur `main` DANS CE TICK.

    🚨 LE REPLI DANS LE MÊME TICK EST LE CŒUR DE CE MÉCANISME, et il couvre TROIS cas
       qui mènent tous au même endroit — `main` — parce qu'une branche d'essai est, par
       nature, l'endroit où les choses cassent :

       ① la ref est introuvable. La règle du dépôt est « branche supprimée au merge »,
          donc le geste NORMAL de promotion détruit la ref interrogée ;
       ② la branche est DÉJÀ FUSIONNÉE mais survit (merge sans `--delete-branch`, ou
          `ota_ref` qu'on a oublié de remettre à NULL). Sans cette garde, le boîtier
          suivrait un plan qui ne bougera plus et RATERAIT toutes les releases
          suivantes — en silence. Promouvoir demande deux gestes ; celui-ci rattrape
          l'oubli du second ;
       ③ le `compatibility.yaml` de la branche est ILLISIBLE. `yaml.safe_load` lève une
          `YAMLError`, pas une `CalledProcessError` : avec un `except` étroit l'erreur
          traversait et le tick échouait, puis re-échouait toutes les 10 minutes.

    ⇒ D'où un `except Exception` ASSUMÉ sur le chemin de la branche : tout ce qui
      cloche sur une ref d'essai doit rendre la main à `main`, jamais arrêter les mises
      à jour. ⚠️ Et il ne vaut QUE pour ce chemin — un `main` cassé, lui, doit lever :
      c'est une panne réelle, qui doit se voir.
    """
    if ref == REF_DEFAUT:
        fetch_origin(repo_path, REF_DEFAUT)
        return load_compatibility_from_remote(repo_path, REF_DEFAUT), REF_DEFAUT
    try:
        fetch_origin(repo_path, ref)
        # `main` AUSSI, parce qu'un boîtier sur une branche ne fetcherait QUE sa ref : sa
        # vue d'`origin/main` daterait d'avant son basculement, et la comparaison
        # « fusionnée ? » porterait sur un `main` d'hier — donc ne verrait jamais la fusion.
        # ⚠️ GARDE NON DÉMONTRÉE, et je préfère l'écrire : la retirer laisse le banc VERT.
        #    Sur un dépôt de banc, fetcher la seule branche d'essai a parfois rafraîchi
        #    `origin/main` au passage — et parfois non, selon la forme du dépôt. Je n'ai pas
        #    isolé la cause. ⇒ On garde le fetch explicite précisément pour que la comparaison
        #    ne dépende PAS d'un comportement de git qu'on ne sait pas énoncer ; mais il ne
        #    faut pas lui créditer une protection qu'on n'a pas vue tomber.
        #    ⓘ Mesuré par ailleurs : `origin/main` absent fait sortir `merge-base` en 128, donc
        #      `deja_fusionnee` rend False — le défaut serait « on suit la branche pour
        #      toujours », silencieux, exactement ce que cette garde existe pour éviter.
        fetch_origin(repo_path, REF_DEFAUT)
        if deja_fusionnee(repo_path, ref):
            log.info("branche %s déjà fusionnée dans %s — on suit %s : elle ne peut plus "
                     "rien apporter, et %s a pu avancer depuis",
                     ref, REF_DEFAUT, REF_DEFAUT, REF_DEFAUT)
        else:
            return load_compatibility_from_remote(repo_path, ref), ref
    except Exception as e:  # noqa: BLE001
        motif = (getattr(e, "stderr", None) or str(e)).strip()[:200]
        log.warning("ref %s inutilisable (%s) — repli sur %s DANS CE TICK",
                    ref, motif, REF_DEFAUT)
    fetch_origin(repo_path, REF_DEFAUT)
    return load_compatibility_from_remote(repo_path, REF_DEFAUT), REF_DEFAUT


# ---------------------------------------------------------------------------
# Transition resolution
# ---------------------------------------------------------------------------

def _rev_num(rev: str) -> int:
    """'rev03' → 3"""
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
        env=_ENV_GIT,
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
