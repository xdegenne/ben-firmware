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
    #    `un_octet_corrompu_ne_coute_QUE_SON_GROUPE` et `le_releve_dit_POURQUOI`.


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
def un_octet_corrompu_ne_coute_QUE_SON_GROUPE():
    """
    ⭐ La granularité de la perte, et c'est le protocole TIC qui la permet :
    chaque ligne porte SON checksum, donc une ligne corrompue n'empoisonne pas
    le groupe.
    """
    # ⚠️ Ce commentaire disait « le checksum injecté refuse toute ligne amputée —
    #    ce que fait le vrai ». C'ÉTAIT FAUX, et le cas suivant le prouve : le vrai
    #    checksum ne voit la somme que modulo 64, donc il accepte certaines lignes
    #    amputées. Ici on injecte un checksum PARFAIT — plus strict que le vrai —
    #    pour que ce cas ne porte que sur le découpage.
    attendues = {"PREMIERE 111 X", "TROISIEME 333 X"}

    def checksum_parfait(l):
        return l in attendues or l == "DEUXIEME 222 X"

    flux = [sain(STX)] + ligne("PREMIERE 111 X")
    abimee = ligne("DEUXIEME 222 X")
    abimee[3] = corrompu(abimee[3] & 0x7F)      # un caractère de la 2ᵉ ligne
    flux += abimee + ligne("TROISIEME 333 X") + [sain(ETX)]

    labels = m.read_frame(FauxPort(flux), checksum_parfait, range_brut)

    assert "PREMIERE" in labels and "TROISIEME" in labels, (
        f"les lignes saines ont été perdues : {labels}")
    assert "DEUXIEME" not in labels, "la ligne amputée a été gardée"


@cas
def un_groupe_ampute_est_REJETE_meme_si_le_checksum_le_valide():
    """
    🚨 LE SEUL CAS DE TOUT CE CHANTIER QUI FABRIQUAIT UNE DONNÉE FAUSSE.

       Le code jetait le CARACTÈRE hors parité et gardait la ligne, en comptant
       sur le checksum pour rattraper l'amputation. Il ne peut pas : il vaut
       `(somme & 0x3F) + 0x20`, donc il est aveugle à tout retrait dont la somme
       est un multiple de 64.

    ⚖️ LE TÉMOIN EST DANS LE CAS LUI-MÊME, et c'est ce qui le rend concluant :
       on donne ici le VRAI checksum, celui du compteur. La ligne amputée et la
       ligne entière ont le MÊME — on l'affirme par assertion avant de mesurer le
       comportement du lecteur. Sans cette première assertion, un lecteur qui
       rejetterait la ligne pour n'importe quelle autre raison passerait le test.
    """
    def ck(corps):
        return chr((sum(map(ord, corps)) & 0x3F) + 0x20)

    entiere = "HCHC 001000000"          # un INDEX : la donnée la plus lourde
    amputee = "HCHC 00100"              # quatre '0' retirés → 4 × 0x30 = 192
    assert ck(entiere) == ck(amputee), (
        "prémisse fausse : le checksum distingue ces deux lignes, "
        "le cas ne prouverait plus rien")

    # ⭐ On injecte le VRAI validateur de production, pas une imitation : si
    #    `tic_checksum_ok` changeait de convention, ce cas le saurait.
    flux = [sain(STX), sain(LF)] + [sain(ord(c)) for c in amputee]
    flux += [corrompu(ord("0")) for _ in range(4)]          # les quatre perdus
    flux += [sain(ord(c)) for c in " " + ck(entiere)]        # le checksum ÉMIS
    flux += [sain(CR), sain(ETX)]

    labels = m.read_frame(FauxPort(flux), m.tic_checksum_ok, range_brut)

    assert not labels or "HCHC" not in labels, (
        f"index FAUX enregistré : {labels} — la ligne amputée a passé les deux "
        f"contrôles, exactement le défaut que ce cas existe pour interdire")
    assert m._derniere_trame["rejetees"] == 1, (
        f"la ligne n'est pas comptée comme rejetée : {m._derniere_trame} — "
        f"le relevé mentirait sur la santé de la liaison")


