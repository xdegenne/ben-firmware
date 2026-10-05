#!/usr/bin/env python3
"""
ben_publisher — pousse les mesures du boîtier vers `ben-api`.

⚠️ Le document de conception de l'ingestion est INTERNE — ce dépôt est PUBLIC.
Ce fichier décrit le comportement du boîtier ; le contrat de l'API et la
politique serveur n'y figurent pas.

    hello  au démarrage, puis une fois par JOUR (santé + compteurs ; la VERSION,
           elle, part par la DÉCLARATION, sur sa condition — voir VERSION_DECLAREE).
           S'il échoue, ON CONTINUE — et c'est le rejeu quotidien qui rattrape,
           il n'y a pas de reprise immédiate.
    boucle toutes les 60 s : jusqu'à BATCH points non envoyés, du plus ancien.
           ⭐ 10 s seulement TANT QU'IL RESTE DU RETARD — sinon un boîtier à
           courte fenêtre de connectivité ne rattrape jamais (cf. PERIOD_RETARD).

La colonne `sent` de `measurements` est l'outbox : elle existe dans le schéma
DEPUIS LE PREMIER JOUR et n'avait jamais été écrite.

═══ CE QUI COMPTE VRAIMENT, ET POURQUOI ══════════════════════════════════════

1. `sent=1` PAR `rowid`, JAMAIS par plage de `ts`.
   Le LoRa livre des points EN RETARD, avec de VIEUX horodatages (courbe v0x05,
   horodatage par point). Un `UPDATE … WHERE ts BETWEEN ?` marquerait envoyées des
   lignes arrivées entre-temps — perdues en silence, sans le moindre message.

2. `sent=1` SEULEMENT APRÈS le 2xx.
   Un plantage rejoue le lot : c'est le comportement VOULU. Le serveur absorbe le
   doublon (`ON CONFLICT DO NOTHING` sur `(device_id, pdl_index, ts)`), parce que
   `record_measurement()` fait un INSERT NU côté boîtier — aucune PK, aucun UNIQUE.
   `inserted < received` dans la réponse est donc NORMAL, ce n'est pas une erreur.

3. GIGUE TOTALE en cas d'échec — `random.uniform(0, borne)` et NON `borne`.
   C'est la seule ligne dont l'absence crée un vrai mode de défaillance : le serveur
   tombe une heure, tous les boîtiers échouent, RÉESSAIENT AU MÊME RYTHME, se
   synchronisent, et reviennent dans la même seconde. Si la salve le refait
   trébucher, IL NE REDÉCOLLE JAMAIS. Tirer DANS l'intervalle disperse ; attendre
   l'intervalle synchronise.

4. `ORDER BY rowid`, PAS `ORDER BY ts`.
   `idx_meas_sent` stocke `(sent, rowid)` : le parcourir rend déjà les lignes en
   ordre de rowid, sans tri. Avec `ORDER BY ts`, SQLite trierait les 5,4 M lignes
   non envoyées À CHAQUE TOUR. L'ordre rowid est l'ordre d'insertion — assez proche
   du chronologique, et la rétention à 180 j a supprimé la course contre la purge
   qui justifiait un ordre strict.

5. Connexion HTTP RÉUTILISÉE.
   Une poignée de main TLS échange les deux certificats RSA 2048 : 3 à 5 ko. À une
   connexion neuve par minute, cela ferait ~5,8 Mo/jour de négociation pour
   0,74 Mo/jour de données utiles — HUIT FOIS la charge utile. Le serveur a
   `IdleTimeout: 120 s` et la période est de 60 s : la connexion ne se ferme jamais.
   ⚠️ Ne PAS allonger la période au-delà de 120 s sans changer l'autre côté.

Tourne en `ben`, sans sudo. Aucune écriture hors de `measurements.sent`.
"""

import gzip
import http.client
import json
import logging
import os
import pathlib
import random
import signal
import socket
import sqlite3
import ssl
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # src/pi
import capabilities as caps  # noqa: E402
import health  # noqa: E402
from store import access  # noqa: E402
from store import claim_ticket  # noqa: E402
from store import db  # noqa: E402

# ── Réglages ──────────────────────────────────────────────────────────────────
# ⚠️ L'URL n'est écrite QU'ICI. La changer est une OTA d'une ligne — comme
#    RETENTION_DAYS. Déplacer le serveur, en revanche, ne demande QUE de changer
#    l'enregistrement DNS : c'est tout l'intérêt de passer par un nom.
API_HOST = os.environ.get("BEN_API_HOST", "api.benpilote.fr")
API_PORT = int(os.environ.get("BEN_API_PORT", "8443"))

BATCH = int(os.environ.get("BEN_PUB_BATCH", "1000"))
PERIOD = float(os.environ.get("BEN_PUB_PERIOD", "60"))
# 🚨 LA CADENCE QUAND IL RESTE DU RETARD. Sans elle, un boîtier à courte fenêtre de
#    connectivité ne rattrape JAMAIS : il envoyait 1 lot de 500 points toutes les 60 s,
#    soit 500/min, pour une production radio de 64,5/min — un débit NET de 435/min. Une
#    journée hors ligne (93 000 points) demandait donc plus de 3 h 30 de connectivité
#    rien que pour ne pas reculer, et en dessous l'écart grandissait chaque jour.
#
# ⭐ Le goulot n'était PAS la taille du lot, et la mesure l'a tranché : un aller-retour
#    coûte ~400 ms pour 1000 points (mesuré sur un Pi Zero du parc, 3 essais), contre
#    60 s de sommeil. Le service travaillait 0,7 % du temps.
#
# ⚠️ Et 10 s plutôt que 0 : un sommeil nul, c'est TOUT LE PARC à plein débit sur l'API
#    en même temps après une panne d'opérateur. À 10 s la charge parc plafonne à
#    ~670 points/s pour sept boîtiers, et une journée de retard se résorbe quand même en
#    ~16 min — soit largement au-delà du besoin. On ne paie pas un risque serveur pour
#    un gain qui ne sert à rien.
PERIOD_RETARD = float(os.environ.get("BEN_PUB_PERIOD_RETARD", "10"))
BACKOFF_MAX = 300.0

# Au-delà de ce nombre d'échecs consécutifs, l'échec de publication passe de `warning` à
# `error`. 🚨 POURQUOI UN SEUIL ET PAS « error » TOUT DE SUITE : un réseau cligne, et une
# panne de quelques secondes ne doit pas remplir le journal d'erreurs — sinon le niveau
# `error` ne veut plus rien dire et on cesse de le regarder.
#
# ⭐ POURQUOI 5 : avec la gigue totale `uniform(0, min(2**n, 300))`, cinq échecs représentent
#    déjà de l'ordre de la minute cumulée. Ce n'est plus un clignement, c'est un état.
ECHECS_ERREUR = int(os.environ.get("BEN_PUB_ECHECS_ERREUR", "5"))
# Le hello est rejoué périodiquement, pas seulement au démarrage :
#   - un NOUVEAU COMPTEUR peut apparaître en cours de route (resolve_pdl() crée un
#     pdl_index dès qu'un ADCO inconnu se présente : changement de compteur, nouvel
#     émetteur LoRa) — sans nouveau hello, le cloud reçoit des mesures pour un
#     pdl_index qu'il ne sait pas nommer ;
#   ⓘ La VERSION, elle, ne voyage plus ici : depuis 0.9.27 elle est portée par la
#     DÉCLARATION (`/hello`), que sa propre condition déclenche — pas par ce
#     battement périodique, qui ne transporte aucun `sw`. Croire le contraire est
#     exactement ce qui a laissé `devices.sw_version` périmé (ben-docs#15).
#
# ⏱️ QUOTIDIEN, et non horaire (décidé le 2026-09-21). Le hello va porter en plus
# l'INSTANTANÉ COMPLET de `contract_epoch`, `tariff_labels` et `meter_profile` —
# des tables qui bougent quelques fois par AN. Mesuré sur les vraies données de
# ben-0001 : **524 o bruts, 276 o gzippés**, soit 98 ko/an contre 270 Mo/an de
# mesures — 0,036 % du trafic.
#
# 🚨 Ce n'est PAS de la vivacité : `deviceAuth` appelle `SeenDevice()` à CHAQUE
# requête, donc `devices.last_seen` est déjà rafraîchi toutes les 60 s par les
# mesures. Rallonger la période du hello ne dégrade rien de ce côté.
#
# ⭐ Pourquoi un instantané COMPLET plutôt qu'un delta ou une négociation : le
# boîtier n'a alors AUCUN état à mémoriser sur ce que le cloud sait. Si le cloud
# perd la métadonnée (restauration, migration, reconstruction), elle revient
# d'elle-même sous 24 h. Un envoi « sur changement seulement » créerait une
# divergence SILENCIEUSE ET DÉFINITIVE — une table absente est indiscernable
# d'une table vide. Cf. docs/chantier-ingestion-cloud.md.
HELLO_EVERY = float(os.environ.get("BEN_PUB_HELLO_EVERY", "86400"))

# Plancher entre deux hellos déclenchés par un ÉCHEC.
#
# 🚨 POURQUOI UN HELLO SUR ÉCHEC — mesuré sur ben-0012 le 2026-10-01. Le boîtier mesurait,
#    le lien était debout (hello en 17 ms), le publisher tournait sans un seul plantage, et
#    NEUF JOURS de données ne partaient pas. La raison était écrite dans son journal à chaque
#    tentative, et personne ne pouvait la lire : l'instantané de santé ne voyage qu'avec le
#    hello, et le hello ne part qu'une fois par jour. On attendait 24 h pour apprendre une
#    chose que le boîtier savait depuis la première minute.
#
# ⭐ L'ÉCHEC SE SIGNALE DE LUI-MÊME. Au franchissement du seuil d'erreur, le publisher envoie
#    un hello : l'instantané part donc PENDANT la panne, avec les lignes de son propre journal.
#
# ⚠️ Plafonné à un par heure, et JAMAIS plus d'un par panne continue : un boîtier coupé du
#    monde ne doit pas se mettre à heartbeat. Le coût, c'est `snapshot()` (≤ 20 s) une fois
#    l'heure — et sur un lien mort le hello échoue sans rien coûter de plus (`hello()` ne lève
#    jamais).
HELLO_SUR_ECHEC_S = float(os.environ.get("BEN_PUB_HELLO_SUR_ECHEC", "3600"))

