#!/usr/bin/env python3
"""
ben_certd — le boîtier entretient son propre certificat.

Côté boîtier → docs/pki-renouvellement.md   (ce dépôt est PUBLIC)

⚠️ La POLITIQUE — quand un certificat est jugé à remplacer, qui signe, comment on
révoque — appartient au serveur et n'est pas décrite ici. Le boîtier ne la
connaît pas : il se présente, on lui dit quoi faire.

    réveil quotidien (+ gigue) → GET :8444/ → une directive → on obéit.

═══ POURQUOI UN SERVICE À PART, ET PAS DANS LE PUBLISHER ═════════════════════

Une bascule de certificat exige de REDÉMARRER le publisher. Du code qui
redémarre son propre processus au milieu d'une opération, c'est la recette d'un
état à moitié appliqué.

    ben-publisher   pousse les mesures     ← ne doit JAMAIS être bloqué par la PKI
    ben-certd       gère l'identité        ← peut échouer sans conséquence

═══ CE QUI COMPTE VRAIMENT ═══════════════════════════════════════════════════

1. ⭐ TOUT ÉCHEC LAISSE LE BOÎTIER EXACTEMENT DANS L'ÉTAT OÙ IL ÉTAIT.
   L'ancien certificat reste valide jusqu'à son échéance : il n'y a JAMAIS
   urgence à basculer. C'est ce qui rend l'opération sans risque — donc ce qui
   interdit de la bâcler.

2. 🚨 ON ESSAIE LE NOUVEAU CERTIFICAT AVANT DE REMPLACER L'ANCIEN.
   Une poignée de main mTLS complète sur :8443, avec le certificat candidat,
   pendant que l'ancien est toujours en place. On ne DÉDUIT plus qu'il marchera :
   on l'a UTILISÉ. Ça coûte une connexion TLS et supprime le seul scénario
   catastrophique — un boîtier muet ne peut même plus signaler qu'il est cassé.

3. 🚨 LE CONTRÔLE QUI ÉVITE LA BRIQUE : clé publique(nouveau) == device.key.
   Un certificat qui ne correspond pas à la clé privée locale rend le boîtier
   définitivement muet, sur un appareil potentiellement injoignable.

4. ⚠️ ÉCRITURE ATOMIQUE (`os.replace`). Une coupure pendant l'écriture laisserait
   sinon un PEM tronqué — donc un boîtier sans identité lisible.

5. Le boîtier NE SE SOUVIENT DE RIEN. L'état vit chez ben-api : s'il tenait le
   sien, les deux pourraient diverger — et ce serait toujours le boîtier qui
   aurait tort, sans moyen de le savoir.

Tourne en `ben`. La SEULE opération privilégiée est le redémarrage du publisher.
"""
from __future__ import annotations

import http.client
import json
import logging
import os
import random
import re
import ssl
import subprocess
import sys
import tempfile
import time

API_HOST = os.environ.get("BEN_API_HOST", "api.benpilote.fr")
PKI_PORT = int(os.environ.get("BEN_PKI_PORT", "8444"))
API_PORT = int(os.environ.get("BEN_API_PORT", "8443"))
CERT_DIR = os.environ.get("BEN_CERT_DIR", "/etc/ben-firmware/certs")

CRT = f"{CERT_DIR}/device.crt"
KEY = f"{CERT_DIR}/device.key"
CA = f"{CERT_DIR}/root-ca.crt"
CRT_NEW = f"{CERT_DIR}/device.crt.new"
KEY_NEW = f"{CERT_DIR}/device.new.key"

# Un pointage par jour suffit : la politique tolère 90 jours de retard avant
# l'échéance (cf. §4.1). ⭐ La GIGUE est pleine, pas un intervalle fixe : sinon
# les sept boîtiers, redémarrés par la même OTA, pointeraient à la même seconde
# et se resynchroniseraient à chaque échec. Tirer DANS l'intervalle disperse ;
# attendre l'intervalle synchronise. (Même leçon que le publisher, point 3.)
PERIODE = float(os.environ.get("BEN_CERTD_PERIODE", "86400"))

log = logging.getLogger("ben-certd")


# ── Outils ────────────────────────────────────────────────────────────────────