@cas
def le_releve_n_accuse_PAS_la_parite_pour_un_octet_hors_trame():
    """
    🚨 LE CAS DU TRIPHASÉ, celui qui a coûté un après-midi d'oscilloscope.

       Un compteur triphasé émet `IINST1/2/3` et JAMAIS `IINST`. La cause est
       « cette étiquette n'est pas émise », et c'est tout ce qu'on veut lire.

       Mais si un seul octet bruité traîne pendant l'attente du STX — et ces
       octets-là sont la QUEUE DE LA TRAME PRÉCÉDENTE — l'ancien relevé
       annonçait « 1 caractère rejeté sur parité ». Il accusait la liaison pour
       une trame dont aucune ligne n'avait été rejetée, remettant exactement le
       mauvais diagnostic que `_cause_rejets` existe pour tuer.

    ⚠️ Le compteur de BRUIT, lui, doit toujours voir cet octet : c'est bien une
       erreur de transmission. Les deux affirmations coexistent, et le cas
       vérifie les deux — sinon « ne pas accuser » se confondrait avec
       « ne pas compter ».
    """
    m._parite_cumul.update(car=0, trames=0, debut=0.0)
    flux = ([corrompu(ord("A"))]                      # queue de la trame d'avant
            + [sain(STX)] + ligne("ADCO 021861000000 X") + [sain(ETX)])
    labels = m.read_frame(FauxPort(flux), tout_bon, range_brut)

    assert labels == {"ADCO": "021861000000"}, f"trame mal lue : {labels}"
    assert "n'est pas émise" in m._cause_rejets("IINST"), (
        f"la parité est accusée alors qu'aucune ligne n'a été rejetée : "
        f"{m._cause_rejets('IINST')!r}")
    assert m._parite_cumul["car"] == 1, (
        f"l'octet n'est plus compté comme bruit : {m._parite_cumul} — "
        f"ne pas accuser ne veut pas dire ne pas mesurer")


@cas
def le_releve_dit_si_le_groupe_rejete_PORTAIT_l_etiquette_cherchee():
    """
    ⭐ Des lignes SONT tombées, mais aucune ne portait celle qu'on cherche. Dire
       « 2 lignes rejetées » laisse croire à un lien de cause à effet qui n'existe
       pas — c'est la même faute, un cran plus subtil.

    ⚖️ Deux moitiés opposées, et c'est ce qui rend le cas concluant : la même
       trame, la même étiquette manquante, mais selon que la ligne tombée la
       portait ou non, le message doit changer de camp.
    """
    def checksum_refuse_PTEC(l):
        return not l.startswith("PTEC")

    flux = ([sain(STX)] + ligne("ADCO 021861000000 X") + ligne("PTEC HP.. X")
            + [sain(ETX)])
    m.read_frame(FauxPort(flux), checksum_refuse_PTEC, range_brut)

    # On cherchait PTEC, et c'est bien PTEC qui est tombée.
    #
    # ⚠️ Assertion NÉGATIVE, et c'est ce qui la rend concluante. Vérifier que le
    #    message contient « rejetée(s) sur checksum » ne prouvait RIEN : la
    #    branche « aucune ne portait PTEC » contient la même chaîne, donc le test
    #    passait même en supprimant tout le relevé d'étiquettes. Mesuré par
    #    sabotage — il restait vert.
    cause = m._cause_rejets("PTEC")
    assert "aucun ne portait" not in cause, (
        f"PTEC est tombée, et le relevé prétend qu'aucune ligne ne la portait : "
        f"{cause!r}")
    assert "rejeté(s) sur checksum" in cause, f"cause muette : {cause!r}"

    # On cherchait IINST : une ligne est tombée, mais ce n'était pas elle.
    cause = m._cause_rejets("IINST")
    assert "aucun ne portait IINST" in cause, (
        f"une ligne sans rapport est imputée à IINST : {cause!r}")
    assert "probablement" in cause, (
        "l'indice est présenté comme une certitude : une étiquette corrompue "
        f"par la parité peut avoir un nom relevé faux — {cause!r}")


