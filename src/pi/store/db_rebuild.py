#!/usr/bin/env python3
"""Reconstruit `measurements.db` en recopiant ce qui est LISIBLE dans un fichier neuf.

🚨 POURQUOI CE MODULE EXISTE — ben-0012, le 2026-10-01.

Le boîtier mesurait (8 322 lignes écrites en 3 h 12, à 0,2 % du débit nominal), il était en
ligne (hello 204 en 17 ms), son publisher tournait sans un seul plantage — et NEUF JOURS de
données ne partaient pas. Le hello d'escalade de `pi-0.9.24` a livré la cause vingt secondes
après l'OTA :

    [15:41:07][WARNING] échec n°1 (database disk image is malformed) …
    [15:41:27][ERROR]   échec n°5 (database disk image is malformed) …

Une zone de la table `measurements` est ILLISIBLE. `fetch_batch` lève avant même d'atteindre
le réseau : le serveur n'a jamais rien eu à refuser.

⭐ CE QUI MARCHE ENCORE, ET POURQUOI ÇA DIT OÙ EST LE DOMMAGE :

    l'écriture                   ajouter écrit dans des pages NEUVES, au bout de l'arbre
    le hello                     lit `pdl`, `contract_epoch`, … — intactes
    `pending`, `unsent`          servis par des INDEX, arbres séparés
    `radio` (rssi/snr)           lit des pages de table RÉCENTES — et réussit
    `fetch_batch`                ÉCHOUE : 11 colonnes, et il part du rowid LE PLUS ANCIEN

Les écritures réussissant, la page corrompue n'est pas sur le chemin menant au bout de
l'arbre : le dommage est dans la partie ANCIENNE. Le publisher marche droit dessus à chaque
tentative.

ⓘ Cause probable : l'usure de la carte SD — ~64 000 lignes écrites par jour, 24 h sur 24.
  Aucun logiciel ne répare ça ; le changement de carte reste nécessaire. Ce module récupère
  les données et remet la base dans un état sain.

═══ CE QU'ON NE FAIT PAS, ET POURQUOI ═══════════════════════════════════════════════════════

    VACUUM INTO            relit TOUTES les pages pour reconstruire → avorte sur le dommage
    Connection.backup()    🚨 copie page par page SOUS les arbres : recopierait la corruption
                              à l'identique. Le piège est qu'il RÉUSSIRAIT
    sqlite3 .recover        l'outil fait pour ça — mais le binaire `sqlite3` est ABSENT des
                              boîtiers (vérifié sur les deux modèles), et installer un paquet
                              par OTA sur une machine injoignable est hors de question
    DELETE / UPDATE         récrit les pages corrompues : on aggrave ce qu'on contourne

⇒ On recopie, en stdlib, par tranches de `rowid`, avec dichotomie sur les tranches qui lèvent.
  On ne perd ainsi que ce qui est RÉELLEMENT détruit, pas la tranche entière.

═══ LES RÈGLES QUI ENCADRENT TOUT ══════════════════════════════════════════════════════════

🚨 AUCUN ÉTAT DE LA DONNÉE NE FAIT ÉCHOUER L'UPDATE (règle posée en 0.9.21). Un échec laisse
   `device.json` non bumpé, donc l'update REJOUE toutes les 10 min — la mécanique qui a brûlé
   pi-0.9.12. Toute situation inattendue ⇒ on laisse l'original INTACT et on le dit.

🚨 L'ORIGINAL N'EST JAMAIS SUPPRIMÉ. Il est conservé sous `.corrupt-<horodatage>`, et c'est
   un lien DUR, donc instantané et sans copie. Règle posée : jamais supprimer une sauvegarde
   avant validation.

🚨 ON NE BASCULE QU'APRÈS VALIDATION de la base neuve — `integrity_check` comprise. C'est le
   seul endroit où payer un contrôle intégral est justifié : il répond à la vraie question.

⚠️ Les ÉCRIVAINS TIENNENT LE FICHIER OUVERT. Renommer sous leurs pieds les laisserait écrire
   dans l'ancien inode — leurs mesures partiraient dans un fichier que plus personne ne lit.
   D'où les deux phases, et d'où l'arrêt des services par `update.sh`, qui a le `trap` qui les
   relève quoi qu'il arrive.
"""
import json
import os
import sqlite3
import sys
import time

sys.path[:0] = [os.path.dirname(os.path.abspath(__file__))]

import db  # noqa: E402

sys.path[:0] = [os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]

import capabilities as caps  # noqa: E402

# Taille d'une tranche de recopie. 5 000 lignes de `measurements` pèsent ~500 ko en mémoire,
# sur un Pi Zero qui en a 244 de libres — et c'est aussi la granularité de la dichotomie :
# plus la tranche est grosse, moins il y a de tranches, mais plus la première coupe est large.
CHUNK = 5000

# Au-delà, on arrête de chercher : une base dont tout est illisible n'est pas réparable par
# recopie, et insister coûterait des heures de dichotomie sur un mono-cœur.
MAX_ZONES_ABIMEES = 200

# Marge de disque exigée, en plus de la taille de la base. On écrit un second fichier de même
# taille ; le lien dur vers l'original ne coûte rien.
# Les colonnes que lit le publisher. 🚨 Une par une, pas `*` : c'est la LECTURE DE LIGNE
# COMPLÈTE qu'on veut reproduire, et `*` masquerait un ajout de colonne.
COLS_MEASUREMENTS = ("ts, pdl_index, base, hchc, hchp, papp, iinst, tariff, src_standard, "
                     "index_id, index_value, inject_total, meter_ts")