def openssl(*args: str, entree: bytes | None = None) -> bytes:
    """openssl, en échouant fort. ⚠️ `check=True` : un contrôle qui avale son
    erreur est pire que pas de contrôle."""
    return subprocess.run(["openssl", *args], input=entree,
                          capture_output=True, check=True).stdout


def device_id() -> str:
    """Le CN de NOTRE certificat. ⭐ Source de vérité : le certificat lui-même,
    pas `device.json` — c'est lui qui fait foi auprès du serveur."""
    txt = openssl("x509", "-in", CRT, "-noout", "-subject").decode()
    m = re.search(r"CN\s*=\s*([A-Za-z0-9-]+)", txt)
    if not m:
        raise RuntimeError(f"CN illisible dans {CRT}")
    return m.group(1)


def empreinte_cle_publique(chemin: str, depuis_cle_privee: bool = False) -> bytes:
    """SHA-256 de la clé PUBLIQUE — extraite d'un certificat ou d'une clé privée.

    ⭐ C'est ce qui permet de comparer un certificat reçu à la clé qu'on détient,
    sans jamais manipuler le secret lui-même.
    """
    if depuis_cle_privee:
        pub = openssl("pkey", "-in", chemin, "-pubout")
    else:
        pub = openssl("x509", "-in", chemin, "-pubkey", "-noout")
    return openssl("dgst", "-sha256", entree=pub)


def ecrire_atomique(chemin: str, contenu: str, mode: int = 0o644) -> None:
    """⚠️ Fichier temporaire PUIS `os.replace`. Une coupure de courant pendant
    l'écriture laisserait sinon un PEM tronqué — un boîtier sans identité."""
    d = os.path.dirname(chemin)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".certd-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(contenu)
        os.chmod(tmp, mode)
        os.replace(tmp, chemin)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── Le canal PKI ──────────────────────────────────────────────────────────────

def contexte(crt: str = CRT, key: str = KEY) -> ssl.SSLContext:
    """Authentification MUTUELLE, complète — comme le publisher.

    🚨 Ni `check_hostname = False` ni `CERT_NONE` : si le serveur est rejeté, on
    corrige le certificat, on ne désactive jamais la vérification.
    """
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=CA)
    ctx.load_cert_chain(crt, key)
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def pointer() -> tuple[int, dict]:
    """GET / — « me voici ».

    ⭐ Aucun corps : le certificat est DÉJÀ dans la poignée de main. C'est aussi
    l'ACQUITTEMENT d'une bascule précédente — le serveur voit le nouveau
    certificat et ferme la tâche, sans qu'on ait rien à lui dire.
    """
    c = http.client.HTTPSConnection(API_HOST, PKI_PORT, context=contexte(), timeout=30)
    try:
        c.request("GET", "/")
        r = c.getresponse()
        corps = r.read()
        if r.status == 204:
            return 204, {}
        return r.status, (json.loads(corps) if corps else {})
    finally:
        c.close()


def deposer_csr(csr_pem: str) -> int:
    c = http.client.HTTPSConnection(API_HOST, PKI_PORT, context=contexte(), timeout=30)
    try:
        c.request("POST", "/", body=csr_pem.encode(),
                  headers={"Content-Type": "application/x-pem-file"})
        r = c.getresponse()
        r.read()
        return r.status
    finally:
        c.close()


def fabriquer_csr(cle: str, cn: str) -> str:
    """Un CSR : clé PUBLIQUE + signature prouvant qu'on détient la privée.

    ⭐ PUBLIC par construction — aucun secret n'en sort jamais.
    ⚠️ Il n'est PAS persisté : il se refabrique à volonté depuis la clé. Le poser
       sur disque créerait un fichier de plus à nettoyer, donc à oublier.
    """
    return openssl("req", "-new", "-key", cle, "-subj", f"/CN={cn}/O=BEN").decode()


# ── Les gardes ────────────────────────────────────────────────────────────────

