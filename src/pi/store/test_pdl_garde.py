#!/usr/bin/env python3
"""Banc du garde de PDL — la porte par où sont entrés les fantômes.

Ce n'est pas un banc d'accesseurs : chaque cas correspond à une valeur qui a créé, ou
aurait pu créer, un PDL portant des mesures qui ne sont pas les siennes.

  · `resolve_pdl` est le SEUL `INSERT INTO pdl` du dépôt, et AUCUNE contrainte FOREIGN
    KEY ne relie les neuf tables portant `pdl_index` à la table `pdl`. Ce garde est donc
    le point unique de la CRÉATION — rien en aval ne rattrapera ce qu'il laisse passer ;
  · l'ancien garde ne testait que le VIDE. `.strip()` ne retire pas les octets NUL, donc
    `'\\x00\\x00'` le passait : deux octets de rien ont créé un PDL portant 13 056 mesures ;
  · ⚖️ le TÉMOIN est aussi important que les refus : sans lui, un garde qui refuserait
    TOUT passerait chacun des autres cas, et le boîtier cesserait de stocker en silence ;
  · `bind_emitter` doit refuser SANS TOUCHER la table `emitter` — sinon on n'aurait plus
    de fantôme dans `pdl` mais une liaison émetteur→compteur fausse, ce qui est pire :
    elle survit au nettoyage.

    python3 src/pi/store/test_pdl_garde.py
"""
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import db  # noqa: E402

CAS = []


def cas(fn):
    CAS.append(fn)
    return fn


def neuf():
    return db.connect(str(pathlib.Path(tempfile.mkdtemp()) / "measurements.db"))


def combien(conn):
    return conn.execute("SELECT count(*) FROM pdl").fetchone()[0]


# ── La forme, et rien que la forme ───────────────────────────────────────────

#  Chaque entrée dit POURQUOI elle est là : un banc dont les cas ne s'expliquent pas
#  finit par être « corrigé » en ajustant le prédicat jusqu'à ce qu'il passe.
REFUSES = [
    ("",                 "vide — le seul cas que l'ancien garde attrapait"),
    ("   ",              "blancs seuls : `.strip()` les mange, il reste le vide"),
    ("\x00\x00",         "LA valeur mesurée sur le parc : `.strip()` ne retire pas les NUL"),
    ("\x00" * 12,        "douze NUL : la bonne LONGUEUR, aucun chiffre"),
    ("06194700",         "huit chiffres — l'AMPUTATION QUI GARDE LE CHECKSUM : 4 zéros "
                         "retirés somment à 192, soit 0 modulo 64, donc le groupe raccourci "
                         "porte le même checksum et passe le contrôle TIC"),
    ("0619470000000",    "treize chiffres"),
    ("06194700000A",     "onze chiffres et une lettre — le défaut du bit 6 (#2)"),
    ("061947 00000",     "un séparateur au milieu"),
    ("06194700000\n",    "une fin de ligne collée : `.strip()` la retire, donc 11 chiffres"),
    ("²" * 12,           "⚖️ LE CAS QUI JUSTIFIE isascii() : douze caractères, et "
                         "`isdigit()` est VRAI pour les chiffres Unicode. Sans isascii() "
                         "celui-ci créerait un PDL que le CHECK du cloud refuserait"),
]

TEMOIN = "021861000000"   # forme valide ; matricule INVENTÉ — ce dépôt est PUBLIC, un ADCO identifie un foyer


@cas
def le_predicat_refuse_ce_qui_n_est_pas_un_adco():
    for valeur, pourquoi in REFUSES:
        assert not db.adco_valide(valeur.strip()), f"{valeur!r} accepté — {pourquoi}"


@cas
def le_predicat_accepte_un_adco():
    """⚖️ LE TÉMOIN. Sans lui, `return False` passerait tous les cas ci-dessus."""
    assert db.adco_valide(TEMOIN)


# ── Ce que le garde empêche vraiment : la création ───────────────────────────

@cas
def aucun_pdl_cree_pour_un_adco_non_conforme():
    conn = neuf()
    avant = combien(conn)
    for valeur, pourquoi in REFUSES:
        assert db.resolve_pdl(conn, valeur) is None, f"{valeur!r} a rendu un index — {pourquoi}"
    assert combien(conn) == avant, "la table `pdl` a bougé alors que tout était refusé"


@cas
def un_adco_conforme_cree_bien_son_pdl():
    """⚖️ Le témoin, côté magasin : le garde ne doit pas fermer la porte à tout le monde."""
    conn = neuf()
    assert db.resolve_pdl(conn, TEMOIN, graine=0) == 0
    assert combien(conn) == 1


@cas
def resoudre_deux_fois_ne_cree_pas_deux_lignes():
    conn = neuf()
    premier = db.resolve_pdl(conn, TEMOIN, graine=0)
    assert db.resolve_pdl(conn, TEMOIN, graine=0) == premier
    assert combien(conn) == 1


@cas
def les_blancs_autour_sont_toleres():
    """Un ADCO encadré d'espaces reste le MÊME compteur — sinon on fabriquerait un
    doublon à la première trame mal découpée."""
    conn = neuf()
    assert db.resolve_pdl(conn, f"  {TEMOIN} ", graine=0) == 0
    assert combien(conn) == 1


# ── La voie LoRa : refuser sans rien lier ────────────────────────────────────

@cas
def bind_emitter_refuse_sans_toucher_la_table_emitter():
    conn = neuf()
    assert db.bind_emitter(conn, 0x11, "\x00\x00") == (None, False)
    lignes = conn.execute("SELECT count(*) FROM emitter").fetchone()[0]
    assert lignes == 0, "l'émetteur a été lié à un compteur qui n'existe pas"
    assert combien(conn) == 0


@cas
def bind_emitter_lie_bien_un_adco_conforme():
    """⚖️ Le témoin de la voie LoRa."""
    conn = neuf()
    pdl, change = db.bind_emitter(conn, 0x11, TEMOIN, graine=0)
    assert (pdl, change) == (0, False)
    assert db.emitter_pdl(conn, 0x11) == 0


@cas
def un_refus_ne_defait_pas_une_liaison_deja_etablie():
    """🚨 Le cas qui coûterait le plus cher : un émetteur sain, une trame de boot abîmée.
    Le refus ne doit RIEN casser — sinon un octet perdu débrancherait un boîtier."""
    conn = neuf()
    db.bind_emitter(conn, 0x11, TEMOIN, graine=0)
    assert db.bind_emitter(conn, 0x11, "\x00\x00") == (None, False)
    assert db.emitter_pdl(conn, 0x11) == 0, "la liaison saine a été perdue"


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