# La taille du lot du publisher — MÊME variable d'environnement que `ben_publisher.BATCH`.
# ⭐ C'est elle qui définit le symptôme : « le prochain lot du publisher passe-t-il ? »
N_LOT = int(os.environ.get("BEN_PUB_BATCH", "1000"))

MARGE_DISQUE = 1.25

# ═══ LE RYTHME, ET POURQUOI IL N'EST PAS NÉGOCIABLE ═════════════════════════════════════════
#
# 🚨 LE CHIEN DE GARDE MATÉRIEL EST ARMÉ À 60 SECONDES. Mesuré sur un boîtier du parc :
#
#       RuntimeWatchdogUSec=1min
#       systemd[1]: Using hardware watchdog 'Broadcom BCM2835 Watchdog timer'
#       systemd[1]: Watchdog running with a hardware timeout of 1min.
#
#    Si PID 1 n'arrive pas à le caresser dans la minute, le SoC fait un RESET DUR — sans
#    journal, sans séquence d'arrêt, sans erreur noyau. C'est exactement ce qui est arrivé à un
#    boîtier de banc pendant un essai de recopie : `throttled=0x0`, aucune trace, le journal du
#    boot précédent s'arrête net en pleine activité normale.
#
# ⚠️ Et ce n'est PAS un problème de CPU, donc `nice` n'y suffit pas. `ionice` non plus :
#    l'ordonnanceur de la carte SD est `mq-deadline`, qui N'HONORE PAS les classes d'E/S
#    (seuls BFQ et CFQ le font) — vérifié sur la cible.
#
# ⭐ LE VRAI DANGER EST L'ACCUMULATION DE PAGES SALES. Le boîtier a 427 Mo de RAM dont ~39
#   libres et 300 en cache. Recopier des centaines de mégaoctets sans écouler laisse le noyau
#   avec une masse de pages à écrire, et alors TOUTE écriture bloque longuement — y compris
#   celle de systemd dans son journal. D'où trois leviers, et surtout pas `synchronous=OFF`,
#   qui aurait AGGRAVÉ les choses en supprimant les points d'écoulement :
#
#     ① une PAUSE entre les tranches : notre processus cesse d'être exécutable, PID 1 passe ;
#     ② un COMMIT fréquent : les données descendent par petits paquets ;
#     ③ un CHECKPOINT périodique : le WAL ne grossit pas jusqu'à devoir se vider d'un bloc.
#
# Le coût total est négligeable : ~860 tranches pour 4,3 M lignes, soit +43 s de pauses.
PAUSE_TRANCHE_S = float(os.environ.get("BEN_REBUILD_PAUSE", "0.05"))
COMMIT_TRANCHES = 4        # ~20 000 lignes, soit ~1,5 Mo
CHECKPOINT_TRANCHES = 40   # ~200 000 lignes

RAPPORT = "/var/lib/ben-firmware/db-rebuild.json"

# Les messages de SQLite qui signifient « la structure du fichier est cassée ».
#
# 🚨 `database is locked` N'EN FAIT PAS PARTIE, et c'est capital : il est TRANSITOIRE (le
#    lecteur écrit en continu). Le confondre avec une corruption ferait abandonner des
#    données parfaitement saines.
CORRUPTION = ("malformed", "corrupt", "not a database", "database disk image")


def est_corruption(e: Exception) -> bool:
    """La structure du fichier est-elle en cause, ou est-ce passager ?

    ⭐ Fonction PURE, comme `cadence()` et `niveau_echec()` — c'est ce qui permet de
       l'éprouver sans base abîmée.
    """
    return isinstance(e, sqlite3.DatabaseError) and any(
        m in str(e).lower() for m in CORRUPTION)


# ── Quels services arrêter ──────────────────────────────────────────────────────────────────

# Les services qui tiennent `measurements.db` ouvert SANS dépendre du modèle. Mesuré sur un
# boîtier Radio du parc, en lisant `/proc/<pid>/fd` : exactement trois unités l'ont ouvert —
# `ben-telemetry` (l'écrivain), `ben-publisher` et `ben-local-api`. Les deux dernières sont ici ;
# la première vient des capabilities.
HORS_MODELE = ("ben-publisher.service", "ben-local-api.service")

# Le service qui possède le GPIO de la radio. Il démarre en PREMIER (cf. le commentaire de
# `CAP_SERVICES` : « L'ORDRE compte : ben-radio d'abord »), donc il s'arrête en DERNIER.
MAITRE_GPIO = "ben-radio.service"

# 🚨 CE QU'ON NE TOUCHE PAS, ET C'EST DÉLIBÉRÉ : `ben-certd` et `wifi-watchdog`. Ni l'un ni
#    l'autre n'ouvre la base — les arrêter n'apporte RIEN. Et `wifi-watchdog` est précisément
#    ce qui maintient le boîtier sur le réseau : le couper vingt minutes sur une machine qu'on
#    ne peut pas atteindre serait un risque sans contrepartie. `ben-certd` tient le canal PKI.
#
# 🚨 ET SURTOUT PAS `ben-update` : c'est l'unité qui nous exécute.