@cas
def une_liaison_si_bruyante_quelle_expire_est_QUAND_MEME_comptee():
    """
    🚨 Le cas que le compteur existe pour mesurer, et le seul où il se taisait.

       Les deux sorties sur expiration de délai rendaient `None` sans rien
       signaler. Or une liaison assez bruyante pour qu'AUCUNE trame n'aboutisse
       est précisément celle dont on veut connaître le bruit : sans ces appels,
       `parite_ko` était perdu à chaque tour, le résumé périodique restait muet,
       et le silence se lisait « tout va bien ».

    ⚠️ Deux sorties, donc deux moitiés : expirer en cherchant le STX, et expirer
       en lisant la trame. Une seule des deux corrigée laisserait un trou.
    """
    vrai_delai, m.TIC_TIMEOUT_S = m.TIC_TIMEOUT_S, 0.05
    try:
        # ── moitié 1 : on n'accroche jamais le STX ──────────────────────────
        m._parite_cumul.update(car=0, trames=0, debut=0.0)
        m.read_frame(FauxPort([corrompu(ord("A"))] * 3), tout_bon, range_brut)
        assert m._parite_cumul["car"] == 3, (
            f"expiration en synchronisation : {m._parite_cumul['car']} rejet(s) "
            f"compté(s) au lieu de 3 — la mesure de bruit sous-estime le réel")

        # ── moitié 2 : STX accroché, mais jamais d'ETX ──────────────────────
        m._parite_cumul.update(car=0, trames=0, debut=0.0)
        m.read_frame(FauxPort([sain(STX)] + [corrompu(ord("A"))] * 2),
                     tout_bon, range_brut)
        assert m._parite_cumul["car"] == 2, (
            f"expiration en lecture : {m._parite_cumul['car']} rejet(s) compté(s) "
            f"au lieu de 2")
        assert m._derniere_trame["parite"] == 2, (
            f"le relevé de la dernière trame est resté en arrière : "
            f"{m._derniere_trame} — `_cause_rejets()` accuserait le compteur")
    finally:
        m.TIC_TIMEOUT_S = vrai_delai


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

    # ⚠️ Un octet fautif ENTRE le CR et l'ETX n'est dans AUCUNE ligne : il n'a
    #    donc rien coûté à cette trame, et l'accuser serait le même mensonge.
    flux = ([sain(STX)] + ligne("ADCO 021861000000 X")
            + [corrompu(ord("A"))] + [sain(ETX)])
    m.read_frame(FauxPort(flux), tout_bon, range_brut)
    assert "n'est pas émise" in m._cause_rejets(), (
        f"un octet hors de toute ligne est imputé à tort : {m._cause_rejets()!r}")

    # Celui-ci, en revanche, tombe DANS la ligne : il la condamne, et il doit
    # être rapporté. ⚖️ C'est le témoin de l'assertion précédente — sans lui,
    # un `_cause_rejets` qui ne parlerait JAMAIS de parité la satisferait aussi.
    abimee = ligne("PAPP 00450 X")
    abimee[7] = corrompu(abimee[7] & 0x7F)      # dans la VALEUR : le nom survit
    m.read_frame(FauxPort([sain(STX)] + abimee + [sain(ETX)]), tout_bon, range_brut)
    assert "hors parité" in m._cause_rejets("PAPP"), (
        f"une ligne condamnée par la parité n'est pas rapportée : "
        f"{m._cause_rejets('PAPP')!r}")

    # 🚨 ET LA LIMITE, dite plutôt que tue : si la parité mange un caractère du
    #    NOM, l'étiquette relevée est fausse et l'imputation ne peut plus
    #    trancher. Le message doit alors NOMMER ce doute — conclure « pas
    #    émise » serait se tromper dans l'autre sens, puisqu'elle l'était.
    abimee = ligne("PAPP 00450 X")
    abimee[3] = corrompu(abimee[3] & 0x7F)      # dans le NOM cette fois
    m.read_frame(FauxPort([sain(STX)] + abimee + [sain(ETX)]), tout_bon, range_brut)
    cause = m._cause_rejets("PAPP")
    assert "abîmer le nom" in cause, (
        f"le doute sur le nom relevé est passé sous silence : {cause!r}")
    assert "pas émise" not in cause, (
        f"conclusion fausse : l'étiquette ÉTAIT émise, elle a été abîmée — {cause!r}")


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
