#!/usr/bin/env python3
"""Banc du ménage des fantômes — la MARCHE À BLANC de l'issue #7.

🚨 Pourquoi un banc et pas un essai sur la cible : **le boîtier atteint est injoignable**
(fenêtres de connectivité courtes, pas de SSH). On ne peut donc pas lire une marche à
blanc sur place. On reconstitue sa maladie ici, à l'identique, et c'est ce banc qui
tient lieu d'essai — avec l'avantage de rester dans le dépôt.

La maladie reproduite est celle de ben-0004, relevée dans le cloud le 30/09 : trois ADCO
dont le **bit 6** d'un caractère s'est mis à 1 (`0`→`p`, `1`→`q`) et quatre époques
`HC..`→`HCn.` (`.`→`n`). 0x40 = 64, et le checksum TIC `(somme & 0x3F) + 0x20` est
aveugle à tout multiple de 64 : `HC..` et `HCn.` ont le MÊME checksum, 0x47.

    python3 src/pi/store/test_menage_fantomes.py
"""
import pathlib
import sqlite3
import sys

R = pathlib.Path(__file__).resolve().parent
sys.path[:0] = [str(R)]

import db                  # noqa: E402
import menage_fantomes as m  # noqa: E402

CAS = []


def lignes(curseur) -> list:
    """⚠️ `db.connect` pose `row_factory = sqlite3.Row` — un `Row` n'est jamais égal à
    un tuple, et la comparaison échouerait en silence sur une base pourtant correcte."""
    return [tuple(r) for r in curseur]


def cas(fn):
    CAS.append(fn)
    return fn


def malade() -> sqlite3.Connection:
    """La base de ben-0004 telle qu'elle est : un vrai PDL, trois fantômes, quatre époques."""
    c = db.connect(":memory:")
    c.execute("INSERT INTO pdl VALUES(0,'061961403012',1,9)")
    for i, adco in enumerate(("p61961403012", "061961403p12", "0619614030q2"), start=1):
        c.execute("INSERT INTO pdl VALUES(?,?,1,1)", (i, adco))
        # ⚠️ `index_id` N'EST PAS DÉCORATIF : sans lui `_rollup_ingest` ignore le point, et
        #    tout le chemin de reconstruction du rollup resterait NON EXERCÉ — c'est
        #    exactement ce qui a laissé passer un retour arrière incomplet.
        c.execute("INSERT INTO measurements(ts,pdl_index,papp,index_id,index_value,sent) "
                  "VALUES(?,?,?,1,?,1)", (100 + i, i, 42, 5000 + i))
        c.execute("INSERT INTO level_profile(pdl_index,computed_ts) VALUES(?,0)", (i,))
    for t in range(1000):                       # le vrai compteur, qu'on ne doit PAS toucher
        c.execute("INSERT INTO measurements(ts,pdl_index,papp,sent) VALUES(?,0,?,1)", (t, t))
    for ts, ngtf in ((0, "HC.."), (10, "HCn."), (12, "HC.."), (20, "HCn."), (21, "HC..")):
        c.execute("INSERT INTO contract_epoch VALUES(0,?,?)", (ts, ngtf))
    # Ce qu'une époque abîmée traîne AVEC elle — `record_ngtf` écrit les quatre d'un bloc.
    # ⚠️ Le corps porte des APOSTROPHES et `donnees` du JSON : c'est ce couple qui casse
    #    une sauvegarde citée avec `repr()`.
    c.execute("INSERT INTO event(id,ts,pdl_index,type,titre,corps,donnees) VALUES"
              "('d:1',10,0,'changement_offre','Changement d''offre',"
              "'C''est fait : votre contrat est passé de « HC.. » à « HCn. ».',"
              "'{\"avant\": \"HC..\", \"apres\": \"HCn.\"}')")
    c.execute("INSERT INTO event(id,ts,pdl_index,type,titre,corps,donnees) VALUES"
              "('d:2',12,0,'changement_offre','Changement d''offre',"
              "'C''est fait : votre contrat est passé de « HCn. » à « HC.. ».',"
              "'{\"avant\": \"HCn.\", \"apres\": \"HC..\"}')")
    c.execute("INSERT INTO tariff_labels VALUES(0,1,1,'HC..','HC',9)")
    c.execute("INSERT INTO tariff_labels VALUES(0,1,1,'HCn.','HC',9)")   # le libellé chimère
    c.execute("INSERT INTO level_profile(pdl_index,computed_ts,ngtf) VALUES(0,0,'HC..')")
    # Une tranche de courbe DÉJÀ construite pour le vrai compteur, dans la même fenêtre que
    # les points fantômes : la ré-ingestion va la modifier, le retour arrière doit la rendre.
    db._rollup_ingest(c, 0, [(101, 0, 1, 900, 4900)])
    c.execute("INSERT INTO rollup_state(id,watermark,done) VALUES(0,0,1)")   # backfill TERMINÉ
    c.commit()
    return c


