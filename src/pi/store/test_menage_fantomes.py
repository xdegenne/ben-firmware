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
        c.execute("INSERT INTO measurements(ts,pdl_index,papp,sent) VALUES(?,?,?,1)", (100 + i, i, 42))
        c.execute("INSERT INTO level_profile(pdl_index,computed_ts) VALUES(?,0)", (i,))
    for t in range(1000):                       # le vrai compteur, qu'on ne doit PAS toucher
        c.execute("INSERT INTO measurements(ts,pdl_index,papp,sent) VALUES(?,0,?,1)", (t, t))
    for ts, ngtf in ((0, "HC.."), (10, "HCn."), (12, "HC.."), (20, "HCn."), (21, "HC..")):
        c.execute("INSERT INTO contract_epoch VALUES(0,?,?)", (ts, ngtf))
    c.commit()
    return c


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
    f = tempfile.NamedTemporaryFile("w+", suffix=".sql", delete=False)
    f.close()
    n = m.sauvegarde(c, f.name)
    assert n > 0, "sauvegarde vide alors qu'il y a de quoi sauver"
    sql = pathlib.Path(f.name).read_text()
    assert "p61961403012" in sql and "'HCn.'" in sql

    m.menage(c, a_blanc=False)
    assert m.conforme(c) == []
    # ⚖️ LE TÉMOIN de la sauvegarde : elle doit se REJOUER. Un fichier qu'on ne peut pas
    #    réinjecter n'est pas une sauvegarde, c'est un souvenir.
    c.executescript(sql)
    assert m.pdls_fantomes(c) == [1, 2, 3]


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
