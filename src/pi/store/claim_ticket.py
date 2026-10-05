"""Le ticket de revendication — le FICHIER et le CONTRAT, en un seul endroit.

⇒ Conception : ben-docs/specs/003-acces-multi-utilisateur.md, § `POST /claim`

🚨 POURQUOI CE MODULE EXISTE. Deux processus manipulent le ticket, et pour des raisons
différentes :

    provisioner   le REÇOIT en BLE et le POSE sur le disque
    publisher     le LIT au démarrage et le PRÉSENTE au cloud  (`fonder: true`)
    local_api     le reçoit par `/claim` sur le LAN et le relaie (`fonder: false`)

⭐ Trois appelants, donc trois occasions de faire diverger la charge, le verdict ou le
chemin du fichier. Et la divergence ne se verrait pas : chacun marcherait seul.

⚠️ Mesuré trois fois pendant ce chantier : une logique enfouie dans une fonction qu'un
banc SIMULE n'est jamais exécutée par ce banc. D'où des fonctions PURES ici —
`charge()` et `verdict()` s'éprouvent sans réseau, sans base et sans boîtier.

🔒 LE TICKET EST LE SEUL SECRET QUE LE BOÎTIER STOCKE EN CLAIR, et c'est inévitable :
il doit le PRÉSENTER, pas le vérifier. Le risque est borné par construction — un
ticket vaut pour UN uid, UN boîtier, UN usage, et il EXPIRE (900 s). Volé sur la carte
SD, il ne fonde que ce boîtier-là, au profit de son propriétaire légitime.
"""
from __future__ import annotations

import json
import os
import pathlib

# ── Le fichier ──────────────────────────────────────────────────────────────
#
# ⭐ UN FICHIER, ET PAS UNE TABLE — le point était resté ouvert dans `specs/003`.
#    Ce n'est NI un `token` (il ne donne accès à rien localement) NI une ligne
#    `access` (il ne porte aucun droit). L'y ranger pour économiser une table
#    rendrait le schéma menteur : `role_of` et `for_cloud` liraient une ligne qui
#    n'est ni l'un ni l'autre.
#
# ⚠️ Et il doit SURVIVRE AU REDÉMARRAGE : il est reçu en BLE avant que le WiFi
#    existe, et présenté après le reboot. C'est toute l'asymétrie du déballage.
CHEMIN = pathlib.Path(
    os.environ.get("BEN_CLAIM_TICKET_PATH", "/var/lib/ben-firmware/claim_ticket"))


def poser(ticket: str) -> None:
    """Écrit le ticket, en 0600, et de façon ATOMIQUE.

    ⚠️ `write` puis `rename` : une coupure de courant pendant l'écriture laisserait
    sinon un fichier tronqué, c'est-à-dire un ticket invalide qu'on présenterait une
    fois pour rien. Le déballage se joue précisément au moment où l'alimentation est
    la moins sûre.
    """
    ticket = (ticket or "").strip()
    if not ticket:
        raise ValueError("ticket vide")
    CHEMIN.parent.mkdir(parents=True, exist_ok=True)
    tmp = CHEMIN.with_suffix(".tmp")
    tmp.write_text(ticket, encoding="utf-8")
    os.chmod(tmp, 0o600)
    _donner_au_lecteur(tmp)
    tmp.replace(CHEMIN)


def _donner_au_lecteur(chemin: pathlib.Path) -> None:
    """Donne le fichier au compte qui devra le LIRE puis l'EFFACER.

    🚨 SANS CECI, LE TICKET EST DÉPOSÉ ET JAMAIS PRÉSENTÉ. Mesuré sur ben-0005 le
    05/10 : le provisioner BLE tourne en `User=root`, le publisher en `User=ben`.
    Un fichier 0600 root:root est donc ILLISIBLE par celui qui doit s'en servir —
    `lire()` rendait `""`, `presenter_le_ticket` concluait « pas de ticket », et le
    déballage se terminait sans propriétaire. Le journal du boîtier annonçait
    pourtant `claim_ticket:stored` : tout avait l'air d'avoir marché.

    ⭐ On s'aligne sur le PROPRIÉTAIRE DU RÉPERTOIRE plutôt que sur un nom codé en
    dur : c'est lui qui désigne le compte des agents (`/var/lib/ben-firmware` est à
    `ben`), et ça reste juste si ce compte change un jour. Effacer demande en plus
    le droit d'écrire dans le répertoire — que ce même compte a, puisqu'il le
    possède.

    ⚠️ Sans droit de `chown` (donc hors root), on ne fait RIEN : c'est le cas où
    l'écrivain est déjà le bon compte, et il n'y a rien à corriger.
    """
    try:
        st = CHEMIN.parent.stat()
        os.chown(chemin, st.st_uid, st.st_gid)
    except (PermissionError, OSError):
        pass


def lire() -> str:
    """Le ticket en attente, ou `""`. Ne lève jamais : un disque illisible au
    démarrage ne doit pas empêcher le publisher de publier des mesures."""
    try:
        return CHEMIN.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        # Le cas NORMAL, et de loin le plus fréquent : aucun déballage en cours.
        return ""
    except Exception as e:  # noqa: BLE001
        # 🚨 TOUT LE RESTE EST UN DÉFAUT, ET IL DOIT SE VOIR. Avaler l'erreur ici
        #    confondait « pas de ticket » avec « un ticket que je n'ai pas le droit
        #    de lire » — deux situations dont l'une est normale et l'autre fait
        #    échouer le déballage en silence. C'est exactement ce qui est arrivé le
        #    05/10 sur ben-0005 (0600 root:root, lu par `ben`).
        print(f"[ticket] ILLISIBLE ({type(e).__name__}: {e}) — un ticket est "
              f"peut-être en attente et ne sera pas présenté", flush=True)
        return ""


