"""access — qui a le droit de parler à l'API locale, et avec quels appareils.

Base SÉPARÉE : /var/lib/ben-firmware/access.db

═══ POURQUOI UNE BASE, ET PAS UN FICHIER JSON ════════════════════════════════

Parce que DEUX PROCESSUS ÉCRIVENT :

    local_api.py      frappe un jeton à /claim, révoque depuis l'app
    ben_publisher.py  remonte les lignes `sent=0` au hello, les marque `sent=1`

Avec un JSON c'est une course perdue : le publisher lit, l'API lit, l'API écrit,
le publisher écrit — et la modification de l'API disparaît, en silence. Une
écriture atomique (`os.replace`) rend l'ÉCRITURE sûre, pas la séquence
lire-modifier-écrire. C'est exactement ce que SQLite gère.

═══ POURQUOI PAS DANS measurements.db ════════════════════════════════════════

L'API locale ouvre `measurements.db` en LECTURE SEULE — invariant du projet, les
lecteurs possèdent l'écriture. Y mettre les accès obligerait l'API à l'ouvrir en
écriture et ferait tomber l'invariant. D'où un fichier à part, que l'API ouvre en
écriture sans rien remettre en cause.

═══ DEUX ÉTAGES, ET C'EST CE QUI REND LA RÉVOCATION UTILISABLE ═══════════════

    une ligne `access`  = le DROIT d'une PERSONNE   (uid Firebase)
    une ligne `token`   = UN APPAREIL               (un téléphone, une intégration)

Un même uid porte PLUSIEURS jetons : deux téléphones, la même identité Google.

    révoquer un jeton  « j'ai perdu mon téléphone »  → l'iPad continue de marcher
    révoquer l'access  « Claire est partie »          → tous ses appareils tombent

Ne faire que le second laisse un jeton parfaitement vivant dans un téléphone :
c'est l'oubli qui donne l'illusion d'avoir coupé quelqu'un.

═══ CE QUI EST STOCKÉ ════════════════════════════════════════════════════════

JAMAIS le jeton — son empreinte. Carte SD volée ⇒ on voit que deux téléphones ont
accès, pas comment se faire passer pour eux.

L'`uid` Firebase est stocké EN CLAIR : c'est une ÉTIQUETTE, pas un secret. Il
circule dans l'app et dans les journaux, et le hacher ruinerait tout diagnostic.

═══ LE FLUX EST ENTIÈREMENT MONTANT ══════════════════════════════════════════

`device_access` (cloud) NE REDESCEND JAMAIS. Les lignes `access` d'ici naissent
LOCALEMENT — unboxing BLE, invitation par QR — et remontent au hello. Le rôle
d'un demandeur à /claim est lu chez `ben-api` À L'INSTANT où la question se pose ;
il n'y a donc aucune raison de verser la table au boîtier à l'avance.

⇒ Cette table est une BOÎTE D'ENVOI (`sent`), pas un miroir.

═══ CE QUE CE MODULE N'EST PAS ═══════════════════════════════════════════════

Un magasin. Il n'IMPOSE rien : c'est l'API locale qui décide d'exiger un jeton ou
non. En 0.9.16 elle l'ACCEPTE sans l'exiger — un boîtier du parc doit continuer
de répondre aux apps qui ne savent pas encore en présenter un.
"""
from __future__ import annotations

import contextlib
import hashlib
import secrets
import sqlite3
import threading
import time
from pathlib import Path

ACCESS_PATH = "/var/lib/ben-firmware/access.db"

# 32 octets d'entropie → 43 caractères en base64url. Très au-delà du devinable, et
# tient largement dans un PDU BLE (cf. l'invariant « 1 lecture = 1 PDU »).
TOKEN_OCTETS = 32

# Une invitation se consomme en main propre, dans la pièce. Dix minutes suffisent,
# et bornent la fenêtre pendant laquelle un QR photographié vaut quelque chose.
INVITATION_TTL_SEC = 600

# Rôles que LE BOÎTIER peut frapper. `mcp` n'est pas ici : ces jetons-là sont
# frappés par le cloud et ne descendent jamais (cf. §6ter.1 du chantier).
ROLE_OWNER = "owner"      # celui qui a déballé — UN SEUL par boîtier
ROLE_MEMBER = "member"    # conjoint, enfants — lire et piloter
ROLE_VIEWER = "viewer"    # intégrations (Home Assistant) — lecture seule, locale

ROLES = (ROLE_OWNER, ROLE_MEMBER, ROLE_VIEWER)

