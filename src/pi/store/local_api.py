#!/usr/bin/env python3
"""
local_api.py — API HTTP locale BEN (lecture seule) lue par l'app Flutter sur le LAN.

Sert la conso stockée par les readers dans `measurements.db` (cf. db.py).
L'app récupère l'IP du device au provisioning (statut `connected:<ip>`) puis
tape cette API sur le port 8087.

Endpoints :
  GET /ping
      → {"ben":true} — sonde de présence ultra-light, AUCUNE lecture
        (ni fichier ni base) ; pensée pour du polling régulier
  GET /health
      → {"deviceId","model","softwareVersion","db":true,
         "last_tic_ts":<ts dernière trame TIC ou null>,"now":...}
  GET /pdls
      → [{"pdl_index":0,"adco":"0217...","first_ts":...,"last_ts":...,"points":N}, ...]
        `adco` identifie le COMPTEUR ("" tant qu'aucun ADCO n'est lié).
  GET /live[?pdl_index=N]
      → dernière mesure (≤ ~30 s) par PDL (ou pour un PDL donné)
  GET /measurements?pdl_index=N[&since=ts&until=ts&limit=N]
      → points de conso ordonnés par ts croissant (since défaut = -24 h).
        Degrade-safe : si > limit points fins → agrégé à ~limit buckets
        (min/max/avg) au lieu d'être tronqué. `downsampled` indique le cas.
  GET /curve?pdl_index=N[&since=ts&until=ts&buckets=K]
      → courbe agrégée par bucket (min/max/avg, pics préservés). `buckets` =
        résolution voulue par l'app (défaut 500, max 2000). BRUT uniquement,
        INTACT (app courante). Ne sert PAS le rollup ni les bandes → cf. /chart.
  GET /chart?pdl_index=N[&since=ts&until=ts&buckets=K][&raw=1]
      → courbe RICHE prête-à-tracer : {points, tariff_bands, source}. Le serveur
        arbitre la source des points (rollup rapide sur vue large / brut au zoom) ;
        `tariff_bands` = zones HP/HC depuis le rollup (jamais un parcours de points).
        `raw=1` force le brut (haute fidélité, période bornée). Endpoint de la
        nouvelle app ; forme /curve + tariff_bands (additif). Cf. rollup-par-index.md.
  GET /consumption?pdl_index=N[&since=ts&until=ts]
      → conso PAR REGISTRE (Wh) sur la plage : {by_register:[{src_standard,
        index_id,wh}],total_wh}. Carry-forward server-side (MAX-MIN par registre,
        bi-mode). Contrat commun Pi/cloud ; l'app applique le prix (Σ wh×prix).
  GET /lora-link?pdl_index=N[&since=ts&limit=N]
      → qualité de réception LoRa (rssi/snr) — modèles pi0-lora

Stdlib only (zéro dépendance — idéal Pi Zero W). Read-only sur `measurements.db`
(WAL → lectures concurrentes pendant que le reader écrit).

═══ DEUX PORTS, DEUX RÉGIMES — ET C'EST LE CANAL QUI DURCIT ═══════════════════

  :8087  en clair   l'existant. `Authorization: Bearer` est RECONNU s'il est
                    présent, et RIEN N'EST EXIGÉ — ni jeton, ni rôle. TOUT y
                    reste faisable, y compris `/settings` et `/unprovision`.

                    🚨 C'EST ASSUMÉ, pas un oubli. Ce port n'a jamais rien
                    exigé ; y ajouter une règle casserait Home Assistant et les
                    apps déjà installées, chez des gens, sans rien protéger —
                    quiconque est sur le LAN peut de toute façon l'appeler
                    directement. On ne durcit pas un canal ouvert, on en offre
                    un autre.

  :8088  chiffré    le neuf. JETON EXIGÉ sur toutes les routes sauf `/claim`,
                    et RÔLE `owner` EXIGÉ sur les écritures (`/settings`,
                    `/unprovision`). `/health` y ANNONCE le rôle du porteur.

⭐ Il n'y a donc PAS de « on exigera le jeton plus tard, sur preuve que plus
personne n'appelle sans ». Ce jour-là n'arrive jamais tout seul, et il faudrait
l'organiser. Ici l'exigence naît AVEC le port : une app qui parle :8088 a
forcément revendiqué, c'est impossible autrement. Aucune population à migrer,
aucune date à tenir.

Cf. `docs/chantier-acces-multi-utilisateur.md`.
"""

import http.client
import json
import os
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import access
import db
import levels
import settings

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))  # src/pi
import capabilities as caps  # noqa: E402  (source de vérité « capability → services »)

HOST = "0.0.0.0"
PORT = 8087
DEVICE_JSON = "/etc/ben-firmware/device.json"

# ── L'écoute CHIFFRÉE, en plus — jamais à la place ────────────────────────────
#
# ⭐ :8087 RESTE EN CLAIR, et ce n'est pas une étape transitoire qu'on oublierait
#    de finir. Home Assistant refuse un certificat présenté sur une IP (le SAN
#    porte `DNS:ben-0001`, pas une adresse) et les versions de l'app déjà
#    installées ne connaissent que le port en clair. Couper :8087 casserait les
#    deux, chez des gens, sans prévenir.
#
#    On l'enlèvera sur PREUVE que plus personne n'y appelle — jamais sur une
#    date. Même règle que l'en-tête `Authorization` ci-dessus.
#
# ⚠️ Le SAN du certificat est `DNS:<device-id>`, JAMAIS une IP : une IP change au
#    gré du DHCP, et un certificat ne se réémet pas à chaque bail. C'est donc à
#    l'app de mettre le NOM dans l'URL et l'IP dans la socket — le nom n'a pas
#    besoin de résoudre, il doit seulement correspondre.
PORT_TLS = 8088
CERT_DIR = os.environ.get("BEN_CERT_DIR", "/etc/ben-firmware/certs")

# Le cloud, pour le RELAIS de /claim uniquement (mêmes valeurs que le publisher).
API_HOST = os.environ.get("BEN_API_HOST", "api.benpilote.fr")
API_PORT = int(os.environ.get("BEN_API_PORT", "8443"))
DEFAULT_WINDOW_SEC = 24 * 3600
MAX_LIMIT = 10000
DEFAULT_CURVE_BUCKETS = 500   # points servis par défaut (≈ largeur écran)
MAX_CURVE_BUCKETS = 2000      # plafond : au-delà, inutile (densité > pixels)
DEFAULT_MAXVA = 9000          # échelle jauge par défaut (≈ 45 A × 230 V) tant qu'aucun abonnement lu


def _goodbye_flash() -> None:
    """« Au revoir » : 3 flashs VIOLETS sur la LED RGB avant l'extinction (désappairage).
    LED à cathode commune, R=GPIO12 / G=GPIO13 / B=GPIO16 → violet = R+B (12+16), G éteint.
    Les readers (qui tiennent ces pins) doivent avoir été stoppés juste avant. Best-effort :
    jamais bloquant, une exception (pins occupés, GPIO indispo) est simplement loguée."""
    try:
        import RPi.GPIO as GPIO
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        for pin in (12, 13, 16):
            GPIO.setup(pin, GPIO.OUT)
            GPIO.output(pin, GPIO.LOW)
        for _ in range(3):
            GPIO.output(12, GPIO.HIGH); GPIO.output(16, GPIO.HIGH)   # violet = rouge + bleu
            time.sleep(0.18)
            GPIO.output(12, GPIO.LOW); GPIO.output(16, GPIO.LOW)
            time.sleep(0.18)
        GPIO.cleanup([12, 13, 16])
        print("[unprovision] au revoir (3 flashs violets)", flush=True)
    except Exception as e:
        print(f"[unprovision] flash « au revoir » ignoré: {e}", flush=True)


def _reader_units() -> list:
    """Les units de mesure de CE boîtier, dérivées des capabilities.

    Utilisé par le désappairage, où l'asymétrie des erreurs est nette : en arrêter
    trop ne coûte rien (le boîtier s'éteint juste après), en arrêter trop peu laisse
    la LED tenue et la base ouverte pendant le wipe. D'où le repli sur l'UNION de tous
    les services connus si les capabilities sont illisibles — jamais sur une liste
    figée de noms, qui est précisément ce qui avait rendu ce stop inopérant.
    """
    try:
        declared = caps.capabilities()
        units = [s for cap in declared for s in caps.services_for(cap)]
        if units:
            return list(dict.fromkeys(units))
        print("[unprovision] aucune capability déclarée — arrêt de TOUS les lecteurs connus",
              flush=True)
    except Exception as e:
        print(f"[unprovision] capabilities illisibles ({e}) — arrêt de TOUS les lecteurs connus",
              flush=True)
    return list(dict.fromkeys(s for v in caps.CAP_SERVICES.values() for s in v))