def effacer() -> None:
    """Retire le ticket. ⭐ À appeler aussi sur un REFUS définitif : un ticket
    consommé ou invalide ne doit pas être réessayé à chaque démarrage."""
    try:
        CHEMIN.unlink()
    except FileNotFoundError:
        pass


# ── Les verdicts du cloud ───────────────────────────────────────────────────


class TicketInvalide(Exception):
    """403 `bad_ticket` — inconnu, expiré, consommé, ou frappé pour un AUTRE boîtier.

    ⭐ LE SEUL REFUS RÉPARABLE PAR L'APP SEULE : elle refrappe un ticket et réessaie
    UNE fois. Les quatre causes se répondent pareil — distinguer renseignerait qui
    tente quoi.
    """


class AucunDroit(Exception):
    """403 `no_access` — l'identité est attestée, mais aucun droit possible ici.

    ⚠️ Pas réessayable : ni le ticket ni le réseau n'y changeront rien. Il faut une
    invitation.
    """


class BoitierRefuse(Exception):
    """403 `device_mismatch` / `device_revoked` / motif inconnu — c'est LE BOÎTIER.

    🚨 À NE PAS CONFONDRE AVEC `AucunDroit`. Les deux sont des 403, mais l'un dit
    « cette personne n'a pas de droit ici » et l'autre « ce boîtier n'a rien à faire
    là ». Les mélanger invite la personne à réclamer une invitation alors que le
    problème est le certificat du boîtier — elle chercherait du mauvais côté, et pour
    toujours.
    """


class ContratRompu(Exception):
    """400 — ben-api n'a pas compris notre requête. Réessayer n'y changera RIEN.

    ⚠️ Présenté comme une panne, ça ferait boucler l'app sur un défaut de version :
    typiquement un cloud plus ancien que le boîtier, qui ne connaît pas le ticket.
    """


class CloudInjoignable(Exception):
    """Réseau, serveur, certificat — tout ce qui n'est pas un verdict.

    🚨 À NE JAMAIS CONFONDRE AVEC UN REFUS. L'app réessaie une panne et abandonne sur
    un refus : présenter l'une pour l'autre fait lire « vous n'êtes pas reconnu » à
    quelqu'un dont tout était bon.
    """


def charge(ticket: str, role_invitation: str = "", fonder: bool = False) -> dict:
    """Ce que le boîtier ENVOIE au cloud sur `/claim`. Pure, pour être éprouvée.

    🚨 `fonder` EST TOUJOURS SÉRIALISÉ, MÊME À `False`, et ce n'est pas du style.
    `ben-api` refuse les champs inconnus (`DisallowUnknownFields`) — mais cette garde
    ne se déclenche QUE SI LE CHAMP EST PRÉSENT. L'omettre quand il vaut `False`,
    comme on le fait pour `role`, laisserait un firmware récent passer SANS BRUIT
    devant un cloud ancien : celui-ci accepterait la charge, fonderait un premier
    propriétaire sans ordre, et le TOFU resterait ouvert. ⚠️ C'est précisément le cas
    dangereux — l'intrus sur le LAN avec un compte Google et un `deviceId` que
    l'ANNONCE BLE porte en clair.

    ⭐ L'asymétrie avec `role` est voulue : l'absence de `role` ne crée AUCUN droit, sa
    présence est ce qui DEMANDE quelque chose. `fonder` est l'inverse — son absence
    serait permissive.

    🔒 ET JAMAIS DE CLÉ D'IDENTITÉ. Le boîtier ne doit pas voir l'`ID token` Firebase,
    encore moins le transporter.
    """
    out = {"ticket": ticket, "fonder": bool(fonder)}
    if role_invitation:
        out["role"] = role_invitation
    return out


def verdict(statut: int, brut: bytes) -> Exception | None:
    """Ce que la réponse de ben-api SIGNIFIE. `None` quand tout va bien.

    🚨 SORTIE POUR ÊTRE ÉPROUVÉE. Un banc qui simule l'appel réseau n'exécute JAMAIS
    cette classification : confondre `device_revoked` et `no_access` laissait tous les
    bancs verts, mesuré le 05/10.

    ⭐ QUATRE VERDICTS, et les confondre coûte cher à chaque fois :

        bad_ticket                      réparable par l'app seule — refrappe, 1 essai
        device_mismatch/device_revoked  c'est LE BOÎTIER qui est refusé
        no_access                       la personne n'a aucun droit ici — invitation
        400                             contrat rompu : réessayer n'y changera RIEN
    """
    if statut == 200:
        return None
    motif = ""
    try:
        motif = (json.loads(brut.decode("utf-8")) or {}).get("error", "")
    except Exception:  # noqa: BLE001
        pass
    if statut == 403:
        if motif == "bad_ticket":
            return TicketInvalide("ticket refusé par ben-api")
        if motif == "no_access":
            return AucunDroit(motif)
        # ⭐ TOUT LE RESTE EST UN REFUS DU BOÎTIER, et c'est délibérément UNE seule
        #    branche : `device_mismatch`, `device_revoked`, un motif inconnu, un corps
        #    illisible. Une branche dédiée aux deux premiers rendait la même exception
        #    que ce défaut — donc aucune mutation ne pouvait détecter sa disparition.
        #
        # ⚠️ Le défaut est de CE côté-ci, et c'est le point qui compte : on ne doit
        #    SURTOUT pas faire croire à la personne que ça vient d'elle.
        return BoitierRefuse(motif or "403 sans motif")
    if statut == 400:
        return ContratRompu(motif or "400")
    return CloudInjoignable(f"ben-api a répondu {statut}")
