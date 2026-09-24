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
    sent       INTEGER NOT NULL DEFAULT 0,  -- plus lue : le hello pousse tout
    -- ⭐ RÉVOCATION = UN DRAPEAU, PAS UNE SUPPRESSION.
    --    Une ligne supprimée ne se propage pas : le boîtier ne peut plus rien en
    --    dire, et le cloud ne peut pas l'inférer (sa liste est un SUR-ensemble).
    --    Marquée, elle part au hello comme le reste, et le hello suivant la
    --    redit si le cloud l'a ratée. Auto-réparateur, sans double appel.
    revoked_ts INTEGER,
    -- ⭐ 🔒 STRICTEMENT LOCAL AU BOÎTIER. Un prénom pour que l'écran de partage
    --    dise « Claire » au lieu de « Membre ». Il ne part PAS au hello : le
    --    cloud n'a besoin que de l'uid et du rôle pour décider, et un prénom
    --    est une donnée personnelle qui n'a aucune raison de voyager pour
    --    rendre une liste plus jolie. La personne le donne en entrant son code.
    nom TEXT
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
    # ⚠️ `CREATE TABLE IF NOT EXISTS` n'ajoute RIEN à une table déjà là : sur un
    #    boîtier du parc, la colonne n'apparaîtrait jamais. SQLite n'a pas de
    #    `ADD COLUMN IF NOT EXISTS`, d'où la lecture du schéma réel.
    #
    # ⭐ On n'AJOUTE que : l'ancien code doit pouvoir tourner sur la nouvelle
    #    base — c'est ce qui rend un retour arrière possible sans la restaurer.
    colonnes = {r["name"] for r in conn.execute("PRAGMA table_info(access)")}
    if "revoked_ts" not in colonnes:
        conn.execute("ALTER TABLE access ADD COLUMN revoked_ts INTEGER")
    if "nom" not in colonnes:
        conn.execute("ALTER TABLE access ADD COLUMN nom TEXT")
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
        try:
            yield _shared
        except Exception:
            # 🚨 FILET DE DERNIER RECOURS. La connexion est PARTAGÉE : une
            #    transaction laissée ouverte par un appelant qui échoue serait
            #    validée par la prochaine écriture réussie, sans rapport.
            #    Mesuré le 24/09 sur `mint` — qui se défait maintenant lui-même,
            #    mais on ne compte pas sur chaque appelant pour y penser.
            _shared.rollback()
            raise


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
    # 🚨 JOINTURE SUR `access` : un jeton dont la PERSONNE est révoquée ne vaut
    #    plus rien, même s'il a survécu. `revoke_person` supprime bien les jetons,
    #    mais s'appuyer là-dessus serait faire dépendre la sécurité d'un ménage
    #    réussi. Ici le refus est STRUCTUREL — il tient même si le ménage a raté.
    #
    # ⚠️ `LEFT JOIN` et non `JOIN` : un jeton d'INTÉGRATION (Home Assistant) n'a
    #    pas d'`uid`, donc aucune ligne `access`. Un `JOIN` le ferait disparaître.
    row = conn.execute(
        "SELECT t.id, t.role, t.uid, t.last_used_ts, "
        "       a.uid AS acces_uid, a.revoked_ts "
        "  FROM token t LEFT JOIN access a ON a.uid = t.uid "
        " WHERE t.token_hash = ?",
        (_digest(presented),),
    ).fetchone()
    if row is None or row["revoked_ts"] is not None:
        return None
    # 🚨 UN JETON QUI PORTE UN `uid` DOIT AVOIR SA LIGNE `access`.
    #
    #    Le `LEFT JOIN` rend une ligne même quand `access` n'en a aucune — et
    #    `revoked_ts` vaut alors NULL, donc « non révoqué ». Un jeton orphelin
    #    passait ainsi pour parfaitement valable, avec le rôle écrit SUR LUI.
    #    Mesuré le 24/09 : un `token(uid_B, owner)` laissé par un `mint` échoué
    #    rendait `role_of → owner`, alors que `grant` avait refusé ce droit.
    #
    # ⭐ On ne se contente donc pas de corriger la cause (le rollback de `mint`)
    #    : on ferme la CLASSE. Toute ligne `token` non liée — bogue futur,
    #    migration ratée, écriture à la main — est désormais refusée ici.
    #
    # ⚠️ `uid` NUL reste exempt, et c'est tout l'objet du `LEFT JOIN` : une
    #    intégration n'appartient à personne et n'a donc aucune ligne `access`.
    if row["uid"] is not None and row["acces_uid"] is None:
        return None
    _touch(conn, row)
    return row["role"]