# Seuls `owner` et `member` désignent une PERSONNE (un uid Firebase) et ont donc
# une contrepartie dans `device_access`. Une intégration n'a pas de compte Google.
PERSON_ROLES = (ROLE_OWNER, ROLE_MEMBER)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS access (
    uid        TEXT PRIMARY KEY,
    role       TEXT    NOT NULL,
    updated_ts INTEGER NOT NULL,
    sent       INTEGER NOT NULL DEFAULT 0   -- 0 = pas encore remontée au cloud
);
CREATE INDEX IF NOT EXISTS idx_access_sent ON access(sent);

CREATE TABLE IF NOT EXISTS token (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash      TEXT UNIQUE,   -- sha256 du VRAI jeton — NULL tant que non échangé
    invitation_hash TEXT UNIQUE,   -- sha256 de l'invitation — NULL une fois consommée
    uid             TEXT,          -- NULL si non échangé, ou si intégration
    role            TEXT    NOT NULL,
    label           TEXT,
    created_ts      INTEGER NOT NULL,
    invitation_expiry_ts INTEGER,  -- INSTANT epoch, PAS une durée. L'invitation seulement
    last_used_ts    INTEGER        -- pour que l'owner VOIE ce qui ne sert plus
);
CREATE INDEX IF NOT EXISTS idx_token_uid ON token(uid);
"""


def connect(path: str = ACCESS_PATH) -> sqlite3.Connection:
    """Ouvre la base en écriture, crée le schéma, active WAL.

    `check_same_thread=False` pour la même raison que `db.connect` : le serveur
    HTTP peut servir depuis un autre thread que celui qui a ouvert la connexion.
    """
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_SCHEMA)
    conn.commit()
    try:
        Path(path).chmod(0o600)
    except OSError:
        pass
    return conn


_shared: sqlite3.Connection | None = None
_lock = threading.Lock()


@contextlib.contextmanager
def session(path: str = ACCESS_PATH):
    """Connexion UNIQUE du processus, sérialisée par un verrou.

    `local_api` sert avec un `ThreadingHTTPServer` : un thread par requête. Il
    ouvre une connexion par requête sur `measurements.db`, ce qui va bien pour une
    lecture occasionnelle — mais `access.db` est sur le chemin de CHAQUE appel, et
    `with sqlite3.connect(...)` ne ferme rien (c'est un contexte de TRANSACTION,
    pas de connexion) : on fuirait un descripteur par requête.

    Une connexion unique + un verrou règle les deux : pas de fuite, aucune question
    de thread-safety. Les opérations se comptent en microsecondes sur une table de
    quelques lignes — sérialiser ne coûte rien de mesurable.

    Les fonctions de ce module prennent `conn` en argument et restent donc
    utilisables telles quelles au banc, sans ce singleton.
    """
    global _shared
    with _lock:
        if _shared is None:
            _shared = connect(path)
        yield _shared


def _digest(secret: str) -> str:
    return "sha256:" + hashlib.sha256(secret.encode("utf-8")).hexdigest()


# ── Le chemin chaud ──────────────────────────────────────────────────────────

# Fenêtre de non-réécriture de `last_used_ts`. Écrire à CHAQUE appel de /live
# userait la carte SD pour rien ; une granularité à l'heure suffit largement à
# dire « cet appareil n'a pas servi depuis huit mois ».
_LAST_USED_THROTTLE_SEC = 3600


def role_of(conn: sqlite3.Connection, presented: str | None) -> str | None:
    """Le rôle porté par ce jeton, ou None. UNE table, UNE requête indexée.

    Pas de réseau, pas d'horloge juste, pas de Google : l'API locale doit répondre
    quand la box Internet est en panne.

    Une INVITATION en attente a `token_hash = NULL`, et en SQL `NULL = ?` est faux.
    Elle ne peut donc jamais être acceptée ici — sans qu'on ait le moindre
    garde-fou à écrire. Un garde-fou qu'on ne peut pas oublier vaut mieux qu'un
    garde-fou qu'il faut penser à écrire.

    Pas de comparaison à temps constant, et ce n'est pas un oubli : on cherche
    l'empreinte de ce qui est présenté, pas le secret. Un attaquant ne contrôle
    pas la sortie de SHA-256, donc ne peut pas s'en servir pour explorer l'index
    octet par octet — et connaître une empreinte n'authentifie personne.
    """
    if not presented:
        return None
    row = conn.execute(
        "SELECT id, role, last_used_ts FROM token WHERE token_hash = ?",
        (_digest(presented),),
    ).fetchone()
    if row is None:
        return None
    _touch(conn, row)
    return row["role"]


def _touch(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    """Marque l'usage — au plus une écriture par heure et par jeton.

    🚨 UNE ÉCRITURE COSMÉTIQUE NE DOIT JAMAIS FAIRE ÉCHOUER UNE AUTORISATION.
    `ben_publisher` écrit dans la même base depuis un AUTRE PROCESSUS ; s'il tient
    le verrou d'écriture au mauvais moment, ce commit attend (jusqu'à `timeout`)
    puis lève « database is locked » — sur le chemin chaud de /live, pour une
    colonne d'affichage. On avale donc l'échec : au pire `last_used_ts` reste
    vieux d'une heure, ce qui n'a aucune conséquence.

    La fenêtre d'une heure existe pour la carte SD : écrire à chaque appel de
    /live userait la flash pour une granularité dont personne n'a besoin — on
    veut pouvoir dire « cet appareil n'a pas servi depuis huit mois », pas
    « depuis douze minutes ».
    """
    now = int(time.time())
    if row["last_used_ts"] is not None and now - row["last_used_ts"] <= _LAST_USED_THROTTLE_SEC:
        return
    try:
        conn.execute("UPDATE token SET last_used_ts = ? WHERE id = ?", (now, row["id"]))
        conn.commit()
    except sqlite3.Error:
        pass


# ── Frapper ──────────────────────────────────────────────────────────────────

def mint(conn: sqlite3.Connection, *, uid: str = "", label: str = "",
         role: str = ROLE_MEMBER) -> str:
    """Frappe un jeton et renvoie le CLAIR — la seule et unique fois.

    L'appelant doit le transmettre immédiatement (BLE, réponse HTTP) : il n'est
    stocké nulle part et ne peut pas être retrouvé. C'est voulu — un jeton qu'on
    peut relire sur le disque n'est plus un secret.

    Frapper ne révoque rien : un nouveau téléphone n'invalide pas l'ancien. C'est
    tout le point des deux étages, et c'est pourquoi le ménage est un geste séparé,
    à la main de l'owner.
    """
    if role not in ROLES:
        raise ValueError(f"rôle inconnu : {role}")
    clear = secrets.token_urlsafe(TOKEN_OCTETS)
    now = int(time.time())
    conn.execute(
        "INSERT INTO token (token_hash, uid, role, label, created_ts) VALUES (?,?,?,?,?)",
        (_digest(clear), (uid or None), role, (label or "appareil")[:64], now),
    )
    if role in PERSON_ROLES and uid:
        grant(conn, uid, role)
    conn.commit()
    return clear


def grant(conn: sqlite3.Connection, uid: str, role: str) -> None:
    """Pose le DROIT d'une personne, à remonter au prochain hello (`sent=0`).

    L'owner est en ÉCRITURE UNIQUE : une fois posé, il ne se remplace pas. Il n'y
    a pas de chemin de secours, et c'est assumé — on ne construit pas une porte de
    sortie pour une erreur évitable sur un parc connu.
    """
    if role not in PERSON_ROLES:
        raise ValueError(f"{role} ne désigne pas une personne")
    existing = conn.execute("SELECT role FROM access WHERE uid = ?", (uid,)).fetchone()
    if existing and existing["role"] == ROLE_OWNER:
        return
    if role == ROLE_OWNER and has_owner(conn):
        raise ValueError("ce boîtier a déjà un owner")
    conn.execute(
        "INSERT INTO access (uid, role, updated_ts, sent) VALUES (?,?,?,0) "
        "ON CONFLICT(uid) DO UPDATE SET role=excluded.role, updated_ts=excluded.updated_ts, sent=0",
        (uid, role, int(time.time())),
    )
    conn.commit()


def has_owner(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM access WHERE role = ? LIMIT 1", (ROLE_OWNER,)
    ).fetchone() is not None


# ── Invitation : un BON DE DROIT, pas un secret ──────────────────────────────

def create_invitation(conn: sqlite3.Connection, *, role: str = ROLE_MEMBER,
                      ttl_sec: int = INVITATION_TTL_SEC) -> str:
    """Frappe une invitation temporaire et renvoie le CLAIR (à mettre dans un QR).

    Le QR porte un DROIT, pas une identité — l'owner ne connaît pas l'uid Firebase
    de la personne qu'il invite, personne ne le connaît. L'identité arrive ensuite,
    de l'invitée elle-même, vérifiée par `ben-api`.

    Le rôle est décidé ICI, par l'owner, au moment où il invite. L'invitée ne peut
    donc pas se promouvoir : le rôle ne vient jamais du demandeur.
    """
    if role not in PERSON_ROLES:
        raise ValueError(f"on n'invite pas une personne comme {role}")
    clear = secrets.token_urlsafe(TOKEN_OCTETS)
    now = int(time.time())
    conn.execute(
        "INSERT INTO token (invitation_hash, role, created_ts, invitation_expiry_ts) "
        "VALUES (?,?,?,?)",
        (_digest(clear), role, now, now + ttl_sec),
    )
    conn.commit()
    return clear


def consume_invitation(conn: sqlite3.Connection, invitation: str, *,
                       uid: str, label: str = "") -> str | None:
    """Transforme une invitation valide en vrai jeton. Renvoie le CLAIR, ou None.

    L'`uid` doit avoir été VÉRIFIÉ par `ben-api` — jamais celui que le demandeur
    annonce. Le QR prouve le consentement de l'owner, Firebase prouve l'identité de
    l'invitée : aucune des deux moitiés ne suffit seule.
    """
    if not invitation or not uid:
        return None
    now = int(time.time())
    row = conn.execute(
        "SELECT id, role FROM token WHERE invitation_hash = ? AND invitation_expiry_ts > ?",
        (_digest(invitation), now),
    ).fetchone()
    if row is None:
        return None
    clear = secrets.token_urlsafe(TOKEN_OCTETS)
    conn.execute(
        "UPDATE token SET token_hash = ?, invitation_hash = NULL, uid = ?, label = ?, "
        "invitation_expiry_ts = NULL WHERE id = ?",
        (_digest(clear), uid, (label or "appareil")[:64], row["id"]),
    )
    grant(conn, uid, row["role"])
    conn.commit()
    return clear


def purge_expired_invitations(conn: sqlite3.Connection) -> int:
    """Retire les invitations périmées. Cosmétique : elles sont déjà inutilisables
    (`invitation_expiry_ts` dépassé, et `token_hash` NULL ne matche jamais)."""
    cur = conn.execute(
        "DELETE FROM token WHERE token_hash IS NULL AND invitation_expiry_ts <= ?",
        (int(time.time()),),
    )
    conn.commit()
    return cur.rowcount


# ── Révoquer — deux étages ───────────────────────────────────────────────────

def revoke_token(conn: sqlite3.Connection, token_id: int) -> int:
    """Coupe UN APPAREIL. Les autres jetons de la même personne survivent."""
    cur = conn.execute("DELETE FROM token WHERE id = ?", (token_id,))
    conn.commit()
    return cur.rowcount


def revoke_person(conn: sqlite3.Connection, uid: str) -> int:
    """Coupe UNE PERSONNE : sa ligne `access` ET tous ses jetons.

    Les deux, toujours. Retirer la seule ligne `access` laisserait des jetons
    vivants dans des téléphones — et l'illusion d'avoir coupé quelqu'un.

    Côté cloud, la ligne `device_access` doit être retirée AUSSI : c'est l'app qui
    s'en charge, elle est le seul acteur à être à la fois sur le LAN et sur
    Internet. Rien ne descend du cloud vers le boîtier.
    """
    n = conn.execute("DELETE FROM token WHERE uid = ?", (uid,)).rowcount
    n += conn.execute("DELETE FROM access WHERE uid = ?", (uid,)).rowcount
    conn.commit()
    return n


# ── Lire ─────────────────────────────────────────────────────────────────────

def list_tokens(conn: sqlite3.Connection) -> list[dict]:
    """Les appareils autorisés, SANS les empreintes — de quoi peupler un écran
    « appareils autorisés » sans exposer de quoi en dériver quoi que ce soit.

    C'est `id` qu'on renvoie pour révoquer : un entier inerte. Révoquer par
    l'empreinte obligerait à exposer ce qu'on refuse justement d'exposer.
    """
    return [dict(r) for r in conn.execute(
        "SELECT id, uid, role, label, created_ts, last_used_ts FROM token "
        "WHERE token_hash IS NOT NULL ORDER BY created_ts")]


def list_access(conn: sqlite3.Connection) -> list[dict]:
    """Les personnes connues LOCALEMENT.

    Sous-ensemble de `device_access` : une personne préremplie côté cloud qui n'a
    jamais réclamé n'apparaît pas ici. L'écran d'administration doit donc lire les
    DEUX listes — les personnes chez `ben-api`, les appareils ici.
    """
    return [dict(r) for r in conn.execute(
        "SELECT uid, role, updated_ts, sent FROM access ORDER BY updated_ts")]


# ── La boîte d'envoi (consommée par ben_publisher) ───────────────────────────

def pending(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    """Les lignes nées localement, pas encore remontées. Comme `measurements.sent`."""
    return [dict(r) for r in conn.execute(
        "SELECT uid, role, updated_ts FROM access WHERE sent = 0 ORDER BY updated_ts LIMIT ?",
        (limit,))]


def mark_sent(conn: sqlite3.Connection, uids: list[str]) -> None:
    """Marque remontées les lignes ACQUITTÉES par le cloud, jamais les autres."""
    if not uids:
        return
    conn.executemany("UPDATE access SET sent = 1 WHERE uid = ?", [(u,) for u in uids])
    conn.commit()