# ── La DÉCLARATION, et son plancher ───────────────────────────────────────────
#
# ⭐ LES DEUX DÉCLENCHEURS SONT DES CONDITIONS, pas des événements : « il existe un
#    pdl sans ref » et « la version installée n'est pas celle que le cloud a
#    acceptée ». Toutes deux se vérifient à chaque tour, localement. La première
#    couvre d'un seul énoncé le compteur neuf, le compteur remplacé et la ref perdue
#    en local (carte reflashée, désappairage) ; la seconde, l'OTA, le retour arrière
#    et le refus du cloud. ⇒ Réconcilier un état est plus solide que rattraper un
#    événement : un événement raté est définitif, une condition se re-vérifie.
#    ⓘ Il y avait un TROISIÈME déclencheur, événementiel — un drapeau d'OTA posé par
#      `update.sh`. Il a été oublié par 36 scripts sur 37 et la vue parc en est
#      restée fausse deux releases durant : cf. `VERSION_DECLAREE`, ben-docs#15.
#
# 🚨 MAIS IL FAUT UN PLANCHER, et ce n'est pas du zèle. Un pdl dont l'ADS n'est
#    pas conforme — le PDL fantôme de ben-0001 en est un, deux octets nuls —
#    n'obtiendra JAMAIS de ref : le CHECK du schéma cloud l'interdit, et c'est
#    voulu. Sans plancher, ce boîtier redéclarerait toutes les 60 s, pour toujours,
#    et chaque tentative écrirait une ligne dans le journal du cloud.
DECLARATION_PLANCHER_S = float(os.environ.get("BEN_PUB_DECLARATION_PLANCHER", "300"))

# ⭐ Le plancher LONG : quand tous les pdl sans ref portent déjà un motif, le cloud
#    a répondu et sa réponse ne changera pas tant que la cause n'aura pas changé.
#    Six heures laissent la place à un changement côté cloud (un rattachement
#    retiré, un ADS corrigé) sans saturer les 8 lignes d'erreur que l'instantané de
#    santé transporte.
DECLARATION_PLANCHER_REFUS_S = float(
    os.environ.get("BEN_PUB_DECLARATION_PLANCHER_REFUS", "21600"))

# ── LA VERSION DÉCLARÉE, mémorisée LOCALEMENT ─────────────────────────────────
#
# 🚨 CE QUI REMPLACE LE DRAPEAU D'OTA (ben-docs#15, #38). Une OTA doit faire
#    redéclarer : c'est ainsi, et SEULEMENT ainsi, que le cloud apprend la version
#    neuve — `devices.sw_version` n'est écrite que par la route `/hello`, jamais par
#    le battement. Le mécanisme était un ÉVÉNEMENT : chaque `update.sh` devait poser
#    `/var/lib/ben-firmware/declaration-requise.json`, que ce service consommait au
#    tour suivant. UN SEUL des 37 scripts du dépôt l'a fait — celui qui a introduit
#    le mécanisme — et il a été OUBLIÉ dès la transition suivante. Mesuré le 04/10 :
#    `devices.sw_version` annonçait 0.9.27 pour les 8 boîtiers, dont deux tournaient
#    en 0.9.28, et la vue parc serait restée fausse INDÉFINIMENT.
#
# ⭐ UNE CONDITION, PAS UN ÉVÉNEMENT — la doctrine est déjà écrite vingt lignes plus
#    haut pour « il existe un pdl sans ref », elle n'avait simplement pas été
#    appliquée ici : on mémorise la version que le cloud a ACCEPTÉE, et on la compare
#    à chaque tour à celle de `device.json`. Un événement raté est définitif ; une
#    condition se re-vérifie au tour suivant. ⇒ Un seul énoncé couvre l'OTA, le
#    RETOUR ARRIÈRE, un `device.json` édité à la main, une déclaration REFUSÉE
#    (4xx/5xx, réseau), et un publisher redémarré au mauvais moment.
#
# ⭐ MÉMOIRE ABSENTE = JAMAIS DÉCLARÉE, et c'est ce qui recale TOUT LE PARC sans
#    qu'on touche à un boîtier : au premier battement après l'OTA qui livre ce code,
#    aucun boîtier n'a ce fichier, donc chacun déclare une fois.
#
# ⓘ UN FICHIER, PAS UNE TABLE, et c'est le MODE DE DÉFAILLANCE qui tranche. Une
#    table demanderait un DDL dans `update.sh` — `open_db()` ouvre en écriture SANS
#    rejouer le schéma (délibéré) et l'API locale est en lecture seule, donc aucun
#    des deux ne peut la créer : oubliée, elle ferait lever `no such table` à chaque
#    tour et le boîtier cesserait de publier EN SILENCE (la classe de panne que
#    0.9.27 a failli livrer). Un fichier absent ou illisible, lui, vaut « jamais
#    déclarée » : une déclaration de trop, bornée par le plancher.
# ⓘ Hors de `measurements.db` AUSSI parce qu'elle se RECONSTRUIT (0.9.25) — et si la
#    mémoire ne survit pas, on redéclare, ce qui est sans conséquence. Il vit là où
#    vivait le drapeau, `/var/lib/ben-firmware`, propriété de `ben` : aucun état
#    nouveau à provisionner.
VERSION_DECLAREE = os.environ.get("BEN_PUB_VERSION_DECLAREE",
                                  "/var/lib/ben-firmware/version-declaree.json")

# ⓘ L'ANCIEN DRAPEAU, gardé pour être RETIRÉ — sans jamais être lu. Un boîtier peut
#    en porter un, posé par un vieux script ou laissé par une déclaration jamais
#    acceptée. L'INTERPRÉTER reviendrait à garder les deux mécanismes, donc à garder
#    celui qu'on retire ; le LAISSER laisserait sur le disque un fichier qui ne veut
#    plus rien dire, et que quelqu'un relira un jour comme s'il voulait dire quelque
#    chose.
DRAPEAU_LEGUE = os.environ.get("BEN_PUB_DRAPEAU_LEGUE",
                               "/var/lib/ben-firmware/declaration-requise.json")

CERT_DIR = os.environ.get("BEN_CERT_DIR", "/etc/ben-firmware/certs")
DEVICE_JSON = caps.DEVICE_JSON

log = logging.getLogger("ben-publisher")


# ── TLS ───────────────────────────────────────────────────────────────────────

def make_context() -> ssl.SSLContext:
    """Authentification MUTUELLE, complète.

    🚨 `check_hostname = False` ou `verify_mode = CERT_NONE` annulent la moitié de
    l'authentification — le boîtier parlerait alors à n'importe qui. Si le serveur
    est rejeté, on CORRIGE LE CERTIFICAT, on ne désactive jamais la vérification.

    ⚠️ Python 3.13 active `VERIFY_X509_STRICT` par défaut : la CA doit porter
    `basicConstraints = CA:TRUE`. Celle d'avant le 2026-09-19 n'avait AUCUNE
    extension et était refusée avec un message trompeur (« Missing Authority Key
    Identifier »). La CA conforme est livrée par cette même OTA.
    """
    ctx = ssl.create_default_context(
        ssl.Purpose.SERVER_AUTH, cafile=f"{CERT_DIR}/root-ca.crt")
    ctx.load_cert_chain(f"{CERT_DIR}/device.crt", f"{CERT_DIR}/device.key")
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


class Client:
    """Connexion persistante vers l'API. Se rouvre toute seule si elle tombe."""

    def __init__(self, device_id: str):
        self.device_id = device_id
        self.ctx = make_context()
        self.conn: http.client.HTTPSConnection | None = None

    def _connect(self) -> http.client.HTTPSConnection:
        if self.conn is None:
            self.conn = http.client.HTTPSConnection(
                API_HOST, API_PORT, context=self.ctx, timeout=30)
        return self.conn

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None

    def post(self, path: str, payload: dict) -> tuple[int, str]:
        body = json.dumps(payload, separators=(",", ":")).encode()
        headers = {"Content-Type": "application/json"}
        # gzip niveau 6 : ×10,6 mesuré sur des données réelles, pour ~25 ms sur un
        # Pi Zero. Le niveau 9 ne gagne que 6 % pour nettement plus de CPU.
        if len(body) > 1024:
            body = gzip.compress(body, compresslevel=6)
            headers["Content-Encoding"] = "gzip"

        for tentative in (1, 2):
            try:
                c = self._connect()
                c.request("POST", f"/api/devices/{self.device_id}{path}", body, headers)
                r = c.getresponse()
                data = r.read().decode("utf-8", "replace")
                return r.status, data
            except (http.client.HTTPException, OSError, socket.timeout) as e:
                # Une connexion gardée ouverte peut avoir été fermée par le serveur
                # ou par un pare-feu intermédiaire : on la rouvre UNE fois avant
                # de considérer que c'est une vraie panne.
                self.close()
                if tentative == 2:
                    raise
                log.debug("connexion rouverte après %s", e)
        raise RuntimeError("inatteignable")


# ── Base locale ───────────────────────────────────────────────────────────────

def open_db() -> sqlite3.Connection:
    """Ouvre la base EN ÉCRITURE, sans rejouer le schéma.

    ⚠️ On n'utilise PAS `db.connect()` : en écriture, il exécute `_SCHEMA` et les
    migrations. Les faire jouer par un SECOND processus, en concurrence du lecteur,
    n'apporte rien et peut surprendre. Le lecteur a déjà posé le WAL.
    `timeout=30` : le lecteur écrit en continu, on attend plutôt que d'échouer.
    """
    conn = sqlite3.connect(db.DB_PATH, timeout=30.0)
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