def _device_info() -> dict:
    try:
        with open(DEVICE_JSON) as f:
            d = json.load(f)
        info = {k: d.get(k) for k in ("deviceId", "model", "softwareVersion")}
    except (FileNotFoundError, json.JSONDecodeError):
        info = {"deviceId": None, "model": None, "softwareVersion": None}
    # Date de la dernière MAJ firmware (epoch s) = mtime de device.json : ce
    # fichier n'est réécrit qu'à un changement de version (OTA) ou au
    # provisioning. Pas de champ stocké, on lit la métadonnée filesystem.
    try:
        info["lastUpdateTs"] = int(os.path.getmtime(DEVICE_JSON))
    except OSError:
        info["lastUpdateTs"] = None
    return info


# ── Le frein de /claim ────────────────────────────────────────────────────────
#
# 🚨 /claim est NON AUTHENTIFIÉ par construction — le demandeur n'a pas encore de
#    jeton, c'est tout l'objet de l'appel — et il déclenche une poignée de main
#    TLS SORTANTE vers ben-api. Sans frein, n'importe qui sur le WiFi du foyer
#    transforme le boîtier en amplificateur : il martèle le cloud, et sur un Pi
#    Zero les poignées de main RSA le rendent inutilisable au passage.
#
# ⭐ Deux bornes qui suffisent, parce qu'elles collent à l'usage réel : une
#    revendication se fait UNE FOIS PAR TÉLÉPHONE. Une seule à la fois, et pas
#    plus d'une toutes les deux secondes. Un utilisateur légitime ne les voit
#    jamais ; un script les prend en pleine face.
#
# ⚠️ Volontairement EN MÉMOIRE, pas en base : c'est un frein, pas un journal. Le
#    perdre à un redémarrage est sans conséquence.
_CLAIM_VERROU = threading.Lock()
_CLAIM_INTERVALLE_SEC = 2.0
_claim_dernier = 0.0


class _CloudRefuse(Exception):
    """ben-api a répondu que l'identité n'est pas valable (401)."""


class _CloudInjoignable(Exception):
    """Réseau, serveur, certificat — tout ce qui n'est pas un verdict."""


def _demander_au_cloud(jeton_firebase: str) -> tuple[str, str | None]:
    """« Qui est-ce, et qu'a-t-il ICI ? » — relayé à ben-api en mTLS.

    🚨 LE BOÎTIER NE VÉRIFIE PAS LE JETON, IL LE TRANSPORTE. Vérifier un JWT
    demande un cache de clés Google, une horloge fiable et de la crypto RSA —
    sur un Pi Zero SANS RTC. C'est `ben-api` qui sait déjà le faire.

    ⭐ Renvoie TOUJOURS l'uid quand l'identité est prouvée, et le rôle peut être
    None : « je sais qui tu es, tu n'as aucun droit ici ». Ce n'est pas une
    erreur — c'est le cas normal de quelqu'un qu'on vient d'inviter, et l'uid est
    précisément ce que le boîtier attendait pour pouvoir consommer l'invitation.

    ⚠️ Conséquence assumée : cloud injoignable ⇒ revendication impossible. C'est
    la contrepartie d'avoir sorti la crypto JWT du Pi Zero, pour une opération
    qu'on fait une fois par téléphone.
    """
    device_id = (_device_info() or {}).get("deviceId")
    if not device_id:
        raise _CloudInjoignable("device.json illisible")
    try:
        ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH,
                                         cafile=f"{CERT_DIR}/root-ca.crt")
        ctx.load_cert_chain(f"{CERT_DIR}/device.crt", f"{CERT_DIR}/device.key")
        conn = http.client.HTTPSConnection(API_HOST, API_PORT, context=ctx, timeout=20)
    except Exception as e:  # noqa: BLE001
        raise _CloudInjoignable(f"mTLS impossible : {e}") from e

    try:
        corps = json.dumps({"firebase_token": jeton_firebase}).encode()
        conn.request("POST", f"/api/devices/{device_id}/claim", body=corps,
                     headers={"Content-Type": "application/json"})
        r = conn.getresponse()
        brut = r.read()
        if r.status == 401:
            raise _CloudRefuse("jeton d'identité refusé")
        if r.status != 200:
            raise _CloudInjoignable(f"ben-api a répondu {r.status}")
        rep = json.loads(brut.decode("utf-8"))
    except (_CloudRefuse, _CloudInjoignable):
        raise
    except Exception as e:  # noqa: BLE001
        raise _CloudInjoignable(str(e)) from e
    finally:
        conn.close()

    uid = (rep.get("uid") or "").strip()
    if not uid:
        raise _CloudInjoignable("réponse sans uid")
    return uid, rep.get("role") or None


def _rows(conn, sql, params) -> list:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _int(qs, key, default=None):
    try:
        return int(qs[key][0])
    except (KeyError, ValueError, IndexError):
        return default