def uid_of(conn: sqlite3.Connection, presented: str | None) -> str | None:
    """À QUI appartient ce jeton, ou None. Vide pour une intégration.

    ⭐ Existe pour que personne n'ait à toucher `_digest` de l'extérieur : le
    hachage est un détail de ce module, et un appelant qui le recopie casse en
    silence le jour où il change.
    """
    if not presented:
        return None
    r = conn.execute("SELECT uid FROM token WHERE token_hash = ?",
                     (_digest(presented),)).fetchone()
    return r["uid"] if r else None


LIBELLE_LONGUEUR_MAX = 64


def normaliser_libelle(label: str | None) -> str:
    """La forme SOUS LAQUELLE un libellé est stocké.

    🚨 Écrite une fois, parce que la comparer à la main a déjà échoué :
    `mint` rangeait `normaliser_libelle(label)` tandis qu'`elaguer_doublons`
    comparait au libellé BRUT. Un libellé vide ou de plus de 64 caractères ne
    correspondait donc jamais — et l'élagage ne s'appliquait pas, en silence.
    """
    return (label or "appareil")[:LIBELLE_LONGUEUR_MAX]


NOM_LONGUEUR_MAX = 32


def nommer(conn: sqlite3.Connection, uid: str, nom: str | None,
           *, seulement_si_vide: bool = False) -> bool:
    """Donne (ou retire) le prénom affiché d'une personne. 🔒 NE SORT JAMAIS DU
    BOÎTIER — ni au hello, ni nulle part ailleurs.

    ⭐ Écrase ce qui existe, délibérément : le propriétaire doit pouvoir
       corriger « ffff » en « Claire ». Une valeur vide remet à NULL, donc
       l'écran repasse à « Membre » — pas à une chaîne vide invisible.

    ⚠️ Ne CRÉE pas la ligne. Nommer quelqu'un qui n'a pas d'accès n'a pas de
       sens, et le faire ouvrirait une route d'écriture sur une table de droits.

    🚨 [seulement_si_vide] POUR LE CHEMIN AUTOMATIQUE, et il est indispensable.
       Un téléphone se re-revendique tout seul (réinstallation, jeton retiré).
       S'il réécrivait le prénom à chaque fois, la correction du propriétaire —
       « ffff » devenu « Claire » — serait effacée dans les secondes qui
       suivent, sans que personne comprenne pourquoi. On ne remplit donc que le
       vide ; corriger reste un geste délibéré.
    """
    propre = (nom or "").strip()[:NOM_LONGUEUR_MAX]
    sql = "UPDATE access SET nom = ? WHERE uid = ?"
    if seulement_si_vide:
        sql += " AND (nom IS NULL OR nom = '')"
    cur = conn.execute(sql, (propre or None, uid))
    conn.commit()
    return cur.rowcount > 0


