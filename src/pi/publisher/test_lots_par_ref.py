"""Banc de `fetch_batch` — le lot part GROUPÉ PAR COMPTEUR, et `pdl_index` n'en sort pas.

🚨 CE BANC GARDE L'INVARIANT QUI TIENT TOUT LE PLAN DE BASCULE : `sent=1` est posé
   PAR ROWID, et seulement pour des points RÉELLEMENT PARTIS. Un `rowid` qui se
   glisserait dans la liste sans que son point soit dans un lot ferait marquer
   `sent=1` sur une mesure que personne n'a reçue — perte SILENCIEUSE et DÉFINITIVE,
   puisque `sent=1` ne se défait pas.

⭐ Écrit AVANT le code, et vérifié ROUGE avant de l'écrire.

Lancer : python3 -m pytest test_lots_par_ref.py -q    (ou pytest tout court)
"""

import pathlib
import sys
import time

R = pathlib.Path(__file__).resolve().parent
sys.path[:0] = [str(R.parent / "store"), str(R.parent), str(R)]

import ben_publisher as pub  # noqa: E402
import db  # noqa: E402


def _base(pdls):
    """Une base en mémoire avec les pdl donnés : [(index, adco, ref ou None), …]."""
    c = db.connect(":memory:")
    now = int(time.time())
    for i, adco, ref in pdls:
        c.execute("INSERT INTO pdl (pdl_index, adco, first_seen, last_seen, ref) "
                  "VALUES (?,?,?,?,?)", (i, adco, now, now, ref))
    c.commit()
    return c


def _mesure(c, pdl_index, ts, papp=None, iinst=None):
    c.execute("INSERT INTO measurements (ts, pdl_index, papp, iinst, sent) "
              "VALUES (?,?,?,?,0)", (ts, pdl_index, papp, iinst))
    c.commit()


# ── LE CAS DÉCISIF ────────────────────────────────────────────────────────────

def un_pdl_SANS_ref_ne_part_pas_et_son_rowid_n_est_PAS_marque():
    """🚨 Le défaut qu'on ne pourrait pas rattraper.

    Un compteur sans `ref` n'est pas publiable : le cloud refuserait le lot. Ses
    points doivent donc rester `sent=0` et repartir dès que la `ref` arrive.

    ⚠️ Si son `rowid` entrait quand même dans la liste rendue, `mark_sent` le
       passerait à 1 après le 2xx du lot des AUTRES compteurs — et la mesure serait
       perdue pour toujours, sans trace.
    """
    c = _base([(0, "031864467282", "R-zero"), (1, "042164377549", None)])
    _mesure(c, 0, 1790000000, papp=430)
    _mesure(c, 1, 1790000001, papp=120)      # celui-là n'a pas de ref

    rowids, lots = pub.fetch_batch(c, 100)

    refs = [l["ref"] for l in lots]
    assert refs == ["R-zero"], f"seul le compteur avec ref doit partir, obtenu {refs}"
    assert len(rowids) == 1, (
        f"{len(rowids)} rowid(s) marqués pour 1 point parti — "
        "un point non envoyé serait marqué sent=1 et perdu")

    # Et la preuve par le bout de la chaîne : le point sans ref reste à envoyer.
    pub.mark_sent(c, rowids)
    # ⚠️ `db.connect` pose `row_factory = sqlite3.Row` : on compare les VALEURS, pas
    #    les lignes — sinon le banc échoue sur la représentation et non sur le fond.
    restant = [r[0] for r in
               c.execute("SELECT pdl_index FROM measurements WHERE sent = 0")]
    assert restant == [1], f"le point sans ref doit rester à envoyer, obtenu {restant}"


def aucun_pdl_n_a_de_ref_donc_rien_ne_part():
    """Au premier démarrage, aucune ref n'est connue : le lot est VIDE, et aucun
    rowid n'est marqué. ⭐ Ce n'est pas une panne, c'est une attente — la
    déclaration apportera les refs au tour suivant."""
    c = _base([(0, "031864467282", None)])
    _mesure(c, 0, 1790000000, papp=430)

    rowids, lots = pub.fetch_batch(c, 100)
    assert lots == [], f"rien ne doit partir, obtenu {lots}"
    assert rowids == [], f"aucun rowid ne doit être marqué, obtenu {rowids}"