# 🚨 LA JOINTURE SUR `pdl` N'EST PAS UN CONFORT : ELLE EST LE FILTRE.
#
#    Un compteur sans `ref` n'est pas publiable — le cloud refuserait le lot. Ses
#    points doivent donc rester `sent = 0` et repartir dès que la `ref` arrive, et
#    surtout leur `rowid` ne doit JAMAIS entrer dans la liste rendue : `mark_sent`
#    les passerait à 1 après le 2xx du lot des AUTRES compteurs, et la mesure serait
#    perdue POUR TOUJOURS, sans trace. C'est l'invariant qui tient tout le plan de
#    bascule, et c'est le seul défaut de ce fichier qu'on ne pourrait pas rattraper.
#
# ⭐ Filtrer en SQL plutôt qu'en Python a un second effet voulu : la LIMITE porte
#    alors sur les points PUBLIABLES. Filtrer après aurait rendu des lots plus petits
#    que demandé, donc ralenti le rattrapage sans que rien ne le dise.
#
# ⚠️ Le prix, à connaître : si un pdl sans `ref` accumulait beaucoup de points non
#    envoyés, SQLite devrait les parcourir pour trouver les `limit` lignes qui
#    passent le filtre. Supportable ici — un pdl sans ref est transitoire, la
#    déclaration l'équipe au tour suivant — mais ce serait le premier endroit à
#    regarder si le rattrapage ralentissait sans raison apparente.
SELECT_BATCH = """
SELECT m.rowid, m.ts, m.papp, m.iinst, m.src_standard,
       m.index_id, m.index_value, m.inject_total, m.tariff, m.meter_ts, p.ref
FROM measurements m
JOIN pdl p ON p.pdl_index = m.pdl_index
WHERE m.sent = 0 AND p.ref IS NOT NULL AND p.ref <> ''
ORDER BY m.rowid
LIMIT ?
"""


def fetch_batch(conn: sqlite3.Connection, limit: int) -> tuple[list, list]:
    """Rend `(rowids, lots)` — les lots GROUPÉS PAR COMPTEUR.

    ⭐ Une `ref` par LOT, jamais par point : un uuid répété 1000 fois coûte 36 ko,
       un par compteur en coûte 36. Et le multi-compteurs cesse d'être un cas
       particulier — c'est la forme normale du payload.

    🚨 `pdl_index` NE SORT PAS d'ici. C'est littéralement l'énoncé du chantier : un
       index local au boîtier ne veut rien dire ailleurs, donc il n'a rien à faire
       dans le payload le plus volumineux du protocole.

    🚨 Les `rowid` rendus sont EXACTEMENT ceux des points présents dans les lots —
       ni plus (une mesure marquée sans être partie serait perdue), ni moins (une
       mesure partie sans être marquée repartirait indéfiniment).
    """
    rows = conn.execute(SELECT_BATCH, (limit,)).fetchall()
    rowids = []
    par_ref: dict = {}
    for (rid, ts, papp, iinst, std, idx_id, idx_val,
         inject, tariff, meter_ts, ref) in rows:
        rowids.append(rid)
        p = {"ts": ts}
        # On n'envoie que ce qui existe : un champ absent pèse moins qu'un null,
        # et le serveur distingue « absent » de « zéro » (0 W est une vraie valeur).
        if papp is not None:
            p["papp"] = papp
        if iinst is not None:
            p["iinst"] = iinst
        if std is not None:
            p["std"] = bool(std)
        if idx_id is not None:
            p["index_id"] = idx_id
        if idx_val is not None:
            p["index_value"] = idx_val
        if inject is not None:
            p["inject"] = inject
        if tariff is not None:
            p["tariff"] = tariff
        if meter_ts is not None:
            p["meter_ts"] = meter_ts
        # ⓘ `dict` conserve l'ordre d'insertion (Python 3.7+) : les lots sortent donc
        #    dans l'ordre d'apparition des compteurs, lui-même l'ordre des `rowid`.
        #    Rien n'en dépend, mais une sortie stable se diagnostique mieux.
        par_ref.setdefault(ref, []).append(p)
    lots = [{"ref": r, "points": pts} for r, pts in par_ref.items()]
    return rowids, lots


def mark_sent(conn: sqlite3.Connection, rowids: list) -> None:
    """🚨 PAR ROWID, et seulement après un 2xx. Voir l'en-tête, point 1."""
    conn.executemany("UPDATE measurements SET sent = 1 WHERE rowid = ?",
                     [(r,) for r in rowids])
    conn.commit()


def cadence(en_attente: int) -> float:
    """Combien de temps dormir avant le prochain lot.

    ⭐ Fonction PURE, et ce n'est pas du zèle : c'est ce qui la rend éprouvable sans
       réseau, sans base et sans Pi — la même raison qui a rendu `read_frame` testable.

    ⚠️ Le seuil est `BATCH`, pas zéro. En dessous d'un lot plein il ne reste au plus
       qu'un lot partiel : accélérer pour lui ne gagnerait rien et ferait osciller la
       cadence à chaque tour.
    """
    return PERIOD_RETARD if en_attente >= BATCH else PERIOD


# Niveau Python → priorité syslog. C'est la table de `sd_journal_print`.
PRIORITE_SYSLOG = {
    logging.CRITICAL: 2,   # crit
    logging.ERROR:    3,   # err
    logging.WARNING:  4,   # warning
    logging.INFO:     6,   # info
    logging.DEBUG:    7,   # debug
}


class _FormatJournal(logging.Formatter):
    """Préfixe chaque ligne de `<N>`, la priorité syslog que systemd lit sur stderr.

    🚨 SANS CE PRÉFIXE, LE NIVEAU PYTHON N'ATTEINT JAMAIS LE JOURNAL — et c'est un défaut
       qui a coûté un diagnostic entier.

       `logging.basicConfig()` écrit du texte brut sur stderr. systemd capte stderr et range
       TOUT à une priorité FIXE : `SyslogLevel=6` (info) par défaut, vérifié sur l'unité. Le
       niveau Python ne change donc que le TEXTE de la ligne, pas sa priorité.

       Mesuré sur un boîtier du parc, sur un vrai échec de publication :

           [2026-09-23 03:22:51][WARNING] échec n°1 ([Errno -3] Temporary failure in name…
                                 ↑ le texte dit WARNING        →  PRIORITY=6

       ⇒ `health.errors()` interroge `journalctl -p 3`. Il ne voyait RIEN de ce que le
         publisher dit de ses pannes, et monter `log.warning` en `log.error` n'y aurait rien
         changé : les deux auraient atterri en priorité 6.

    ⭐ Le préfixe `<N>` est le mécanisme documenté de systemd (`SyslogLevelPrefix=yes`, actif
       par défaut) : il le lit, il l'applique, et il le RETIRE du message. Vérifié sur la
       cible — `<4>` donne `PRIORITY=4`, `<3>` donne `PRIORITY=3`.

    ⚠️ Zéro dépendance : pas de `python-systemd` à embarquer sur sept boîtiers pour ça.
    """

    def format(self, record: logging.LogRecord) -> str:
        return "<%d>%s" % (PRIORITE_SYSLOG.get(record.levelno, 6), super().format(record))


def installer_journal(niveau: int = logging.INFO) -> logging.Logger:
    """Branche la journalisation sur stderr AVEC la priorité syslog. Rend la racine."""
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(_FormatJournal("[%(asctime)s][%(levelname)s] %(message)s",
                                  datefmt="%Y-%m-%d %H:%M:%S"))
    racine = logging.getLogger()
    racine.handlers[:] = [h]       # on REMPLACE : pas de ligne en double
    racine.setLevel(niveau)
    return racine


def signaler_echec(echecs: int, dernier: float, maintenant: float) -> bool:
    """Faut-il dépenser un hello pour signaler cet échec ?

    ⭐ Fonction PURE, pour la même raison que `cadence()` : la décision s'éprouve sans réseau,
       sans base et sans Pi. C'est ce qui permet de vérifier le plancher horaire sans attendre
       une heure.

    ⚠️ Le seuil est celui de l'ERREUR, pas le premier échec. Une coupure de quelques secondes
       arrive tous les jours sur tous les boîtiers ; elle ne mérite pas un instantané de santé.
       On ne signale que ce qui PERSISTE — la même frontière que `niveau_echec()`, et c'est
       voulu : un seul seuil, pas deux à garder d'accord.
    """
    return echecs >= ECHECS_ERREUR and maintenant - dernier >= HELLO_SUR_ECHEC_S


def niveau_echec(echecs: int) -> int:
    """Le niveau de journalisation d'un échec de publication. Fonction PURE.

    🚨 UN ÉCHEC ISOLÉ EST UN AVERTISSEMENT, UN ÉCHEC QUI PERSISTE EST UNE ERREUR.

    Un réseau cligne : échouer une ou deux fois est normal et ne doit pas crier. Au-delà du
    seuil, ce boîtier NE LIVRE PLUS SES DONNÉES — l'état le plus grave qu'il puisse connaître
    sans être mort.

    ⭐ CE N'EST PAS UN DÉTAIL DE JOURNALISATION. Tout était en `warning`, donc en priorité
       syslog 4 — un cran sous le `-p 3` de l'instantané de santé. Un boîtier du parc mesurait,
       était en ligne, son publisher tournait, il avait 566 248 points en attente et n'envoyait
       RIEN : le diagnostic à distance ne pouvait pas voir POURQUOI, parce que la seule ligne
       qui l'expliquait était sous le seuil (#18).

    ⚠️ Et pas `error` dès le premier échec : sinon le niveau `error` ne veut plus rien dire, et
       on cesse de le regarder. C'est le même raisonnement que la gigue — un garde-fou qui crie
       tout le temps ne garde plus rien.
    """
    return logging.ERROR if echecs >= ECHECS_ERREUR else logging.WARNING


