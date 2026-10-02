"""La parité attrape-t-elle ce que le checksum laisse passer ?

═══ POURQUOI CE BANC EXISTE ═══════════════════════════════════════════════════

ben-0004 déclare QUATRE PDL alors qu'il ne lit qu'un compteur :

    0619…012      p619…012      0619…p12      0619…0q2

Ce ne sont pas quatre compteurs : c'est le même, corrompu d'un caractère, et à
chaque fois sur le BIT 6 (`'0'`=0x30 → `'p'`=0x70).

🚨 Le checksum TIC vaut `(somme & 0x3F) + 0x20` : il ne voit la somme que MODULO
   64. Basculer le bit 6 ajoute exactement 64, et le masque jette la retenue.
   L'erreur lui est INVISIBLE PAR CONSTRUCTION — ce n'est pas un défaut de la
   spec (le checksum doit tenir dans un caractère imprimable, donc 6 bits),
   c'est une limite connue.

⭐ Le bit de parité, lui, la voit. Il arrivait jusqu'au lecteur, qui l'effaçait.

⚠️ Ce banc n'a besoin d'AUCUN boîtier — c'est le principal argument du contrôle
   logiciel sur le contrôle matériel. Le test sur ben-0003 a montré que
   `pyserial` n'active même pas `INPCK` : passer le port en 7E1 n'aurait rien
   changé, tout en donnant l'air d'un correctif.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import tic_parite as m  # noqa: E402

CAS = []


def cas(fn):
    CAS.append(fn)
    return fn


def trame(c: str) -> int:
    """Le caractère tel qu'il CIRCULE : 7 bits de donnée + parité paire."""
    d = ord(c) & 0x7F
    return d | ((bin(d).count("1") & 1) << 7)


LIGNE = "ADCO 061947000000"


@cas
def un_caractere_sain_passe():
    """⚖️ LE TÉMOIN. Sans lui, un contrôle qui rejetterait TOUT satisferait
    tous les autres cas — et le lecteur ne lirait plus rien, en silence."""
    for c in LIGNE:
        assert m.octet_valide(trame(c)), f"{c!r} sain est rejeté"


@cas
def le_bit_6_est_attrape_alors_que_le_checksum_est_AVEUGLE():
    """🚨 LE CAS DE ben-0004. On vérifie les DEUX faces — sans la première, la
    seconde ne prouverait rien : il faut établir que le checksum est bien
    aveugle avant de se féliciter que la parité voie."""

    def chk(s):
        return chr((sum(ord(x) for x in s) & 0x3F) + 0x20)

    testes = 0
    for i, c in enumerate(LIGNE):
        if not c.isdigit():
            continue
        corrompu = chr(ord(c) | 0x40)                 # '0' → 'p', '1' → 'q'
        faux = LIGNE[:i] + corrompu + LIGNE[i + 1:]

        # ⚖️ face 1 — le checksum ne voit RIEN
        assert chk(faux) == chk(LIGNE), \
            f"témoin faux : le checksum voit déjà {c!r} → {corrompu!r}"

        # face 2 — la parité, elle, voit. Le bit de donnée a changé ; le bit de
        # parité qui l'accompagnait, non.
        octet = (ord(corrompu) & 0x7F) | ((bin(ord(c) & 0x7F).count("1") & 1) << 7)
        assert not m.octet_valide(octet), \
            f"basculement du bit 6 sur {c!r} non détecté par la parité"
        testes += 1

    assert testes >= 10, f"trop peu de caractères éprouvés ({testes})"


@cas
def TOUT_basculement_d_un_seul_bit_est_attrape():
    """Pas seulement le bit 6 : la parité couvre les sept bits de donnée, plus
    le bit de parité lui-même."""
    for c in LIGNE:
        sain = trame(c)
        for bit in range(8):
            assert not m.octet_valide(sain ^ (1 << bit)), \
                f"bit {bit} basculé sur {c!r} non détecté"


