#!/usr/bin/env python3
"""
ben_publisher — pousse les mesures du boîtier vers `ben-api`.

Conception → ~/work/ben/docs/chantier-ingestion-cloud.md

    hello  au démarrage, puis toutes les heures (versions + compteurs).
           S'il échoue, ON CONTINUE.
    boucle toutes les 60 s : jusqu'à 500 points non envoyés, du plus ancien.

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
from store import db  # noqa: E402

# ── Réglages ──────────────────────────────────────────────────────────────────
# ⚠️ L'URL n'est écrite QU'ICI. La changer est une OTA d'une ligne — comme
#    RETENTION_DAYS. Déplacer le serveur, en revanche, ne demande QUE de changer
#    l'enregistrement DNS : c'est tout l'intérêt de passer par un nom.
API_HOST = os.environ.get("BEN_API_HOST", "api.benpilote.fr")
API_PORT = int(os.environ.get("BEN_API_PORT", "8443"))

BATCH = int(os.environ.get("BEN_PUB_BATCH", "500"))
PERIOD = float(os.environ.get("BEN_PUB_PERIOD", "60"))
BACKOFF_MAX = 300.0
# Le hello est rejoué périodiquement, pas seulement au démarrage :
#   - un NOUVEAU COMPTEUR peut apparaître en cours de route (resolve_pdl() crée un
#     pdl_index dès qu'un ADCO inconnu se présente : changement de compteur, nouvel
#     émetteur LoRa) — sans nouveau hello, le cloud reçoit des mesures pour un
#     pdl_index qu'il ne sait pas nommer ;
#   - après une OTA, `softwareVersion` change, mais nos updates redémarrent les
#     LECTEURS, pas forcément ce service : `devices.sw_version` resterait périmé.
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


SELECT_BATCH = """
SELECT rowid, ts, pdl_index, papp, iinst, src_standard,
       index_id, index_value, inject_total, tariff, meter_ts
FROM measurements
WHERE sent = 0
ORDER BY rowid
LIMIT ?
"""


def fetch_batch(conn: sqlite3.Connection, limit: int) -> tuple[list, list]:
    rows = conn.execute(SELECT_BATCH, (limit,)).fetchall()
    rowids, points = [], []
    for (rid, ts, pdl, papp, iinst, std, idx_id, idx_val,
         inject, tariff, meter_ts) in rows:
        rowids.append(rid)
        p = {"ts": ts, "pdl": pdl}
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
        points.append(p)
    return rowids, points


def mark_sent(conn: sqlite3.Connection, rowids: list) -> None:
    """🚨 PAR ROWID, et seulement après un 2xx. Voir l'en-tête, point 1."""
    conn.executemany("UPDATE measurements SET sent = 1 WHERE rowid = ?",
                     [(r,) for r in rowids])
    conn.commit()


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
    row = conn.execute(
        "SELECT (SELECT max(rowid) FROM measurements), "
        "       (SELECT min(rowid) FROM measurements WHERE sent = 0)").fetchone()
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