def elaguer_doublons(conn: sqlite3.Connection, uid: str, label: str,
                     garder: str | None = None) -> int:
    """Ne garde que le DERNIER jeton d'un (uid, label) donné. Renvoie le nombre
    de jetons retirés.

    ⭐ POURQUOI CE N'EST PAS UNE DEVINETTE. Le libellé est ce que l'app dit du
       matériel sur lequel elle tourne. Deux lignes avec le même `uid` ET le
       même libellé, c'est la même personne sur le même modèle — en pratique une
       réinstallation, qui emporte le coffre et force une nouvelle
       revendication. L'ancien jeton reste alors parfaitement VALIDE et plus
       personne ne le détient : c'est une clé qui traîne, pas une ligne en trop.

    ⚠️ LE CAS OÙ ON A TORT, et ce qu'il coûte : une personne possédant DEUX
       téléphones du même modèle, sur le même compte. Le second perd son jeton,
       s'en aperçoit à son appel suivant — moins de cinq secondes — et se
       revendique. Une ligne qui clignote contre une liste qui se remplit
       silencieusement de clés vivantes : le choix est vite fait.

    ⚠️ On élague APRÈS avoir frappé le nouveau, jamais avant : si la frappe
       échoue, on n'aura rien détruit.

    🚨 [garder] EST LE JETON QU'ON VIENT DE RENDRE, et il faut le passer.
       Le repérer par `MAX(id)` serait faux : `consume_invitation` ne CRÉE pas
       de ligne, elle TRANSFORME celle de l'invitation — dont le numéro date du
       moment où le propriétaire a frappé le code, pas du moment où la personne
       l'utilise. Un jeton obtenu entre les deux porterait un numéro plus grand,
       et l'élagage supprimerait celui qu'on vient tout juste de remettre. Sans
       [garder] on ne supprime donc RIEN : mieux vaut une ligne en trop qu'un
       accès coupé à la seconde où il est accordé.
    """
    if not uid or not label:
        return 0
    # ⚠️ Comparer à la forme STOCKÉE, pas à ce qu'on a reçu.
    label = normaliser_libelle(label)
    id_garde = id_of(conn, garder)
    if id_garde is None:
        return 0
    cur = conn.execute(
        "DELETE FROM token WHERE uid = ? AND label = ? AND token_hash IS NOT NULL "
        "  AND id <> ?",
        (uid, label, id_garde))
    conn.commit()
    return cur.rowcount


def id_of(conn: sqlite3.Connection, presented: str | None) -> int | None:
    """Le NUMÉRO du jeton présenté, pour que l'appelant reconnaisse le sien.

    ⭐ Sert à marquer « cet appareil » dans la liste : l'app ne peut pas faire ce
    rapprochement seule, puisqu'on ne lui rend aucune empreinte. Le boîtier, lui,
    vient de résoudre ce jeton pour autoriser la requête — autant qu'il le dise.
    """
    if not presented:
        return None
    r = conn.execute("SELECT id FROM token WHERE token_hash = ?",
                     (_digest(presented),)).fetchone()
    return r["id"] if r else None


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
        (_digest(clear), (uid or None), role, normaliser_libelle(label), now),
    )
    # 🚨 DÉFAIRE L'INSERTION SI `grant` REFUSE. Sans ce bloc, le jeton restait
    #    dans une transaction ouverte sur une connexion PARTAGÉE — et la
    #    prochaine écriture réussie, sans aucun rapport, le validait.
    #
    #    Reproduit le 24/09 : `grant` refuse un second owner, `mint` lève, puis
    #    un simple jeton d'intégration frappé ensuite commettait au passage
    #    `token(uid_B, owner)`. Une ligne OWNER pour quelqu'un qui n'a aucune
    #    ligne `access` — et `role_of` la lisait comme valable.
    #
    # ⚠️ Le clair n'est jamais rendu dans ce cas, donc personne ne détenait ce
    #    jeton. C'était une corruption d'invariant, pas une porte ouverte — mais
    #    on ne laisse pas une écriture survivre à l'échec qui l'a annulée.
    try:
        if role in PERSON_ROLES and uid:
            grant(conn, uid, role)
    except Exception:
        conn.rollback()
        raise
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
    # ⭐ `revoked_ts = NULL` : réinviter quelqu'un LÈVE le drapeau. Sans ça, une
    #    personne révoquée puis réinvitée resterait refusée par `role_of`, et on
    #    chercherait longtemps pourquoi son jeton tout neuf ne marche pas.
    conn.execute(
        "INSERT INTO access (uid, role, updated_ts, sent, revoked_ts) VALUES (?,?,?,0,NULL) "
        "ON CONFLICT(uid) DO UPDATE SET role=excluded.role, "
        "  updated_ts=excluded.updated_ts, sent=0, revoked_ts=NULL",
        (uid, role, int(time.time())),
    )
    conn.commit()


def est_revoquee(conn: sqlite3.Connection, uid: str) -> bool:
    """Cette personne a-t-elle été coupée ICI ?

    🚨 Le boîtier est L'AUTORITÉ pour ses propres révocations ; le cloud n'en est
    que le miroir, et un miroir EN RETARD — le hello est quotidien. Sans cette
    lecture, quelqu'un de révoqué il y a dix minutes revendique, le cloud répond
    encore « member » (il l'ignore), et le boîtier le laisse revenir. Sans
    invitation, et sans que le propriétaire en sache rien.
    """
    r = conn.execute("SELECT revoked_ts FROM access WHERE uid = ?", (uid,)).fetchone()
    return r is not None and r["revoked_ts"] is not None