def cadence_sure(conn: sqlite3.Connection) -> float:
    """La cadence, ou la croisière si on ne sait pas mesurer le retard.

    🚨 `pending_approx()` INTERROGE LA BASE, et son appelant est HORS du `try` de la
       boucle. L'ancien `_sleep(PERIOD)` ne pouvait rien lever ; celui-ci si — verrou
       tenu au-delà du timeout pendant que le lecteur écrit, ou erreur d'E/S sur la
       carte SD. Sans ce garde, l'exception remonte hors de `main()` : le process meurt
       sans passer par « arrêté proprement », systemd le relance, et on perd le backoff.

    ⭐ Le repli est `PERIOD`, JAMAIS `PERIOD_RETARD` : ne pas savoir mesurer le retard
       ne doit pas faire ACCÉLÉRER.

    ⚠️ Et on n'incrémente pas `echecs`, qui parle du SERVEUR — une base locale qui
       bronche n'est pas un serveur en panne, et la confondre ferait partir le publisher
       en backoff long pour une raison qui n'a rien à voir.
    """
    try:
        return cadence(pending_approx(conn))
    except sqlite3.Error as e:
        log.warning("retard illisible (%s) — cadence de croisière par défaut", e)
        return PERIOD


def pending_approx(conn: sqlite3.Connection) -> int:
    """Estimation du retard, en O(1).

    🚨 PAS `SELECT count(*) WHERE sent=0` : il balaie les 5,4 M entrées de
    `idx_meas_sent` et prend **37 SECONDES sur un Pi Zero** (mesuré le 2026-09-19).
    Appelé à chaque lot, il aurait rendu le service inutilisable ; même une seule
    fois au démarrage, il retardait le premier envoi de 37 s.

    On encadre par les rowid, que l'index rend instantanés :
    le plus petit non envoyé, et le plus grand tout court. L'écart surestime un peu
    (les lignes purgées laissent des trous) — c'est une ligne de journal, pas une
    comptabilité.
    """
    # 🚨 UN BOÎTIER VIERGE N'A PAS ENCORE CETTE TABLE, et le publisher y mourait.
    #    Mesuré sur ben-0005 le 05/10, au premier démarrage après un déballage :
    #    `sqlite3.OperationalError: no such table: measurements`. Or cet appel est
    #    la DEUXIÈME ligne de `main()`, donc bien avant `presenter_le_ticket()` —
    #    dont le commentaire dit pourtant « LE TICKET D'ABORD ». Le ticket n'était
    #    présenté qu'au redémarrage de systemd, 30 s plus tard, et un plantage
    #    durable aurait fait expirer sa fenêtre de 900 s.
    #
    # ⭐ La table naît à la première ouverture en ÉCRITURE par un LECTEUR
    #    (`db.connect()` rejoue le schéma) ; le publisher, lui, ouvre en lecture.
    #    Sur un boîtier neuf il peut donc démarrer AVANT que le premier lecteur
    #    n'ait écrit — et « zéro point en attente » est alors la réponse VRAIE.
    #
    # ⚠️ On ne ravale que ce défaut-là : toute autre `OperationalError` (base
    #    verrouillée, fichier corrompu) doit continuer de remonter.
    try:
        row = conn.execute(
            "SELECT (SELECT max(rowid) FROM measurements), "
            # 🚨 LE MÊME FILTRE QUE `SELECT_BATCH`, et sans lui ce chiffre MENT.
            #    Une seule ligne non publiable — compteur refusé par le cloud, ou mesure
            #    orpheline dont le `pdl_index` n'est plus dans `pdl` — ÉPINGLE le
            #    minimum : le retard affiché ne redescend plus JAMAIS. Conséquences
            #    mesurées : la cadence reste verrouillée sur PERIOD_RETARD (10 s au lieu
            #    de 60, en permanence), et le contrôle « le retard doit DÉCROÎTRE » que
            #    l'update exige ne peut plus passer.
            "       (SELECT min(m.rowid) FROM measurements m "
            "          JOIN pdl p ON p.pdl_index = m.pdl_index "
            "         WHERE m.sent = 0 AND p.ref IS NOT NULL AND p.ref <> '')").fetchone()
    except sqlite3.OperationalError as e:
        if "no such table" not in str(e):
            raise
        log.info("base encore vide (%s) — 0 point en attente", e)
        return 0
    if not row or row[0] is None or row[1] is None:
        return 0
    return max(0, row[0] - row[1] + 1)


# ── Hello ─────────────────────────────────────────────────────────────────────

def _meta(conn: sqlite3.Connection, quoi: str, sql: str, mapper) -> list:
    """Lit une table de métadonnées SANS jamais faire échouer le hello.

    🚨 Ces colonnes sont ajoutées par `ALTER TABLE` à l'ouverture en ÉCRITURE
    (`db.connect()`), or le publisher ouvre en LECTURE SEULE : il ne peut rien
    créer. Sur une base plus ancienne que le firmware — retour arrière, boîtier
    dont le lecteur n'a pas encore tourné — la requête lève `OperationalError`.

    Sans cette garde, une colonne manquante ferait échouer TOUT le hello, y
    compris la liste des PDL, qui elle est toujours lisible. On dégrade table par
    table : on renvoie une liste vide, on le dit, et le reste part quand même.
    Le cloud fusionne sans supprimer — une liste vide n'efface donc rien.
    """
    try:
        return [mapper(r) for r in conn.execute(sql)]
    except sqlite3.Error as e:
        log.warning("hello : %s illisible (%s) — envoyé sans", quoi, e)
        return []


def version_declaree(chemin: str = "") -> str:
    """La version que le cloud a ACCEPTÉE, ou `""` si on n'en sait rien.

    ⚠️ NE LÈVE JAMAIS, et tout ce qui cloche vaut `""` : fichier absent, illisible,
       tronqué par une coupure, JSON invalide. « On ne sait pas » et « jamais
       déclarée » doivent donner le MÊME comportement — une déclaration de plus,
       bornée par le plancher. L'arbitrage inverse (supposer que le cloud sait)
       laisserait la vue parc fausse pour toujours, et EN SILENCE : c'est très
       exactement le défaut qu'on ferme.
    """
    try:
        brut = pathlib.Path(chemin or VERSION_DECLAREE).read_text()
    except FileNotFoundError:
        return ""
    except OSError as e:
        log.warning("mémoire de version illisible (%s) — on redéclare", e)
        return ""
    try:
        return str(json.loads(brut).get("version") or "")
    except Exception as e:  # noqa: BLE001
        log.warning("mémoire de version incompréhensible (%s) — on redéclare", e)
        return ""


def memoriser_version_declaree(version: str, chemin: str = "") -> None:
    """Range la version que le cloud VIENT d'accepter. Ne lève jamais.

    🚨 APRÈS LE 2xx, JAMAIS AVANT. Écrite trop tôt, la condition deviendrait fausse
       alors que le cloud ignore encore la version : on retomberait sur le défaut
       d'origine, et sans drapeau pour le rattraper.

    ⭐ ÉCRITURE ATOMIQUE, et ce n'est pas du zèle : un fichier tronqué par une
       coupure se relirait comme une AUTRE version, donc ferait taire la déclaration
       au lieu de la provoquer — le seul mode de défaillance de ce fichier qui soit
       SILENCIEUX. Avec `os.replace`, on a l'ancienne valeur ou la neuve.

    ⚠️ Un échec d'écriture ne fait rien échouer : on redéclarera au tour suivant.
       Bruyant et sans perte plutôt que silencieux.
    """
    p = pathlib.Path(chemin or VERSION_DECLAREE)
    tmp = p.with_name(p.name + ".tmp")
    try:
        tmp.write_text(json.dumps({"version": version, "ts": int(time.time())}) + "\n")
        os.replace(tmp, p)
    except OSError as e:
        log.warning("version déclarée non mémorisée (%s) — on redéclarera", e)
        try:
            tmp.unlink()
        except OSError:
            pass


def motif_declaration(installee: str, declaree: str, manquants: list, refuses: set,
                      dernier: float, maintenant: float) -> str:
    """Le MOTIF de la déclaration à faire à ce tour, ou `""` s'il n'y en a pas.

    ⭐ Pure, et c'est délibéré : la DÉCISION et la ligne de journal qui l'explique
       sortent du même calcul. Deux calculs séparés finiraient par dire deux choses,
       et on relirait un journal qui n'explique plus ce que le code a fait.

    ① LA VERSION — la version installée n'est pas celle que le cloud a acceptée.
    ② LE PDL SANS REF — compteur neuf, remplacé, ou ref perdue en local.

    Les deux sont des CONDITIONS : ni l'une ni l'autre ne se perd si le tour qui
    l'observe échoue.
    """
    # ⚠️ UNE VERSION INSTALLÉE VIDE NE SE DÉCLARE PAS. `device.json` illisible ou
    #    amputé apprendrait au cloud un `sw` VIDE — pire que périmé, puisque plus
    #    rien ne dirait que la valeur est à retrouver.
    version_a_dire = bool(installee) and declaree != installee
    motifs = []
    if version_a_dire:
        motifs.append(f"version installée {installee}, "
                      f"déclarée {declaree or 'jamais'}")
    if manquants:
        motifs.append(f"pdl sans ref {sorted(manquants)}")
    if not motifs:
        return ""

    # 🚨 LE PLANCHER S'APPLIQUE AUSSI À LA VERSION. Un cloud qui refuse (pas encore
    #    déployé, 4xx, 5xx) laisse la condition VRAIE — c'est sa force — donc sans
    #    plancher le boîtier redéclarerait à chaque tour, soit toutes les 10 s quand
    #    le retard est gros. C'est la rafale que le plancher existe pour empêcher, et
    #    elle s'est déjà produite avec le drapeau.
    plancher = DECLARATION_PLANCHER_S
    # ⭐ LE PLANCHER LONG ne vaut QUE si la version est à jour : « plus rien à
    #    apprendre » est faux tant qu'une version attend d'être dite, et six heures
    #    de vue parc fausse à cause d'un ADS non conforme serait un défaut pour un
    #    autre.
    if not version_a_dire and set(manquants) <= set(refuses):
        plancher = DECLARATION_PLANCHER_REFUS_S
    if maintenant - dernier < plancher:
        return ""
    return " · ".join(motifs)