def services_a_arreter(dev: dict) -> list:
    """Les unités à arrêter, DÉRIVÉES DES CAPABILITIES du boîtier.

    ⭐ PAS UNE LISTE EN DUR, et ce n'est pas du zèle : sur un boîtier **Radio**,
      `ben-tic-reader` (capability `tic-uart`) n'a rien à faire là — il y est `dead` avec 172
      redémarrages, justement parce qu'il n'est pas de ce modèle. Une liste en dur l'aurait
      arrêté puis redémarré pour rien, et aurait manqué tout service ajouté plus tard.
      `capabilities.CAP_SERVICES` est la source de vérité, et elle est en CODE.

    ⚠️ L'ORDRE compte, et il est inverse de celui du démarrage : `ben-radio` possède le GPIO de
       la radio et démarre en PREMIER, donc il s'arrête en DERNIER.

    ⭐ Fonction PURE du `device.json` — donc éprouvable sans boîtier, pour les deux modèles.
    """
    lecteurs = [s for cap in caps.capabilities(dev) for s in caps.services_for(cap)]
    # 🚨 L'invariant est EXPLICITE, pas émergent. `list(reversed(lecteurs))` plaçait bien
    #    `ben-radio` en dernier — mais seulement parce que l'ordre des clés de `capabilities`
    #    dans `device.json` s'y prêtait. Rien ne garantit cet ordre : c'est du JSON écrit par
    #    le provisioning, et un boîtier reprovisionné pourrait le livrer autrement. On nomme
    #    donc la contrainte au lieu d'en dépendre.
    lecteurs = [u for u in lecteurs if u != MAITRE_GPIO] + \
               [u for u in lecteurs if u == MAITRE_GPIO]
    return list(HORS_MODELE) + lecteurs


def ouvreurs(db_path: str) -> list | None:
    """Les processus qui tiennent `db_path` OUVERT, lus dans `/proc/<pid>/fd`.

    🚨 DÉFAUT TROUVÉ EN REVUE, et c'est une perte de données SILENCIEUSE. `update.sh` arrêtait
       les services listés comme ACTIFS — mais une unité en cours de redémarrage n'est pas
       `active`, donc elle était ignorée, elle revenait, elle gardait la base ouverte, et après
       le `os.replace` ses écritures partaient dans le fichier devenu `.corrupt-*`. Les mesures
       de cet intervalle étaient perdues sans que rien ne le dise.

    ⭐ On ne vérifie donc plus une DÉCISION (« j'ai demandé l'arrêt ») mais son EFFET
      (« personne ne tient plus le fichier »). C'est l'invariant réel, et c'est celui qu'on a
      mesuré pour établir la liste : trois unités seulement ouvrent la base.

    🚨 `None` ≠ `[]`, ET LA DIFFÉRENCE EST TOUT. Une liste VIDE veut dire « j'ai regardé,
       personne ne tient le fichier » ; `None` veut dire « JE N'AI PAS PU REGARDER ». Les
       confondre autoriserait une bascule sous un écrivain sur tout système où `/proc` est
       absent ou illisible — un contrôle qui ne peut pas voir doit dire qu'il ne voit pas, pas
       répondre « voie libre ».

    ⚠️ Demande les droits root pour voir les descripteurs des autres utilisateurs — appelé sous
       `sudo` par `update.sh`. Sans eux on ne verrait que ses propres descripteurs, donc on
       conclurait à tort.
    """
    cible = os.path.realpath(db_path)
    vus = []
    try:
        pids = [d for d in os.listdir("/proc") if d.isdigit()]
    except OSError:
        return None          # pas de /proc : on ne sait PAS, et on le dit
    for pid in pids:
        rep = f"/proc/{pid}/fd"
        try:
            for fd in os.listdir(rep):
                try:
                    if os.path.realpath(os.path.join(rep, fd)).startswith(cible):
                        vus.append(int(pid))
                        break
                except OSError:
                    continue
        except OSError:
            continue          # processus disparu, ou pas les droits
    return sorted(set(vus))


# ── Ce qui empêche d'agir ────────────────────────────────────────────────────────────────────

def refus(conn: sqlite3.Connection, db_path: str) -> str | None:
    """Pourquoi on ne doit PAS reconstruire. `None` = on peut.

    🚨 UNE SEULE DÉFINITION, consultée par le préflight ET par `phase1()` — comme `refus()`
       de `menage_fantomes.py`. Deux définitions divergeraient à la première correction.
    """
    # ① LE SYMPTÔME DOIT ÊTRE RÉEL — et il se juge sur CE QUE LIT LE PUBLISHER, pas sur une
    #    ligne.
    #
    # 🚨 DÉFAUT TROUVÉ EN REVUE, et il rendait cette update INOPÉRANTE sur le boîtier même pour
    #    lequel elle est faite. La première version lisait UNE ligne (`LIMIT 1`) ; or
    #    `fetch_batch` en lit **mille**, étalées sur une dizaine de pages. Si la page détruite
    #    n'est pas celle qui porte la plus vieille ligne non envoyée mais, disons, la 437ᵉ du
    #    lot, alors la sonde passait, `refus()` disait « rien à reconstruire », et le boîtier
    #    restait bloqué — exactement l'état qu'on vient passer une journée à diagnostiquer.
    #
    # ⚠️ Et ce cas est le PLUS PROBABLE : le dernier lot parti s'est arrêté juste avant la page
    #    abîmée, donc la frontière `sent = 0` tombe quelque part AVANT elle, pas dessus.
    #
    # ⭐ On exécute donc la requête du publisher, à l'identique, `LIMIT` comprise. Mesuré sur un
    #   Pi Zero : 131 ms pour 1 000 lignes — le prix d'une seule décision, et elle est juste.
    #   On ITÈRE sans accumuler (`for _ in …`) : mêmes pages touchées, aucune mémoire retenue.
    for sql in (f"SELECT {COLS_MEASUREMENTS} FROM measurements "
                f"WHERE sent = 0 ORDER BY rowid LIMIT {N_LOT}",
                f"SELECT {COLS_MEASUREMENTS} FROM measurements ORDER BY rowid DESC LIMIT 1"):
        try:
            for _ in conn.execute(sql):
                pass
        except Exception as e:  # noqa: BLE001
            if not est_corruption(e):
                return f"erreur non structurelle en lisant la base ({e}) — on ne touche à rien"
            break
    else:
        return "la base se lit sans erreur — rien à reconstruire"

    # ② La place. On écrit un fichier de même taille que l'original.
    try:
        taille = os.path.getsize(db_path)
        libre = os.statvfs(os.path.dirname(db_path) or "/")
        dispo = libre.f_bavail * libre.f_frsize
    except OSError as e:
        return f"taille ou espace disque illisibles ({e})"
    if dispo < taille * MARGE_DISQUE:
        return (f"disque insuffisant : {dispo // 1048576} Mo libres pour une base de "
                f"{taille // 1048576} Mo (marge ×{MARGE_DISQUE} exigée)")

    return None


