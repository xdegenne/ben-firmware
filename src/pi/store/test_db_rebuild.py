#!/usr/bin/env python3
"""Banc de la reconstruction de base (#23).

🚨 CE BANC A UN CAS QUI PORTE TOUT LE RESTE : il fabrique une VRAIE corruption de page, puis
   reconstruit. Sans lui, le module ne serait éprouvé que sur ses fonctions pures — et c'est
   exactement le piège de pi-0.9.23, où le banc et le préflight étaient verts parce qu'ils
   n'éprouvaient qu'une décision, jamais son effet.

   On brouille donc des pages au milieu du fichier (4 096 o chacune) et on EXIGE que SQLite
   lève `database disk image is malformed` — si la corruption n'était pas détectée, le cas ne
   prouverait rien, et il le dit plutôt que de passer.

⭐ Et il reproduit le diagnostic de terrain à l'identique : après brouillage, `max(rowid)`,
  `min(rowid) WHERE sent = 0` et `max(ts) WHERE pdl_index = ?` répondent tous CORRECTEMENT —
  les index sont des arbres séparés, intacts. Seule la lecture d'une LIGNE COMPLÈTE échoue.
  C'est ce qui a rendu la panne invisible neuf jours.

    python3 src/pi/store/test_db_rebuild.py
"""
import os
import pathlib
import shutil
import sqlite3
import sys
import tempfile

R = pathlib.Path(__file__).resolve().parent
sys.path[:0] = [str(R.parent), str(R)]

import db  # noqa: E402
import db_rebuild as rb  # noqa: E402

CAS = []
PAGE = 4096


def cas(fn):
    CAS.append(fn)
    return fn


COLS = ("ts, pdl_index, base, hchc, hchp, papp, iinst, tariff, src_standard, "
        "index_id, index_value, inject_total, meter_ts")

SQL_VIEILLE = (f"SELECT {COLS} FROM measurements WHERE sent = 0 ORDER BY rowid LIMIT 1")
SQL_SCAN = f"SELECT {COLS} FROM measurements"


def _leve(chemin: str, sql: str) -> bool:
    """La requête lève-t-elle une erreur de STRUCTURE ?"""
    c = sqlite3.connect(f"file:{chemin}?mode=ro", uri=True)
    try:
        c.execute(sql).fetchall()
        return False
    except Exception as e:  # noqa: BLE001
        if rb.est_corruption(e):
            return True
        raise
    finally:
        c.close()


def base(n: int = 30000, repertoire: str = None, envoyees: int = 0) -> tuple:
    """Une base au schéma RÉEL, remplie, WAL rapatrié. Renvoie `(chemin, page_frontiere)`.

    ⭐ `envoyees` lignes sont marquées `sent = 1` AVANT que les autres soient insérées, et on
      relève `page_count` entre les deux. C'est le seul moyen FIABLE de savoir où vivent les
      lignes non envoyées : SQLite ajoute les lignes neuves dans des pages NEUVES, à la suite.
      `dbstat`, qui donnerait les pages exactes par table, n'est pas compilé dans le SQLite
      de Python (vérifié : 3.45.3 sur le Mac).
    """
    d = repertoire or tempfile.mkdtemp()
    chemin = os.path.join(d, "measurements.db")
    c = db.connect(chemin)
    c.execute("INSERT INTO pdl (pdl_index, adco, first_seen, last_seen) VALUES (0,'000000000001',1790000000,1790099999)")
    c.execute("INSERT INTO emitter (lora_addr, adco, pdl_index, updated_ts) "
              "VALUES (31,'000000000001',0,1790000000)")
    c.executemany("INSERT INTO lora_link(ts,pdl_index,rssi,snr,sent) VALUES(?,0,?,?,1)",
                  [(1790000000 + i * 40, -80.0, 10.0) for i in range(200)])
    if envoyees:
        c.executemany("INSERT INTO measurements(ts,pdl_index,papp,sent) VALUES(?,0,?,1)",
                      [(1790000000 + i, 100 + (i % 500)) for i in range(envoyees)])
        c.commit()
        # 🚨 Sans ce checkpoint, les lignes vivent dans le `-wal` et le brouillage de pages ne
        #    toucherait rien — le banc serait vert et vide de sens.
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    frontiere = c.execute("PRAGMA page_count").fetchone()[0]
    c.executemany("INSERT INTO measurements(ts,pdl_index,papp,sent) VALUES(?,0,?,0)",
                  [(1790000000 + envoyees + i, 100 + (i % 500)) for i in range(n - envoyees)])
    c.commit()
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()
    return chemin, frontiere


def brouille(chemin: str, debut: int, pages: int = 3) -> None:
    """Écrase `pages` pages à partir de `debut` (numérotation SQLite, 1-based)."""
    with open(chemin, "r+b") as f:
        for p in range(debut, debut + pages):
            f.seek((p - 1) * PAGE)
            f.write(b"\xa5" * PAGE)