def declare(cli: Client, conn: sqlite3.Connection, dev: dict) -> bool:
    """LA DÉCLARATION — « ce que le boîtier EST ». Rend True sur un 2xx.

    ⭐ Rare, et sur DEUX CONDITIONS réconciliées à chaque tour : tant qu'un pdl est
       sans `ref`, et tant que la version installée n'est pas celle que le cloud a
       acceptée (`motif_declaration`). 🚨 PAS au démarrage du publisher — un
       redémarrage n'est ni une OTA ni une redéclaration d'identité ; c'est la
       MÉMOIRE de la version, pas le démarrage, qui décide.

    🚨 C'EST LA SEULE REQUÊTE QUI TRANSPORTE L'ADS, et c'est tout l'objet du
       chantier : l'identifiant du compteur ne voyage qu'ici, jamais avec les
       mesures. À un envoi par minute, l'alternative aurait été 525 600 fois par an.

    ⚠️ On déclare TOUT ce que contient la table `pdl`, y compris un ADCO vide ou
       aberrant. Le PDL fantôme de ben-0001 (ADCO de deux octets nuls, 13 056 lignes
       du 18/08) est un FAIT : le boîtier a bien reçu quelque chose. Le filtrer ici
       le détruirait, et un filtre de format trop strict perdrait en plus de la
       vraie donnée, en silence. Le cloud rend un MOTIF pour ce pdl.
    """
    pdls = [{"index": r[0], "adco": r[1] or ""}
            for r in conn.execute("SELECT pdl_index, adco FROM pdl ORDER BY pdl_index")]
    payload = {"sw": dev.get("softwareVersion", ""),
               "model": dev.get("model", ""),
               "pdl": pdls}
    # ⓘ `fw` est la version de l'ÉMETTEUR, relayée par le boîtier (ben-docs#12).
    #    Absente aujourd'hui : on ne l'envoie donc PAS plutôt que d'envoyer "" —
    #    le serveur distingue « absent » de « vide », et `COALESCE(NULLIF(…,''))`
    #    n'écraserait rien, mais autant ne pas mentir sur le fil.
    fw = dev.get("firmwareVersion") or ""
    if fw:
        payload["fw"] = fw

    status, body = cli.post("/hello", payload)
    if not (200 <= status < 300):
        log.warning("déclaration refusée : HTTP %d %s", status, body[:200])
        return False

    # ── Ranger les refs rendues ───────────────────────────────────────────────
    #
    # 🚨 C'EST ICI QUE LE BOÎTIER APPREND À PUBLIER. Sans ces refs, `fetch_batch`
    #    ne rend aucun lot et rien ne part — ce n'est pas une panne, c'est une
    #    attente, mais elle ne se lève que par cette réponse.
    #
    # ⚠️ Un corps illisible ne doit PAS faire échouer la déclaration : le serveur a
    #    accepté (2xx), donc l'identité est enregistrée de son côté. On le crie et
    #    on réessaiera au prochain déclencheur, plutôt que de rejouer en boucle une
    #    requête qui a réussi.
    try:
        refs = {int(e["pdl"]): e.get("ref") or "" for e in json.loads(body).get("refs", [])}
        motifs = {int(e["pdl"]): e["motif"] for e in json.loads(body).get("refs", [])
                  if e.get("motif")}
    except Exception as e:  # noqa: BLE001
        log.error("déclaration acceptée mais réponse illisible (%s) — aucune ref rangée : %s",
                  e, body[:200])
        return True

    n = db.store_refs(conn, refs)
    # 🚨 RANGER LE MOTIF, et c'est ce qui empêche la redéclaration en boucle : un
    #    refus est un ÉTAT STABLE, pas un échec transitoire.
    db.store_motifs(conn, motifs)
    for pdl_index, motif in motifs.items():
        # 🚨 ON CRIE LE MOTIF. Un pdl sans ref ne publiera JAMAIS. Si la raison
        #    n'apparaît nulle part, on observera un boîtier qui déclare en boucle
        #    sans jamais savoir pourquoi — exactement l'angle mort de neuf jours.
        log.error("pdl %d SANS ref — motif « %s » : ses mesures ne partiront pas",
                  pdl_index, motif)
    log.info("déclaration OK — %d compteur(s) déclaré(s), %d ref(s) rangée(s), %d refusé(s)",
             len(pdls), n, len(motifs))
    return True


def heartbeat(cli: Client, conn: sqlite3.Connection, dev: dict) -> None:
    """LE BATTEMENT — « ce que le boîtier VIT ». Ne lève jamais.

    ⭐ C'est l'ancien `/hello` reconnu pour ce qu'il était déjà : un signe de vie
       qui transporte l'instantané de santé. Son code est conservé ; seul le
       clavage des métadonnées change, de `pdl_index` vers `ref`.

    ⚠️ PUREMENT INFORMATIF : s'il échoue, les mesures partent quand même. Coupler
       les deux ferait qu'une coupure réseau bloquerait la collecte pour une requête
       de métadonnées.
    """
    # 🚨 LE CLAVAGE PAR `ref` CONTRAINT L'ORDRE TOUT SEUL : un pdl sans ref est
    #    absent de cette table, donc ses métadonnées ne partent pas — et il n'y a
    #    aucun drapeau à tenir pour obtenir ce comportement.
    refs = db.known_refs(conn)

    def key_by_ref(lignes, champ_pdl="pdl"):
        """Remplace l'index local par la ref, et JETTE ce qui n'en a pas."""
        out = []
        for e in lignes:
            r = refs.get(e[champ_pdl])
            if not r:
                continue
            e = {k: v for k, v in e.items() if k != champ_pdl}
            e["ref"] = r
            out.append(e)
        return out

    # ⭐ On envoie le contenu ENTIER de ces tables à chaque battement, même
    #    inchangé. C'est délibéré : le boîtier n'a alors AUCUN état à mémoriser sur
    #    ce que le cloud sait. Un envoi « sur changement seulement » créerait une
    #    divergence SILENCIEUSE ET DÉFINITIVE — une table absente est indiscernable
    #    d'une table vide. Coût mesuré sur les vraies données de ben-0001 : 524 o
    #    bruts, 276 o gzippés, soit 98 ko/an contre 270 Mo/an de mesures.
    #
    # 🚨 `contract_epoch` n'est pas un confort : sans elle, `index_id` est un entier
    #    sans signification. Sur ben-0001, `index_id = 1` valait « BASE » avant le
    #    12/08 et « HC BLEU » après — même numéro, deux tarifs.
    epochs = _meta(conn, "contract_epoch",
                   "SELECT pdl_index, ts_start, ngtf FROM contract_epoch "
                   "ORDER BY pdl_index, ts_start",
                   lambda r: {"pdl": r[0], "ts_start": r[1], "ngtf": r[2] or ""})

    labels = _meta(conn, "tariff_labels",
                   "SELECT pdl_index, src_standard, index_id, ngtf, label "
                   "FROM tariff_labels ORDER BY pdl_index, src_standard, index_id, ngtf",
                   lambda r: {"pdl": r[0], "std": bool(r[1]), "id": r[2],
                              "ngtf": r[3] or "", "label": r[4] or ""})

    # ⚠️ `pref` est stocké en kVA côté boîtier (c'est l'unité du champ TIC standard
    #    `PREF`) alors que la colonne cloud est documentée en VA. On convertit ICI,
    #    explicitement, plutôt que de laisser deux unités porter le même nom.
    profile = _meta(conn, "level_profile",
                    "SELECT pdl_index, isousc, pref, ngtf, src_standard, "
                    "       papp_max_alltime FROM level_profile ORDER BY pdl_index",
                    lambda r: {"pdl": r[0], "isousc": r[1],
                               "pref": None if r[2] is None else r[2] * 1000,
                               "ngtf": r[3] or "",
                               "std": None if r[4] is None else bool(r[4]),
                               "papp_max": r[5]})

    # ── Les DROITS nés localement ────────────────────────────────────────────
    #
    # 🚨 DÉPLACÉS DU HELLO VERS LE BATTEMENT au rebase du 03/10, et le critère le
    #    commande : /hello porte ce que le boîtier EST — identité, versions,
    #    compteurs lus, et ça change par ÉVÉNEMENT. Des droits d'accès changent en
    #    continu : quelqu'un revendique, quelqu'un est coupé. C'est donc ce qu'il
    #    VIT, et ça voyage dans /ping.
    #
    # ⚠️ Et ce n'était pas qu'une question de rangement : depuis pi-0.9.27 le
    #    serveur refuse les champs inconnus (`DisallowUnknownFields`), et le
    #    `/hello` étroit ne déclare que sw·fw·model·pdl. Laissé dans le hello, ce
    #    champ partait en 400 `bad_body` — le rebase aurait « réussi » et la
    #    fonctionnalité serait morte EN SILENCE.
    #
    # ⭐ Le déplacement SERT leur intention d'origine. Elle était : « poussés en
    #    entier à chaque hello, donc auto-réparateur — une ligne que le cloud aurait
    #    perdue revient au hello suivant ». Or le hello est devenu RARE
    #    (événementiel) et le battement est QUOTIDIEN : la réparation est plus
    #    rapide dans le ping qu'elle ne l'était dans le hello.
    #
    # ⭐ Poussés EN ENTIER, sans boîte d'envoi : la table fait 1 à 5 lignes — un
    #    foyer et ses habitants. Avec un `sent = 1`, une ligne perdue le serait
    #    POUR TOUJOURS, et le boîtier se croirait à jour.
    #
    # 🚨 AJOUT SEULEMENT, le cloud ne retire rien sur cette base. La liste du
    #    boîtier est un SOUS-ENSEMBLE de `device_access` : quelqu'un d'inscrit côté
    #    cloud qui n'a jamais revendiqué n'apparaît pas ici. En déduire une
    #    suppression effacerait des droits parfaitement valides.
    #
    # ⚠️ La RÉVOCATION ne passe donc pas par là : c'est l'app qui la fait des deux
    #    côtés, elle seule étant à la fois sur le LAN et sur Internet
    #    (cf. `access.revoke_person`). Rien ne descend du cloud vers le boîtier.
    #
    # ⚠️ L'import est au niveau du MODULE (voir en tête), pas ici. Écrit d'abord en
    #    local sous `try`, il échouait en silence — `import access` ne résout pas
    #    depuis ce répertoire, c'est `from store import access`. Le battement serait
    #    parti sans les droits, avec un warning quotidien que personne ne lit. Un
    #    import de module échoue AU DÉMARRAGE, et bruyamment.
    #
    # ⓘ `access.db` est une base SÉPARÉE (/var/lib/ben-firmware/access.db) avec sa
    #    propre connexion sérialisée : aucune interférence avec `conn`.
    acces = []
    try:
        with access.session() as ac:
            # 🔒 La projection vit dans `access`, pas ici : c'est elle qui garantit
            #    que le prénom local ne monte JAMAIS au cloud, et le banc la vérifie
            #    nommément. Le cloud reçoit de quoi DÉCIDER — qui, quel rôle, coupé
            #    ou non — et rien de plus.
            acces = access.for_cloud(ac)
    except Exception as e:  # noqa: BLE001
        # La LECTURE peut légitimement échouer (base verrouillée) — et ne doit pas
        # empêcher le battement de partir, ni les mesures.
        log.warning("accès locaux illisibles (%s) — battement sans eux", e)

    payload = {"contract_epoch": key_by_ref(epochs),
               "tariff_labels": key_by_ref(labels),
               "meter_profile": key_by_ref(profile),
               "access": acces}

    # ── L'instantané de santé (#16) ───────────────────────────────────────────
    #
    # 🚨 POURQUOI ICI, dans une requête qu'on qualifie soi-même de « purement
    #    informative » : un boîtier qui CESSE DE MESURER continue de dire bonjour.
    #    `last_seen` reste frais côté cloud, l'OTA passe, et rien ne le signale.
    #    Constaté le 2026-10-01 sur un boîtier du parc — NEUF JOURS de silence,
    #    découverts par hasard, sur une machine injoignable (ni SSH ni VPN).
    #
    # ⭐ Coût mesuré sur un Pi Zero du parc : ~2,4 s de collecte, 169 o gzippés.
    #
    # ⚠️ `snapshot()` NE LÈVE JAMAIS : chaque sonde y est isolée et se replie sur
    #    l'absence de son champ. Le `try` ci-dessous est une ceinture de plus, pas
    #    une excuse — si un jour il attrape quelque chose, c'est health.py qui a un
    #    défaut, et le battement doit partir quand même.
    try:
        snap = health.snapshot(conn, dev, db.DB_PATH)
    except Exception as e:  # noqa: BLE001
        log.warning("santé illisible (%s) — battement envoyé sans", e)
        snap = None
    if snap:
        payload["health"] = snap

    status, body = cli.post("/ping", payload)
    if 200 <= status < 300:
        log.info("battement OK — %d époque(s), %d libellé(s), %d profil(s)",
                 len(payload["contract_epoch"]), len(payload["tariff_labels"]),
                 len(payload["meter_profile"]))
    else:
        log.warning("battement refusé : HTTP %d %s", status, body[:200])