def controler(candidat: str, cle: str, cn: str) -> str | None:
    """Renvoie le motif du REFUS, ou None si le certificat est acceptable.

    🚨 C'est ici qu'on évite de briquer un boîtier. Chaque garde correspond à une
    panne précise, et l'ordre importe : on va du moins cher au plus cher.
    """
    # ⚠️ L'EXPIRATION D'ABORD, et l'ordre n'est pas cosmétique : `openssl verify`
    #    contrôle les dates DANS la validation de chaîne. Placée après, cette
    #    vérification ne serait JAMAIS atteinte, et un certificat expiré serait
    #    journalisé « chaîne invalide » — on chercherait un problème de CA à 3 h
    #    du matin alors que c'est une date. Trouvé par le banc, pas par relecture.
    try:
        openssl("x509", "-in", candidat, "-noout", "-checkend", "0")
    except subprocess.CalledProcessError:
        return "certificat déjà expiré"

    try:
        openssl("verify", "-CAfile", CA, candidat)
    except subprocess.CalledProcessError:
        return "chaîne invalide contre root-ca.crt"

    # 🚨 LE contrôle qui compte. Un certificat qui ne correspond pas à la clé
    #    privée locale rend le boîtier MUET — et sans mTLS, il ne peut même plus
    #    signaler qu'il est cassé, sur un appareil potentiellement injoignable.
    if empreinte_cle_publique(candidat) != empreinte_cle_publique(cle, True):
        return "la clé publique ne correspond pas à la clé privée locale"

    txt = openssl("x509", "-in", candidat, "-noout", "-subject").decode()
    if not re.search(rf"CN\s*=\s*{re.escape(cn)}\b", txt):
        return f"CN différent de {cn}"

    return None


def essayer_sur_8443(candidat: str, cle: str) -> str | None:
    """⭐⭐ LE GARDE QUI VAUT LES QUATRE AUTRES.

    Une poignée de main mTLS COMPLÈTE avec le certificat candidat, pendant que
    l'ancien est toujours en place. On ne déduit plus qu'il marchera : ON L'A
    UTILISÉ.

    ⚠️ Si elle échoue, on n'a rien remplacé — donc rien n'est cassé.
    """
    try:
        ctx = contexte(candidat, cle)
        c = http.client.HTTPSConnection(API_HOST, API_PORT, context=ctx, timeout=30)
        try:
            c.request("GET", "/health")
            c.getresponse().read()
        finally:
            c.close()
        return None
    except Exception as e:  # noqa: BLE001
        return f"poignée de main refusée sur :{API_PORT} — {type(e).__name__}: {e}"


# ── La bascule ────────────────────────────────────────────────────────────────

def basculer(candidat: str, cle_neuve: str | None) -> None:
    """Remplace, en gardant l'ancien de côté, puis redémarre le publisher.

    ⚠️ L'ordre importe : la CLÉ d'abord, le certificat ensuite. À l'instant
    intermédiaire, une clé neuve avec l'ancien certificat ne valide pas — mais
    cet instant dure quelques millisecondes, et le publisher ne se reconnecte
    qu'au redémarrage qui suit.
    """
    horo = time.strftime("%Y%m%d-%H%M%S")
    if cle_neuve:
        os.replace(KEY, f"{KEY}.bak-{horo}")
        os.replace(cle_neuve, KEY)
        os.chmod(KEY, 0o600)
    os.replace(CRT, f"{CRT}.bak-{horo}")
    os.replace(candidat, CRT)
    os.chmod(CRT, 0o644)
    log.warning("certificat remplacé (ancien conservé en .bak-%s)", horo)

    # Le publisher tient une connexion TLS PERSISTANTE ouverte avec l'ancien
    # certificat : sans redémarrage, il continuerait de l'utiliser jusqu'à ce que
    # le serveur la ferme.
    try:
        subprocess.run(["sudo", "-n", "systemctl", "restart", "ben-publisher"],
                       check=True, capture_output=True, timeout=60)
        log.info("ben-publisher redémarré")
    except Exception as e:  # noqa: BLE001
        # ⚠️ Non fatal : le certificat est en place ET PROUVÉ (essai sur :8443).
        #    `Restart=always` relèvera le publisher de toute façon.
        log.error("redémarrage du publisher impossible : %s", e)


# ── Un tour ───────────────────────────────────────────────────────────────────