class Handler(BaseHTTPRequestHandler):
    # Silence le logging par défaut (sinon une ligne stderr par requête).
    def log_message(self, *args):  # noqa: D401
        pass

    def _send(self, payload, status=200, headers=None):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")  # app mobile LAN
        # L'app lit ces en-têtes depuis JS/Dart : sans exposition explicite, CORS les masque.
        self.send_header("Access-Control-Expose-Headers",
                         "X-Ben-Last-Event, X-Ben-Last-Event-Severity")
        for k, v in (headers or {}).items():
            if v is not None:
                self.send_header(k, str(v))
        self.end_headers()
        self.wfile.write(body)

    # ── Accès : ACCEPTÉ, JAMAIS EXIGÉ (0.9.16) ──────────────────────────────
    #
    # 🚨 Cette version ne REFUSE rien. Elle se contente de reconnaître un jeton
    #    quand l'app en présente un. C'est ce qui rend la livraison invisible :
    #    les apps du parc, qui n'en ont pas, continuent de fonctionner à
    #    l'identique, et Home Assistant aussi.
    #
    # L'exigence s'arme plus tard, sur PREUVE que plus personne n'appelle sans
    # jeton — jamais sur une date (cf. chantier, vague 5).
    def _role(self) -> str | None:
        """Le rôle porté par l'en-tête `Authorization: Bearer`, ou None.

        Ne lève jamais : une base d'accès illisible doit dégrader vers « aucun
        rôle », pas rendre l'API plus dangereuse que le défaut qu'elle corrige.
        """
        entete = self.headers.get("Authorization", "")
        if not entete.startswith("Bearer "):
            return None
        try:
            with access.session() as conn:
                return access.role_of(conn, entete[7:].strip())
        except Exception:  # noqa: BLE001
            return None

    # ── Le durcissement, c'est LE CANAL ───────────────────────────────────────
    #
    # ⭐ Pas de « on exigera le jeton plus tard, sur preuve que plus personne
    #    n'appelle sans ». Ce jour-là n'arrive jamais tout seul, et il faudrait
    #    l'organiser. Ici l'exigence naît AVEC le port chiffré :
    #
    #      :8087  en clair    l'existant — Home Assistant, apps déjà installées.
    #                         GELÉ, aucune exigence. On ne casse rien.
    #      :8088  chiffré     le neuf — jeton EXIGÉ dès le premier jour.
    #
    # ⭐ Une app qui parle :8088 a forcément revendiqué : c'est impossible
    #    autrement. Il n'y a donc aucune population à migrer, aucune date à
    #    tenir, aucun basculement à négocier.
    #
    # ⚠️ UNE SEULE exemption, et elle est structurelle : `/claim` est l'appel par
    #    lequel on OBTIENT le jeton. L'exiger là reviendrait à demander d'avoir
    #    déjà ce qu'on vient chercher.
    #
    # 🚨 Ne pas ajouter d'autre exemption « pour la commodité ». `/ping` et
    #    `/health` sont tentants — ils servent à sonder la joignabilité — mais
    #    ils restent servis EN CLAIR sur :8087, qui répond exactement à ce
    #    besoin. Une exemption de confort sur un canal dont l'intérêt est la
    #    rigueur, c'est le trou par lequel tout repasse.
    # ── Les routes qui MODIFIENT ou DÉTRUISENT ────────────────────────────────
    #
    # 🚨 Jusqu'ici `ben_role` était calculé à chaque requête et consulté NULLE
    #    PART : un `member` pouvait donc appeler `/unprovision`, c'est-à-dire
    #    effacer les mesures et éteindre le boîtier de quelqu'un d'autre. Les
    #    deux rôles étaient identiques en effet — seul leur nom différait.
    #
    # ⚠️ L'exigence vaut sur le canal CHIFFRÉ seulement. Sur :8087 il n'y a
    #    jamais eu ni jeton ni rôle ; y appliquer la règle refuserait `/settings`
    #    à tout le monde, au nom d'une protection que ce port ne peut pas offrir.
    # Ce qu'un membre ne peut pas faire, et pourquoi c'est bien ces trois-là :
    #
    #   /settings      la LUMINOSITÉ de la LED — elle est sur le mur de quelqu'un
    #   /unprovision   efface les mesures et éteint le boîtier
    #   /invitations   frappe un droit d'entrée pour un tiers
    #
    # ⭐ La LECTURE reste ouverte : ce garde n'est branché que sur `do_POST`.
    #    Voir le réglage ne fait de mal à personne, le changer si.
    #
    # ⚠️ LA RÈGLE PORTE SUR LA ROUTE, PAS SUR LE CHAMP. Aujourd'hui `/settings`
    #    ne transporte que `led_level`, donc les deux coïncident. Le jour où il
    #    gagne une préférence qu'un membre devrait légitimement poser — un seuil
    #    d'alerte, un affichage — elle se prendra un 403 sans raison lisible.
    #    À ce moment-là, il faudra discriminer par CHAMP, pas par route.
    # 🚨 CE QUE `:8087` A LE DROIT DE SERVIR. Tout le reste y est ABSENT.
    #
    #    ⭐ LISTE INVERSÉE, ET C'EST TOUT L'INTÉRÊT. La première version listait
    #       les routes réservées au canal chiffré — donc ce qu'on OUBLIAIT d'y
    #       inscrire était servi partout, en silence. C'est exactement ainsi que
    #       `/access`, `/invitations` et `/tokens/integration` se sont retrouvées
    #       ouvertes sans jeton sur le port clair (mesuré le 24/09 sur ben-0001 :
    #       `/access` rendait uid ET prénom, `/tokens/integration` un jeton
    #       `member` permanent, `/access/revoke` coupait l'owner sans recours).
    #
    #    ⭐ Ici, un oubli penche du BON CÔTÉ : une route nouvelle est absente du
    #       canal clair par construction, ce qui se voit au premier essai — au
    #       lieu de s'ouvrir sans bruit. Et cette liste-ci ne bougera jamais,
    #       puisque `:8087` est GELÉ par décision.
    #
    #    ⚠️ 404 et non 426 : du point de vue de ce port, ces routes n'existent
    #       réellement pas. Un 426 confirmerait à qui balaie le port clair qu'il
    #       y a une surface d'administration ailleurs. Le journal, lui, le dit.
    #       (`/claim` fait exception et répond 426 LUI-MÊME : l'app a besoin de
    #       l'indication pour basculer.)
    ROUTES_CLAIR = ("/ping", "/health", "/pdls", "/live", "/measurements",
                    "/curve", "/chart", "/consumption", "/registers",
                    "/lora-link", "/events", "/settings", "/unprovision",
                    "/claim")

    def _hors_canal_clair(self, path: str) -> bool:
        if getattr(self.server, "chiffre", False):
            return False
        if path in self.ROUTES_CLAIR:
            return False
        print(f"[:{PORT}] {path} refusée — cette route n'existe que sur "
              f"le canal chiffré :{PORT_TLS}")
        self._send({"error": "not_found"}, 404)
        return True

    ECRITURES = ("/settings", "/unprovision", "/invitations",
                 "/access/revoke", "/tokens/revoke", "/tokens/integration",
                 "/access/rename")
    # ⚠️ Une LECTURE réservée au propriétaire : savoir QUI d'autre a accès est
    #    une vue d'administration. Elle expose les identifiants des autres
    #    habitants — ça regarde celui qui les a invités.
    LECTURES_OWNER = ("/access",)

    def _exige_owner(self, path: str, methode: str = "POST") -> bool:
        if not getattr(self.server, "chiffre", False):
            return False
        reserve = self.ECRITURES if methode == "POST" else self.LECTURES_OWNER
        if path not in reserve:
            return False
        if self.ben_role != access.ROLE_OWNER:
            self._send({"error": "owner_required",
                        "detail": "seul le propriétaire peut faire cela"}, 403)
            return True
        return False

    def _exige_jeton(self, path: str) -> bool:
        if not getattr(self.server, "chiffre", False):
            return False
        # ⭐ DEUX ROUTES SANS JETON, ET SEULEMENT DEUX.
        #
        #    `/claim` : il faut bien un chemin pour en OBTENIR un.
        #
        #    `/ping`  : il rend `{"ben": true}` et RIEN d'autre — pas de lecture,
        #    pas de base, pas d'identité. Le mettre derrière un jeton ne protège
        #    donc rien : le `deviceId` qu'il pourrait trahir est déjà diffusé en
        #    clair sur le réseau local par mDNS. En revanche, l'exiger casse le
        #    seul geste qu'une app doit pouvoir faire AVANT d'avoir le moindre
        #    droit : vérifier qu'un BEN répond à cette adresse. Sans ça, « ce
        #    boîtier ne répond pas » et « je ne suis pas invité » deviennent
        #    indiscernables — et c'est la deuxième qu'il faut dire.
        if path in ("/claim", "/ping"):
            return False
        if self.ben_role is None:
            self._send({"error": "token_required",
                        "detail": "POST /claim pour obtenir un jeton"}, 401)
            return True
        return False

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path.rstrip("/") or "/"
        qs = parse_qs(url.query)
        self.ben_role = self._role()
        if self._hors_canal_clair(path) or self._exige_jeton(path) \
                or self._exige_owner(path, "GET"):
            return
        try:
            if path == "/access":
                return self._lister_acces()
            if path == "/ping":
                return self._ping()
            if path == "/health":
                return self._health()
            if path == "/pdls":
                return self._pdls(qs)
            if path == "/live":
                return self._live(qs)
            if path == "/measurements":
                return self._measurements(qs)
            if path == "/curve":
                return self._curve(qs)
            if path == "/chart":
                return self._chart(qs)
            if path == "/consumption":
                return self._consumption(qs)
            if path == "/registers":
                return self._registers(qs)
            if path == "/lora-link":
                return self._lora_link(qs)
            if path == "/events":
                return self._events(qs)
            if path == "/settings":
                return self._send(settings.load())
            return self._send({"error": "not_found"}, 404)
        except sqlite3.OperationalError:
            # base pas encore créée (aucune trame écrite) → réponse dégradée
            return self._send({"error": "db_unavailable"}, 503)
        except Exception as e:  # noqa: BLE001
            return self._send({"error": "internal", "detail": str(e)}, 500)

    def do_POST(self):
        url = urlparse(self.path)
        path = url.path.rstrip("/") or "/"
        self.ben_role = self._role()
        if self._hors_canal_clair(path) or self._exige_jeton(path) \
                or self._exige_owner(path):
            return
        if path == "/claim":
            return self._claim()
        if path == "/invitations":
            return self._inviter()
        if path == "/access/revoke":
            return self._revoquer_personne()
        if path == "/tokens/revoke":
            return self._revoquer_appareil()
        if path == "/tokens/integration":
            return self._creer_integration()
        if path == "/access/rename":
            return self._renommer_personne()
        if path == "/unprovision":
            return self._unprovision(parse_qs(url.query))
        if path != "/settings":
            return self._send({"error": "not_found"}, 404)
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(body, dict):
                return self._send({"error": "invalid_body"}, 400)
            # settings.save valide/clamp (ex. led_level 0..5) et persiste.
            return self._send(settings.save(body))
        except (ValueError, json.JSONDecodeError):
            return self._send({"error": "invalid_json"}, 400)
        except Exception as e:  # noqa: BLE001
            return self._send({"error": "internal", "detail": str(e)}, 500)

    # --- handlers -----------------------------------------------------------
    def _ping(self):
        # Présence ultra-light : AUCUNE lecture (ni fichier ni base). Pensé pour
        # du polling régulier (voyant de joignabilité, heartbeat de l'app).
        self._send({"ben": True})

    def _health(self):
        info = _device_info()
        db_ok = True
        last_tic = None
        try:
            with db.connect(read_only=True) as conn:
        # ⚠️ NE JAMAIS interroger `measurements` sans filtrer sur `pdl_index` :
        # l'index est (pdl_index, ts, papp), donc une requête non filtrée y est
        # AVEUGLE et balaye toute la table. Mesuré sur ben-0001 (3,1 M lignes) :
        # MAX(ts) 15,6 s sans filtre contre 0,00 s avec.
                row = conn.execute(
                    "SELECT MAX(ts) FROM measurements WHERE pdl_index=?", (0,)).fetchone()
                last_tic = row[0] if row else None
        except sqlite3.OperationalError:
            db_ok = False
        # ⭐ Le rôle est ANNONCÉ, pas stocké par l'app. Un rôle mémorisé à la
        #    revendication PÉRIME : rétrograder quelqu'un ne changerait rien sur
        #    son téléphone, qui a déjà son jeton et ne revendiquera plus jamais.
        #    Ici il est relu à chaque appel, donc toujours juste.
        #
        # ⚠️ `null` sur :8087 — ce port ne connaît ni jeton ni rôle, et prétendre
        #    le contraire ferait croire à l'app qu'elle sait quelque chose.
        self._send({**info, "db": db_ok, "last_tic_ts": last_tic,
                    "role": self.ben_role,
                    "now": int(time.time())})

    def _claim(self):
        """POST /claim — une personne revendique un accès sur CE boîtier.

        🚨 CHIFFRÉ OBLIGATOIRE. C'est le seul endpoint qui DÉLIVRE un secret : le
        jeton part dans la réponse. En clair sur le WiFi du foyer, n'importe qui
        le capterait — et un jeton capté donne accès à la consommation du foyer.
        D'où le refus franc sur :8087 plutôt qu'une tolérance « le temps de la
        transition ».

        ⭐ DEUX MOITIÉS, ET AUCUNE NE SUFFIT SEULE. `ben-api` fournit le QUI
        (identité Google vérifiée) ; l'invitation fournit le QUOI (le rôle, décidé
        par l'owner au moment où il invite). Une invitation ne peut pas se donner
        d'identité, un jeton Firebase ne peut pas se donner de rôle.
        """
        if not getattr(self.server, "chiffre", False):
            return self._send({"error": "tls_required",
                               "detail": f"/claim n'est servi que sur :{PORT_TLS} (HTTPS)"}, 426)

        entete = self.headers.get("Authorization", "")
        if not entete.lower().startswith("bearer "):
            return self._send({"error": "missing_identity",
                               "detail": "Authorization: Bearer <ID token Firebase>"}, 401)
        jeton_firebase = entete[7:].strip()

        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = json.loads((self.rfile.read(length) if length else b"{}").decode() or "{}")
            if not isinstance(body, dict):
                raise ValueError
        except (ValueError, json.JSONDecodeError):
            return self._send({"error": "invalid_json"}, 400)
        label = str(body.get("label") or "")
        invitation = str(body.get("invitation") or "")
        # 🔒 Prénom d'affichage, STRICTEMENT LOCAL. Facultatif : sans lui la
        #    liste dira « Membre », ce qui marche — juste moins bien.
        nom = str(body.get("name") or "")

        # 🚨 Le frein, AVANT le moindre octet vers le cloud. Non bloquant : on
        #    refuse franchement plutôt que d'empiler des threads en attente, ce
        #    qui serait exactement la panne qu'on veut éviter.
        global _claim_dernier
        if not _CLAIM_VERROU.acquire(blocking=False):
            return self._send({"error": "busy", "detail": "revendication déjà en cours"}, 429)
        try:
            if time.monotonic() - _claim_dernier < _CLAIM_INTERVALLE_SEC:
                return self._send({"error": "too_many", "detail": "réessayer dans un instant"}, 429)
            _claim_dernier = time.monotonic()
            return self._claim_relais(jeton_firebase, label, invitation, nom)
        finally:
            _CLAIM_VERROU.release()

    def _claim_relais(self, jeton_firebase: str, label: str, invitation: str,
                      nom: str = ""):
        """La partie coûteuse : un aller-retour mTLS, puis la décision."""
        try:
            uid, role_cloud = _demander_au_cloud(jeton_firebase)
        except _CloudRefuse as e:
            print(f"[claim] identité refusée par ben-api : {e}")
            return self._send({"error": "bad_identity"}, 401)
        except _CloudInjoignable as e:
            # ⚠️ 503 et NON 500 : rien n'est cassé, le cloud n'est pas là.
            #    L'app doit proposer de réessayer, pas afficher une erreur.
            print(f"[claim] cloud injoignable : {e}")
            return self._send({"error": "cloud_unreachable",
                               "detail": "réessayer plus tard"}, 503)

        with access.session() as conn:
            # 🚨 LE DRAPEAU LOCAL L'EMPORTE SUR LE CLOUD. Le hello est quotidien :
            #    pendant 24 h après une révocation, ben-api répond encore l'ancien
            #    rôle. Faire confiance à sa réponse laisserait revenir quelqu'un
            #    qu'on vient de couper — sans invitation, et en silence.
            #
            # ⭐ Une réinvitation reste possible : l'invitation lève le drapeau
            #    (`grant`). Ce qui devient impossible, c'est le retour FURTIF.
            if access.est_revoquee(conn, uid) and not invitation:
                print(f"[claim] {uid} est révoqué ici — refusé malgré le cloud")
                return self._send({"error": "revoked"}, 403)

            if role_cloud and not access.est_revoquee(conn, uid):
                # ⚠️ Ligne 1 — UN DROIT EXISTANT L'EMPORTE, et l'invitation n'est
                #    PAS consommée. Claire déjà `member` qui réinstalle son app n'a
                #    besoin d'aucun QR ; et si on lui en présente un par mégarde,
                #    elle ne doit pas se retrouver rétrogradée. Changer le rôle de
                #    quelqu'un se fait par révocation puis réinvitation, jamais par
                #    effet de bord.
                jeton = access.mint(conn, uid=uid, label=label, role=role_cloud)
            else:
                # Lignes 2 et 3 — sans droit connu, seule une invitation valide
                # ouvre. `consume_invitation` renvoie None si elle est absente,
                # inconnue ou périmée : les trois cas se traitent pareil.
                jeton = access.consume_invitation(conn, invitation, uid=uid, label=label)
                if jeton is None:
                    print(f"[claim] aucun droit pour {uid} et pas d'invitation valable")
                    return self._send({"error": "no_access"}, 403)

            # 🚨 Élaguer APRÈS la frappe : une réinstallation emporte le coffre
            #    du téléphone et le force à se revendiquer, laissant derrière
            #    elle un jeton VALIDE que plus personne ne détient. Sans ça la
            #    liste se remplit de clés vivantes, toutes du même nom, sans
            #    qu'on puisse dire laquelle couper.
            if nom:
                # ⭐ Une revendication PAR INVITATION est un geste délibéré : la
                #    personne a vu le prénom proposé et l'a validé, il fait
                #    autorité. Une revendication automatique, elle, ne fait que
                #    combler un vide — sinon elle écraserait la correction du
                #    propriétaire au tour suivant.
                access.nommer(conn, uid, nom,
                              seulement_si_vide=not invitation)

            elagues = access.elaguer_doublons(conn, uid, label, garder=jeton)
            if elagues:
                print(f"[claim] {elagues} jeton(s) périmé(s) de {label!r} retiré(s)")

            # ⭐ Le rôle rendu est RELU dans la base, pas celui qu'on croit avoir
            #    écrit. Si `grant` a refusé une promotion (owner en écriture
            #    unique), la réponse dit la vérité plutôt que l'intention.
            role = access.role_of(conn, jeton)

        print(f"[claim] {uid} → rôle {role} (label={label or 'appareil'})")
        return self._send({"token": jeton, "role": role})

    def _inviter(self):
        """POST /invitations — le propriétaire frappe un code à communiquer.

        ⭐ UN CODE QU'ON DIT, PAS UN QR QU'ON SCANNE. Le scan viendra, avec la
        permission caméra ; mais une fonction qui EXIGE une permission disparaît
        pour qui la décline, et le QR n'est de toute façon pas ce qui rend
        l'invitation sûre : elle est consommée par `/claim` SUR LE BOÎTIER, par
        le LAN. L'invité doit donc déjà être sur le WiFi du foyer.
        
        🚨 Réservée au propriétaire (`_exige_owner`) et au canal chiffré
        (`_exige_jeton`) : un code d'invitation est un DROIT au porteur, il n'a
        rien à faire sur un canal que tout le LAN peut écouter.

        ⚠️ Le rôle est décidé ICI, par celui qui invite. L'invité ne le choisit
        jamais — sinon n'importe qui se frapperait `owner`.
        """
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = json.loads((self.rfile.read(length) if length else b"{}").decode() or "{}")
            if not isinstance(body, dict):
                raise ValueError
        except (ValueError, json.JSONDecodeError):
            return self._send({"error": "invalid_json"}, 400)

        role = str(body.get("role") or access.ROLE_MEMBER)
        # ⚠️ On n'invite PAS un second propriétaire : `grant` est en écriture
        #    unique et lèverait au moment où l'invité consomme le code. Refuser
        #    à la FRAPPE évite de remettre à quelqu'un un code condamné, qu'il
        #    découvrirait cassé chez lui, sans comprendre pourquoi.
        if role != access.ROLE_MEMBER:
            return self._send({"error": "bad_role",
                               "detail": "on n'invite qu'en tant que member"}, 400)

        with access.session() as conn:
            access.purge_expired_invitations(conn)
            code = access.create_invitation(conn, role=role)

        print(f"[invitation] code frappé pour un rôle {role}")
        # ⚠️ Le CLAIR n'est rendu qu'ici, une seule fois : seule son empreinte
        #    est stockée. Personne ne pourra le relire, pas même nous.
        return self._send({"code": access.formater_code(code),
                           "role": role,
                           "expires_in": access.INVITATION_TTL_SEC})

    def _corps(self):
        """Le corps JSON, ou None (la réponse d'erreur est déjà envoyée)."""
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = json.loads((self.rfile.read(length) if length else b"{}").decode() or "{}")
            if not isinstance(body, dict):
                raise ValueError
            return body
        except (ValueError, json.JSONDecodeError):
            self._send({"error": "invalid_json"}, 400)
            return None

    def _lister_acces(self):
        """GET /access — qui a accès, et avec quels appareils.

        ⭐ DEUX ÉTAGES, et c'est ce qui rend la révocation utilisable : une ligne
        par PERSONNE, une ligne par APPAREIL. « J'ai perdu mon téléphone » coupe
        un appareil ; « Claire est partie » coupe une personne et tous les siens.
        Ne proposer que le second obligerait à tout couper pour un téléphone
        égaré ; ne proposer que le premier laisserait Claire entrer.

        ⚠️ Aucune empreinte n'est exposée — ni de jeton, ni d'invitation. On dit
        QUI et QUOI, jamais COMMENT se faire passer pour eux.
        """
        with access.session() as conn:
            access.purge_expired_invitations(conn)
            personnes = [
                # 🔒 `nom` sort ICI et NULLE PART AILLEURS : cette route est
                #    réservée au propriétaire, sur son réseau local, pour son
                #    propre écran. Il ne part pas au hello — cf.
                #    `access.pour_le_hello`, dont le banc le vérifie.
                {"uid": a["uid"], "role": a["role"],
                 "revoked": a["revoked_ts"] is not None,
                 "updated_ts": a["updated_ts"], "nom": a["nom"]}
                for a in access.list_access(conn)
            ]
            # ⭐ « Cet appareil » : sans ce repère, retirer sa propre ligne
            #    donne un spectacle absurde — elle disparaît et revient aussitôt
            #    sous un autre numéro, puisque le téléphone se revendique. On
            #    préfère ne pas proposer le geste plutôt que de l'annuler.
            mien = access.id_of(conn, self._jeton_presente())
            appareils = [{**t, "self": t["id"] == mien}
                         for t in access.list_tokens(conn)]
        return self._send({"people": personnes, "devices": appareils})

    def _revoquer_personne(self):
        """POST /access/revoke — couper QUELQU'UN, et tous ses appareils."""
        body = self._corps()
        if body is None:
            return
        uid = str(body.get("uid") or "")
        if not uid:
            return self._send({"error": "missing_uid"}, 400)

        # 🚨 On ne se coupe pas soi-même. Un propriétaire qui se révoque laisse un
        #    boîtier SANS PROPRIÉTAIRE, et `grant` étant en écriture unique, il
        #    n'y a aucun chemin de retour — il faudrait désappairer.
        with access.session() as conn:
            jeton = self._jeton_presente()
            if self.ben_role == access.ROLE_OWNER \
                    and access.uid_of(conn, jeton) == uid:
                return self._send({"error": "self_revoke",
                                   "detail": "un propriétaire ne peut pas se révoquer"}, 400)
            n = access.revoke_person(conn, uid)
        print(f"[access] {uid} révoqué ({n} ligne(s))")
        return self._send({"revoked": uid, "rows": n})

    def _revoquer_appareil(self):
        """POST /tokens/revoke — couper UN appareil, sans toucher à la personne."""
        body = self._corps()
        if body is None:
            return
        try:
            token_id = int(body.get("id"))
        except (TypeError, ValueError):
            return self._send({"error": "missing_id"}, 400)
        with access.session() as conn:
            n = access.revoke_token(conn, token_id)
        print(f"[access] appareil {token_id} révoqué ({n})")
        return self._send({"revoked_device": token_id, "rows": n})

    def _renommer_personne(self):
        """POST /access/rename — mettre un prénom sur un uid. 🔒 Reste au boîtier.

        ⭐ Réservé au propriétaire, et ce n'est pas un excès de zèle : c'est SON
           écran. Sans ça, un membre pourrait se renommer « Propriétaire » ou
           prendre le prénom de quelqu'un d'autre dans une liste où le vrai
           identifiant n'est volontairement pas affiché.
        """
        body = self._corps()
        if body is None:
            return
        uid = str(body.get("uid") or "")
        if not uid:
            return self._send({"error": "missing_uid"}, 400)
        with access.session() as conn:
            if not access.nommer(conn, uid, str(body.get("name") or "")):
                return self._send({"error": "unknown_uid"}, 404)
        return self._send({"renamed": uid})

    def _creer_integration(self):
        """POST /tokens/integration — un jeton pour une MACHINE, pas un humain.

        Home Assistant, un script, un tableau de bord : ça ne se connecte pas à
        Google et ça n'a pas de téléphone à revendiquer. Le propriétaire frappe
        le jeton ici et le colle dans la configuration de l'outil.

        ⭐ `uid` reste NUL, et ce n'est pas un raccourci : une intégration
           n'appartient à PERSONNE. Si le propriétaire s'en va, le chauffe-eau
           piloté par Home Assistant ne doit pas s'arrêter avec lui. C'est
           exactement ce que le `LEFT JOIN` de `role_of` rend possible — et
           c'est pourquoi elle se révoque à la main, comme un appareil.

        ⚠️ Rôle `member`, jamais `owner`. Un jeton collé dans un fichier de
           configuration, en clair, sur une machine qu'on n'administre pas
           forcément, n'a aucune raison de pouvoir inviter ni désappairer.
        """
        body = self._corps()
        if body is None:
            return
        libelle = str(body.get("label") or "").strip()[:64]
        if not libelle:
            return self._send({"error": "missing_label",
                               "detail": "nommez l\'intégration pour la reconnaître plus tard"}, 400)
        with access.session() as conn:
            jeton = access.mint(conn, uid="", label=libelle,
                                role=access.ROLE_MEMBER)
        print(f"[access] jeton d'intégration frappé pour {libelle!r}")
        # Le CLAIR ne repassera jamais : il n'est stocké nulle part.
        return self._send({"token": jeton, "label": libelle,
                           "role": access.ROLE_MEMBER})

    def _jeton_presente(self) -> str:
        entete = self.headers.get("Authorization", "")
        return entete[7:].strip() if entete.lower().startswith("bearer ") else ""

    def _unprovision(self, qs):
        """Désappaire le boîtier : oublie le WiFi (→ provisioning BLE au prochain
        allumage), efface optionnellement les données (`?wipe=1`), puis S'ÉTEINT.

        Le boîtier s'éteint TOUJOURS (poweroff), avec ou sans wipe : il part hors
        tension ; quand l'utilisateur le rallume, l'absence de WiFi le fait
        démarrer en mode configuration (BLE).

        ORDRE CRITIQUE : on répond AVANT de couper le réseau, puis on fait le
        désappairage + reboot en ASYNCHRONE. Sinon supprimer la connexion WiFi
        tue le lien TCP et l'app ne reçoit jamais la réponse.
        Raccourci assumé (pas en prod) : AUCUNE auth. Garde l'identité (certs)."""
        wipe = (qs.get("wipe", ["0"])[0]).lower() in ("1", "true", "yes")
        print(f"[unprovision] requête reçue (wipe={wipe})", flush=True)
        # 1. Réponse immédiate — le réseau est encore là, l'app reçoit l'ack.
        self._send({"ok": True, "wipe": wipe, "rebooting": True})

        # 2. Désappairage + reboot DIFFÉRÉS (laisse la réponse HTTP partir).
        def _teardown():
            # Oublie TOUTES les connexions WiFi (ben-provisioned + éventuel profil
            # opérateur du golden) → repart en provisioning BLE au prochain boot.
            listing = subprocess.run(
                ["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"],
                capture_output=True, text=True)
            for line in listing.stdout.splitlines():
                name, _, ctype = line.partition(":")
                if ctype == "802-11-wireless" and name:
                    r = subprocess.run(
                        ["sudo", "nmcli", "connection", "delete", name],
                        capture_output=True, text=True)
                    print(f"[unprovision] delete '{name}' rc={r.returncode} "
                          f"{r.stderr.strip()}", flush=True)
            # Stop les readers TOUJOURS : (1) ils tiennent les pins de la LED RGB → il faut
            # les libérer pour le flash « au revoir » ci-dessous ; (2) si wipe, ça ferme la
            # base (WAL) AVANT le rm → wipe propre. On NE touche PAS ben-local-api (c'est lui
            # qui exécute _teardown). Service absent sur ce boîtier → ignoré.
            #
            # ⚠️ Cette liste venait des CAPABILITIES depuis la 0.9.12, et pas avant : elle
            # était figée sur « ben-tic-reader ben-lora-receiver », or le monolithe est
            # découpé en ben-radio + ben-telemetry depuis la 0.9.1. Sur tout boîtier LoRa
            # moderne ce stop n'arrêtait donc AUCUN lecteur : la LED restait tenue pendant
            # le flash d'au revoir, et surtout le wipe supprimait une base que
            # `ben-telemetry` gardait ouverte en WAL — elle se recréait dans la seconde.
            # Même défaut que le wipe illusoire du script de preshipping (corrigé le 15/08).
            subprocess.run(
                ["sudo", "systemctl", "stop", *_reader_units()],
                stderr=subprocess.DEVNULL)

            # 🚨 LES DROITS PARTENT TOUJOURS, `wipe` ou pas. `wipe` concerne les
            #    MESURES — une question de vie privée sur la consommation. Les
            #    droits, eux, DOIVENT disparaître : `grant` est en écriture
            #    unique pour `owner`, donc un boîtier qui garderait sa ligne
            #    refuserait le propriétaire suivant, sans aucun recours depuis
            #    l'app. Lier ça à `wipe` rendrait le boîtier inutilisable pour
            #    quiconque décoche la case.
            try:
                with access.session() as ac:
                    a, j = access.tout_effacer(ac)
                print(f"[unprovision] droits effacés : {a} accès, {j} jeton(s)",
                      flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"[unprovision] ⚠️ droits NON effacés ({e}) — "
                      f"le prochain propriétaire sera refusé", flush=True)

            if wipe:
                for suffix in ("", "-wal", "-shm"):
                    try:
                        os.remove(db.DB_PATH + suffix)
                        print(f"[unprovision] wipe {db.DB_PATH}{suffix}", flush=True)
                    except OSError:
                        pass
            # « Au revoir » : 3 flashs VIOLETS avant l'extinction (signal clair de désappairage).
            time.sleep(0.3)          # laisse les readers relâcher les pins GPIO de la LED
            _goodbye_flash()
            # Désappairage → le boîtier S'ÉTEINT TOUJOURS (poweroff), avec ou sans wipe : il part
            # hors tension. Au prochain allumage, plus de WiFi → mode configuration (BLE).
            print(f"[unprovision] poweroff (wipe={wipe})", flush=True)
            subprocess.Popen(["sudo", "systemctl", "poweroff"])

        threading.Timer(2.0, _teardown).start()

    def _pdls(self, qs=None):
        # ⚠️ NE JAMAIS interroger `measurements` sans filtrer sur `pdl_index` :
        # l'index est (pdl_index, ts, papp), donc une requête non filtrée y est
        # AVEUGLE et balaye toute la table. Mesuré sur ben-0001 (3,1 M lignes) :
        # MIN/MAX/COUNT GROUP BY 37 s. La liste des PDL vient donc de `pdl`
        # (repli `level_profile`) — quelques lignes, 0,01 s — puis MIN/MAX FILTRÉS
        # par PDL, à 0,00 s chacun.
        #
        # `points` (COUNT) reste cher même filtré (11,8 s) et n'est lu par personne :
        # l'app ne prend que `first_ts` d'ici. Renvoyé sur `?count=1` seulement.
        compter = bool(qs and qs.get("count"))
        with db.connect(read_only=True) as conn:
            try:
                pdls = [r[0] for r in conn.execute("SELECT pdl_index FROM pdl")]
                adcos = {r[0]: r[1] for r in conn.execute("SELECT pdl_index, adco FROM pdl")}
            except sqlite3.OperationalError:
                pdls, adcos = [], {}          # base antérieure à 0.9.6, lecture seule
            if not pdls:
                pdls = [r[0] for r in conn.execute("SELECT pdl_index FROM level_profile")]
            if not pdls:
                pdls = [0]
            rows = []
            for pdl in sorted(set(pdls)):
                # ⚠️ DEUX requêtes, pas une. SQLite n'applique son optimisation d'index
                # que s'il y a UN SEUL agrégat : `SELECT MIN(ts), MAX(ts) … WHERE
                # pdl_index=?` retombe sur un balayage — mesuré 16,55 s sur ben-0001,
                # contre 0,00 s pour chacune prise séparément.
                mn = conn.execute("SELECT MIN(ts) FROM measurements WHERE pdl_index=?",
                                  (pdl,)).fetchone()[0]
                mx = conn.execute("SELECT MAX(ts) FROM measurements WHERE pdl_index=?",
                                  (pdl,)).fetchone()[0]
                if mn is None:
                    continue                  # PDL déclaré, aucune mesure encore
                n = conn.execute("SELECT COUNT(*) FROM measurements WHERE pdl_index=?",
                                 (pdl,)).fetchone()[0] if compter else None
                rows.append({"pdl_index": pdl, "adco": adcos.get(pdl, ""),
                             "first_ts": mn, "last_ts": mx, "points": n})
        self._send(rows)

    def _live(self, qs):
        # ⚠️ NE JAMAIS interroger `measurements` sans filtrer sur `pdl_index` :
        # l'index est (pdl_index, ts, papp), donc une requête non filtrée y est
        # AVEUGLE et balaye toute la table. Mesuré sur ben-0001 (3,1 M lignes) :
        # dernier point 114 s sans filtre contre 0,01 s avec.
        # L'app envoie `pdl_index` ; à défaut on prend 0 (tout le parc n'a qu'un PDL).
        pdl = _int(qs, "pdl_index")
        if pdl is None:
            pdl = 0
        with db.connect(read_only=True) as conn:
            rows = _rows(
                conn,
                "SELECT ts, pdl_index, base, hchc, hchp, papp, iinst, tariff, "
                "index_id, index_value, src_standard, inject_total "
                "FROM measurements WHERE pdl_index=? ORDER BY ts DESC LIMIT 1",
                (pdl,),
            )
            # Niveau de conso 1..4 (visuel app), seuils pré-calculés par
            # ben-level-profiler ; ici lecture seule (cf. levels.py).
            now = int(time.time())
            for row in rows:
                row["level"] = levels.level_for(
                    conn, row["pdl_index"], row.get("papp"), now)
                # Mode TIC du PDL ('standard'/'historique'/null) — affiché par l'app.
                # En standard, `papp` est le NET SIGNÉ (>0 soutiré, <0 surplus injecté).
                mode = db.tic_mode(conn, row["pdl_index"])
                row["tic_mode"] = mode
                # Producteur = injection CONSTATÉE (pas juste EAIT présent) → l'app
                # affiche la jauge bidir soutirage⇄injection (Lot C). inject_total
                # est déjà dans la ligne (index monotone → dernière valeur = MAX).
                row["producer"] = db.producer(
                    conn, row["pdl_index"], row.get("inject_total"))
                # Abonnement souscrit (réglages app + étalonnage jauge), exposé brut
                # dans son unité d'origine : ISOUSC (A) en histo, PREF (kVA) en standard.
                # Étalonnage maxVa : on prend la source du MODE COURANT (standard→PREF×1000,
                # histo→ISOUSC×230), avec repli sur l'autre si la valeur du mode manque encore
                # (transition / cold-start). None tant que rien n'est reçu.
                isousc = db.get_isousc(conn, row["pdl_index"])
                pref = db.get_pref(conn, row["pdl_index"])
                row["isousc"] = isousc
                row["pref"] = pref
                from_isousc = isousc * 230 if isousc else None
                from_pref = pref * 1000 if pref else None
                if mode == "standard":
                    row["maxVa"] = from_pref or from_isousc
                else:
                    row["maxVa"] = from_isousc or from_pref
                # Jauge : ÉCHELLE RÉSOLUE CÔTÉ BOÎTIER (autoritatif, comme level/tariff_label).
                # Tant que le foyer n'est pas CONNU (même gate que le niveau : ≥ MIN_SAMPLES,
                # ≥ 2 j, dynamique), le high-water mark observé ≈ la conso courante → une conso
                # faible apparaîtrait ROUGE juste après l'unboxing. On renvoie donc l'ABONNEMENT
                # (maxVa PREF/ISOUSC, sinon DEFAULT_MAXVA) comme échelle pendant l'apprentissage,
                # et le plafond OBSERVÉ (auto-calibré) une fois le foyer connu. L'app n'arbitre
                # plus : elle affiche `plafond` tel quel.
                plafond, inject_max = db.gauge_bounds(conn, row["pdl_index"])
                if not levels.is_known(conn, row["pdl_index"]):
                    plafond = row["maxVa"] or DEFAULT_MAXVA
                    inject_max = inject_max or row["maxVa"] or DEFAULT_MAXVA
                row["plafond"] = plafond
                row["injectMax"] = inject_max
                # Libellé tarifaire EN COURS (jauge HP/HC) — résolu côté serveur :
                # standard→LTARF autoritatif, histo→convention PTEC. None → l'app garde
                # sa propre convention (rétro-compat). Cf. chantier unification labels.
                row["tariff_label"] = db.resolve_label(
                    conn, row["pdl_index"], row.get("src_standard"), row.get("index_id"))
                # Couleur Tempo en CHAMP EXPLICITE, résolue côté serveur comme le
                # libellé : sans elle, l'app devrait chercher « rouge »/« blanc »/
                # « bleu » dans un texte français, et connaître les deux conventions
                # (histo « Heures Pleines Jours Rouges » vs standard « HP  ROUGE »).
                # None hors Tempo.
                row["tempo_color"] = db.resolve_tempo_color(
                    conn, row["pdl_index"], row.get("src_standard"), row.get("index_id"))
                # Contrat (NGTF, quasi-statique) — distinct du tarif en cours ci-dessus.
                row["contract"] = db.get_ngtf(conn, row["pdl_index"])
            # Cloche de l'app : /live est DÉJÀ polé en continu, on y adosse donc
            # « il y a du nouveau » plutôt que d'ajouter un second appel périodique
            # (Pi Zero mono-cœur). En-tête plutôt que champ : /live renvoie un tableau
            # nu, l'envelopper casserait le contrat pour une feature pas encore éprouvée.
            # Un seul dernier événement pour l'INSTALLATION, pas un par PDL.
            ev_id, ev_sev = db.last_event(conn)
            self._send(rows, headers={"X-Ben-Last-Event": ev_id,
                                      "X-Ben-Last-Event-Severity": ev_sev})

    def _measurements(self, qs):
        pdl = _int(qs, "pdl_index")
        if pdl is None:
            return self._send({"error": "pdl_index_required"}, 400)
        now = int(time.time())
        since = _int(qs, "since", now - DEFAULT_WINDOW_SEC)
        until = _int(qs, "until", now)
        limit = min(_int(qs, "limit", MAX_LIMIT) or MAX_LIMIT, MAX_LIMIT)
        with db.connect(read_only=True) as conn:
            # Degrade-safe : la lecture au fil de l'eau densifie la courbe (~7×).
            # Si la plage contient plus de `limit` points fins, on AGRÈGE à ~limit
            # buckets (min/max/avg) au lieu de tronquer aux plus VIEUX via
            # `ORDER BY ts ASC LIMIT` — qui affichait un bord périmé. Une app pas
            # à jour reçoit ainsi une courbe complète et allégée, sans rien changer
            # côté app (cf. chantier-courbe-temps-reel.md, compat).
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM measurements "
                "WHERE pdl_index=? AND ts>=? AND ts<=? AND papp IS NOT NULL",
                (pdl, since, until),
            ).fetchone()["c"]
            if total > limit and until > since:
                bucket_sec = max(1, (until - since) // limit)
                rows = db.curve_buckets(conn, pdl, since, until, bucket_sec)
                downsampled = True
            else:
                rows = _rows(
                    conn,
                    "SELECT ts, base, hchc, hchp, papp, iinst, tariff, "
                    "index_id, index_value, src_standard, inject_total FROM measurements "
                    "WHERE pdl_index=? AND ts>=? AND ts<=? ORDER BY ts ASC LIMIT ?",
                    (pdl, since, until, limit),
                )
                downsampled = False
        self._send({"pdl_index": pdl, "since": since, "until": until,
                    "count": len(rows), "downsampled": downsampled, "points": rows})

    def _curve(self, qs):
        """Courbe agrégée par bucket — endpoint riche pour l'app à jour. L'app
        pilote la résolution (`buckets` = largeur de son viewport) ; le firmware
        agrège (min/max/avg, pics préservés). Le lissage anti-Hawthorne est
        appliqué PAR L'APP par-dessus (présentation pure, volet D)."""
        pdl = _int(qs, "pdl_index")
        if pdl is None:
            return self._send({"error": "pdl_index_required"}, 400)
        now = int(time.time())
        since = _int(qs, "since", now - DEFAULT_WINDOW_SEC)
        until = _int(qs, "until", now)
        if until <= since:
            return self._send({"error": "bad_range"}, 400)
        buckets = _int(qs, "buckets", DEFAULT_CURVE_BUCKETS) or DEFAULT_CURVE_BUCKETS
        buckets = max(1, min(buckets, MAX_CURVE_BUCKETS))
        bucket_sec = max(1, (until - since) // buckets)
        with db.connect(read_only=True) as conn:
            pts = db.curve_buckets(conn, pdl, since, until, bucket_sec)
        self._send({"pdl_index": pdl, "since": since, "until": until,
                    "bucket_sec": bucket_sec, "count": len(pts), "points": pts})

    def _chart(self, qs):
        """Courbe RICHE prête-à-tracer : `points` + `tariff_bands` (zones HP/HC) — endpoint
        de la nouvelle app (le rollup + les bandes NE PASSENT PAS par /curve, laissé intact pour
        l'app courante). Le SERVEUR arbitre la source des points (rollup rapide vs brut fidèle) ;
        les bandes viennent TOUJOURS du rollup (jamais un parcours de points). Param `raw=1` →
        force le brut (haute fidélité), MAIS **garde-fou serveur : ignoré au-delà de 24 h**
        (un scan brut sur 7j/30j écroulerait le Pi — c'est justement ce que le rollup évite).
        Au-delà, `raw` est silencieusement ignoré (arbitrage normal → rollup) ; l'app le voit
        via `source` ≠ `raw`. Cf. docs/rollup-par-index.md §5/§6."""
        pdl = _int(qs, "pdl_index")
        if pdl is None:
            return self._send({"error": "pdl_index_required"}, 400)
        now = int(time.time())
        since = _int(qs, "since", now - DEFAULT_WINDOW_SEC)
        until = _int(qs, "until", now)
        if until <= since:
            return self._send({"error": "bad_range"}, 400)
        buckets = _int(qs, "buckets", DEFAULT_CURVE_BUCKETS) or DEFAULT_CURVE_BUCKETS
        buckets = max(1, min(buckets, MAX_CURVE_BUCKETS))
        bucket_sec = max(1, (until - since) // buckets)
        # Garde-fou SERVEUR : `raw=1` n'est honoré que sur une fenêtre BORNÉE (≤ 24 h).
        # Au-delà, on l'ignore (le brut y serait un scan lourd → le rollup existe pour ça).
        RAW_MAX_WINDOW_SEC = 86400
        force_raw = (qs.get("raw", ["0"])[0] in ("1", "true")
                     and (until - since) <= RAW_MAX_WINDOW_SEC)
        with db.connect(read_only=True) as conn:
            # Arbitrage : rollup si (pas forcé brut) ET tranche demandée ≥ finesse rollup (2 min)
            # ET la fenêtre est couverte (since ≥ watermark). Sinon → brut (zoom serré, ou zone
            # pas encore backfillée). Même forme de retour dans les 2 cas → l'app ne voit rien.
            wm = db.rollup_watermark(conn)
            use_rollup = (not force_raw and bucket_sec >= db.ROLLUP_BUCKET_SEC
                          and wm is not None and since >= wm)
            if use_rollup:
                pts = db.curve_from_rollup(conn, pdl, since, until, bucket_sec)
                source = "rollup"
            else:
                pts = db.curve_buckets(conn, pdl, since, until, bucket_sec)
                source = "raw" if force_raw else "brut"
            bands = db.tariff_bands(conn, pdl, since, until)
        self._send({"pdl_index": pdl, "since": since, "until": until,
                    "bucket_sec": bucket_sec, "count": len(pts), "points": pts,
                    "tariff_bands": bands, "source": source})

    def _consumption(self, qs):
        """Conso par registre sur [since, until] — carry-forward server-side
        (cf. db.consumption). Contrat commun Pi/cloud ; l'app appelle en débounce
        au repos du pan/zoom et applique le prix (Σ wh × prix)."""
        pdl = _int(qs, "pdl_index")
        if pdl is None:
            return self._send({"error": "pdl_index_required"}, 400)
        now = int(time.time())
        since = _int(qs, "since", now - DEFAULT_WINDOW_SEC)
        until = _int(qs, "until", now)
        if until <= since:
            return self._send({"error": "bad_range"}, 400)
        with db.connect(read_only=True) as conn:
            res = db.consumption(conn, pdl, since, until)
        self._send({"pdl_index": pdl, "since": since, "until": until, **res})

    def _registers(self, qs):
        """Registres tarifaires d'un PDL (libellé résolu server-side + dernier index).
        Sert la carte réglages de l'app — un registre par tarif (Base / HC / HP…)."""
        pdl = _int(qs, "pdl_index")
        if pdl is None:
            return self._send({"error": "pdl_index_required"}, 400)
        with db.connect(read_only=True) as conn:
            regs = db.registers(conn, pdl)
            contract = db.get_ngtf(conn, pdl)   # NGTF = le contrat (calendrier fournisseur)
        self._send({"pdl_index": pdl, "contract": contract, "registers": regs})

    def _events(self, qs):
        """Événements constatés par le boîtier (cf. docs/chantier-events-et-notifications.md).

        Appelé UNIQUEMENT au tap sur la cloche : c'est l'en-tête X-Ben-Last-Event
        posé sur /live (déjà polé) qui signale qu'il y a du nouveau. Charge nulle
        sur un Pi Zero mono-cœur tant que l'utilisateur ne demande rien.
        """
        limit = min(_int(qs, "limit", 50) or 50, 200)
        since = _int(qs, "since")
        with db.connect(read_only=True) as conn:
            return self._send(db.get_events(conn, limit=limit, since_ts=since))

    def _lora_link(self, qs):
        pdl = _int(qs, "pdl_index")
        if pdl is None:
            return self._send({"error": "pdl_index_required"}, 400)
        now = int(time.time())
        since = _int(qs, "since", now - DEFAULT_WINDOW_SEC)
        limit = min(_int(qs, "limit", MAX_LIMIT) or MAX_LIMIT, MAX_LIMIT)
        with db.connect(read_only=True) as conn:
            rows = _rows(
                conn,
                "SELECT ts, rssi, snr FROM lora_link "
                "WHERE pdl_index=? AND ts>=? ORDER BY ts ASC LIMIT ?",
                (pdl, since, limit),
            )
        self._send({"pdl_index": pdl, "since": since,
                    "count": len(rows), "points": rows})


class _ServeurTLS(ThreadingHTTPServer):
    """Identique au serveur en clair, mais qui DIT quand une poignée de main rate.

    ⚠️ Ce n'est pas `handle_error` qu'il faut surcharger, et c'est contre-intuitif :
    avec une socket enveloppée, la poignée de main TLS a lieu dans `accept()`,
    donc dans `get_request()`. Or `socketserver._handle_request_noblock` avale
    tout `OSError` levé là — et `ssl.SSLError` en hérite. Un `handle_error`
    serait du code MORT pour le cas même qui l'a motivé.

    ⭐ Par défaut, un client qui n'arrive pas à négocier disparaît donc en
    silence. C'est précisément ce qu'on ne veut pas pendant le déploiement : une
    app qui ne fait pas confiance à la CA BEN échouerait sans laisser la moindre
    trace côté boîtier, et on chercherait le défaut du mauvais côté.

    Le volume s'autorégule : sur un LAN, une poignée de main ratée est un
    événement rare. Si ça inonde, c'est l'information.
    """

    def get_request(self):
        try:
            return super().get_request()
        except ssl.SSLError as e:
            print(f"[:{PORT_TLS}] poignée de main refusée — {e.reason or e} "
                  f"(client sans la CA BEN, ou http:// sur un port chiffré)")
            raise


def _ecoute_tls() -> ThreadingHTTPServer | None:
    """Prépare l'écoute chiffrée, ou renvoie None en expliquant pourquoi.

    🚨 AUCUNE raison de ne pas démarrer ici ne doit empêcher :8087 de servir.
    Un boîtier qui devient injoignable parce que son certificat est illisible
    serait une panne créée par une amélioration de sécurité — et elle se
    manifesterait chez le client, pas ici.
    """
    crt, key = f"{CERT_DIR}/device.crt", f"{CERT_DIR}/device.key"
    if not (os.path.exists(crt) and os.path.exists(key)):
        print(f"[:{PORT_TLS}] pas de certificat dans {CERT_DIR} — écoute chiffrée "
              f"désactivée, :{PORT} continue de servir")
        return None
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(crt, key)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        # ⭐ Le boîtier ne demande AUCUN certificat au client. L'app s'authentifie
        #    par jeton (cf. `access.py`) ; le TLS ne sert ici qu'à prouver l'identité
        #    du BOÎTIER et à chiffrer. Exiger un certificat client fermerait la porte
        #    à Home Assistant et à tout ce qui n'est pas l'app.
        ctx.verify_mode = ssl.CERT_NONE
    except Exception as e:  # noqa: BLE001
        print(f"[:{PORT_TLS}] certificat inutilisable ({e}) — écoute chiffrée "
              f"désactivée, :{PORT} continue de servir")
        return None

    serveur = _ServeurTLS((HOST, PORT_TLS), Handler)
    # ⭐ Le drapeau que lit /claim. Le handler est le MÊME sur les deux écoutes ;
    #    c'est le SERVEUR qui sait s'il est chiffré, pas la requête. Se fier à un
    #    en-tête (`X-Forwarded-Proto` et consorts) serait se fier à l'appelant.
    serveur.chiffre = True
    # ⚠️ Le contexte lit le certificat MAINTENANT, une fois pour toutes. Quand
    #    `ben-certd` le remplace, il redémarre CE service — sans quoi l'écoute
    #    servirait l'ancien certificat jusqu'à son expiration, sans une erreur
    #    au journal. Voir config/etc/sudoers.d/ben-certd.
    serveur.socket = ctx.wrap_socket(serveur.socket, server_side=True)
    return serveur


def main() -> None:
    tls = _ecoute_tls()
    if tls is not None:
        threading.Thread(target=tls.serve_forever, daemon=True).start()
        print(f"ben-local-api en écoute CHIFFRÉE sur {HOST}:{PORT_TLS}")

    # L'écoute en clair reste dans le thread principal : si tout le reste échoue,
    # c'est elle qui survit.
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"ben-local-api en écoute sur {HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        if tls is not None:
            tls.shutdown()


if __name__ == "__main__":
    main()