# ── Boucle ────────────────────────────────────────────────────────────────────

_stop = False


def _on_signal(signum, _frame):
    global _stop
    _stop = True
    log.info("signal %d — arrêt après le lot en cours", signum)


def presenter_le_ticket(cli) -> None:
    """Présente le ticket reçu en BLE, UNE fois, à la première connexion au cloud.

    🚨 C'EST LE CHEMIN QUI REND UN BOÎTIER NEUF REVENDICABLE. Sans lui, `grant()` n'est
    appelé que depuis `mint()` et `consume_invitation()` : un boîtier envoyé à un
    client est inrevendicable, et les 7 du parc ne marchent que parce que leurs lignes
    ont été semées à la main — un rattrapage de migration, pas une procédure.

    ⭐ TOUTE L'ASYMÉTRIE DU DÉBALLAGE TIENT ICI : le téléphone avait Internet, le
    boîtier non. Le ticket a donc été frappé par l'app, passé en BLE, persisté sur le
    disque — et c'est maintenant, au premier lien, qu'il se présente.

    🚨 `fonder=True` NE PART QUE D'ICI, et l'existence du fichier en est la PREUVE.
    Un `/claim` arrivé par le LAN envoie toujours `false` : le boîtier ne sait pas si
    le cloud a déjà un owner, mais il sait par quel CANAL le ticket est arrivé — et
    c'est la seule moitié de la garde qu'il soit en position de tenir.

    ⚠️ ON NE FRAPPE AUCUN JETON LOCAL ICI, et ce n'est pas un oubli : la session BLE
    est finie, il n'y a personne à qui le rendre. ⇒ Voie A (tranchée le 05/10) : l'app
    refrappe un ticket et rejoue un `/claim` ordinaire sur le LAN. C'est ce qui a rendu
    `CLAIM_TOKEN` inutile.

    ⓘ Ne lève jamais : un déballage qui échoue ne doit pas empêcher la collecte.
    """
    ticket = claim_ticket.lire()
    if not ticket:
        return

    try:
        statut, brut = cli.post("/claim", claim_ticket.charge(ticket, fonder=True))
    except Exception as e:  # noqa: BLE001
        # ⭐ ON GARDE LE FICHIER. Le cloud peut être injoignable longtemps au premier
        #    démarrage — WiFi qui se monte, DNS, ADSL. Effacer ici rendrait le boîtier
        #    définitivement inrevendicable pour une panne passagère.
        log.warning("ticket non présenté (%s) — conservé, on réessaiera", e)
        return

    v = claim_ticket.verdict(statut, brut.encode())

    if isinstance(v, claim_ticket.CloudInjoignable):
        log.warning("ticket : le cloud a répondu %d — conservé", statut)
        return

    if v is not None:
        # 🚨 REFUS DÉFINITIF ⇒ ON EFFACE. Un ticket consommé, expiré ou frappé pour un
        #    autre boîtier ne redeviendra jamais valable : le garder le ferait
        #    présenter à CHAQUE démarrage, pour rien, et masquerait le vrai état du
        #    boîtier dans les journaux.
        log.error("ticket REFUSÉ (%s: %s) — effacé, un nouveau déballage est "
                  "nécessaire", type(v).__name__, v)
        claim_ticket.effacer()
        return

    try:
        rep = json.loads(brut)
        uid = str(rep.get("uid") or "").strip()
        role = str(rep.get("role") or "").strip()
    except Exception as e:  # noqa: BLE001
        log.error("ticket : réponse illisible (%s) — conservé", e)
        return

    # 🚨 ON NE VALIDE QUE L'uid, PAS LE RÔLE — et la distinction s'est mesurée.
    #
    #    `access.grant` refuse DÉJÀ un rôle inconnu (`ValueError`), attrapé plus bas :
    #    valider ici aussi ne changeait RIEN d'observable, donc aucune mutation ne
    #    pouvait détecter la disparition de ce contrôle. Deux endroits où la liste des
    #    rôles pouvait diverger, pour rien.
    #
    # ⚠️ L'uid, LUI, N'EST VALIDÉ NULLE PART AILLEURS : `grant` ne le regarde pas, et
    #    un uid vide s'insérerait tel quel — posant un `owner` qui n'est personne, donc
    #    un boîtier qui se croit revendiqué et ne l'est pas.
    #
    # ⓘ Conservé dans les deux cas : une réponse 200 mal formée est un défaut de
    #    CONTRAT, pas un refus. L'effacer condamnerait le boîtier pour un bug de notre
    #    côté.
    if not uid:
        log.error("ticket : réponse 200 sans uid (role=%r) — conservé", role)
        return

    try:
        with access.session() as ac:
            access.grant(ac, uid, role)
    except Exception as e:  # noqa: BLE001
        # ⚠️ Conservé AUSSI : le cloud a écrit sa ligne, mais pas nous. Réessayer
        #    rejouera le `/claim`, qui trouvera la ligne EXISTANTE et la rendra — le
        #    ticket étant consommé, il faudra toutefois un nouveau déballage.
        log.error("ticket : droit local non écrit (%s) — conservé", e)
        return

    # 🔒 L'uid est journalisé, jamais le ticket : c'est un secret à usage unique, et
    #    `journalctl` est lisible.
    log.info("DÉBALLAGE : %s est désormais %s de ce boîtier", uid, role)
    claim_ticket.effacer()