def has_owner(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM access WHERE role = ? LIMIT 1", (ROLE_OWNER,)
    ).fetchone() is not None


# ── Invitation : un BON DE DROIT, pas un secret ──────────────────────────────

# ── Le code d'invitation : fait pour être DIT, pas pour être scanné ──────────
#
# ⭐ On commence par un code court qu'on communique de vive voix. Le scan de QR
#    viendra plus tard : il demande la permission caméra, examinée par Apple, et
#    une fonction qui EXIGE une permission disparaît pour qui la décline.
#
# ⭐ Et le QR n'est pas ce qui rend l'invitation sûre : elle est consommée par
#    `/claim` SUR LE BOÎTIER, par le LAN. L'invité doit donc déjà être sur le
#    WiFi du foyer — c'est ça qui impose « tu es vraiment là », pas le QR.
#
# ⚠️ ALPHABET SANS AMBIGUÏTÉ : ni `I`, ni `L`, ni `O`. On ne les ÉMET jamais, et
#    on les RATTRAPE à la lecture (O→0, I→1, L→1) — parce que celui qui épelle
#    « O » au téléphone voulait dire zéro, et qu'un code refusé sans raison
#    visible est pire qu'un code trop long.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTUVWXYZ"   # 33 caractères
_CONFUSIONS = str.maketrans({"O": "0", "I": "1", "L": "1"})
CODE_LONGUEUR = 6                                  # 33^6 ≈ 1,3 milliard


def normaliser_code(saisi: str) -> str:
    """Ce que la personne a tapé → ce qu'on compare.

    Majuscules, séparateurs jetés, confusions rattrapées. Sans ça, `ben-4k7q`
    tapé en minuscules échouerait et personne ne comprendrait pourquoi.
    """
    s = (saisi or "").upper().translate(_CONFUSIONS)
    return "".join(c for c in s if c in _ALPHABET)


def _frapper_code() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(CODE_LONGUEUR))


def formater_code(code: str) -> str:
    """`4K7QMX` → `4K7-QMX`. Pour l'écran et pour l'oreille, jamais pour comparer."""
    m = CODE_LONGUEUR // 2
    return f"{code[:m]}-{code[m:]}"


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
    now = int(time.time())
    # ⚠️ `invitation_hash` est UNIQUE : sur un code court, une collision avec une
    #    invitation encore vivante est improbable mais pas impossible. On
    #    retente plutôt que de lever une exception au nez de l'utilisateur.
    for _ in range(8):
        clear = _frapper_code()
        try:
            conn.execute(
                "INSERT INTO token (invitation_hash, role, created_ts, invitation_expiry_ts) "
                "VALUES (?,?,?,?)",
                (_digest(clear), role, now, now + ttl_sec),
            )
            conn.commit()
            return clear
        except sqlite3.IntegrityError:
            continue
    raise RuntimeError("impossible de frapper un code d'invitation libre")