def un_tour() -> None:
    cn = device_id()
    statut, rep = pointer()

    if statut == 204:
        log.debug("conforme, rien à faire")
        return
    if statut == 403:
        # 🚨 Journaliser FORT, mais CONTINUER à pointer : c'est le seul moyen
        #    d'apprendre qu'on a été dé-révoqué.
        log.error("REFUSÉ par ben-api (%s) — ce boîtier est révoqué", rep.get("error"))
        return
    if statut != 200:
        log.warning("réponse inattendue %s : %s", statut, rep)
        return

    directive = rep.get("directive", "")
    raison = rep.get("reason", "")

    if directive == "pending":
        # ⭐ Ne demande rien de plus. Mais le JOURNAL du boîtier dit pourquoi il
        #    n'est pas conforme — précieux quand on est en SSH dessus, donc quand
        #    on a le moins de contexte.
        log.info("CSR déposé, en attente de signature — %s", raison)
        return

    if directive in ("csr", "csr+newkey"):
        if directive == "csr+newkey":
            # ⚠️ Rare et délibéré : change l'empreinte, donc réveille
            #    l'épinglage de toutes les apps. Jamais automatique.
            log.warning("génération d'une clé NEUVE demandée")
            openssl("ecparam", "-genkey", "-name", "prime256v1", "-out", KEY_NEW)
            os.chmod(KEY_NEW, 0o600)
            cle = KEY_NEW
        else:
            cle = KEY
        st = deposer_csr(fabriquer_csr(cle, cn))
        log.info("CSR déposé (%s) — motif : %s", st, raison)
        return

    if directive == "cert":
        pem = rep.get("cert", "")
        if not pem:
            log.warning("directive « cert » sans certificat")
            return
        ecrire_atomique(CRT_NEW, pem)
        cle = KEY_NEW if os.path.exists(KEY_NEW) else KEY

        refus = controler(CRT_NEW, cle, cn)
        if refus is None:
            refus = essayer_sur_8443(CRT_NEW, cle)
        if refus:
            # 🚨 On garde l'ancien. La tâche reste `armed` côté serveur, donc
            #    VISIBLE dans la file : « ce boîtier a téléchargé et refusé ».
            #    C'est ce qu'un simple fichier effacé au téléchargement ne
            #    pourrait pas dire.
            log.error("certificat REFUSÉ, ancien conservé : %s", refus)
            os.unlink(CRT_NEW)
            return

        basculer(CRT_NEW, KEY_NEW if cle == KEY_NEW else None)

        # ⭐ Acquitter TOUT DE SUITE, sans attendre le tour suivant.
        #
        # 🚨 Sinon la tâche resterait `armed` pendant 12 à 24 h, et l'état
        #    `armed` PORTE UN SENS : c'est lui qui signale « ce boîtier a
        #    téléchargé et REFUSÉ » (voir juste au-dessus). Sans cet
        #    acquittement, un succès et un refus se ressemblent toute une
        #    journée — le dispositif brouillerait son propre signal par son
        #    chemin NOMINAL, et une bascule de parc deviendrait illisible.
        #
        # ⚠️ Aucun contexte SSL n'est gardé en mémoire : `pointer()` rebâtit le
        #    sien à chaque appel, donc celui-ci part bien avec le certificat
        #    NEUF. C'est la raison pour laquelle certd, contrairement au
        #    publisher, ne met JAMAIS son certificat en cache — c'est la chose
        #    qu'il modifie.
        #
        # Un échec ici n'est pas grave : le tour suivant acquittera.
        try:
            statut, _ = pointer()
            log.info("acquitté auprès de ben-api (%s)", statut)
        except Exception as e:  # noqa: BLE001
            log.info("acquittement remis au prochain tour : %s", e)
        return

    log.warning("directive inconnue : %r", directive)


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("BEN_CERTD_LOG", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s")
    log.info("ben-certd démarre — %s:%s, période %.0f s", API_HOST, PKI_PORT, PERIODE)

    while True:
        try:
            un_tour()
        except ssl.SSLError as e:
            log.warning("TLS : %s", e)
        except OSError as e:
            # ⭐ Cas NORMAL d'un boîtier hors ligne. Pas un échec à signaler.
            log.debug("réseau indisponible : %s", e)
        except Exception as e:  # noqa: BLE001
            log.exception("tour interrompu : %s", e)
        # ⭐ Gigue PLEINE, pas un intervalle fixe (cf. l'en-tête).
        time.sleep(random.uniform(PERIODE / 2, PERIODE))


if __name__ == "__main__":
    sys.exit(main())