def base_abimee(n: int = 30000, repertoire: str = None) -> tuple:
    """La situation DE TERRAIN : la plus vieille ligne NON ENVOYÉE est ILLISIBLE.

    ⭐ C'est ce que le premier montage manquait. Brouiller au milieu d'une base dont TOUTES les
      lignes sont `sent = 0` laisse la plus vieille non envoyée à la ligne 1, dans une page
      intacte : le publisher ne buterait jamais, et `refus()` dirait à juste titre qu'il n'y a
      rien à réparer. Sur ben-0012, tout ce qui précède le 22/09 15:44 est DÉJÀ ENVOYÉ et le
      dommage commence exactement là — c'est ça qu'il faut reproduire.

    ⚠️ Et on ne peut pas y arriver en marquant `sent = 1` APRÈS avoir brouillé : l'`UPDATE`
       navigue dans l'arbre et retombe sur la zone abîmée. Mesuré — c'est ce qui faisait lever
       la version précédente de cet helper. On marque donc AVANT, et on brouille les pages
       écrites ENSUITE.

    ⚖️ L'helper VÉRIFIE SA PROPRE PRÉMISSE et élargit le brouillage jusqu'à l'obtenir. Un
       montage qui ne reproduit pas le symptôme rendrait tous les cas verts et vides de sens.
    """
    d = repertoire or tempfile.mkdtemp()
    chemin, frontiere = base(n, d, envoyees=n * 2 // 3)
    intact = chemin + ".intact"
    shutil.copy(chemin, intact)
    # 🚨 ON BROUILLE JUSTE AVANT LA FRONTIÈRE, PAS APRÈS — et c'est contre-intuitif.
    #    Mesuré : brouiller 120 pages à partir de `page_count` casse un balayage complet mais
    #    laisse la première ligne non envoyée parfaitement LISIBLE. Parce que SQLite remplit
    #    d'abord la dernière feuille EXISTANTE, partiellement pleine, avant d'en ouvrir une
    #    neuve : la ligne charnière vit donc dans une page ANTÉRIEURE à `page_count`.
    for debut, largeur in ((frontiere - 2, 6), (frontiere - 5, 12),
                           (frontiere - 10, 30), (frontiere - 20, 60)):
        shutil.copy(intact, chemin)
        brouille(chemin, max(2, debut), largeur)
        if _leve(chemin, SQL_VIEILLE):
            os.unlink(intact)
            return chemin, frontiere
    raise AssertionError(
        f"aucun brouillage autour de la page {frontiere} ne rend la plus vieille ligne non "
        "envoyée illisible : ce banc ne prouverait rien. Revoir le montage (taille de page, "
        "WAL non rapatrié, disposition des pages)")


def base_abimee_a_la_FIN(n: int = 30000, repertoire: str = None) -> str:
    """Le dommage sur les pages de FIN — donc sur la feuille la plus à DROITE de la table.

    🚨 CE MONTAGE A ÉTÉ AJOUTÉ APRÈS UN DÉFAUT TROUVÉ SUR CIBLE, et il montre la limite du
       premier : en corrompant vers le MILIEU, `SELECT max(rowid)` continuait de répondre. En
       corrompant la FIN, il LÈVE — il lit justement cette feuille. `rebuild()` remontait alors
       l'exception, alors que sa docstring promet de ne jamais lever sur un état de la donnée.

       Et la conséquence n'était pas cosmétique : update en échec ⇒ `device.json` non bumpé ⇒
       REJEU toutes les 10 minutes AVEC ARRÊT DES SERVICES à chaque fois. La mécanique de
       pi-0.9.12.
    """
    d = repertoire or tempfile.mkdtemp()
    chemin, _ = base(n, d, envoyees=n * 2 // 3)
    c = sqlite3.connect(f"file:{chemin}?mode=ro", uri=True)
    npages = c.execute("PRAGMA page_count").fetchone()[0]
    c.close()
    intact = chemin + ".intact"
    shutil.copy(chemin, intact)
    # ⚠️ ON CHERCHE LA POSITION, on ne la devine pas. Sur la base de 492 Mo d'un boîtier du
    #    parc, brouiller `page_count - 8` suffisait à faire lever `max(rowid)`. Sur une base de
    #    banc, les dernières pages sont des pages d'INDEX et la feuille la plus à droite de la
    #    table est ailleurs. `dbstat`, qui donnerait les pages exactes par table, n'est pas
    #    compilé dans le SQLite de Python. On balaie donc le dernier tiers du fichier.
    # ⚠️ ON PART DE 0, pas de 4 : avec `start = npages - recul` et une largeur de 4, un recul
    #    de 4 couvre npages-4..npages-1 et MANQUE la page `npages` elle-même — or c'est
    #    justement là que vit la feuille la plus à droite. Défaut de la version précédente de
    #    ce balayage, qui ne trouvait jamais la position.
    for recul in range(0, max(8, npages // 3), 4):
        shutil.copy(intact, chemin)
        brouille(chemin, max(2, npages - recul), 4)
        c = sqlite3.connect(f"file:{chemin}?mode=ro", uri=True)
        try:
            c.execute("SELECT max(rowid) FROM measurements").fetchone()
            leve = False
        except Exception as e:  # noqa: BLE001
            leve = rb.est_corruption(e)
        finally:
            c.close()
        if leve:
            os.unlink(intact)
            return chemin
    raise AssertionError(
        f"aucun brouillage dans le dernier tiers ({npages} pages) ne fait lever "
        "`max(rowid)` : ce cas ne prouverait rien")


def base_abimee_DANS_le_lot(n: int = 30000, repertoire: str = None,
                            decalage: int = 300) -> tuple:
    """Le dommage est dans le LOT du publisher, mais PAS sur sa première ligne.

    🚨 LE CAS QUI A ÉCHAPPÉ AU PREMIER BANC, trouvé en revue — et il rendait l'update
       INOPÉRANTE sur le boîtier même pour lequel elle est faite. `fetch_batch` lit MILLE
       lignes ; si la page détruite porte la 300ᵉ et non la 1ʳᵉ, une sonde qui ne lit qu'UNE
       ligne passe, `refus()` annonce « rien à reconstruire », et le boîtier reste bloqué.

    ⚠️ Et c'est le cas LE PLUS PROBABLE sur le terrain : le dernier lot parti s'est arrêté
       juste AVANT la page abîmée, donc la frontière `sent = 0` tombe avant elle, pas dessus.

    Montage : on marque `sent = 1`, on insère `decalage` lignes non envoyées, on relève
    `page_count`, puis on insère le reste — et on brouille autour de ce relevé.
    Renvoie `(chemin, rowid_frontiere)`.
    """
    d = repertoire or tempfile.mkdtemp()
    chemin = os.path.join(d, "measurements.db")
    envoyees = n * 2 // 3
    c = db.connect(chemin)
    c.execute("INSERT INTO pdl (pdl_index, adco, first_seen, last_seen) VALUES (0,'000000000001',1790000000,1790099999)")
    c.executemany("INSERT INTO measurements(ts,pdl_index,papp,sent) VALUES(?,0,?,1)",
                  [(1790000000 + i, 100 + (i % 500)) for i in range(envoyees)])
    c.commit()
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.executemany("INSERT INTO measurements(ts,pdl_index,papp,sent) VALUES(?,0,?,0)",
                  [(1790000000 + envoyees + i, 100 + (i % 500)) for i in range(decalage)])
    c.commit()
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    repere = c.execute("PRAGMA page_count").fetchone()[0]
    c.executemany("INSERT INTO measurements(ts,pdl_index,papp,sent) VALUES(?,0,?,0)",
                  [(1790000000 + envoyees + decalage + i, 100 + (i % 500))
                   for i in range(n - envoyees - decalage)])
    c.commit()
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()

    intact = chemin + ".intact"
    shutil.copy(chemin, intact)
    for debut, larg in ((repere - 2, 6), (repere - 5, 12), (repere - 10, 30)):
        shutil.copy(intact, chemin)
        brouille(chemin, max(2, debut), larg)
        # ⚖️ LA PRÉMISSE : la PREMIÈRE ligne du lot doit se lire, et le LOT doit lever.
        une = not _leve(chemin, f"SELECT {COLS} FROM measurements WHERE sent = 0 "
                                "ORDER BY rowid LIMIT 1")
        lot = _leve(chemin, f"SELECT {COLS} FROM measurements WHERE sent = 0 "
                            f"ORDER BY rowid LIMIT {rb.N_LOT}")
        if une and lot:
            os.unlink(intact)
            return chemin, envoyees + 1
    raise AssertionError(
        "impossible de placer le dommage DANS le lot sans toucher sa première ligne : "
        "ce cas ne prouverait rien")


def reconstruit(chemin: str) -> dict:
    """`rb.rebuild()` avec le contrôle des ouvreurs neutralisé.

    🚨 POURQUOI NEUTRALISER, et pourquoi ce n'est pas tricher : le contrôle lit
       `/proc/<pid>/fd`, et ce banc tourne sur un Mac, qui n'a pas de `/proc`. La sonde rend
       donc `None` — « je n'ai pas pu regarder » — et la bascule est refusée, À JUSTE TITRE.

    ⭐ Un cas doit éprouver UNE chose. Les cas de reconstruction neutralisent donc ce contrôle,
      et DEUX cas dédiés l'éprouvent pour lui-même (`None` et « quelqu'un tient »). Sans cette
      séparation, le garde-fou masquerait tout le reste et on ne saurait plus ce qui est vert.
    """
    d = os.path.dirname(chemin)
    vrai, rb.ouvreurs = rb.ouvreurs, lambda _p: []
    # ⚠️ `RAPPORT` et `ETAT` sont des chemins ABSOLUS de la cible (/var/lib/ben-firmware). Il
    #    faut détourner LES DEUX, sinon la phase 1 échoue sur un FileNotFoundError et tous les
    #    cas tombent pour une raison qui n'a rien à voir avec ce qu'ils éprouvent.
    vr, ve = rb.RAPPORT, rb.ETAT
    rb.RAPPORT, rb.ETAT = os.path.join(d, "rapport.json"), os.path.join(d, "etat.json")
    try:
        return rb.rebuild(chemin)
    finally:
        rb.ouvreurs, rb.RAPPORT, rb.ETAT = vrai, vr, ve


def _detourne(chemin: str):
    """Détourne RAPPORT/ETAT vers le répertoire d'essai et neutralise le contrôle /proc."""
    d = os.path.dirname(chemin)
    sauv = (rb.ouvreurs, rb.RAPPORT, rb.ETAT)
    rb.ouvreurs = lambda _p: []
    rb.RAPPORT = os.path.join(d, "rapport.json")
    rb.ETAT = os.path.join(d, "etat.json")
    return sauv


def _restaure(sauv):
    rb.ouvreurs, rb.RAPPORT, rb.ETAT = sauv


# ── Les fonctions pures ─────────────────────────────────────────────────────────────────────

@cas
def est_corruption_reconnait_la_STRUCTURE_cassee():
    for msg in ("database disk image is malformed", "database corruption detected",
                "file is not a database"):
        assert rb.est_corruption(sqlite3.DatabaseError(msg)), msg


@cas
def est_corruption_NE_confond_PAS_avec_un_verrou():
    """🚨 LE TÉMOIN LE PLUS IMPORTANT DU MODULE. `database is locked` est TRANSITOIRE — le
    lecteur écrit en continu, la contention est normale. Le confondre avec une corruption
    ferait abandonner des données parfaitement saines, et déclencherait un arrêt de services
    sur un boîtier qui n'a rien."""
    for e in (sqlite3.OperationalError("database is locked"),
              sqlite3.OperationalError("database table is locked"),
              sqlite3.OperationalError("disk I/O error"),
              ValueError("malformed"),              # bon mot, mauvais type
              TimeoutError("timed out")):
        assert not rb.est_corruption(e), f"{type(e).__name__}: {e} pris pour une corruption"


@cas
def les_services_sont_DERIVES_des_capabilities():
    """⭐ PAS UNE LISTE EN DUR. Sur un boîtier **Radio**, `ben-tic-reader` (capability
    `tic-uart`) n'a rien à faire là — il y est `dead` avec 172 redémarrages, précisément parce
    qu'il n'est pas de ce modèle. Une liste en dur l'aurait arrêté puis redémarré pour rien."""
    radio = rb.services_a_arreter({"capabilities": {"lora": {}, "lora-tic-receiver": {},
                                                    "rgb-led-indicator": {}}})
    assert "ben-tic-reader.service" not in radio, radio
    assert "ben-telemetry.service" in radio and "ben-radio.service" in radio, radio
    filaire = rb.services_a_arreter({"capabilities": {"tic-uart": {},
                                                      "rgb-led-indicator": {}}})
    assert "ben-tic-reader.service" in filaire, filaire
    assert "ben-radio.service" not in filaire, filaire
    # Ceux qui tiennent la base sans dépendre du modèle — mesuré via /proc sur un boîtier.
    for m in (radio, filaire):
        assert "ben-publisher.service" in m and "ben-local-api.service" in m, m


@cas
def ben_certd_et_wifi_watchdog_ne_sont_JAMAIS_arretes():
    """🚨 DÉLIBÉRÉ. Mesuré en lisant `/proc/<pid>/fd` sur un boîtier du parc : seules trois
    unités ouvrent `measurements.db` — ben-telemetry, ben-publisher, ben-local-api. Ni
    `ben-certd` ni `wifi-watchdog` ne la touchent, donc les arrêter n'apporte RIEN.

    Et `wifi-watchdog` est précisément ce qui maintient le boîtier sur le réseau : le couper
    vingt minutes sur une machine qu'on ne peut pas atteindre serait un risque sans
    contrepartie. ⚠️ Et surtout jamais `ben-update`, qui est l'unité qui nous exécute."""
    for caps_ in ({"lora": {}, "lora-tic-receiver": {}}, {"tic-uart": {}}, {}):
        liste = rb.services_a_arreter({"capabilities": caps_})
        for interdit in ("ben-certd", "wifi-watchdog", "ben-update"):
            assert not any(interdit in u for u in liste), f"{interdit} dans {liste}"


@cas
def ben_radio_s_arrete_en_DERNIER_quel_que_soit_l_ordre_des_cles():
    """⚠️ `ben-radio` possède le GPIO de la radio et démarre en PREMIER (cf. `CAP_SERVICES`),
    donc il s'arrête en DERNIER.

    ⭐ Cas STRUCTUREL sur un invariant qui était ÉMERGENT : la première version faisait
      `reversed(lecteurs)`, ce qui plaçait bien ben-radio en dernier — mais seulement parce que
      l'ordre des clés de `capabilities` dans `device.json` s'y prêtait. C'est du JSON écrit par
      le provisioning ; rien ne le garantit. On éprouve donc l'ordre DÉFAVORABLE."""
    liste = rb.services_a_arreter({"capabilities": {"lora-tic-receiver": {}, "tic-uart": {},
                                                    "lora": {}}})
    assert liste[-1] == "ben-radio.service", liste


# ── Le refus ────────────────────────────────────────────────────────────────────────────────

@cas
def une_base_SAINE_est_refusee():
    """⚖️ LE TÉMOIN QUI PROTÈGE LE PARC. Cette update part aux sept boîtiers. Reconstruire une
    base saine, c'est arrêter les services d'un boîtier de terrain pour rien — et c'est le
    pire risque de tout le dispositif."""
    chemin, _ = base(2000)
    c = sqlite3.connect(f"file:{chemin}?mode=ro", uri=True)
    pourquoi = rb.refus(c, chemin)
    c.close()
    assert pourquoi and "sans erreur" in pourquoi, pourquoi


@cas
def une_base_ABIMEE_est_acceptee():
    chemin, _ = base_abimee()
    assert _leve(chemin, SQL_SCAN), "le montage ne produit pas de corruption détectable"
    c = sqlite3.connect(f"file:{chemin}?mode=ro", uri=True)
    pourquoi = rb.refus(c, chemin)
    c.close()
    assert pourquoi is None, f"refusé alors que la base est abîmée : {pourquoi}"


@cas
def les_INDEX_survivent_a_la_corruption_de_la_TABLE():
    """⭐ LE CŒUR DU DIAGNOSTIC DE TERRAIN, reproduit ici. Après brouillage, tout ce qui est
    servi par un index répond CORRECTEMENT — `max(rowid)`, `min(rowid) WHERE sent = 0`,
    `max(ts) WHERE pdl_index = ?`. Les index sont des arbres SÉPARÉS, dans d'autres pages.

    C'est pour ça que l'instantané de santé affichait `pending`, `unsent` et `last_ts` comme si
    tout allait bien pendant neuf jours : aucun de ces champs ne lit une ligne complète."""
    chemin, _ = base_abimee()
    c = sqlite3.connect(f"file:{chemin}?mode=ro", uri=True)
    try:
        for q, nom in (("SELECT max(rowid) FROM measurements", "max(rowid)"),
                       ("SELECT min(rowid) FROM measurements WHERE sent=0", "min rowid sent=0"),
                       ("SELECT max(ts) FROM measurements WHERE pdl_index=0", "max(ts) par pdl")):
            r = c.execute(q).fetchone()
            assert r and r[0], f"{nom} ne répond plus — le cas doit être revu"
    finally:
        c.close()


# ── La reconstruction, sur une vraie corruption ─────────────────────────────────────────────

@cas
def la_reconstruction_RECUPERE_tout_sauf_ce_qui_est_DETRUIT():
    """🚨 LE CAS QUI PORTE LE MODULE.

    On compte d'abord les lignes AVANT brouillage, on brouille, on reconstruit, et on exige :
      · la base neuve passe `integrity_check` ;
      · elle contient le total MOINS les lignes réellement détruites, pas une de moins ;
      · les plages perdues sont RAPPORTÉES — on ne perd rien en silence ;
      · l'original est CONSERVÉ ;
      · et la ligne la plus récente est bien là, à son `rowid` d'origine.

    ⭐ Ce dernier point n'est pas cosmétique : un comptage juste ne prouve pas que les BONNES
      lignes sont là. `pending_approx()` fait de l'arithmétique de rowid ; un décalage rendrait
      tous les chiffres du parc incomparables entre avant et après."""
    d = tempfile.mkdtemp()
    chemin, _ = base_abimee(30000, d)
    c = sqlite3.connect(f"file:{chemin}?mode=ro", uri=True)
    total = c.execute("SELECT max(rowid) FROM measurements").fetchone()[0]
    dernier = c.execute("SELECT rowid, ts FROM measurements ORDER BY rowid DESC LIMIT 1").fetchone()
    c.close()

    res = reconstruit(chemin)
    assert res.get("ok"), f"reconstruction refusée : {res.get('refus')}"

    m = res["detail"]["measurements"]
    assert m["lignes_perdues"] > 0, "aucune ligne perdue ? le brouillage n'a rien touché"
    assert m["perdues"], "les plages perdues ne sont pas rapportées"
    assert m["copiees"] == total - m["lignes_perdues"], (
        f"{m['copiees']} copiées, {total} au départ, {m['lignes_perdues']} perdues — "
        "il manque des lignes qui n'ont PAS été déclarées perdues")

    c = sqlite3.connect(f"file:{chemin}?mode=ro", uri=True)
    try:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert c.execute("SELECT count(*) FROM measurements").fetchone()[0] == m["copiees"]
        vu = c.execute("SELECT rowid, ts FROM measurements ORDER BY rowid DESC LIMIT 1").fetchone()
        assert vu == dernier, f"la dernière ligne est {vu}, elle valait {dernier}"
        # 🚨 LA MÉTADONNÉE A SUIVI — et ce cas a attrapé un vrai bug. `pdl` est déclarée
        #    `pdl_index INTEGER PRIMARY KEY`, donc `pdl_index` EST le `rowid`, et le premier
        #    PDL vaut TOUJOURS 0. La recopie partait de `rowid = 1` : la base reconstruite
        #    n'avait AUCUN compteur, et sans `pdl` aucune mesure n'a de sens.
        assert c.execute("SELECT count(*) FROM pdl").fetchone()[0] == 1, (
            "`pdl` est vide après reconstruction — la borne basse part-elle de 1 au lieu de "
            "min(rowid) ? Le premier PDL a le rowid 0")
        assert c.execute("SELECT pdl_index FROM pdl").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM emitter").fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM lora_link").fetchone()[0] == 200
    finally:
        c.close()

    sauvegardes = [f for f in os.listdir(d) if ".corrupt-" in f]
    assert sauvegardes, f"l'original n'a pas été conservé : {os.listdir(d)}"
    shutil.rmtree(d, ignore_errors=True)


@cas
def apres_reconstruction_une_SECONDE_passe_REFUSE():
    """🚨 L'IDEMPOTENCE, et elle est GRATUITE grâce à la garde de symptôme. Si l'OTA se
    redéclenche — elle rejoue toutes les 10 min quand `device.json` n'est pas bumpé — elle doit
    constater que la base est saine et ne RIEN faire. Sans ça, on arrêterait les services d'un
    boîtier déjà réparé, en boucle."""
    d = tempfile.mkdtemp()
    chemin, _ = base_abimee(10000, d)
    assert reconstruit(chemin).get("ok"), "la première passe a échoué"
    second = reconstruit(chemin)
    assert not second.get("ok"), "la seconde passe a reconstruit une base saine"
    assert "sans erreur" in second.get("refus", ""), second
    shutil.rmtree(d, ignore_errors=True)


@cas
def le_ROLLUP_ne_garde_pas_de_seaux_pour_des_points_disparus():
    """🚨 SANS ÇA, UN AGRÉGAT MENTIRAIT POUR TOUJOURS. `curve_rollup` garde `papp_sum` et
    `papp_count` ; s'ils comptent des points qui n'existent plus, la moyenne rendue par
    `/curve` reste fausse sur ces tranches, définitivement, sans que rien ne le signale."""
    d = tempfile.mkdtemp()
    chemin, _ = base_abimee(20000, d)
    # Un seau de rollup dans la zone détruite, et un autre très loin.
    c = db.connect(chemin)
    c.execute("INSERT INTO curve_rollup VALUES(0,?,0,1,?,?,1,9,10,2,0)",
              (1790009880, 1790009880, 1790009999))
    c.execute("INSERT INTO curve_rollup VALUES(0,?,0,1,?,?,1,9,10,2,0)",
              (1700000000, 1700000000, 1700000119))
    c.execute("INSERT INTO rollup_state VALUES(0, 1700000000, 1)")
    c.commit()
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()
    res = reconstruit(chemin)
    assert res.get("ok"), res.get("refus")
    c = sqlite3.connect(f"file:{chemin}?mode=ro", uri=True)
    try:
        etat = c.execute("SELECT watermark, done FROM rollup_state WHERE id=0").fetchone()
        loin = c.execute("SELECT count(*) FROM curve_rollup WHERE bucket_ts=1700000000").fetchone()[0]
    finally:
        c.close()
    assert etat and etat[1] == 0, f"`done` reste à 1 : le backfill ne reconstruira jamais ({etat})"
    assert loin == 1, "un seau HORS de la zone perdue a été supprimé — trop large"
    shutil.rmtree(d, ignore_errors=True)


@cas
def on_ne_remplace_JAMAIS_une_base_par_une_base_VIDE():
    """⚖️ LE TÉMOIN DE LA VALIDATION. Tout le reste de `valide()` — `integrity_check`, les
    comptages — passerait sur un fichier parfaitement sain et parfaitement vide."""
    d = tempfile.mkdtemp()
    vide = os.path.join(d, "vide.db")
    db.connect(vide).close()
    assert rb.valide(vide, {"measurements": 0}), "une base vide a été acceptée"
    assert rb.valide(vide, {"measurements": 5}), "un comptage faux a été accepté"
    shutil.rmtree(d, ignore_errors=True)


@cas
def la_bascule_CONSERVE_l_original_et_ne_laisse_aucune_fenetre():
    """🚨 LIEN DUR PUIS `os.replace`, ET L'ORDRE EST LE FOND.

    Renommer l'original puis mettre la neuve à sa place fait DEUX renommages, et entre les deux
    il n'existe aucun `measurements.db` : si le processus meurt là, le lecteur en recrée une
    VIDE au redémarrage et tout l'historique est perdu. Le lien dur préexiste à la bascule, donc
    il ne reste qu'UN geste, et `os.replace` est atomique.

    ⚖️ Cas STRUCTUREL : les deux constructions laissent le même état final quand tout va bien.
       Seule une mort au mauvais moment les distingue, et on ne peut pas la provoquer ici."""
    src = pathlib.Path(rb.__file__).read_text()
    corps = src[src.index("def bascule("):]
    corps = corps[:corps.index("\ndef ")]
    i_link = corps.find("os.link(")
    i_repl = corps.find("os.replace(")
    assert i_link >= 0, "la sauvegarde n'est plus un lien dur"
    assert i_repl > i_link, "`os.replace` précède le lien dur : fenêtre sans base"
    assert "os.rename(original" not in corps, (
        "l'original est RENOMMÉ : il existe un instant sans `measurements.db`")
    # Et l'effet, sur de vrais fichiers.
    d = tempfile.mkdtemp()
    orig, neuve = os.path.join(d, "a.db"), os.path.join(d, "a.db.rebuild")
    pathlib.Path(orig).write_bytes(b"ancienne")
    pathlib.Path(neuve).write_bytes(b"neuve")
    sauvegarde = rb.bascule(orig, neuve)
    assert pathlib.Path(orig).read_bytes() == b"neuve"
    assert pathlib.Path(sauvegarde).read_bytes() == b"ancienne", "l'original est perdu"
    assert not os.path.exists(neuve), "le fichier intermédiaire traîne encore"
    shutil.rmtree(d, ignore_errors=True)


@cas
def un_disque_PLEIN_fait_refuser_sans_rien_toucher():
    """⚠️ On écrit un second fichier de la taille de la base. Sans cette garde, on remplirait
    la carte SD d'un boîtier de terrain — et une carte pleine arrête l'écriture des mesures,
    donc on casserait ce qui marchait encore."""
    chemin, _ = base_abimee(8000)
    vrai = rb.MARGE_DISQUE
    rb.MARGE_DISQUE = 10 ** 12          # exigence absurde : il faut que ça refuse
    try:
        c = sqlite3.connect(f"file:{chemin}?mode=ro", uri=True)
        pourquoi = rb.refus(c, chemin)
        c.close()
    finally:
        rb.MARGE_DISQUE = vrai
    assert pourquoi and "disque insuffisant" in pourquoi, pourquoi


@cas
def la_borne_haute_survit_a_une_table_abimee_a_son_EXTREMITE():
    """🚨 `SELECT max(rowid)` lit la feuille la plus à DROITE. Quand c'est elle qui est
    détruite — le cas quand le dommage touche les lignes les plus RÉCENTES — il lève.

    ⭐ Le repli passe par les INDEX, arbres séparés donc intacts : l'index `(sent)` contient
      implicitement le `rowid`, donc sa dernière entrée en donne un sans toucher la table. Et
      on MAJORE : lire une plage de rowid inexistante ne rend rien, donc surestimer est
      gratuit, tandis que sous-estimer perdrait des lignes EN SILENCE."""
    d = tempfile.mkdtemp()
    chemin = base_abimee_a_la_FIN(20000, d)
    c = sqlite3.connect(f"file:{chemin}?mode=ro", uri=True)
    try:
        leve = False
        try:
            c.execute("SELECT max(rowid) FROM measurements").fetchone()
        except Exception:  # noqa: BLE001
            leve = True
        assert leve, "max(rowid) ne lève pas — le montage ne prouve rien"
        borne = rb._max_rowid(c, "measurements")
    finally:
        c.close()
    assert borne > 0, "la borne haute est nulle : aucune ligne ne serait recopiée"
    assert borne >= 20000, f"borne {borne} < 20000 : des lignes seraient perdues EN SILENCE"
    shutil.rmtree(d, ignore_errors=True)


@cas
def rebuild_ne_LEVE_JAMAIS_sur_un_etat_de_la_donnee():
    """🚨 LA PROMESSE EST DANS LE CODE, PLUS SEULEMENT DANS LA DOCSTRING.

    Un défaut trouvé en répétant l'opération sur cible la démentait : `_max_rowid` levait hors
    de tout `try`. Et la conséquence n'était pas un message d'erreur mais une update en échec,
    donc `device.json` non bumpé, donc REJEU toutes les 10 minutes AVEC ARRÊT DES SERVICES à
    chaque passage — la mécanique qui a brûlé pi-0.9.12.

    ⚖️ On éprouve les DEUX formes de dommage, parce que c'est leur différence qui a révélé le
       défaut : au milieu (max(rowid) répond) et à la fin (max(rowid) lève)."""
    for fabrique, nom in ((lambda d: base_abimee(12000, d)[0], "au milieu"),
                          (lambda d: base_abimee_a_la_FIN(12000, d), "à la fin")):
        d = tempfile.mkdtemp()
        chemin = fabrique(d)
        rb.RAPPORT = os.path.join(d, "rapport.json")
        res = reconstruit(chemin)       # ne doit PAS lever
        assert isinstance(res, dict), f"{nom} : rebuild() ne rend pas un dict"
        assert "ok" in res, f"{nom} : {res!r}"
        shutil.rmtree(d, ignore_errors=True)

    # 🚨 ET ON ÉPROUVE L'ENVELOPPE ELLE-MÊME, en injectant une panne IMPRÉVUE.
    #
    #    Sans ça, ce cas ne prouvait rien de l'enveloppe : vérifié par mutation — en la
    #    retirant, le cas restait VERT, parce que le repli de `_max_rowid` fait qu'aucune
    #    exception ne remonte plus naturellement. Un cas qui passe sur le code mutilé est un
    #    cas qui ne garde rien.
    #
    # ⭐ Et l'enjeu n'est pas théorique : une exception qui remonte fait échouer l'update, donc
    #   `device.json` n'est pas bumpé, donc elle REJOUE toutes les 10 minutes EN ARRÊTANT LES
    #   SERVICES à chaque passage.
    d = tempfile.mkdtemp()
    chemin, _ = base_abimee(8000, d)
    rb.RAPPORT = os.path.join(d, "rapport.json")
    vrai = rb.tables
    rb.tables = lambda conn: (_ for _ in ()).throw(RuntimeError("panne imprévue"))
    try:
        res = reconstruit(chemin)
    finally:
        rb.tables = vrai
    assert isinstance(res, dict) and res.get("ok") is False, (
        f"une panne imprévue n'est pas rattrapée : {res!r}")
    assert "imprévue" in res.get("refus", ""), res
    shutil.rmtree(d, ignore_errors=True)


@cas
def un_dommage_EN_FIN_de_table_est_quand_meme_reconstruit():
    """⭐ Et pas seulement « sans lever » : la reconstruction doit RÉCUPÉRER. C'est le cas le
    plus probable sur le terrain, puisque les pages les plus récemment écrites sont celles que
    la carte SD vient de solliciter."""
    d = tempfile.mkdtemp()
    chemin = base_abimee_a_la_FIN(20000, d)
    res = reconstruit(chemin)
    assert res.get("ok"), f"refusé : {res.get('refus')}"
    m = res["detail"]["measurements"]
    assert m["copiees"] > 15000, (
        f"{m['copiees']} lignes seulement recopiées sur ~20 000 : la borne haute est "
        "sous-estimée, on perd des lignes en silence")
    c = sqlite3.connect(f"file:{chemin}?mode=ro", uri=True)
    try:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        c.close()
    shutil.rmtree(d, ignore_errors=True)


@cas
def refus_LIT_LE_LOT_ENTIER_pas_une_ligne():
    """🚨 LE DÉFAUT LE PLUS GRAVE TROUVÉ EN REVUE : l'update aurait été INOPÉRANTE sur le
    boîtier même pour lequel elle est faite.

    `fetch_batch` lit mille lignes. Une sonde qui n'en lit qu'une passe dès que la page
    détruite n'est pas la première du lot — et c'est le cas le plus probable.

    ⚖️ Le témoin est DANS le cas : on vérifie que la lecture d'UNE ligne réussit. Sans ça, on
       ne saurait pas si `refus()` accepte pour la bonne raison."""
    d = tempfile.mkdtemp()
    chemin, _ = base_abimee_DANS_le_lot(30000, d)
    # Le témoin : une seule ligne se lit sans erreur.
    assert not _leve(chemin, f"SELECT {COLS} FROM measurements WHERE sent = 0 "
                             "ORDER BY rowid LIMIT 1"), \
        "la première ligne du lot lève : le montage ne prouve pas ce qu'on veut"
    c = sqlite3.connect(f"file:{chemin}?mode=ro", uri=True)
    pourquoi = rb.refus(c, chemin)
    c.close()
    assert pourquoi is None, (
        f"refus() a laissé passer une base abîmée : {pourquoi!r} — le boîtier resterait bloqué")
    shutil.rmtree(d, ignore_errors=True)


@cas
def la_borne_haute_est_EXACTE_et_non_majoree():
    """🚨 J'avais majoré de 10 %, en supposant que lire une plage de `rowid` inexistante « ne
    coûte rien et ne rend rien ». FAUX, et trouvé en revue : la recherche descend vers la
    droite et retombe sur la MÊME feuille détruite, donc chaque sonde LÈVE. Sur 4,3 M lignes
    c'était ~430 000 lignes fantômes sondées une par une, et surtout **comptées comme
    PERDUES** — un rapport faux d'un ordre de grandeur.

    ⭐ Et la majoration était inutile : `sent` est `INTEGER NOT NULL DEFAULT 0`, donc toute
      ligne porte 0 ou 1 et le maximum des deux index EST le vrai maximum."""
    d = tempfile.mkdtemp()
    chemin = base_abimee_a_la_FIN(20000, d)
    c = sqlite3.connect(f"file:{chemin}?mode=ro", uri=True)
    try:
        borne = rb._max_rowid(c, "measurements")
    finally:
        c.close()
    assert borne == 20000, (
        f"borne = {borne}, attendu exactement 20000 — une majoration ferait sonder des lignes "
        "inexistantes sur la feuille détruite, et les compterait comme perdues")
    shutil.rmtree(d, ignore_errors=True)


@cas
def un_dommage_EN_FIN_ne_GONFLE_pas_le_nombre_de_perdues():
    """⭐ L'effet du cas précédent, mesuré là où il compte : le rapport doit dire la vérité."""
    d = tempfile.mkdtemp()
    chemin = base_abimee_a_la_FIN(20000, d)
    res = reconstruit(chemin)
    assert res.get("ok"), res.get("refus")
    m = res["detail"]["measurements"]
    assert m["copiees"] + m["lignes_perdues"] == 20000, (
        f"{m['copiees']} copiées + {m['lignes_perdues']} perdues != 20000 : le compte est "
        "faux, donc le rapport ment")
    shutil.rmtree(d, ignore_errors=True)


@cas
def les_OUVREURS_du_fichier_sont_vus():
    """🚨 ON VÉRIFIE L'EFFET, PAS LA DÉCISION — défaut trouvé en revue, et c'était une perte de
    données SILENCIEUSE. `update.sh` n'arrêtait que les unités `is-active` ; une unité en cours
    de REDÉMARRAGE ne l'est pas, donc elle était ignorée, revenait, gardait la base ouverte, et
    après le `os.replace` ses écritures partaient dans le fichier devenu `.corrupt-*`.

    ⇒ L'invariant réel n'est pas « j'ai demandé l'arrêt » mais « personne ne tient le
      fichier », et il se lit dans /proc/<pid>/fd."""
    d = tempfile.mkdtemp()
    chemin, _ = base(2000, d)

    # 🚨 `None` ≠ `[]`, ET LA DIFFÉRENCE EST TOUT. Une liste vide dit « j'ai regardé, personne
    #    ne tient le fichier » ; `None` dit « JE N'AI PAS PU REGARDER ». Les confondre
    #    autoriserait une bascule sous un écrivain partout où `/proc` est absent — et ce banc
    #    tourne justement sur un Mac, qui n'en a pas. C'est ce qui a révélé le défaut.
    if not os.path.isdir("/proc"):
        c = sqlite3.connect(chemin)
        try:
            assert rb.ouvreurs(chemin) is None, (
                "sans /proc, la sonde rend une liste au lieu de `None` : `update.sh` "
                "concluerait « voie libre » alors qu'elle n'a rien pu voir")
        finally:
            c.close()
        shutil.rmtree(d, ignore_errors=True)
        return

    c = sqlite3.connect(chemin)            # NOUS tenons le fichier ouvert
    try:
        vus = rb.ouvreurs(chemin)
        assert vus is not None, "/proc existe mais la sonde rend `None`"
        assert os.getpid() in vus, (
            f"notre propre processus n'est pas vu comme ouvreur ({vus}) — le contrôle serait "
            "aveugle et laisserait basculer sous un écrivain")
    finally:
        c.close()
    # ⚖️ LE TÉMOIN : une fois fermé, plus personne. Sans lui, une fonction qui rendrait
    #    toujours notre pid passerait le cas ci-dessus et bloquerait toute reconstruction.
    assert os.getpid() not in (rb.ouvreurs(chemin) or []), \
        "le fichier est encore vu comme ouvert après fermeture"
    shutil.rmtree(d, ignore_errors=True)


@cas
def le_compteur_de_TENTATIVES_survit_au_rapport():
    """🚨 Défaut trouvé en revue : `_ecris_rapport` réécrivait le fichier de zéro, donc le
    compteur que `update.sh` y avait posé AVANT de travailler repartait à 0. Un boîtier qui
    redémarre après chaque reconstruction (chien de garde, brownout) aurait rejoué
    l'opération indéfiniment, en arrêtant ses services à chaque passage."""
    d = tempfile.mkdtemp()
    chemin, _ = base_abimee(8000, d)
    rb.RAPPORT = os.path.join(d, "rapport.json")
    rb.ETAT = os.path.join(d, "etat.json")
    with open(rb.RAPPORT, "w", encoding="utf-8") as f:
        f.write('{"tentatives": 2}')
    res = reconstruit(chemin)
    assert res.get("ok"), res.get("refus")
    with open(rb.RAPPORT, encoding="utf-8") as f:
        import json as _j
        ecrit = _j.load(f)
    assert ecrit.get("tentatives") == 2, (
        f"`tentatives` vaut {ecrit.get('tentatives')!r} après le rapport — le frein est annulé")
    shutil.rmtree(d, ignore_errors=True)


@cas
def le_watermark_du_rollup_ne_DESCEND_jamais():
    """🚨 Défaut trouvé en revue. `watermark` est la borne BASSE couverte : « complet pour
    [watermark, now] ». Le poser sous la valeur courante déclarerait couvert un intervalle
    JAMAIS rempli — et `/curve` rendrait des données VIDES sur cette fenêtre en croyant lire
    un rollup complet.

    ⭐ Un watermark PLUS HAUT revendique MOINS de couverture : c'est le sens sûr."""
    conn = db.connect(":memory:")
    HAUT = 1790500000
    conn.execute("INSERT INTO rollup_state VALUES(0, ?, 0)", (HAUT,))
    conn.commit()
    res = rb.corrige_rollup(conn, [1790000000, 1790000500])   # un trou BIEN plus ancien
    assert res["watermark"] >= HAUT, (
        f"le watermark est descendu à {res['watermark']} (était {HAUT}) : /curve rendrait du "
        "vide sur l'intervalle jamais rempli")
    vu = conn.execute("SELECT watermark, done FROM rollup_state WHERE id=0").fetchone()
    assert vu[0] >= HAUT and vu[1] == 0, vu


@cas
def la_bascule_est_REFUSEE_si_quelqu_un_tient_la_base():
    """🚨 LE GARDE-FOU QUI COUVRE CE QUE JE N'AI PAS SU ÉNUMÉRER — trouvé en revue.

    `update.sh` vérifie avant de commencer que personne ne tient le fichier. Mais la
    reconstruction dure UNE HEURE, et un timer peut tirer entre les deux : `ben-level-profiler`
    (`levels.py`, ouverture en ÉCRITURE) a un timer quotidien, et il n'était dans AUCUNE de mes
    listes — parce qu'il n'est pas une capability, donc aucune dérivation depuis `CAP_SERVICES`
    ne l'aurait trouvé.

    ⭐ Ce contrôle-ci ne dépend d'aucune liste : il regarde qui tient le fichier, juste avant
      de basculer. C'est la réponse à la CLASSE du défaut, pas à son instance."""
    d = tempfile.mkdtemp()
    chemin, _ = base_abimee(8000, d)
    rb.RAPPORT = os.path.join(d, "rapport.json")
    rb.ETAT = os.path.join(d, "etat.json")
    vrai = rb.ouvreurs
    rb.ouvreurs = lambda _p: [4242]
    try:
        res = rb.rebuild(chemin)
    finally:
        rb.ouvreurs = vrai
    assert res.get("ok") is False, "la bascule a eu lieu alors qu'un processus tenait la base"
    assert "réouverte" in res.get("refus", ""), res
    # ⚖️ ET L'ORIGINAL EST INTACT : refuser ne doit rien casser.
    assert not [f for f in os.listdir(d) if ".corrupt-" in f], \
        "une sauvegarde a été créée alors que la bascule était refusée"
    shutil.rmtree(d, ignore_errors=True)


@cas
def la_bascule_est_REFUSEE_si_on_ne_peut_pas_VERIFIER():
    """🚨 `None` ≠ `[]`. « Je n'ai pas pu regarder » ne doit jamais valoir « voie libre » — un
    contrôle qui ne voit pas doit le dire, surtout celui qui autorise à remplacer une base de
    production."""
    d = tempfile.mkdtemp()
    chemin, _ = base_abimee(8000, d)
    rb.RAPPORT = os.path.join(d, "rapport.json")
    rb.ETAT = os.path.join(d, "etat.json")
    vrai = rb.ouvreurs
    rb.ouvreurs = lambda _p: None
    try:
        res = rb.rebuild(chemin)
    finally:
        rb.ouvreurs = vrai
    assert res.get("ok") is False, "la bascule a eu lieu sans avoir pu vérifier"
    assert "vérifier" in res.get("refus", ""), res
    shutil.rmtree(d, ignore_errors=True)


@cas
def le_TIMER_du_profileur_est_arrete_et_EN_PREMIER():
    """🚨 Défaut trouvé en revue, et il était STRUCTUREL. Arrêter un `.service` N'ARRÊTE PAS
    son `.timer` : celui-ci peut le relancer en pleine reconstruction.
    `ben-level-profiler.timer` tire tous les jours (`Persistent=true`) et sa cible ouvre la
    base en ÉCRITURE.

    ⭐ Et ma liste ne pouvait pas le trouver : elle dérive de `CAP_SERVICES`, qui ne décrit que
      les LECTEURS. Une tâche périodique n'est pas une capability."""
    for caps_ in ({"lora": {}, "lora-tic-receiver": {}}, {"tic-uart": {}}, {}):
        liste = rb.services_a_arreter({"capabilities": caps_})
        assert "ben-level-profiler.service" in liste, liste
        assert "ben-level-profiler.timer" in liste, (
            f"le TIMER n'est pas arrêté : il relancera le service en pleine reconstruction "
            f"({liste})")
        assert liste.index("ben-level-profiler.timer") < \
               liste.index("ben-level-profiler.service"), (
            "le timer est arrêté APRÈS son service : entre les deux, il peut le relancer")


@cas
def TOUTES_les_tables_passent_par_la_copie_par_tranches():
    """🚨 Défaut trouvé en revue. `copie_table()` chargeait une table ENTIÈRE en mémoire et
    l'écrivait en UNE transaction. `curve_rollup` peut dépasser 100 000 lignes — exactement
    l'accumulation qu'on prétend éviter, sur la machine qui a déjà fait mordre le chien de
    garde.

    ⭐ Bénéfice en prime : une table de métadonnée partiellement abîmée est désormais recopiée
      pour ce qu'elle a de LISIBLE, au lieu d'être abandonnée en bloc.

    ⚖️ Cas STRUCTUREL : sur une base de banc, les deux chemins donnent le même résultat."""
    src = pathlib.Path(rb.__file__).read_text()
    assert "def copie_table(" not in src, (
        "`copie_table()` est encore là : du code mort dans un module critique, et quelqu'un "
        "finira par s'en resservir")
    for fn in ("def phase1(", "def phase2("):
        corps = src[src.index(fn):]
        corps = corps[:corps.index("\ndef ")]
        corps = "\n".join(l for l in corps.splitlines() if not l.lstrip().startswith("#"))
        assert "copie_par_tranches(" in corps, f"{fn} ne recopie pas par tranches"
        assert "copie_table(" not in corps, f"{fn} recopie encore une table en bloc"
    # Et l'effet : une base avec un gros rollup se recopie quand même.
    d = tempfile.mkdtemp()
    chemin, _ = base_abimee(8000, d)
    c = db.connect(chemin)
    c.executemany("INSERT OR REPLACE INTO curve_rollup VALUES(0,?,0,1,?,?,1,9,10,2,0)",
                  [(1700000000 + i * 120, 1700000000 + i * 120, 1700000000 + i * 120 + 119)
                   for i in range(3000)])
    c.commit()
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()
    res = reconstruit(chemin)
    assert res.get("ok"), res.get("refus")
    assert res["petites"].get("curve_rollup") == 3000, res["petites"]
    shutil.rmtree(d, ignore_errors=True)


@cas
def la_phase1_ne_touche_NI_l_original_NI_la_bascule():
    """⭐ C'EST TOUTE LA RAISON D'ÊTRE DU DÉCOUPAGE. La phase 1 dure ~50 min sur une base de
    306 Mo, et pendant tout ce temps le boîtier doit continuer de mesurer, écrire, publier et
    répondre à l'app. Elle ne doit donc rien arrêter et rien remplacer.

    ⚖️ On le vérifie par l'EMPREINTE de l'original : aucune hypothèse sur ce que fait le code,
       on compare les octets."""
    import hashlib
    d = tempfile.mkdtemp()
    chemin, _ = base_abimee(8000, d)
    avant = hashlib.sha256(pathlib.Path(chemin).read_bytes()).hexdigest()
    sauv = _detourne(chemin)
    try:
        res = rb.phase1(chemin)
    finally:
        _restaure(sauv)
    assert res.get("ok"), res.get("refus")
    apres = hashlib.sha256(pathlib.Path(chemin).read_bytes()).hexdigest()
    assert avant == apres, "la phase 1 a MODIFIÉ l'original"
    assert not [f for f in os.listdir(d) if ".corrupt-" in f], \
        "la phase 1 a basculé : elle ne doit jamais remplacer la base"
    assert os.path.exists(chemin + ".rebuild"), "la base neuve n'a pas été créée"
    shutil.rmtree(d, ignore_errors=True)


@cas
def le_DELTA_arrive_pendant_la_phase1_est_RECUPERE():
    """⭐ LE CŒUR DU DÉCOUPAGE : les mesures écrites PENDANT la phase 1 ne doivent pas être
    perdues. La phase 2 recopie ce qui est au-delà du `rowid` relevé au départ."""
    d = tempfile.mkdtemp()
    chemin, _ = base_abimee(8000, d)
    sauv = _detourne(chemin)
    try:
        assert rb.phase1(chemin).get("ok")
        # Le collecteur a tourné pendant la phase 1 : 500 mesures de plus.
        c = sqlite3.connect(chemin, timeout=30.0)
        c.executemany("INSERT INTO measurements(ts,pdl_index,papp,sent) VALUES(?,0,?,0)",
                      [(1799000000 + i, 777) for i in range(500)])
        c.commit()
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.close()
        res = rb.phase2(chemin)
    finally:
        _restaure(sauv)
    assert res.get("ok"), res.get("refus")
    c = sqlite3.connect(f"file:{chemin}?mode=ro", uri=True)
    try:
        n = c.execute("SELECT count(*) FROM measurements WHERE papp = 777").fetchone()[0]
    finally:
        c.close()
    assert n == 500, (
        f"{n} des 500 mesures arrivées pendant la phase 1 ont été recopiées — le delta est "
        "perdu, et avec lui tout ce que le boîtier a mesuré pendant l'heure")
    shutil.rmtree(d, ignore_errors=True)


@cas
def une_table_MODIFIEE_SUR_PLACE_est_recopiee_en_PHASE_2():
    """🚨 LE CAS QUI JUSTIFIE QUE LE DÉCOUPAGE SE FASSE PAR MUTABILITÉ ET NON PAR TAILLE.

    `pdl.last_seen` est réécrit à chaque trame, `emitter.updated_ts` aussi, `curve_rollup` fait
    un UPSERT par tranche de 2 min, `level_profile` est réécrit par ben-level-profiler. Une
    logique de delta par `rowid` NE VERRAIT PAS ces mises à jour : si on recopiait ces tables
    en phase 1, la base neuve porterait des valeurs PÉRIMÉES, et personne ne le remarquerait.

    ⇒ Elles sont donc recopiées en phase 2, écrivains arrêtés. On l'éprouve en modifiant la
      valeur ENTRE les deux phases et en exigeant la NOUVELLE."""
    d = tempfile.mkdtemp()
    chemin, _ = base_abimee(8000, d)
    sauv = _detourne(chemin)
    try:
        assert rb.phase1(chemin).get("ok")
        c = sqlite3.connect(chemin, timeout=30.0)
        c.execute("UPDATE pdl SET last_seen = 1799999999 WHERE pdl_index = 0")
        c.execute("INSERT OR REPLACE INTO curve_rollup "
                  "VALUES(0,1799000040,0,1,1799000040,1799000159,5,50,100,20,0)")
        c.commit()
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.close()
        res = rb.phase2(chemin)
    finally:
        _restaure(sauv)
    assert res.get("ok"), res.get("refus")
    c = sqlite3.connect(f"file:{chemin}?mode=ro", uri=True)
    try:
        vu = c.execute("SELECT last_seen FROM pdl WHERE pdl_index = 0").fetchone()[0]
        seau = c.execute("SELECT papp_count FROM curve_rollup "
                         "WHERE bucket_ts = 1799000040").fetchone()
    finally:
        c.close()
    assert vu == 1799999999, (
        f"`pdl.last_seen` vaut {vu} au lieu de 1799999999 : la table a été recopiée en phase 1, "
        "donc avec une valeur périmée — et aucun delta par rowid ne l'aurait rattrapée")
    assert seau and seau[0] == 20, f"le seau de rollup modifié n'a pas suivi : {seau!r}"
    shutil.rmtree(d, ignore_errors=True)


@cas
def phase2_SANS_phase1_refuse():
    """⚖️ Le témoin du dispositif : sans l'état de la phase 1, on ne sait pas quel `rowid`
    borne le delta ni où est la base neuve. Il faut refuser, pas deviner."""
    d = tempfile.mkdtemp()
    chemin, _ = base_abimee(4000, d)
    sauv = _detourne(chemin)
    try:
        res = rb.phase2(chemin)       # aucun état écrit
    finally:
        _restaure(sauv)
    assert res.get("ok") is False and "phase 1" in res.get("refus", ""), res
    shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    ko = 0
    for fn in CAS:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            ko += 1
            print(f"  KO   {fn.__name__} : {e}")
    print(f"\n{len(CAS) - ko}/{len(CAS)}")
    sys.exit(1 if ko else 0)