# ── LE GROUPAGE ───────────────────────────────────────────────────────────────

def deux_compteurs_font_deux_lots():
    """⭐ Une `ref` par LOT, jamais par point : un uuid répété 1000 fois coûte 36 ko,
    un par compteur coûte 36 octets. Et le multi-compteurs n'est pas un cas
    particulier — c'est la forme normale du payload."""
    c = _base([(0, "031864467282", "R-zero"), (1, "042164377549", "R-un")])
    _mesure(c, 0, 1790000000, papp=430)
    _mesure(c, 1, 1790000001, papp=120)
    _mesure(c, 0, 1790000002, papp=440)

    rowids, lots = pub.fetch_batch(c, 100)

    assert len(lots) == 2, f"2 lots attendus, obtenu {len(lots)}"
    par_ref = {l["ref"]: l["points"] for l in lots}
    assert len(par_ref["R-zero"]) == 2, "les deux points de pdl 0 doivent être ensemble"
    assert len(par_ref["R-un"]) == 1
    assert len(rowids) == 3, "les trois points partent, donc trois rowid"


def le_point_ne_porte_PLUS_son_index_local():
    """🚨 `pdl_index` ne sort pas du boîtier dans un lot de mesures — c'est
    littéralement l'énoncé du chantier. Le laisser serait reconstruire le clavage
    par index local dans le payload le plus volumineux du protocole."""
    c = _base([(0, "031864467282", "R-zero")])
    _mesure(c, 0, 1790000000, papp=430)

    _, lots = pub.fetch_batch(c, 100)
    p = lots[0]["points"][0]
    assert "pdl" not in p, f"le point porte encore un index local : {p}"
    assert "pdl_index" not in p, f"le point porte encore un index local : {p}"
    assert p["ts"] == 1790000000 and p["papp"] == 430, p


def un_champ_absent_reste_ABSENT():
    """« Absent » n'est pas « zéro » : 0 W est une vraie valeur. Un champ nul ne
    doit pas voyager, et surtout pas voyager en `null`."""
    c = _base([(0, "031864467282", "R-zero")])
    _mesure(c, 0, 1790000000, papp=430)      # iinst reste NULL

    _, lots = pub.fetch_batch(c, 100)
    p = lots[0]["points"][0]
    assert "iinst" not in p, f"un champ NULL ne doit pas voyager : {p}"


def la_limite_borne_les_LIGNES_lues_pas_les_lots():
    """⚠️ La limite protège la mémoire du Pi Zero et la taille du corps HTTP : elle
    porte donc sur le nombre de POINTS, pas sur le nombre de lots. Deux compteurs
    ne doivent pas doubler le lot."""
    c = _base([(0, "031864467282", "R-zero"), (1, "042164377549", "R-un")])
    for k in range(10):
        _mesure(c, k % 2, 1790000000 + k, papp=100 + k)

    rowids, lots = pub.fetch_batch(c, 4)
    total = sum(len(l["points"]) for l in lots)
    assert total == 4, f"4 points attendus, obtenu {total}"
    assert len(rowids) == 4


# ── Exécution directe, sans pytest (les boîtiers n'en ont pas) ────────────────

if __name__ == "__main__":
    cas = [v for k, v in sorted(globals().items())
           if callable(v) and not k.startswith("_") and k[0].islower()
           and v.__module__ == "__main__"]
    echecs = 0
    for f in cas:
        try:
            f()
            print(f"  ok   {f.__name__}")
        except AssertionError as e:
            echecs += 1
            print(f"  ÉCHEC {f.__name__}\n        {e}")
        except Exception as e:  # noqa: BLE001
            echecs += 1
            print(f"  ERREUR {f.__name__}\n        {type(e).__name__}: {e}")
    print(f"\n{len(cas) - echecs}/{len(cas)} cas verts")
    sys.exit(1 if echecs else 0)