@cas
def DEUX_basculements_peuvent_passer_et_c_est_ASSUMÉ():
    """⚠️ La parité ne rend pas les erreurs impossibles. Deux bits qui basculent
    dans le MÊME caractère s'annulent dans le compte. Ce cas existe, il est
    rare, et il est documenté — pas caché."""
    sain = trame("A")
    passe = sum(1 for a in range(8) for b in range(a + 1, 8)
                if m.octet_valide(sain ^ (1 << a) ^ (1 << b)))
    assert passe == 28, f"attendu 28 combinaisons invisibles, obtenu {passe}"


@cas
def la_table_et_le_calcul_direct_disent_la_meme_chose():
    """La table de 256 entrées est une optimisation (2,24 µs contre 5,82 sur Pi
    Zero). Une optimisation qui change le résultat n'en est pas une."""
    for i in range(256):
        assert m.PARITE[i] == (bin(i).count("1") & 1), f"table fausse en {i}"


@cas
def L_ALPHABET_refuse_exactement_ce_que_la_norme_interdit():
    """NOI-CPT_54E §6.2.1.2 : le champ donnée ne porte que des ASCII imprimables
    0x20-0x7E. Les cas listés par l'issue, un par un — et le TÉMOIN, sans lequel
    un prédicat qui refuserait TOUT passerait tous les refus ci-dessous."""
    for octet, nom in ((0x00, "NUL"), (0x04, "EOT"), (0x1F, "US"),
                       (0x7F, "DEL"), (0x80, "0x80 après masque")):
        assert not m.octet_dans_alphabet(octet), f"{nom} accepté en historique"
        assert not m.octet_dans_alphabet_std(octet), f"{nom} accepté en standard"

    # ⚖️ LE TÉMOIN : tout l'alphabet légal passe, dans les DEUX modes.
    for octet in range(0x20, 0x7F):
        assert m.octet_dans_alphabet(octet), f"{octet:#04x} refusé en historique"
        assert m.octet_dans_alphabet_std(octet), f"{octet:#04x} refusé en standard"


@cas
def HT_est_legal_en_STANDARD_et_refuse_en_HISTORIQUE():
    """🚨 ET CE N'EST PAS UNE SUBTILITÉ DE PURISTE. En standard `HT` sépare les
    champs (§5.3.6) : le refuser condamnerait TOUS les groupes et rendrait le
    lecteur muet. En historique le séparateur est l'espace, et accepter `HT`
    « pour simplifier » rouvrirait le trou que ce contrôle ferme — `'I'` vaut
    0x49, `HT` vaut 0x09, soit exactement 64 d'écart, donc MÊME CHECKSUM."""
    assert m.octet_dans_alphabet_std(m.HT), "HT refusé en standard : lecteur muet"
    assert not m.octet_dans_alphabet(m.HT), "HT accepté en historique : trou rouvert"
    assert (ord("I") - m.HT) == 64, \
        "la démonstration de l'écart de 64 ne tient plus — revoir le commentaire"


@cas
def le_bit_de_parite_ne_trouble_PAS_le_jugement_d_alphabet():
    """Les deux contrôles sont INDÉPENDANTS : l'alphabet juge les sept bits de
    donnée, la parité juge le huitième. `'A'` reste `'A'` que son bit de parité
    soit posé ou non — sinon tout caractère de parité impaire serait refusé deux
    fois, et le relevé imputerait à l'alphabet ce qui revient à la parité."""
    for c in "AZ0 9~":
        assert m.octet_dans_alphabet(trame(c)), f"{c!r} refusé avec son bit de parité"


@cas
def les_TABLES_d_alphabet_disent_la_meme_chose_que_les_PREDICATS():
    """Les tables sont une optimisation (2,97 µs contre 10,73 sur Pi Zero W, mesuré).
    Une optimisation qui change le résultat n'en est pas une — et ici elle ferait dire
    deux choses différentes au même contrôle selon l'appelant."""
    for i in range(128):
        assert bool(m.ALPHABET_HISTO[i]) == m.octet_dans_alphabet(i), f"histo diverge en {i}"
        assert bool(m.ALPHABET_STD[i]) == m.octet_dans_alphabet_std(i), f"std diverge en {i}"
    assert len(m.ALPHABET_HISTO) == len(m.ALPHABET_STD) == 128, \
        "128 entrées : read_frame indexe avec l'octet DÉJÀ masqué"


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