# ── La recopie ──────────────────────────────────────────────────────────────────────────────

def colonnes(conn: sqlite3.Connection, table: str) -> list:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def tables(conn: sqlite3.Connection) -> list:
    """Les tables RÉELLEMENT présentes, lues depuis `sqlite_master`.

    🚨 PAS UNE LISTE EN DUR. Une table ajoutée par une version ultérieure serait silencieusement
       PERDUE à la reconstruction — et la perte ne se verrait qu'au prochain usage de la table.
       On lit ce qui est là.
    """
    return [n for (n,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def copie_table(src: sqlite3.Connection, dst: sqlite3.Connection, table: str) -> int:
    """Recopie une table en bloc. Pour les petites tables, dont la lecture est prouvée saine :
    le hello les lit toutes les 24 h sans incident."""
    cols = colonnes(src, table)
    liste = ", ".join(cols)
    trous = ", ".join("?" * len(cols))
    rows = list(src.execute(f"SELECT {liste} FROM {table}"))
    if rows:
        dst.executemany(f"INSERT OR REPLACE INTO {table} ({liste}) VALUES ({trous})", rows)
    dst.commit()
    return len(rows)


def copie_par_tranches(src: sqlite3.Connection, dst: sqlite3.Connection, table: str,
                       hi: int, depuis: int = 0) -> dict:
    """Recopie `table` pour les `rowid` de `depuis + 1` à `hi`, en sautant ce qui est illisible.

    ⭐ LE CŒUR DU MODULE. On lit par tranches ; une tranche qui lève est COUPÉE EN DEUX et
      chaque moitié est retentée. On descend ainsi jusqu'aux lignes individuelles, et on ne
      perd que celles qui sont RÉELLEMENT détruites — pas la tranche de 5 000 qui les contient.
      C'est la différence entre perdre neuf jours et perdre quelques minutes.

    🚨 `list(...)` AVANT l'insertion, et c'est délibéré : un curseur SQLite diffuse les lignes
       au fil de la lecture, donc une tranche peut lever APRÈS en avoir rendu quelques-unes.
       Matérialiser d'abord garantit qu'on n'insère rien d'une tranche qui a échoué — sinon on
       écrirait des lignes en double au moment de retenter les deux moitiés.

    🚨 LES `rowid` SONT PRÉSERVÉS. `rollup_state.watermark` est un horodatage, donc rien ne
       l'exige formellement — mais `pending_approx()` fait de l'arithmétique de rowid, et un
       décalage rendrait tous les chiffres du parc incomparables entre avant et après.
    """
    cols = colonnes(src, table)
    liste = ", ".join(cols)
    trous = ", ".join("?" * (len(cols) + 1))
    lecture = (f"SELECT rowid, {liste} FROM {table} "
               "WHERE rowid BETWEEN ? AND ? ORDER BY rowid")
    ecriture = f"INSERT OR REPLACE INTO {table} (rowid, {liste}) VALUES ({trous})"

    copiees = 0
    perdues = []          # les plages de rowid qu'on abandonne
    ts_perdus = []        # leurs horodatages, pour corriger le rollup
    i_ts = cols.index("ts") + 1 if "ts" in cols else None

    # Pile de travail : des plages à tenter. On empile du plus récent au plus ancien pour
    # dépiler dans l'ordre croissant — l'ordre d'insertion n'a pas d'importance, mais un
    # parcours monotone rend le journal lisible.
    travail = []
    a = depuis + 1
    while a <= hi:
        travail.append((a, min(a + CHUNK - 1, hi)))
        a += CHUNK
    travail.reverse()

    tranches = 0
    while travail:
        a, b = travail.pop()
        try:
            rows = list(src.execute(lecture, (a, b)))
        except Exception as e:  # noqa: BLE001
            if not est_corruption(e):
                raise
            if a == b:
                # Une ligne seule, vraiment illisible : c'est tout ce qu'on abandonne.
                if perdues and perdues[-1][1] == a - 1:
                    perdues[-1] = (perdues[-1][0], a)     # on fusionne les plages contiguës
                else:
                    perdues.append((a, a))
                if len(perdues) > MAX_ZONES_ABIMEES:
                    raise RuntimeError(
                        f"plus de {MAX_ZONES_ABIMEES} zones abîmées dans {table} : la base "
                        "n'est pas réparable par recopie") from e
                continue
            milieu = (a + b) // 2
            travail.append((milieu + 1, b))
            travail.append((a, milieu))
            continue
        if rows:
            dst.executemany(ecriture, rows)
            copiees += len(rows)
        # 🚨 ON ÉCOULE ET ON CÈDE LA MAIN, à chaque tranche. Voir `PAUSE_TRANCHE_S` : le chien
        #    de garde matériel est armé à 60 s, et un boîtier de banc a fait un reset dur
        #    pendant un essai de recopie. Ce n'est pas de la politesse, c'est la condition pour
        #    que l'opération se termine.
        tranches += 1
        if tranches % COMMIT_TRANCHES == 0:
            dst.commit()
        if tranches % CHECKPOINT_TRANCHES == 0:
            try:
                dst.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except sqlite3.Error:
                pass          # un lecteur tient un vieux snapshot — retenté plus loin
        if PAUSE_TRANCHE_S > 0:
            time.sleep(PAUSE_TRANCHE_S)
    dst.commit()

    # Les horodatages perdus, pour que l'appelant puisse corriger les agrégats dérivés.
    if i_ts is not None:
        for a, b in perdues:
            for (ts,) in _ts_voisins(src, table, a, b):
                ts_perdus.append(ts)

    return {"copiees": copiees, "perdues": perdues,
            "lignes_perdues": sum(b - a + 1 for a, b in perdues),
            "ts_perdus": ts_perdus}


def _ts_voisins(src: sqlite3.Connection, table: str, a: int, b: int) -> list:
    """Les horodatages qui ENCADRENT une plage perdue, lus chez ses voisins immédiats.

    🚨 On ne peut évidemment PAS lire le `ts` des lignes abîmées — c'est justement ce qui lève.
       Mais `rowid` croît avec le temps d'insertion : les voisins immédiats bornent donc la
       période perdue. C'est approximatif et suffisant — ça sert à savoir quels seaux de
       `curve_rollup` ne sont plus fiables, pas à reconstituer la donnée.
    """
    out = []
    for q, args in ((f"SELECT ts FROM {table} WHERE rowid < ? ORDER BY rowid DESC LIMIT 1", (a,)),
                    (f"SELECT ts FROM {table} WHERE rowid > ? ORDER BY rowid LIMIT 1", (b,))):
        try:
            r = src.execute(q, args).fetchone()
        except Exception:  # noqa: BLE001
            continue       # le voisin est abîmé lui aussi : on s'en passe
        if r and r[0] is not None:
            out.append((r[0],))
    return out


# ── La validation, avant toute bascule ──────────────────────────────────────────────────────

def valide(chemin: str, attendu: dict) -> str | None:
    """La base neuve est-elle utilisable ? `None` = oui.

    ⭐ C'est le SEUL endroit où payer un `integrity_check` intégral est justifié. Sur
      l'original, il est ruineux et inutile — on sait déjà qu'il est abîmé, et il avait mis
      ben-0001 à genoux (charge 4,50, `sshd` muet, jamais fini en 180 s). Ici il répond à la
      vraie question : « a-t-on le droit de remplacer la base de production avec ce fichier ? »
    """
    try:
        c = sqlite3.connect(f"file:{chemin}?mode=ro", uri=True)
    except sqlite3.Error as e:
        return f"la base neuve ne s'ouvre pas ({e})"
    try:
        r = c.execute("PRAGMA integrity_check").fetchone()
        if not r or r[0] != "ok":
            return f"integrity_check sur la base neuve : {r[0] if r else 'aucune réponse'}"
        for table, n in attendu.items():
            vu = c.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            if vu != n:
                return f"{table} : {vu} lignes dans la base neuve, {n} attendues"
        # ⚖️ LE TÉMOIN : une base vide passerait tout ce qui précède.
        if attendu.get("measurements", 0) == 0:
            return "aucune mesure recopiée — on ne remplace pas une base par une base vide"
    except sqlite3.Error as e:
        return f"la base neuve est illisible ({e})"
    finally:
        c.close()
    return None


# ── La bascule ──────────────────────────────────────────────────────────────────────────────

def bascule(original: str, neuve: str) -> str:
    """Met la base neuve en place et conserve l'original. Renvoie le nom de la sauvegarde.

    🚨 LIEN DUR PUIS `os.replace`, ET L'ORDRE EST LE FOND.

       Renommer l'original puis mettre la neuve à sa place fait DEUX renommages, et entre les
       deux il n'existe aucun `measurements.db` : si le processus meurt là, le lecteur en
       recrée une VIDE au redémarrage et l'historique est perdu.

       Un lien dur ne copie rien (même inode, instantané) et préexiste à la bascule. Il ne
       reste donc qu'UN seul geste non atomique-sûr — `os.replace`, qui l'est. Mourir avant
       ne change rien ; mourir après, c'est fini.

    ⚠️ `-wal` et `-shm` appartiennent à l'ANCIEN inode. Les laisser à côté de la base neuve
       n'est pas correct, même si SQLite rejette un WAL dont le sel ne correspond pas : on les
       retire explicitement, pendant que les services sont arrêtés.
    """
    # ⚠️ On force l'écriture du contenu AVANT de basculer : `os.replace` est atomique sur le
    #    NOM, pas sur le contenu. Une coupure juste après laisserait un fichier en place dont
    #    des pages seraient encore en cache.
    try:
        fd = os.open(neuve, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass

    sauvegarde = f"{original}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
    if not os.path.exists(sauvegarde):
        os.link(original, sauvegarde)
    os.replace(neuve, original)
    for suffixe in ("-wal", "-shm"):
        try:
            os.unlink(original + suffixe)
        except FileNotFoundError:
            pass
    return sauvegarde


# ── Le rollup, qui dérive des mesures ───────────────────────────────────────────────────────

def corrige_rollup(conn: sqlite3.Connection, ts_perdus: list) -> dict:
    """Supprime les seaux de `curve_rollup` qui couvrent les mesures perdues, et fait remonter
    le watermark pour que le backfill les reconstruise.

    🚨 SANS ÇA, UN AGRÉGAT MENTIRAIT POUR TOUJOURS. `curve_rollup` garde `papp_sum` et
       `papp_count` pour des points qui n'existent plus : la moyenne affichée par `/curve`
       resterait fausse sur ces tranches, définitivement, sans que rien ne le signale.

    ⭐ `watermark` = borne BASSE couverte (« le rollup est complet pour [watermark, now] »), et
      le backfill le fait RECULER. Le remettre juste au-dessus du trou marque tout ce qui est
      plus ancien comme non couvert : le backfill redescendra et reconstruira. C'est lent
      (bornes de 2 s par maintenance) mais c'est automatique, et `/curve` retombe sur le brut
      pour les fenêtres non couvertes — donc juste, en attendant.
    """
    if not ts_perdus:
        return {"seaux_supprimes": 0, "watermark": None}
    bas, haut = min(ts_perdus), max(ts_perdus)
    bucket = db.ROLLUP_BUCKET_SEC
    b0 = (bas // bucket) * bucket
    b1 = (haut // bucket) * bucket
    n = conn.execute("DELETE FROM curve_rollup WHERE bucket_ts BETWEEN ? AND ?",
                     (b0, b1)).rowcount
    # 🚨 LE WATERMARK NE DOIT JAMAIS DESCENDRE — défaut trouvé en revue.
    #
    #    `watermark` est la borne BASSE couverte : « le rollup est complet pour
    #    [watermark, now] ». Le poser à `b1 + bucket` sans regarder la valeur courante le fait
    #    DESCENDRE quand le backfill n'était pas encore arrivé jusqu'au trou. On déclarerait
    #    alors couvert un intervalle [nouveau, courant) qui n'a JAMAIS été rempli — et `/curve`
    #    rendrait des données VIDES sur cette fenêtre, en croyant lire un rollup complet.
    #
    # ⭐ Un watermark PLUS HAUT revendique MOINS de couverture : c'est le sens sûr. On prend
    #   donc le maximum, et le backfill redescendra de lui-même.
    courant = conn.execute("SELECT watermark FROM rollup_state WHERE id = 0").fetchone()
    nouveau = b1 + bucket
    if courant and courant[0] is not None:
        nouveau = max(int(courant[0]), nouveau)
    conn.execute("INSERT INTO rollup_state (id, watermark, done) VALUES (0, ?, 0) "
                 "ON CONFLICT(id) DO UPDATE SET watermark = ?, done = 0",
                 (nouveau, nouveau))
    conn.commit()
    return {"seaux_supprimes": n, "watermark": nouveau}


# ── L'opération ─────────────────────────────────────────────────────────────────────────────
#
# ⭐ UNE SEULE PHASE, TOUS LES SERVICES ARRÊTÉS SAUF L'OTA.
#
#    Une première version copiait à chaud puis n'arrêtait les écrivains que pour un delta, afin
#    de ne perdre aucune mesure. Arrêter tout est MEILLEUR, et pour une raison qui n'est pas le
#    confort : avec personne qui écrit, la recopie est UNE SEULE LECTURE COHÉRENTE. Plus de
#    delta à recoller, plus de course sur le WAL, plus de fenêtre où l'original et la copie
#    divergent. Moins de code, donc moins d'endroits où se tromper — sur un boîtier qu'on ne
#    peut pas dépanner.
#
# ⚠️ Le coût est réel et assumé : ~20 min de mesures jamais enregistrées. À comparer aux neuf
#    jours que l'opération récupère.
#
# 🚨 L'ARRÊT ET LE REDÉMARRAGE DES SERVICES NE SONT PAS ICI. Ils sont dans `update.sh`, qui
#    porte un `trap` les relevant sur TOUTE sortie — y compris une erreur, y compris un signal.
#    Un boîtier de terrain ne doit jamais rester avec ses services à l'arrêt, et ce garde-fou
#    doit être lisible d'un coup d'œil, en bash, pas enfoui dans un module Python.


def _ouvre_lecture(chemin: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{chemin}?mode=ro", uri=True)


def _max_rowid(conn: sqlite3.Connection, table: str) -> int:
    """La borne haute des `rowid`, même si la table est abîmée À SON EXTRÉMITÉ.

    🚨 DÉFAUT TROUVÉ EN RÉPÉTANT L'OPÉRATION SUR CIBLE, et aucun banc ne l'avait vu parce que
       le banc corrompait vers le MILIEU du fichier. `SELECT max(rowid)` lit la feuille la plus
       à DROITE de la table : si c'est elle qui est détruite — le cas quand le dommage touche
       les lignes les plus récentes — il LÈVE. Et il levait hors de tout `try`, donc `rebuild()`
       remontait l'exception alors que sa docstring promet de ne jamais lever sur un état de la
       donnée. L'update aurait échoué, `device.json` n'aurait pas été bumpé, et elle aurait
       REJOUÉ toutes les 10 minutes EN ARRÊTANT LES SERVICES à chaque fois : la mécanique qui
       a brûlé pi-0.9.12.

    ⭐ Le repli passe par les INDEX, qui sont des arbres SÉPARÉS et donc intacts : l'index
      `(sent)` contient implicitement le `rowid` en dernière colonne, donc la dernière entrée
      de chaque valeur de `sent` donne un `rowid` sans jamais toucher la table.

    ⭐ ET LA BORNE EST EXACTE, pas majorée. `sent` est déclaré `INTEGER NOT NULL DEFAULT 0` :
      toute ligne porte donc 0 ou 1, et le maximum des deux est le vrai maximum.

    🚨 UNE MAJORATION SERAIT NUISIBLE, contrairement à ce que j'avais écrit — défaut trouvé en
       revue. Je supposais que lire une plage de `rowid` inexistante « ne coûte rien et ne rend
       rien ». FAUX : la recherche descend l'arbre vers la droite et retombe sur la MÊME feuille
       détruite, donc chaque sonde LÈVE. Avec 10 % de marge sur 4,3 M lignes, c'était ~430 000
       lignes fantômes à sonder une par une par dichotomie — des centaines de milliers de
       requêtes, et surtout **430 000 lignes comptées comme PERDUES** alors qu'elles n'ont
       jamais existé. Le rapport aurait été faux d'un ordre de grandeur.
    """
    try:
        r = conn.execute(f"SELECT max(rowid) FROM {table}").fetchone()
        return r[0] or 0
    except sqlite3.Error as e:
        if not est_corruption(e):
            raise
    borne = 0
    for sql in (f"SELECT max(rowid) FROM {table} WHERE sent = 0",
                f"SELECT max(rowid) FROM {table} WHERE sent = 1"):
        try:
            r = conn.execute(sql).fetchone()
        except sqlite3.Error:
            continue
        if r and r[0]:
            borne = max(borne, int(r[0]))
    return borne


GROSSES = ("measurements", "lora_link")


def rebuild(db_path: str = db.DB_PATH) -> dict:
    """Recopie ce qui est lisible dans un fichier neuf, valide, et bascule.

    À n'appeler QUE tous les services arrêtés — `update.sh` s'en charge et le vérifie.

    🚨 NE LÈVE JAMAIS, et c'est garanti PAR LE CODE et plus seulement par la docstring. Un
       défaut trouvé en répétant l'opération sur cible le démentait : `_max_rowid` levait hors
       de tout `try`. Et la conséquence n'était pas « un message d'erreur » mais une update qui
       échoue, donc `device.json` non bumpé, donc REJEU toutes les 10 minutes avec arrêt des
       services à chaque passage — la mécanique qui a brûlé pi-0.9.12.
    """
    try:
        return _rebuild(db_path)
    except Exception as e:  # noqa: BLE001
        res = {"ok": False, "refus": f"erreur inattendue ({type(e).__name__}: {e})"}
        _ecris_rapport(res)
        return res


def _rebuild(db_path: str) -> dict:
    t0 = time.monotonic()
    neuve = db_path + ".rebuild"

    src = _ouvre_lecture(db_path)
    pourquoi = refus(src, db_path)
    if pourquoi:
        src.close()
        res = {"ok": False, "refus": pourquoi}
        _ecris_rapport(res)
        return res

    # On repart toujours de zéro : un fichier laissé par une tentative précédente contiendrait
    # un état dont on ne sait rien.
    for suffixe in ("", "-wal", "-shm"):
        try:
            os.unlink(neuve + suffixe)
        except FileNotFoundError:
            pass

    # Le WAL de l'original, rapatrié dans le fichier. Pas indispensable — une connexion en
    # lecture voit déjà le WAL — mais explicite, et ça évite de laisser traîner un `-wal` qui
    # ne correspondra plus à rien après la bascule.
    try:
        w = sqlite3.connect(db_path, timeout=30.0)
        w.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        w.close()
    except sqlite3.Error:
        pass

    dst = db.connect(neuve)          # rejoue le schéma et les migrations : structure saine
    detail, petites = {}, {}

    # ⚠️ Les petites tables D'ABORD. Si la recopie longue est interrompue, l'essentiel de la
    #    métadonnée est déjà là — et surtout `pdl`, sans qui aucune mesure n'a de sens.
    for t in tables(src):
        if t in GROSSES:
            continue
        try:
            petites[t] = copie_table(src, dst, t)
        except Exception as e:  # noqa: BLE001
            # 🚨 On n'abandonne pas tout pour une table de métadonnée : le cloud en renvoie
            #    l'instantané COMPLET au prochain hello de toute façon (cf. `send_hello`).
            #    Perdre `pdl` serait grave, perdre `tariff_labels` se répare tout seul.
            petites[t] = f"illisible : {e}"

    for t in GROSSES:
        if t not in tables(src):
            continue
        detail[t] = copie_par_tranches(src, dst, t, _max_rowid(src, t))

    rollup = corrige_rollup(dst, detail.get("measurements", {}).get("ts_perdus", []))
    attendu = {t: detail[t]["copiees"] for t in detail}
    dst.close()
    src.close()

    mauvais = valide(neuve, attendu)
    if mauvais:
        # 🚨 ON NE BASCULE PAS, ET ON NE SUPPRIME RIEN. L'original est intact, la base neuve
        #    reste sur le disque pour qu'on puisse l'examiner au prochain passage. L'update
        #    réussit quand même : aucun état de la donnée ne doit la faire échouer.
        res = {"ok": False, "refus": f"validation refusée — {mauvais}", "detail": detail}
        _ecris_rapport(res)
        return res

    sauvegarde = bascule(db_path, neuve)
    res = {"ok": True, "at": int(time.time()), "detail": detail, "rollup": rollup,
           "sauvegarde": sauvegarde, "petites": petites,
           "ms": int((time.monotonic() - t0) * 1000)}
    _ecris_rapport(res)
    return res


def _ecris_rapport(res: dict) -> None:
    """Le compte rendu, posé sur le disque pour que `health` le fasse voyager dans le hello.

    ⭐ C'est le seul moyen d'apprendre, depuis le cloud, ce que la réparation a trouvé et perdu
      sur un boîtier qu'on ne peut pas atteindre. Forme COMPACTE : le champ `health` est écarté
      par le serveur au-delà de 16 Ko, et une sonde bavarde emporterait toutes les autres.
    """
    compact = {
        "at": res.get("at") or int(time.time()),
        "ok": bool(res.get("ok")),
        "sauvegarde": os.path.basename(res.get("sauvegarde") or ""),
        "ms": res.get("ms"),
        "rollup": (res.get("rollup") or {}).get("seaux_supprimes"),
    }
    for t, v in (res.get("detail") or {}).items():
        compact[t] = {"copiees": v["copiees"], "perdues": v["lignes_perdues"],
                      "plages": v["perdues"][:5]}
    if res.get("refus"):
        compact["refus"] = str(res["refus"])[:200]

    # 🚨 LE COMPTEUR DE TENTATIVES DOIT SURVIVRE — défaut trouvé en revue, et il annulait le
    #    frein. `update.sh` écrit `tentatives` dans CE fichier avant de travailler ; en le
    #    réécrivant de zéro, on remettait le compteur à 0. Un boîtier qui redémarre après chaque
    #    reconstruction (chien de garde, brownout) aurait donc rejoué l'opération INDÉFINIMENT,
    #    toutes les 10 minutes, en arrêtant ses services à chaque passage.
    try:
        with open(RAPPORT, encoding="utf-8") as f:
            ancien = json.load(f)
        if isinstance(ancien, dict) and "tentatives" in ancien:
            compact["tentatives"] = ancien["tentatives"]
    except (OSError, ValueError):
        pass

    try:
        with open(RAPPORT, "w", encoding="utf-8") as f:
            json.dump(compact, f, ensure_ascii=False)
    except OSError:
        pass


def main(argv: list) -> int:
    """🚨 SORT TOUJOURS EN 0 SUR UN ÉTAT DE LA DONNÉE. Seul un mauvais argument — donc un
    défaut de CODE — rend autre chose. Règle posée en 0.9.21 : une update qui échoue laisse
    `device.json` non bumpé, donc elle REJOUE toutes les 10 min, services arrêtés comprises.
    """
    if not 2 <= len(argv) <= 3 or argv[1] not in ("--rebuild", "--refus", "--services",
                                                  "--ouvreurs"):
        print("usage: db_rebuild.py --rebuild|--refus|--services|--ouvreurs [chemin]",
              file=sys.stderr)
        return 2
    # ⚠️ Le chemin optionnel est une AFFORDANCE D'ESSAI, et elle a une raison précise : la
    #    séquence « arrêt des services → reconstruction → bascule → redémarrage → hello » est
    #    le geste le plus risqué qu'on livre, et il part sur un boîtier INJOIGNABLE. Sans ce
    #    paramètre, on ne pourrait le répéter qu'en touchant une vraie base de production.
    #    Avec, on le répète sur une base jetable de quelques mégaoctets.
    #    ⭐ Il vit ICI et pas dans `db.py` : surcharger `DB_PATH` pour tout le firmware
    #      exposerait les sept boîtiers au risque qu'une variable mal posée fasse écrire
    #      ailleurs. Ce module est le seul qui en a besoin.
    chemin = argv[2] if len(argv) == 3 else db.DB_PATH
    if argv[1] == "--services":
        # Une unité par ligne, dans l'ORDRE D'ARRÊT. `update.sh` les relance en ordre inverse.
        for u in services_a_arreter(caps.load_device()):
            print(u)
        return 0
    if argv[1] == "--ouvreurs":
        # Un PID par ligne. AUCUNE ligne = personne ne tient le fichier. La ligne littérale
        # `indetermine` = on n'a pas pu regarder, et l'appelant NE DOIT PAS conclure.
        vus = ouvreurs(chemin)
        if vus is None:
            print("indetermine")
            return 0
        for pid in vus:
            print(pid)
        return 0
    if argv[1] == "--refus":
        # Sonde SANS EFFET, pour que `update.sh` puisse décider avant d'arrêter quoi que ce
        # soit. ⭐ On n'arrête pas les services d'un boîtier sain pour constater qu'il est sain.
        src = _ouvre_lecture(chemin)
        pourquoi = refus(src, chemin)
        src.close()
        print(pourquoi or "")
        return 0
    res = rebuild(chemin)
    if res.get("refus"):
        print(f"  ⚠ {res['refus']}", file=sys.stderr)
        return 0
    for t, v in res.get("detail", {}).items():
        print(f"  {t} : {v['copiees']} copiées, {v['lignes_perdues']} perdues "
              f"({len(v['perdues'])} zone(s))", file=sys.stderr)
    illisibles = [t for t, v in res.get("petites", {}).items() if isinstance(v, str)]
    if illisibles:
        print(f"  ⚠ métadonnées illisibles : {', '.join(illisibles)}", file=sys.stderr)
    print(f"  original conservé : {os.path.basename(res['sauvegarde'])}", file=sys.stderr)
    print(f"  rollup : {res['rollup']['seaux_supprimes']} seau(x) à reconstruire",
          file=sys.stderr)
    print(f"  durée : {res['ms'] / 1000:.0f} s", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