def send_hello(cli: Client, conn: sqlite3.Connection, dev: dict) -> None:
    """Déclare le boîtier et ses compteurs.

    ⚠️ PUREMENT INFORMATIF : s'il échoue, les mesures partent quand même. Coupler
    les deux ferait qu'une coupure réseau bloquerait la collecte pour une requête
    de MÉTADONNÉES.

    On déclare TOUT ce que contient la table `pdl`, y compris un ADCO vide ou
    aberrant. Le PDL fantôme de ben-0001 (ADCO de deux octets nuls, 13 056 lignes
    du 18/08) est un FAIT : le boîtier a bien reçu quelque chose. Le filtrer ici
    détruirait ce fait ; un filtre de format trop strict perdrait en plus de la
    vraie donnée, en silence. Le serveur assainit les octets nuls (PostgreSQL les
    refuse dans un `text`) et conserve la ligne.
    """
    pdls = [{"index": r[0], "adco": r[1] or ""}
            for r in conn.execute("SELECT pdl_index, adco FROM pdl ORDER BY pdl_index")]

    # ── Métadonnées : INSTANTANÉ COMPLET, pas un delta ───────────────────────
    #
    # ⭐ On envoie le contenu ENTIER de ces tables à chaque hello, même inchangé.
    #    C'est délibéré : le boîtier n'a alors AUCUN état à mémoriser sur ce que
    #    le cloud sait. Si le cloud perd la métadonnée (restauration, migration,
    #    reconstruction de la VM), elle revient d'elle-même sous 24 h.
    #    Un envoi « sur changement seulement » créerait une divergence SILENCIEUSE
    #    ET DÉFINITIVE — une table absente est indiscernable d'une table vide.
    #
    # Le coût mesuré sur les vraies données de ben-0001 : **524 o bruts, 276 o
    # gzippés**, soit 98 ko/an contre 270 Mo/an de mesures — 0,036 % du trafic.
    # C'est ce chiffre qui a fait renoncer à toute négociation (empreinte, delta,
    # drapeau serveur) : on protégeait trois cents octets avec un protocole.
    #
    # 🚨 `contract_epoch` n'est pas un confort : sans elle, `index_id` est un
    #    entier sans signification. Sur ben-0001, `index_id = 1` valait « BASE »
    #    avant le 12/08 et « HC BLEU » après — même numéro, deux tarifs.
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

    payload = {"sw": dev.get("softwareVersion", ""),
               "model": dev.get("model", ""),
               "pdl": pdls,
               "contract_epoch": epochs,
               "tariff_labels": labels,
               "meter_profile": profile}
    status, body = cli.post("/hello", payload)
    if 200 <= status < 300:
        log.info("hello OK — %d compteur(s) déclaré(s)", len(pdls))
    else:
        log.warning("hello refusé : HTTP %d %s", status, body[:200])


# ── Boucle ────────────────────────────────────────────────────────────────────

_stop = False


def _on_signal(signum, _frame):
    global _stop
    _stop = True
    log.info("signal %d — arrêt après le lot en cours", signum)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s][%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")
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
        """Envoie le hello et renvoie l'échéance du prochain. Ne lève jamais :
        un échec de métadonnées ne doit pas empêcher la collecte de partir."""
        try:
            # On relit device.json à chaque fois : après une OTA, la version a
            # changé sur le disque sans que ce service ait redémarré.
            send_hello(cli, conn, caps.load_device() or dev)
        except Exception as e:
            log.warning("hello impossible (%s) — on publie quand même", e)
        return time.monotonic() + HELLO_EVERY

    prochain_hello = hello()
    echecs = 0
    while not _stop:
        try:
            rowids, points = fetch_batch(conn, BATCH)
            if points:
                status, body = cli.post("/measurements", {"points": points})
                if 200 <= status < 300:
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
                        log.info("envoyé %d · inséré %s · reste ~%d",
                                 len(points), r.get("inserted"), pending_approx(conn))
                    except Exception:
                        log.info("envoyé %d · reste ~%d", len(points), pending_approx(conn))
                elif status == 403:
                    # Révocation : on ne sort PAS. Elle se lève, et le boîtier doit
                    # repartir tout seul sans qu'on aille le redémarrer.
                    log.error("refusé (HTTP 403 %s) — attente longue", body[:120])
                    echecs = max(echecs, 6)
                    raise RuntimeError("refusé par le serveur")
                else:
                    log.warning("HTTP %d %s", status, body[:200])
                    raise RuntimeError(f"HTTP {status}")
            else:
                log.debug("rien à envoyer")
            if time.monotonic() >= prochain_hello:
                prochain_hello = hello()
        except Exception as e:
            echecs += 1
            # 🚨 GIGUE TOTALE : on tire DANS l'intervalle. Voir l'en-tête, point 3.
            delai = random.uniform(0, min(2 ** echecs, BACKOFF_MAX))
            log.warning("échec n°%d (%s) — nouvelle tentative dans %.0f s",
                        echecs, e, delai)
            cli.close()
            _sleep(delai)
            continue

        _sleep(PERIOD)

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
