#!/usr/bin/env python3
"""Banc de l'INVARIANT DU TAMPON PARTAGÉ — issue #30.

🚨 `sendBootFrame` et `curveStart` écrivent TOUS DEUX dans `curveBuf`, dès l'offset 0, et
font TOUS DEUX `msg_count++`. Or l'en-tête écrit à l'offset 0 EST LE NONCE ChaCha20.

⇒ Une trame de boot émise pendant qu'un lot s'accumule écrase son en-tête, et le flush
  suivant scelle la courbe avec l'en-tête du BOOT : **même nonce qu'une trame déjà partie**.
  Qui capte les deux obtient le XOR des deux clairs, et la trame de boot est très prévisible.

⭐ CE BANC NE TESTE PAS UN COMPORTEMENT, IL TESTE UNE STRUCTURE — et c'est voulu. Le défaut
ne produit ni erreur, ni journal, ni compteur : il faut qu'ISOUSC/PREF/CONTRAT change EN COURS
DE LOT, ce qu'aucun banc fonctionnel ne provoque sur commande. Un cas structurel, lui, échoue
dès que quelqu'un rouvre le chemin — y compris en ajoutant un QUATRIÈME appel plus tard.

ⓘ Il lit la source, il ne compile rien : ni AVR ni matériel, et la CI le ramasse.

Lancement :  python3 test_buffer_partage.py
"""
import pathlib
import re
import sys

INO = pathlib.Path(__file__).with_name("tic-reader.ino")
LIGNES = INO.read_text(encoding="utf-8").split("\n")
SRC = "\n".join(LIGNES)

CAS = []


def cas(f):
    CAS.append(f)
    return f


def _appels_sendBootFrame():
    """Les numéros de ligne (1-indexés) des APPELS à sendBootFrame, pas sa définition."""
    return [i + 1 for i, l in enumerate(LIGNES)
            if "sendBootFrame(" in l and not l.lstrip().startswith(("bool ", "//", "*"))]


@cas
def le_tampon_est_bien_PARTAGE_sinon_ce_banc_n_a_plus_d_objet():
    """⚖️ Le témoin de tous les autres cas. Si un jour la trame de boot reçoit son propre
    tampon, l'invariant disparaît — et ce banc doit alors être retiré, pas contourné."""
    assert re.search(r"uint8_t\*\s+buf\s*=\s*curveBuf\s*;", SRC), \
        "sendBootFrame n'écrit plus dans curveBuf : l'invariant a changé, revoir ce banc"
    assert re.search(r"writeHeader\(curveBuf,\s*TYPE_CURVE\)", SRC), \
        "curveStart n'écrit plus son en-tête dans curveBuf : revoir ce banc"
    # ⚠️ On compte les OCCURRENCES dans le CODE, commentaires retirés. Deux versions
    #    précédentes de ce banc se sont trompées ici : la première comptait les `msg_count++`
    #    cités dans les commentaires (ce fichier en parle beaucoup), la seconde comptait les
    #    LIGNES — et laissait donc passer deux incréments écrits sur la même ligne.
    code = "\n".join(l.split("//")[0] for l in LIGNES)
    n = len(re.findall(r"msg_count\s*\+\+", code))
    assert n == 2, (
        f"{n} incréments de msg_count dans le code au lieu de 2 — chaque trame scellée doit "
        f"consommer UN nonce, et un seul : un de plus et deux trames partagent le leur")


@cas
def CHAQUE_appel_a_sendBootFrame_traite_le_lot_en_cours():
    """🚨 LE CŒUR DU BANC. Trois appels existent ; il en suffit d'UN sans garde pour rouvrir
    la réutilisation de nonce. Chacun doit être précédé, dans les lignes qui l'introduisent,
    soit d'un flush du lot, soit de son rejet explicite.

    ⚠️ Et un quatrième appel ajouté plus tard sans garde fera tomber ce cas — c'est tout
       l'intérêt de compter les appels plutôt que de vérifier trois endroits connus.
    """
    appels = _appels_sendBootFrame()
    assert len(appels) == 3, (
        f"{len(appels)} appels à sendBootFrame au lieu de 3 — si c'en est un NOUVEAU, il doit "
        f"traiter le lot en cours comme les autres")
    FENETRE = 30          # lignes de contexte amont : commentaires de ce fichier compris
    sans_garde = []
    for n in appels:
        amont = "\n".join(LIGNES[max(0, n - 1 - FENETRE):n - 1])
        flushe = re.search(r"curveActive\s*\)\s*curveFlush\(\)", amont)
        jete = re.search(r"curveActive\s*=\s*false\s*;\s*curveN\s*=\s*0\s*;", amont)
        if not (flushe or jete):
            sans_garde.append(n)
    assert not sans_garde, (
        f"appel(s) à sendBootFrame ligne(s) {sans_garde} sans flush ni rejet du lot en cours : "
        f"le lot serait écrasé et la courbe scellée avec le nonce du boot (#30)")


@cas
def le_rejet_du_lot_remet_BIEN_les_deux_marqueurs():
    """Jeter un lot demande `curveActive = false` ET `curveN = 0`. Oublier le second laisse
    un compte d'échantillons qui ne correspond plus à rien dans le tampon suivant."""
    for m in re.finditer(r"curveActive\s*=\s*false\s*;\s*curveN\s*=\s*0\s*;", SRC):
        pass
    n = len(re.findall(r"curveActive\s*=\s*false\s*;\s*curveN\s*=\s*0\s*;", SRC))
    assert n >= 2, (
        f"{n} rejet(s) de lot de la forme complète trouvé(s) : le chemin STREAMING (#30) et "
        f"le batch-horloge doivent tous deux remettre les DEUX marqueurs")
    # ⚠️ Un `curveActive = false` SEUL, hors de curveFlush, laisserait curveN en place.
    # ⚠️ Hors la DÉCLARATION (`static bool curveActive = false;`), qui n'est pas un rejet —
    #    une première version de ce banc la comptait comme telle.
    seuls = [i + 1 for i, l in enumerate(LIGNES)
             if re.search(r"curveActive\s*=\s*false\s*;\s*$", l.strip())
             and "curveN" not in l and not l.lstrip().startswith("static ")]
    # curveFlush a le droit : il vient d'émettre, curveN sera réécrit par le prochain curveStart.
    assert len(seuls) <= 1, (
        f"plusieurs `curveActive = false;` sans `curveN = 0` lignes {seuls} — seul curveFlush "
        f"peut se le permettre")


if __name__ == "__main__":
    ko = 0
    for f in CAS:
        try:
            f()
            print(f"  ok   {f.__name__}")
        except AssertionError as e:
            ko += 1
            print(f"  ÉCHEC {f.__name__}\n        {e}")
    print(f"\n{len(CAS) - ko}/{len(CAS)}")
    sys.exit(1 if ko else 0)
