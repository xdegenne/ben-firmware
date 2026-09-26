#!/usr/bin/env python3
"""
Banc de `read_frame` — le découpage des trames face aux erreurs de parité.

⭐ CE BANC N'A PU ÊTRE ÉCRIT QU'APRÈS avoir rendu `main_uart` importable. Avant,
l'ouvrir démarrait le lecteur : LED, port série, watchdog. La boucle la plus
délicate du fichier n'était donc éprouvable que sur un vrai compteur.

Tourne sur n'importe quelle machine : le port est une liste d'octets, ce qui
permet d'injecter exactement la corruption qu'on veut — y compris celle qu'on ne
saurait pas provoquer à la main sur un Linky.

Lancement :  python3 test_read_frame.py
"""
import sys
from unittest.mock import MagicMock

# Le Pi seul a ces modules ; rien de ce qu'on teste ici ne les touche.
for _n in ("RPi", "RPi.GPIO", "serial", "db", "settings"):
    sys.modules.setdefault(_n, MagicMock())
sys.modules["RPi"].GPIO = sys.modules["RPi.GPIO"]

import main_uart as m  # noqa: E402
from tic_parite import PARITE  # noqa: E402

STX, ETX, LF, CR = m.STX, m.ETX, m.LF, m.CR


def sain(b: int) -> int:
    """L'octet tel qu'il arrive sur un port 8N1 depuis une ligne 7E1."""
    return b | (PARITE[b & 0x7F] << 7)


def corrompu(b: int) -> int:
    """Le même, bit de parité inversé — la corruption que le checksum ne voit pas."""
    return sain(b) ^ 0x80


class FauxPort:
    """Le strict minimum de `serial.Serial` qu'utilise `read_frame`."""

    def __init__(self, octets):
        self._o = list(octets)

    def read(self, n=1):
        return bytes([self._o.pop(0)]) if self._o else b""


def ligne(texte: str) -> list[int]:
    return [sain(LF)] + [sain(ord(c)) for c in texte] + [sain(CR)]


# Les deux fonctions injectées : on accepte tout et on range bêtement, pour que
# le test porte sur le DÉCOUPAGE et rien d'autre.
def tout_bon(_l):
    return True


def range_brut(l, labels):
    labels[l.split()[0]] = l.split()[1]


CAS = []


def cas(f):
    CAS.append(f)
    return f


@cas
def une_trame_saine_est_lue_entierement():
    """⚖️ Le témoin de tous les autres : sans lui, « X absent » passerait aussi
    avec un `read_frame` qui ne rendrait jamais rien."""
    flux = [sain(STX)] + ligne("ADCO 021861000000 X") + ligne("PAPP 00450 X") + [sain(ETX)]
    labels = m.read_frame(FauxPort(flux), tout_bon, range_brut)
    assert labels == {"ADCO": "021861000000", "PAPP": "00450"}, f"trame mal lue : {labels}"


@cas
def un_ETX_corrompu_ne_fond_pas_deux_trames():
    """
    Un ETX dont seul le bit de parité est inversé était jeté comme une donnée :
    la lecture continuait dans la trame SUIVANTE et rendait un dict mêlant les
    deux.

    ⓘ Sans gravité en soi — les trames viennent du même compteur et se répètent,
    donc on perd surtout UNE TRAME. Ce qui devient faux, ce sont les compteurs
    rendus : ils porteraient sur deux trames en disant une.
    """
    flux = ([sain(STX)] + ligne("PREMIERE 111 X") + [corrompu(ETX)]
            + ligne("SECONDE 222 X") + [sain(ETX)])
    labels = m.read_frame(FauxPort(flux), tout_bon, range_brut)

    assert labels is not None, "trame perdue alors qu'une ligne était valide"
    assert "PREMIERE" in labels, "la ligne d'avant l'ETX corrompu a disparu"
    assert "SECONDE" not in labels, "FUSION : deux trames dans le même dict"

    # ⚠️ CE TEST NE PROUVE PAS LE CÂBLAGE DE LA PARITÉ, et autant l'écrire :
    #    débranché, l'ETX corrompu retombe sur le masque `& 0x7F` et ferme quand
    #    même la trame — comportement d'avant la PR. Vérifié par sabotage le
    #    26/09 : il reste vert. Ceux qui prouvent le branchement sont
    #    `un_caractere_corrompu_ne_coute_QUE_SA_LIGNE` et `le_releve_dit_POURQUOI`.


@cas
def un_faux_STX_de_parite_fausse_ne_demarre_pas_une_trame():
    """La synchronisation ne regardait que `& 0x7F` : un octet de parité fausse
    dont les 7 bits de poids faible valent 0x02 était pris pour un début de
    trame, et la vraie trame était lue à partir du mauvais endroit."""
    flux = ([corrompu(STX)] + ligne("AVANT 000 X")
            + [sain(STX)] + ligne("APRES 444 X") + [sain(ETX)])
    labels = m.read_frame(FauxPort(flux), tout_bon, range_brut)

    assert labels is not None, "trame perdue"
    assert "AVANT" not in labels, "le faux STX a été accepté : lecture démarrée trop tôt"
    assert "APRES" in labels, "la vraie trame n'a pas été lue"


@cas
def un_caractere_corrompu_ne_coute_QUE_SA_LIGNE():
    """
    ⭐ La granularité de la perte, et c'est le protocole TIC qui la permet :
    chaque ligne porte SON checksum, donc une ligne corrompue n'empoisonne pas
    le groupe.
    """
    # Le checksum injecté refuse toute ligne amputée — ce que fait le vrai.
    attendues = {"PREMIERE 111 X", "TROISIEME 333 X"}

    def checksum_strict(l):
        return l in attendues or l == "DEUXIEME 222 X"

    flux = [sain(STX)] + ligne("PREMIERE 111 X")
    abimee = ligne("DEUXIEME 222 X")
    abimee[3] = corrompu(abimee[3] & 0x7F)      # un caractère de la 2ᵉ ligne
    flux += abimee + ligne("TROISIEME 333 X") + [sain(ETX)]

    labels = m.read_frame(FauxPort(flux), checksum_strict, range_brut)

    assert "PREMIERE" in labels and "TROISIEME" in labels, (
        f"les lignes saines ont été perdues : {labels}")
    assert "DEUXIEME" not in labels, "la ligne amputée a été gardée"


@cas
def le_releve_dit_POURQUOI_une_etiquette_manque():
    """
    🚨 Les messages disaient « (checksum KO?) » — ils DEVINAIENT. Le relevé
    distingue désormais les trois causes, dont celle qui a coûté un après-midi
    à l'oscilloscope : l'étiquette que le compteur n'émet tout simplement pas.
    """
    flux = [sain(STX)] + ligne("ADCO 021861000000 X") + [sain(ETX)]
    m.read_frame(FauxPort(flux), tout_bon, range_brut)
    assert "n'est pas émise" in m._cause_rejets(), (
        f"aucun rejet, la cause devrait pointer le compteur : {m._cause_rejets()!r}")

    flux = ([sain(STX)] + ligne("ADCO 021861000000 X")
            + [corrompu(ord("A"))] + [sain(ETX)])
    m.read_frame(FauxPort(flux), tout_bon, range_brut)
    assert "parité" in m._cause_rejets(), (
        f"un caractère rejeté sur parité n'est pas rapporté : {m._cause_rejets()!r}")


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