def main() -> int:
    installer_journal()
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    dev = caps.load_device()
    device_id = dev.get("deviceId")
    if not device_id:
        log.error("device.json sans deviceId (%s) — rien à publier", DEVICE_JSON)
        return 1

    for f in ("device.crt", "device.key", "root-ca.crt"):
        if not Path(CERT_DIR, f).exists():
            log.error("certificat manquant : %s/%s", CERT_DIR, f)
            return 1

    conn = open_db()
    cli = Client(device_id)
    log.info("démarrage — %s → https://%s:%d · lots de %d toutes les %.0f s",
             device_id, API_HOST, API_PORT, BATCH, PERIOD)
    log.info("~%d point(s) en attente", pending_approx(conn))

    def hello() -> float:
        """Envoie le BATTEMENT et renvoie l'échéance du prochain. Ne lève jamais :
        un échec de métadonnées ne doit pas empêcher la collecte de partir.

        ⓘ Le nom reste `hello` parce que toute la boucle l'appelle et que ce qu'il
           cadence — le battement quotidien et l'escalade sur échec — n'a pas
           changé. Ce qui a changé est la ROUTE : `/ping` au lieu de `/hello`.
        """
        try:
            # On relit device.json à chaque fois : après une OTA, la version a
            # changé sur le disque sans que ce service ait redémarré.
            heartbeat(cli, conn, caps.load_device() or dev)
        except Exception as e:
            log.warning("battement impossible (%s) — on publie quand même", e)
        return time.monotonic() + HELLO_EVERY

    dernier_declare = float("-inf")

    def declare_if_needed() -> None:
        """LA DÉCLARATION, sur ses DEUX conditions. Ne lève jamais.

        ① la version installée n'est pas celle que le cloud a ACCEPTÉE
        ② un pdl est SANS ref — compteur neuf, remplacé, ou ref perdue

        ⭐ DEUX CONDITIONS ET AUCUN ÉVÉNEMENT : les deux se re-vérifient à chaque
           tour, localement. Plus de drapeau à poser, donc plus rien à oublier dans
           un `update.sh` — c'est tout l'objet de #38, et le troisième déclencheur
           (« après une OTA ») a disparu en tant que tel : il est devenu un CAS
           PARTICULIER de ①.
        """
        nonlocal dernier_declare
        # ⓘ L'ANCIEN DRAPEAU SE RETIRE SANS SE LIRE. Cf. `DRAPEAU_LEGUE` : ne rien en
        #    déduire est le seul moyen de n'avoir qu'un mécanisme. Ça ne coûte qu'un
        #    `unlink` qui échoue, dans 100 % des tours une fois le parc passé.
        try:
            os.unlink(DRAPEAU_LEGUE)
            log.info("ancien drapeau %s retiré — la version est désormais réconciliée "
                     "à chaque tour, il ne sert plus", DRAPEAU_LEGUE)
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("ancien drapeau %s non retiré (%s) — sans conséquence, "
                        "il n'est plus lu", DRAPEAU_LEGUE, e)

        # 🚨 ON RELIT `device.json` À CHAQUE TOUR, et c'est ce qui rend la condition
        #    possible : après une OTA la version a changé SUR LE DISQUE sans que ce
        #    service ait redémarré — et quand il redémarre (l'agent le fait à l'étape
        #    ⑩, après le bump ⑨), il relit de toute façon.
        dev_courant = caps.load_device() or dev
        installee = dev_courant.get("softwareVersion", "")
        try:
            manquants = db.pdls_without_ref(conn)
        except sqlite3.Error as e:
            # ⚠️ Une base locale qui bronche n'est pas une raison de déclarer : on ne
            #    sait pas s'il faut. Même garde que `cadence_sure`. ⓘ Et ça ne perd
            #    rien de la version : `declare()` lit la table `pdl` lui aussi, donc
            #    il échouerait de toute façon — mais au tour suivant la condition est
            #    toujours là, ce que le drapeau ne garantissait pas.
            log.warning("pdl sans ref illisible (%s) — déclaration reportée", e)
            return
        refuses: set = set()
        if manquants:
            # ⓘ Lue seulement quand il y a des manquants : c'est le seul cas où elle
            #    peut changer le plancher.
            try:
                refuses = set(db.pdls_refuses(conn))
            except sqlite3.Error:
                refuses = set()
        motif = motif_declaration(installee, version_declaree(), manquants, refuses,
                                  dernier_declare, time.monotonic())
        if not motif:
            return
        dernier_declare = time.monotonic()
        log.info("déclaration : %s", motif)
        try:
            ok = declare(cli, conn, dev_courant)
        except Exception as e:  # noqa: BLE001
            log.warning("déclaration impossible (%s) — on réessaiera", e)
            return
        # 🚨 ON MÉMORISE CE QUI A ÉTÉ ENVOYÉ, pas ce qui est sur le disque MAINTENANT :
        #    `declare()` a construit son corps avec `dev_courant`, donc avec
        #    `installee`. Relire `device.json` ici pourrait mémoriser une version
        #    qu'on n'a pas dite, si une OTA s'est terminée pendant la requête.
        if ok and installee:
            memoriser_version_declaree(installee)
    # ① L'init. Au PREMIER démarrage, aucun pdl n'a de ref ET aucune version n'a été
    #    déclarée : ceci déclare, deux fois motivé.
    #    ⭐ Et c'est ici que le parc se recale : au premier démarrage APRÈS l'OTA qui
    #       livre ce code, la mémoire de version n'existe sur aucun boîtier, donc
    #       chacun déclare une fois — sans qu'on touche à un boîtier.
    #    ⚠️ Mais un redémarrage n'est toujours PAS une redéclaration : dès que la
    #       mémoire existe et vaut la version installée, les deux conditions sont
    #       fausses et rien ne part.
    declare_if_needed()
    # 🚨 LE TICKET D'ABORD, s'il y en a un. Avant le premier battement : c'est ce qui
    #    fonde le propriétaire, et tout le reste du parcours en dépend.
    # ⓘ Ne fait rien dans 99,99 % des démarrages — le fichier n'existe qu'entre le
    #    déballage en BLE et la première connexion réussie.
    presenter_le_ticket(cli)

    prochain_hello = hello()
    echecs = 0
    # ⚠️ `-inf` et non `0.0` : `time.monotonic()` part de l'uptime, pas de zéro. Avec `0.0`,
    #    le tout premier signalement serait immédiat sur un boîtier debout depuis longtemps et
    #    retardé d'une heure sur un boîtier qui vient de démarrer — deux comportements pour un
    #    seul code. `-inf` dit ce qu'on veut dire : aucun signalement n'a encore été fait.
    dernier_hello_echec = float("-inf")
    # 🚨 UN COMPTEUR À PART, et c'est tout l'objet de la garde ci-dessous : `echecs` parle du
    #    SERVEUR, celui-ci parle de la BASE LOCALE. Les mélanger, c'est ce qui a fait accuser
    #    le serveur pendant neuf jours d'un disque abîmé.
    echecs_base = 0
    while not _stop:
        # 🚨 **LE TICKET SE REPRÉSENTE TANT QU'IL EST LÀ.** Il n'était présenté qu'au
        #    DÉMARRAGE — or quatre sorties de `presenter_le_ticket` journalisent
        #    « conservé, on réessaiera » et rien ne réessayait : cloud injoignable,
        #    DNS ou WiFi pas encore montés après le reboot, 5xx de ben-api, réponse
        #    sans uid, octroi local en échec. Aucun de ces cas ne fait mourir le
        #    publisher, donc systemd ne le relançait pas : au bout de 900 s le ticket
        #    expirait et le boîtier restait SANS PROPRIÉTAIRE — exactement ce que le
        #    ticket existe pour éviter, et il faut alors rouvrir une fenêtre BLE.
        #
        # ⭐ AUCUN COMPTEUR D'ESSAIS N'EST NÉCESSAIRE, et c'est ce qui rend la
        #    reprise simple : passé 900 s le cloud répond `bad_ticket`, `verdict()`
        #    en fait un refus DÉFINITIF, et le fichier est effacé. La boucle s'éteint
        #    donc d'elle-même — par le succès, ou par la péremption.
        #
        # ⓘ Le recul est celui de la boucle (60 s), soit 15 tentatives au plus. Et
        #    `lire()` ne coûte qu'un `open()` qui échoue, dans 99,99 % des tours.
        if claim_ticket.lire():
            presenter_le_ticket(cli)

        # 🚨 HORS DU `try`, ET C'EST TOUT L'INTÉRÊT. Dedans, le `raise` d'un lot
        #    refusé sautait la déclaration à chaque tour : un boîtier dont le cloud
        #    ne reconnaît plus une ref ne pouvait JAMAIS la renouveler, puisque
        #    `pdls_without_ref` restait vide. Il restait bloqué pour toujours, et
        #    avec lui les points des AUTRES compteurs du même lot.
        #
        # ⓘ Elle ne coûte qu'une requête sur une table de 7 lignes, et le plancher
        #    empêche toute rafale.
        declare_if_needed()
        try:
            # 🚨 `lots`, PAS `points` : fetch_batch rend des lots groupés par
            #    compteur. La première version de ce chantier a converti
            #    `fetch_batch` sans convertir son appelant — le corps partait en
            #    `{"points": [ {"ref":…, "points":[…]} ]}`, l'API refusait au
            #    décodage (`DisallowUnknownFields`), et le boîtier prenait un 400
            #    à chaque tour POUR TOUJOURS sans rien publier.
            #
            # ⚠️ Aucune perte dans ce cas-là, et uniquement grâce à une ligne du
            #    SERVEUR : sans `DisallowUnknownFields`, `lots` aurait été vide, le
            #    serveur aurait répondu 200 {"received":0}, et `mark_sent` aurait
            #    passé les 1000 rowid à sent=1 — le lot perdu pour toujours. Un
            #    invariant ne doit pas dépendre d'un réglage de l'autre côté du fil :
            #    d'où le contrôle de `received` plus bas.
            rowids, lots = fetch_batch(conn, BATCH)
            # 🚨 ICI, PAS DANS LA BRANCHE 2xx — défaut affiné par la revue. `fetch_batch` a
            #    réussi : la base se LIT, et c'est exactement ce que ce compteur mesure. Le
            #    remettre à zéro seulement après un envoi réussi le laissait grimper pendant
            #    toute une panne SERVEUR (ou sur une base vide), si bien que cinq `database is
            #    locked` isolés — qui sont NORMAUX, le lecteur écrit en continu — finissaient
            #    par déclencher un faux signalement.
            echecs_base = 0
            if lots:
                n_points = sum(len(l["points"]) for l in lots)
                status, body = cli.post("/measurements", {"lots": lots})
                if 200 <= status < 300:
                    # 🚨 ON VÉRIFIE QUE LE SERVEUR A BIEN REÇU CE QU'ON A ENVOYÉ,
                    #    AVANT DE MARQUER. `sent=1` ne se défait pas : un 2xx qui
                    #    annonce moins de points que le lot n'en portait veut dire
                    #    qu'une partie n'est pas entrée, et les marquer serait une
                    #    perte DÉFINITIVE.
                    #
                    # ⭐ C'est ce qui rend l'invariant AUTOPORTANT. Sans ce contrôle,
                    #    il repose sur `DisallowUnknownFields` côté serveur : un
                    #    corps mal formé y décoderait en lot vide, le serveur
                    #    répondrait 200 {"received": 0}, et les 1000 rowid
                    #    passeraient à sent=1 sans qu'un seul point soit entré.
                    #
                    # ⓘ `inserted` < `received` reste NORMAL (8,2 % des points
                    #    partagent leur horodatage à la seconde avec un voisin) :
                    #    on ne compare QUE `received`, jamais `inserted`.
                    recu = None
                    try:
                        recu = json.loads(body).get("received")
                    except Exception:  # noqa: BLE001
                        pass
                    if recu is not None and recu != n_points:
                        raise RuntimeError(
                            f"le serveur annonce received={recu} pour {n_points} "
                            "points envoyés — lot NON marqué")
                    mark_sent(conn, rowids)
                    echecs = 0
                    # ⚠️ `inserted < envoyé` est NORMAL et permanent, ce n'est PAS un
                    # défaut : 8,2 % des points du boîtier partagent leur (pdl_index,
                    # ts) avec un voisin — mesuré sur ben-0001, 449 403 sur 5,46 M.
                    # Le boîtier écrit toutes les ~1,4 s avec un horodatage à la
                    # SECONDE, donc deux lectures tombent parfois dans la même. Le
                    # serveur en garde une (ON CONFLICT DO NOTHING). Sans effet sur la
                    # courbe, le NILM ou les index, qui sont monotones.
                    try:
                        r = json.loads(body)
                        # 🚨 `n_points`, pas `len(lots)` : journaliser le nombre de
                        #    COMPTEURS donnerait « envoyé 1 · reste ~513056 » pour
                        #    1000 points partis — la ligne de diagnostic que tout ce
                        #    fichier existe pour produire, rendue mensongère.
                        log.info("envoyé %d · inséré %s · reste ~%d",
                                 n_points, r.get("inserted"), pending_approx(conn))
                    except Exception:
                        log.info("envoyé %d · reste ~%d", n_points, pending_approx(conn))
                elif status == 403:
                    # Révocation : on ne sort PAS. Elle se lève, et le boîtier doit
                    # repartir tout seul sans qu'on aille le redémarrer.
                    log.error("refusé (HTTP 403 %s) — attente longue", body[:120])
                    echecs = max(echecs, 6)
                    raise RuntimeError("refusé par le serveur")
                else:
                    # 🚨 LE CORPS VOYAGE AVEC L'EXCEPTION, il ne reste pas dans un `warning`.
                    #
                    #    Première version : l'explication du serveur n'était QUE dans ce
                    #    `log.warning`, et la ligne escaladée en `error` au bout de cinq
                    #    échecs ne disait que « HTTP 400 ». Or c'est précisément l'explication
                    #    qui manque quand on diagnostique à distance — savoir qu'un lot est
                    #    refusé sans savoir POURQUOI, c'est le même angle mort que #18
                    #    prétendait fermer.
                    #
                    # ⚠️ Un 400 ou un 413 ne se résout pas en réessayant : le lot est
                    #    malformé ou trop gros, et le boîtier bouclera dessus. Le corps est
                    #    la seule chose qui dira laquelle des deux.
                    # 🚨 SI LE CLOUD NE RECONNAÎT PLUS UNE REF, ON L'OUBLIE.
                    #    Sans ça le boîtier renvoyait indéfiniment une ref périmée :
                    #    `pdls_without_ref` restait vide, donc rien ne redéclarait.
                    #    ⚠️ On efface TOUTES les refs, pas seulement la coupable —
                    #    le refus ne dit pas laquelle l'est, et la déclaration est
                    #    idempotente : elle les rend toutes au tour suivant.
                    if "non_rattache" in body or "ref_invalide" in body:
                        try:
                            n = db.invalider_refs(conn)
                            log.warning("le cloud ne reconnaît plus une ref — %d ref(s) "
                                        "oubliée(s), redéclaration au prochain tour", n)
                        except sqlite3.Error as e:
                            log.error("refs non invalidées (%s)", e)
                    raise RuntimeError(f"HTTP {status} {body[:200]}")
            else:
                # 🚨 `info`, ET PAS `debug` — le niveau racine est INFO, donc `debug` n'écrit
                #    RIEN. La branche « rien à envoyer » était donc totalement MUETTE, et un
                #    publisher qui échoue en boucle produisait exactement la même trace qu'un
                #    publisher qui n'a rien à envoyer : aucune. Impossible de les séparer à
                #    distance, et c'est ce qui a coûté neuf jours sur un boîtier du parc.
                #
                # ⭐ Et on y joint le retard, parce que c'est la CONTRADICTION qui informe :
                #    « rien à envoyer · reste ~569526 » dit en une ligne que `pending` et la
                #    réalité ne s'accordent pas. Les deux chiffres séparés ne disaient rien.
                #
                # ⚠️ Ne coûte rien sur un boîtier sain : à 0,74 point/s et une période de 60 s,
                #    chaque tour porte ~44 points — cette branche n'y est jamais atteinte.
                #
                # 🚨 `pending_approx()` interroge la base, et nous sommes DANS le `try` de la
                #    boucle : une erreur locale compterait comme un échec SERVEUR et ferait
                #    partir le publisher en backoff long. C'est exactement le défaut que
                #    `cadence_sure()` existe pour éviter.
                try:
                    log.info("rien à envoyer · reste ~%d", pending_approx(conn))
                except sqlite3.Error as e:
                    log.info("rien à envoyer (retard illisible : %s)", e)
            if time.monotonic() >= prochain_hello:
                prochain_hello = hello()
        except sqlite3.Error as e:
            # 🚨 UNE ERREUR DE LA BASE LOCALE N'EST PAS UNE PANNE DU SERVEUR.
            #
            #    Mesuré sur un boîtier du parc le 2026-10-01 : `fetch_batch` levait
            #    `database disk image is malformed` — une zone de `measurements` illisible,
            #    probablement l'usure de la carte SD. L'exception tombait dans le `except
            #    Exception` ci-dessous, était comptée dans `echecs`, poussait le backoff
            #    SERVEUR à 300 s et journalisait « échec n°57 » comme un échec de publication.
            #    Pendant neuf jours, ce boîtier a accusé le serveur d'un défaut de son disque.
            #
            # ⭐ Le dépôt avait déjà le précédent et ne l'avait pas appliqué ici :
            #    `cadence_sure()` garde `pending_approx` avec exactement ce commentaire — « une
            #    base locale qui bronche n'est pas un serveur en panne ». `fetch_batch`, lui,
            #    n'avait aucune garde.
            #
            # ⚠️ On se rendort sur `PERIOD`, JAMAIS sur `PERIOD_RETARD` : ne pas savoir LIRE ne
            #    doit pas faire accélérer. Même raisonnement que `cadence_sure()`.
            #
            # ⭐ Mais on SIGNALE quand même, et c'est ce qui a tout débloqué : le hello
            #    d'escalade de 0.9.24 a livré la cause vingt secondes après l'OTA.
            echecs_base += 1
            log.error("base locale illisible (%s) — %d fois de suite ; le serveur n'est PAS "
                      "en cause", e, echecs_base)
            if signaler_echec(echecs_base, dernier_hello_echec, time.monotonic()):
                dernier_hello_echec = time.monotonic()
                prochain_hello = hello()
            _sleep(PERIOD)
            continue
        except Exception as e:
            echecs += 1
            # 🚨 GIGUE TOTALE : on tire DANS l'intervalle. Voir l'en-tête, point 3.
            delai = random.uniform(0, min(2 ** echecs, BACKOFF_MAX))
            # Le niveau dépend de la PERSISTANCE — cf. `niveau_echec()`.
            log.log(niveau_echec(echecs),
                    "échec n°%d (%s) — nouvelle tentative dans %.0f s", echecs, e, delai)

            # 🚨 APRÈS le `log`, JAMAIS AVANT : `snapshot()` lit le journal. Envoyé d'abord,
            #    le hello partirait avec un instantané qui ne contient pas l'échec qui l'a
            #    déclenché — un signalement qui ne signale rien.
            #
            # ⚠️ Et AVANT `cli.close()` : si l'échec était un refus HTTP, la connexion est
            #    saine et le hello la réutilise. Si c'était la socket, `post()` la rouvre
            #    d'elle-même une fois.
            #
            # ⭐ `prochain_hello` est repoussé par `hello()` : le battement quotidien ne vient
            #    pas se superposer au signalement.
            if signaler_echec(echecs, dernier_hello_echec, time.monotonic()):
                dernier_hello_echec = time.monotonic()
                prochain_hello = hello()

            cli.close()
            _sleep(delai)
            continue

        # ⭐ GRATUIT : `pending_approx()` est O(1) — il encadre par les rowid, justement
        #    parce qu'un `count(*) WHERE sent=0` prenait 37 s sur Pi Zero. On peut donc
        #    l'interroger à chaque tour sans rien payer.
        #
        # 🚨 MAIS IL INTERROGE LA BASE, ET CE POINT EST HORS DU `try` DE LA BOUCLE.
        #    `_sleep(PERIOD)` ne pouvait rien lever ; celui-ci si — verrou tenu au-delà
        #    du timeout pendant que le lecteur écrit, ou erreur d'E/S sur la carte SD.
        #    Sans ce garde, l'exception remonte hors de `main()` : le process meurt sans
        #    passer par « arrêté proprement », systemd le relance, et on perd le backoff.
        #
        # ⭐ Le repli est `PERIOD`, jamais `PERIOD_RETARD` : NE PAS SAVOIR MESURER LE
        #    RETARD NE DOIT PAS FAIRE ACCÉLÉRER. Et on ne compte pas cet échec dans
        #    `echecs`, qui parle du SERVEUR — une base locale qui bronche n'est pas un
        #    serveur en panne.
        #
        # ⓘ Ce point est atteint après un envoi réussi, ou quand il n'y avait rien à
        #   envoyer. Le chemin d'échec, lui, sort plus haut par `continue` en gardant son
        #   backoff exponentiel à gigue totale : un serveur en panne ne déclenche donc
        #   JAMAIS la cadence de rattrapage.
        _sleep(cadence_sure(conn))

    cli.close()
    conn.close()
    log.info("arrêté proprement")
    return 0


def _sleep(seconds: float) -> None:
    """Sommeil interruptible : un SIGTERM ne doit pas attendre 60 s."""
    fin = time.monotonic() + seconds
    while not _stop and time.monotonic() < fin:
        time.sleep(min(1.0, fin - time.monotonic()))


if __name__ == "__main__":
    sys.exit(main())