def empreinte(c) -> dict:
    """L'état COMPLET, table par table — pour prouver un retour arrière.

    ⭐ Le `rowid` n'est comparé QUE là où il est l'identité : `measurements` et `lora_link`
    n'ont AUCUNE clé primaire, et le publisher marque `sent` PAR ROWID — un rowid qui
    bouge y serait un vrai dégât. Les autres tables ont une vraie clé primaire, et un
    `INSERT OR REPLACE` leur réalloue un rowid que personne ne regarde ; l'exiger stable
    ferait échouer le banc sur une restauration pourtant parfaite.

    ⚠️ Ce n'est pas un assouplissement pour passer au vert : le défaut visé est le
    DOUBLON, et il se voit entièrement dans le contenu."""
    out = {}
    for (nom,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        cle = "rowid, *" if nom in m.A_REATTRIBUER else "*"
        out[nom] = sorted(tuple(r) for r in c.execute(f"SELECT {cle} FROM {nom}"))
    return out


@cas
def les_trois_fantomes_partent_et_leurs_mesures_reviennent_au_vrai():
    c = malade()
    m.menage(c, a_blanc=False)
    assert [r[0] for r in c.execute("SELECT pdl_index FROM pdl")] == [0]
    assert c.execute("SELECT count(*) FROM measurements WHERE pdl_index=0").fetchone()[0] == 1003
    # ⭐ Ré-attribuées, PAS supprimées — et remises à renvoyer au cloud, qui les a reçues
    #    sous un PDL qui n'existe plus.
    assert c.execute("SELECT count(*) FROM measurements WHERE sent=0").fetchone()[0] == 3


@cas
def il_ne_reste_qu_une_epoque_et_c_est_la_bonne():
    """Les quatre partent : deux abîmées par le bit 6, deux doublons qui redisent `HC..`."""
    c = malade()
    m.menage(c, a_blanc=False)
    assert lignes(c.execute("SELECT ts_start, ngtf FROM contract_epoch")) == [(0, "HC..")]


@cas
def une_VRAIE_bascule_de_contrat_est_INTOUCHABLE():
    """⚖️ LE TÉMOIN, et il n'est pas décoratif : sans lui, une règle qui supprimerait
    toute époque sauf la première passerait tous les cas ci-dessus — et effacerait le
    passage en Tempo d'un boîtier du parc, donc tout calcul de coût antérieur."""
    c = db.connect(":memory:")
    c.execute("INSERT INTO pdl VALUES(0,'031864467282',1,9)")
    for ts, ngtf in ((0, "BASE"), (500, "TEMPO")):
        c.execute("INSERT INTO contract_epoch VALUES(0,?,?)", (ts, ngtf))
    c.commit()
    m.menage(c, a_blanc=False)
    assert lignes(c.execute("SELECT ts_start, ngtf FROM contract_epoch")) == [(0, "BASE"), (500, "TEMPO")]


@cas
def la_marche_a_blanc_ne_touche_a_RIEN():
    c = malade()
    avant = lignes(c.execute("SELECT pdl_index, adco FROM pdl ORDER BY pdl_index"))
    r = m.menage(c, a_blanc=True)
    assert r["fantomes"] == [1, 2, 3] and r["deplacees"]["measurements"] == 3
    assert len(r["epoques"]) == 4
    assert lignes(c.execute("SELECT pdl_index, adco FROM pdl ORDER BY pdl_index")) == avant


@cas
def rejoue_sur_une_base_PROPRE_il_ne_fait_rien():
    """🚨 L'idempotence n'est pas un confort : si le contrôle d'effet échoue, l'update
    REJOUE toutes les 10 min (la mécanique qui a brûlé pi-0.9.12)."""
    c = malade()
    m.menage(c, a_blanc=False)
    r = m.menage(c, a_blanc=False)
    assert r["fantomes"] == [] and r["epoques"] == [] and not r["deplacees"]
    assert m.conforme(c) == []


@cas
def un_boitier_SAIN_est_deja_conforme():
    """⭐ Le succès n'est PAS « j'ai supprimé quelque chose ». Sur six boîtiers du parc il
    n'y a rien à faire — si l'invariant exigeait un effet, l'update échouerait partout
    ailleurs et rejouerait sans fin."""
    c = db.connect(":memory:")
    c.execute("INSERT INTO pdl VALUES(0,'031864467282',1,9)")
    c.execute("INSERT INTO measurements(ts,pdl_index,papp,sent) VALUES(1,0,10,1)")
    c.commit()
    assert m.conforme(c) == []
    assert m.menage(c, a_blanc=False)["fantomes"] == []
    assert m.conforme(c) == []


@cas
def deux_PDL_sains_font_REFUSER_le_menage():
    """🚨 Ré-attribuer suppose de savoir VERS QUI. Avec deux vrais compteurs la
    destination est indécidable : on ne devine pas, on sort sans rien toucher."""
    c = malade()
    c.execute("INSERT INTO pdl VALUES(9,'031864467282',1,9)")
    c.commit()
    r = m.menage(c, a_blanc=False)
    assert r["refus"] and sorted(x[0] for x in c.execute("SELECT pdl_index FROM pdl")) == [0, 1, 2, 3, 9]


@cas
def un_evenement_sans_PDL_n_est_pas_une_orpheline():
    """⚠️ `event.pdl_index` est NULLABLE. Le compter comme orphelin ferait échouer
    l'invariant sur un boîtier parfaitement sain."""
    c = db.connect(":memory:")
    c.execute("INSERT INTO pdl VALUES(0,'031864467282',1,9)")
    c.execute("INSERT INTO event(id,ts,pdl_index,type,titre) VALUES('x',1,NULL,'t','T')")
    c.commit()
    assert m.conforme(c) == []


@cas
def la_sauvegarde_precede_et_rejoue():
    """🚨 Les bases embarquées ne sont PAS sauvegardées : une ligne supprimée est perdue
    définitivement. On écrit les lignes VISÉES — pas la base, des centaines de Mo — et on
    exige qu'elles se rejouent telles quelles."""
    import tempfile
    c = malade()
    origine = empreinte(c)
    f = tempfile.NamedTemporaryFile("w+", suffix=".sql", delete=False)
    f.close()
    n = m.sauvegarde(c, f.name)
    assert n > 0, "sauvegarde vide alors qu'il y a de quoi sauver"
    sql = pathlib.Path(f.name).read_text()
    assert "p61961403012" in sql and "'HCn.'" in sql

    m.menage(c, a_blanc=False)
    assert m.conforme(c) == []
    # 🚨 LE VRAI CRITÈRE, et il ne se devine pas : rejouer doit rendre l'état D'ORIGINE,
    #    à l'octet. Une sauvegarde faite de simples INSERT ne le ferait PAS — les mesures
    #    sont DÉPLACÉES, pas supprimées, et `measurements` n'a aucune clé primaire : on
    #    récupérerait l'exemplaire restauré sous le fantôme EN PLUS de l'exemplaire déplacé
    #    sous le vrai PDL. Chaque ligne en double, et un banc qui ne vérifie que « les
    #    fantômes sont revenus » ne verrait rien.
    c.executescript(sql)
    assert empreinte(c) == origine, "le retour arriere ne rend pas l'etat d'origine"


@cas
def LIMITE_ASSUMEE_un_retour_au_contrat_d_origine_serait_efface():
    """⚠️ Ce cas n'est pas un bug à corriger, c'est la CONTREPARTIE de la simplicité de la
    règle, épinglée pour qu'on ne la redécouvre pas sur le terrain.

    `BASE → TEMPO → BASE` : le retour partage les deux premiers caractères avec la
    référence, donc il part, et le boîtier reste cru en TEMPO. Aucun boîtier du parc
    n'est dans ce cas (BASE, TEMPO, HC.. et BBR( diffèrent tous dès le 2e caractère).
    ⇒ Si ce test se met à gêner, c'est qu'un vrai boîtier est concerné : changer la règle,
      pas le test."""
    c = db.connect(":memory:")
    c.execute("INSERT INTO pdl VALUES(0,'031864467282',1,9)")
    for ts, ngtf in ((0, "BASE"), (500, "TEMPO"), (900, "BASE")):
        c.execute("INSERT INTO contract_epoch VALUES(0,?,?)", (ts, ngtf))
    c.commit()
    m.menage(c, a_blanc=False)
    assert lignes(c.execute("SELECT ts_start, ngtf FROM contract_epoch")) == [(0, "BASE"), (500, "TEMPO")]


@cas
def le_cortege_de_l_epoque_abimee_part_AVEC_elle():
    """⚠️ `record_ngtf` écrit QUATRE choses d'un bloc : l'époque, un événement
    `changement_offre`, une ligne `tariff_labels` et `level_profile.ngtf`. Ne supprimer
    que l'époque laisserait « votre contrat est passé de HC.. à HCn. » dans la cloche de
    l'app, un libellé chimère, et un `level_profile` qui ferait émettre un FAUX changement
    d'offre à la trame suivante — en rouvrant une époque datée d'AUJOURD'HUI."""
    c = malade()
    c.execute("UPDATE level_profile SET ngtf='HCn.' WHERE pdl_index=0")   # dernière abîmée
    c.commit()
    m.menage(c, a_blanc=False)
    assert c.execute("SELECT count(*) FROM event WHERE type='changement_offre'").fetchone()[0] == 0
    assert lignes(c.execute("SELECT ngtf FROM tariff_labels WHERE pdl_index=0")) == [("HC..",)]
    assert c.execute("SELECT ngtf FROM level_profile WHERE pdl_index=0").fetchone()[0] == "HC.."


@cas
def un_VRAI_changement_d_offre_n_est_JAMAIS_efface():
    """⚖️ LE TÉMOIN du cortège : un événement que l'utilisateur a réellement vu passer est
    un FAIT. Sans ce cas, un ménage qui supprimerait TOUS les `changement_offre` passerait
    le cas ci-dessus — et effacerait le passage en Tempo de la cloche d'un boîtier."""
    c = malade()
    c.execute("INSERT INTO event(id,ts,pdl_index,type,titre,corps,donnees) VALUES"
              "('vrai',30,0,'changement_offre','Changement','de BASE a TEMPO',"
              "'{\"avant\": \"BASE\", \"apres\": \"TEMPO\"}')")
    c.commit()
    m.menage(c, a_blanc=False)
    assert lignes(c.execute("SELECT id FROM event WHERE type='changement_offre'")) == [("vrai",)]


@cas
def AUCUN_etat_de_la_donnee_ne_fait_echouer_l_update():
    """🚨 LE CAS QUI PROTÈGE LE PARC. Un `update.sh` qui échoue laisse `device.json` non
    bumpé, donc l'update REJOUE toutes les 10 min — et ce serait DÉFINITIF : aucune version
    ultérieure ne pourrait plus atteindre le boîtier. Ni un refus volontaire, ni un
    invariant encore faux ne doivent produire ça."""
    assert m.code_sortie({"refus": "2 PDL sains : destination indécidable"}, []) == 0
    assert m.code_sortie({"refus": None}, ["tariff_labels : pdl_index orphelins [7]"]) == 0
    assert m.code_sortie({"refus": None}, []) == 0


@cas
def un_ORPHELIN_irreparable_ne_declenche_PAS_le_menage():
    """🚨 La PORTE et le RAPPORT ne sont pas la même question. `conforme()` signale aussi
    des `pdl_index` orphelins, que ce ménage ne sait pas réparer — et ils existent SANS le
    moindre fantôme : sur un boîtier LoRa, après une OTA, `emitter_pdl()` rend None tant
    que l'Arduino n'a pas réémis sa trame de boot, `ben-telemetry` se replie sur l'index de
    `sources.json`, et les mesures atterrissent sous un index sans ligne `pdl`.

    Gater là-dessus ferait arrêter et redémarrer le lecteur d'un boîtier qu'on n'a RIEN à
    nettoyer : des mesures perdues pour rien, la leçon de 0.9.17, et l'inverse exact de ce
    que ce script promet en en-tête."""
    c = db.connect(":memory:")
    c.execute("INSERT INTO pdl VALUES(0,'061961403012',1,9)")
    c.execute("INSERT INTO measurements(ts,pdl_index,papp,sent) VALUES(1,7,10,0)")
    c.commit()
    assert m.a_nettoyer(c) == 0, "on toucherait aux services pour une anomalie irreparable"
    assert m.conforme(c), "mais l'invariant doit quand meme le SIGNALER"
    # ⚖️ LE TÉMOIN : un vrai fantôme, lui, doit bien ouvrir la porte.
    assert m.a_nettoyer(malade()) > 0


@cas
def le_ROLLUP_est_reconstruit_pour_les_mesures_deplacees():
    """⚠️ Les tranches du fantôme sont supprimées et ses mesures déplacées — mais rien ne
    recalcule le rollup du vrai PDL, et `rollup_backfill_step` s'arrête pour de bon une
    fois `rollup_state.done=1`. Sans reconstruction, chaque période enregistrée sous un
    fantôme resterait un TROU dans `/curve` large, les bandes HC/HP, le coût et l'index par
    tarif — alors que les points bruts sont bien là."""
    c = db.connect(":memory:")
    c.execute("INSERT INTO pdl VALUES(0,'061961403012',1,9)")
    c.execute("INSERT INTO pdl VALUES(1,'061961403p12',1,1)")
    c.execute("INSERT INTO measurements(ts,pdl_index,papp,index_id,index_value,sent) "
              "VALUES(1000,1,250,1,777,1)")
    c.execute("INSERT INTO rollup_state(id,watermark,done) VALUES(0,0,1)")   # backfill TERMINÉ
    c.commit()
    m.menage(c, a_blanc=False)

    tranches = lignes(c.execute("SELECT pdl_index, papp_max, papp_count, index_last "
                                "FROM curve_rollup"))
    assert tranches == [(0, 250, 1, 777)], f"rollup non reconstruit : {tranches}"


@cas
def la_marche_a_blanc_annonce_la_MEME_cible_que_l_execution():
    """🚨 Le journal est TOUT ce qu'on relira d'un boîtier injoignable. En marche à blanc
    rien n'est encore supprimé : sans filtrer les époques vouées à la purge, la « dernière
    restante » serait l'ABÎMÉE elle-même, et le journal annoncerait `'HCn.' -> 'HCn.'` là
    où l'exécution fait `'HCn.' -> 'HC..'`. Annoncer autre chose que ce qu'on fait est pire
    que ne rien annoncer."""
    def _base():
        c = db.connect(":memory:")
        c.execute("INSERT INTO pdl VALUES(0,'061961403012',1,9)")
        for ts, ngtf in ((0, "HC.."), (10, "HCn.")):
            c.execute("INSERT INTO contract_epoch VALUES(0,?,?)", (ts, ngtf))
        c.execute("INSERT INTO level_profile(pdl_index,computed_ts,ngtf) VALUES(0,0,'HCn.')")
        c.commit()
        return c
    assert m.menage(_base(), a_blanc=True)["ngtf_recale"] == \
           m.menage(_base(), a_blanc=False)["ngtf_recale"] == (0, "HCn.", "HC..")


@cas
def la_PORTE_refuse_exactement_comme_le_MENAGE():
    """🚨 Deux définitions du refus = le défaut qu'elles produisent. La porte disait « il y a
    du travail », `menage()` refusait — et entre les deux on avait arrêté `ben-tic-reader`,
    `ben-telemetry` et `ben-publisher` puis écrit une sauvegarde, pour que pas une ligne ne
    change. Des mesures perdues pour rien, la leçon de 0.9.17 une fois de plus."""
    for nb_sains, libelle in ((2, "deux vrais compteurs"), (0, "aucun vrai compteur")):
        c = db.connect(":memory:")
        for i, adco in enumerate(["061961403012", "031864467282"][:nb_sains]):
            c.execute("INSERT INTO pdl VALUES(?,?,1,9)", (i, adco))
        c.execute("INSERT INTO pdl VALUES(9,'061961403p12',1,1)")
        c.execute("INSERT INTO measurements(ts,pdl_index,papp,sent) VALUES(1,9,10,1)")
        c.commit()
        assert m.refus(c), libelle
        assert m.a_nettoyer(c) == 0, f"{libelle} : on reveillerait le boitier pour lui dire non"
    # ⚖️ LE TÉMOIN : avec UN seul vrai compteur, la porte s'ouvre bien.
    assert m.refus(malade()) is None and m.a_nettoyer(malade()) > 0


@cas
def le_retour_arriere_rend_AUSSI_le_rollup_du_vrai_PDL():
    """🚨 La reconstruction du rollup écrit les points du fantôme dans les tranches du VRAI
    compteur. Ne pas les sauvegarder laisserait ces points comptés DEUX FOIS après un retour
    arrière — une fois sous le fantôme restauré, une fois dans le rollup du vrai PDL — dans
    /curve large, les bandes HC/HP et le coût. Et `rollup_state.done=1` : plus rien ne les
    recalculerait jamais.

    Les deux formes comptent : une tranche qui EXISTAIT se remet telle quelle, une tranche
    que la ré-ingestion CRÉE doit se supprimer."""
    import tempfile
    c = malade()
    avant = lignes(c.execute("SELECT pdl_index,bucket_ts,papp_sum,papp_count FROM curve_rollup"))
    assert avant, "le banc doit partir d'une tranche existante, sinon il ne teste qu'un cas"
    f = tempfile.NamedTemporaryFile("w+", suffix=".sql", delete=False)
    f.close()
    m.sauvegarde(c, f.name)
    m.menage(c, a_blanc=False)
    pendant = lignes(c.execute("SELECT pdl_index,bucket_ts,papp_sum,papp_count FROM curve_rollup"))
    assert pendant != avant, "le rollup du vrai PDL aurait du changer"
    c.executescript(pathlib.Path(f.name).read_text())
    assert lignes(c.execute("SELECT pdl_index,bucket_ts,papp_sum,papp_count FROM curve_rollup")) == avant


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