def consume_invitation(conn: sqlite3.Connection, invitation: str, *,
                       uid: str, label: str = "") -> str | None:
    """Transforme une invitation valide en vrai jeton. Renvoie le CLAIR, ou None.

    L'`uid` doit avoir été VÉRIFIÉ par `ben-api` — jamais celui que le demandeur
    annonce. Le QR prouve le consentement de l'owner, Firebase prouve l'identité de
    l'invitée : aucune des deux moitiés ne suffit seule.
    """
    code = normaliser_code(invitation)
    if not code or not uid:
        return None
    now = int(time.time())
    row = conn.execute(
        "SELECT id, role FROM token WHERE invitation_hash = ? AND invitation_expiry_ts > ?",
        (_digest(code), now),
    ).fetchone()
    if row is None:
        return None
    clear = secrets.token_urlsafe(TOKEN_OCTETS)
    conn.execute(
        "UPDATE token SET token_hash = ?, invitation_hash = NULL, uid = ?, label = ?, "
        "invitation_expiry_ts = NULL WHERE id = ?",
        (_digest(clear), uid, normaliser_libelle(label), row["id"]),
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
    # Les JETONS meurent tout de suite : marquer la personne sans les tuer
    # laisserait des téléphones parfaitement vivants.
    n = conn.execute("DELETE FROM token WHERE uid = ?", (uid,)).rowcount
    # La LIGNE, elle, survit marquée — c'est elle qui portera la nouvelle au
    # cloud, au prochain hello, et la redira tant qu'il ne l'aura pas prise.
    n += conn.execute(
        "UPDATE access SET revoked_ts = ?, updated_ts = ?, sent = 0 "
        " WHERE uid = ? AND revoked_ts IS NULL",
        (int(time.time()), int(time.time()), uid)).rowcount
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


def tout_effacer(conn: sqlite3.Connection) -> tuple[int, int]:
    """Efface TOUS les droits et TOUS les jetons. Renvoie (accès, jetons).

    🚨 POUR LE DÉSAPPAIRAGE, ET C'EST INDISPENSABLE — pas une politesse.
       `grant` est en ÉCRITURE UNIQUE pour le rôle `owner` : un boîtier qui
       repart en configuration d'usine en gardant sa ligne `owner` refuserait
       le propriétaire SUIVANT avec « ce boîtier a déjà un owner », et il n'y
       aurait aucun chemin de retour depuis l'app. Le boîtier serait à jeter.

    ⭐ Et c'est aussi la seule chose juste : le boîtier quitte ce foyer. Les
       droits de ce foyer, les téléphones de ce foyer et ses intégrations n'ont
       plus rien à décrire. Les garder, ce serait laisser des clés vivantes
       dans un logement qu'on vient de quitter.
    """
    jetons = conn.execute("DELETE FROM token").rowcount
    acces = conn.execute("DELETE FROM access").rowcount
    conn.commit()
    return acces, jetons


def pour_le_hello(conn: sqlite3.Connection) -> list[dict]:
    """Les droits, tels qu'ils REMONTENT au cloud. Rien de plus.

    🔒 SEULE PORTE DE SORTIE des lignes `access`, et c'est tout son intérêt :
       la projection est écrite ICI, une fois, au lieu d'être recopiée dans le
       publisher où personne ne la relira. Le jour où quelqu'un remplace la
       construction champ par champ par un `dict(row)` commode, il le fait dans
       une fonction dont le banc vérifie explicitement le contenu.

    ⭐ Ce que le cloud a besoin de savoir pour DÉCIDER : qui, quel rôle, coupé
       ou non. Le prénom ne sert qu'à rendre un écran lisible sur le téléphone
       du propriétaire — une donnée personnelle n'a pas à voyager pour ça.
    """
    return [{"uid": r["uid"], "role": r["role"],
             "revoked": r["revoked_ts"] is not None}
            for r in list_access(conn)]


def list_access(conn: sqlite3.Connection) -> list[dict]:
    """Les personnes connues LOCALEMENT.

    Sous-ensemble de `device_access` : une personne préremplie côté cloud qui n'a
    jamais réclamé n'apparaît pas ici. L'écran d'administration doit donc lire les
    DEUX listes — les personnes chez `ben-api`, les appareils ici.
    """
    return [dict(r) for r in conn.execute(
        "SELECT uid, role, updated_ts, sent, revoked_ts, nom FROM access "
        "ORDER BY updated_ts")]


# ⭐ `pending()` / `mark_sent()` ONT ÉTÉ SUPPRIMÉES (24/09).
#
# Le hello pousse désormais la liste ENTIÈRE des accès, comme il pousse déjà les
# contrats et les libellés. Sur une table de 1 à 5 lignes, une boîte d'envoi
# n'économise rien et coûte une panne : une ligne marquée `sent = 1` que le
# cloud aurait perdue n'est jamais renvoyée, et le boîtier se croit à jour.
#
# ⚠️ La colonne `sent` reste dans le schéma — on n'enlève jamais une colonne
#    d'une base embarquée, l'ancien code doit pouvoir tourner sur la nouvelle
#    (c'est ce qui rend le retour arrière possible sans restaurer la base).
#    Elle n'est simplement plus lue.

