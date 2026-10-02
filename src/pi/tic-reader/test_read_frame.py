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
#
# 🚨 `db` N'EST PLUS UNE COQUILLE, et c'est délibéré : `garder_adco` appelle
#    `db.adco_valide()`, et un MagicMock rend un objet TOUJOURS VRAI. Le banc aurait
#    donc affiché vert avec un prédicat qui n'aurait rien refusé — le pire des relevés.
#    `db.py` n'importe que la bibliothèque standard, il tourne donc partout, et
#    `main_uart` pose lui-même `../store` dans le chemin avant de l'importer.
for _n in ("RPi", "RPi.GPIO", "serial", "settings"):
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
def un_groupe_perdu_par_son_CR_ou_son_LF_est_COMPTE_quand_meme():
    """
    🚨 Le dernier endroit où le relevé accusait encore le compteur à tort.

       Un groupe n'est évalué qu'à son CR. Si c'est le CR lui-même qui échoue à
       la parité, le LF suivant remet `groupe_douteux` à faux AVANT que le groupe
       soit compté : il disparaît sans laisser de trace dans `rejetees`. Et si
       c'est le LF qui échoue, `in_line` reste faux, donc les octets du groupe
       sont jetés un par un sans que rien ne l'enregistre.

       Dans les deux cas `_cause_rejets` voit `rejetees == 0`, prend la branche
       « aucun groupe rejeté » et annonce « cette étiquette n'est pas émise ».
       On accuse le compteur alors que la liaison est bruyante — exactement le
       mauvais diagnostic que cette PR existe pour supprimer.

    ⚖️ Le témoin est dans le cas : on vérifie d'abord que les groupes SAINS de la
       même trame sont bien lus. Sans ça, un lecteur qui ne rendrait plus rien du
       tout satisferait l'assertion principale.
    """
    for quoi, place in (("CR", -1), ("LF", 0)):
        groupe = ligne("PAPP 00450 X")
        groupe[place] = corrompu(groupe[place] & 0x7F)
        flux = ([sain(STX)] + ligne("ADCO 021861000000 X") + groupe
                + ligne("IINST 003 X") + [sain(ETX)])
        labels = m.read_frame(FauxPort(flux), tout_bon, range_brut)

        assert labels and "ADCO" in labels and "IINST" in labels, (
            f"{quoi} corrompu : les groupes sains sont perdus aussi — {labels}")
        assert "PAPP" not in labels, f"{quoi} corrompu : groupe abîmé accepté — {labels}"
        assert m._derniere_trame["rejetees"] >= 1, (
            f"{quoi} corrompu : le groupe disparaît sans être compté — "
            f"{m._derniere_trame}")
        cause = m._cause_rejets("PAPP")
        assert "n'est pas émise" not in cause, (
            f"{quoi} corrompu : on accuse le COMPTEUR alors que c'est la "
            f"LIAISON — {cause!r}")


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
def cent_trames_REELLES_et_saines_ne_perdent_AUCUN_groupe():
    """
    ⚖️ LE TÉMOIN DE TOUT CE CHANTIER, et il manquait.

       Tous les autres cas prouvent qu'on rejette ce qu'il faut. Aucun ne
       prouvait qu'on ne rejette QUE ça — or un lecteur qui refuserait tout les
       satisferait tous, et ne lirait plus rien, en silence. C'est le risque que
       la condamnation du groupe entier a introduit : elle est plus sévère
       qu'avant, et « plus sévère » doit se mesurer, pas s'espérer.

    ⭐ Sur la VRAIE trame d'un boîtier en production, pas sur une fabrication :
       elle porte le cas limite d'un checksum valant l'espace (PTEC), un PAPP à
       zéro, onze groupes de longueurs différentes. Rejouée cent fois d'affilée,
       elle éprouve aussi l'enchaînement ETX→STX.
    """
    import sys as _sys
    _sys.path.insert(0, "../lora-receiver")
    import banc_frames

    brut = banc_frames.frame_bytes("ben0003_histo_hc_prod")
    flux = [sain(o) for o in brut] * 100        # 7 bits + parité paire, comme sur le fil
    port = FauxPort(flux)

    trames = gardes = rejetes = parite = 0
    while True:
        labels = m.read_frame(port, m.tic_checksum_ok, m._parse_label)
        if labels is None:
            break
        trames += 1
        gardes  += m._derniere_trame["gardees"]
        rejetes += m._derniere_trame["rejetees"]
        parite  += m._derniere_trame["parite"]

    assert trames == 100, f"trames perdues : {trames}/100"
    assert gardes == 1100, f"groupes perdus : {gardes}/1100 gardés"
    assert rejetes == 0, f"SUR-REJET : {rejetes} groupe(s) sains refusés"
    assert parite == 0, f"parité mal calculée : {parite} octet(s) sains refusés"


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

    # ⭐ LA FRONTIÈRE EST À L'ENTRÉE DE LA TRAME, PAS À L'ENTRÉE DU GROUPE.
    #
    #    Un octet fautif ENTRE le CR et l'ETX est bien hors de tout groupe — mais
    #    une trame TIC bien formée ne contient RIEN à cet endroit, donc c'est
    #    presque sûrement un LF mangé, et le groupe qui devait suivre est perdu.
    #    Il est compté, et le relevé parle de rejet.
    #
    # ⚠️ Ce n'était pas le cas avant : l'octet était ignoré et le relevé
    #    concluait « pas émise » — il envoyait chercher un défaut chez le
    #    COMPTEUR alors que le bruit était sur le FIL.
    #
    # ⚖️ Et c'est bien une FRONTIÈRE, pas une règle qui avale tout : le cas
    #    `le_releve_n_accuse_PAS_la_parite_pour_un_octet_hors_trame` met le même
    #    octet fautif AVANT le STX — là il ne coûte rien, et « pas émise » reste
    #    la bonne réponse. Les deux cas se tiennent l'un l'autre.
    flux = ([sain(STX)] + ligne("ADCO 021861000000 X")
            + [corrompu(ord("A"))] + [sain(ETX)])
    m.read_frame(FauxPort(flux), tout_bon, range_brut)
    cause = m._cause_rejets()
    assert "groupe(s) rejeté(s)" in cause, (
        f"un octet fautif DANS la trame ne coûte rien : le groupe qu'il a fait "
        f"perdre disparaît du relevé — {cause!r}")
    assert "n'est pas émise" not in cause, (
        f"le compteur est accusé alors que la liaison est en cause — {cause!r}")

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


# ─── `peut_stocker` : ce qui autorise une écriture ──────────────────────────
#
# 🚨 `0` EST UN pdl_index VALIDE — celui du premier compteur de tout boîtier. C'est
#    toute la raison d'être de ce prédicat : avant, l'amorce valait 0 et personne ne
#    pouvait distinguer « pas encore résolu » de « compteur n°0 ». Une trame arrivée
#    avant la résolution partait donc en base sous l'index d'un AUTRE compteur, en
#    silence, sur tout boîtier déplacé puis redémarré.
#
# ⭐ Le prédicat prend ses deux arguments : c'est ce qui permet de l'éprouver ici,
#    sur une machine sans UART, sans base et sans Pi.
@cas
def peut_stocker_refuse_un_pdl_non_resolu():
    """Le cas qui motive tout : base ouverte, mais on ne SAIT pas encore où écrire."""
    assert m.peut_stocker(object(), None) is False


@cas
def peut_stocker_accepte_le_pdl_zero():
    """⚖️ LE TÉMOIN, et il n'est pas décoratif : sans lui, un prédicat qui refuserait
    TOUT passerait le cas précédent — et le boîtier cesserait de stocker en silence,
    heartbeat vert et journal calme."""
    assert m.peut_stocker(object(), 0) is True


@cas
def peut_stocker_refuse_une_base_fermee():
    """Comportement HISTORIQUE préservé : base indisponible → le lecteur continue
    (LED, journal), simplement sans stocker."""
    assert m.peut_stocker(None, 0) is False
    assert m.peut_stocker(None, None) is False


@cas
def un_adco_non_conforme_n_atteint_jamais_l_etat_persistant():
    """🚨 LE DÉFAUT QUE CE BANC EXISTE POUR TENIR FERMÉ.

    La boucle écrivait `state["adco"] = adco` AVANT toute validation — deux lignes plus
    haut que l'appel à `resolve_pdl`. Le garde du magasin ne voyait donc rien : il
    protège la CRÉATION d'un PDL, pas le fichier d'état. Conséquence, un `'\\x00\\x00'`
    s'installait dans `tic-state.json`, et la trame SAINE suivante était annoncée
    « NOUVEAU PDL » puisqu'elle différait du bidon retenu.

    Le rejet est désormais au DÉCODAGE : `garder_adco` ne pose simplement pas la clé,
    donc `labels.get("ADCO", "")` rend la chaîne vide et tous les gardes `if adco:` en
    aval — écriture de `state.json` comprise — suffisent sans qu'on en ajoute un seul.
    """
    for bidon in ("\x00\x00", "06194700", "0619470000000", "06194700000A", "²" * 12):
        labels = {}
        m.garder_adco(bidon, labels)
        assert "ADCO" not in labels, f"{bidon!r} a été retenu comme ADCO"
        # Ce que la boucle en fait : la clé absente ⇒ rien ne part vers `state.json`.
        assert labels.get("ADCO", "") == ""


@cas
def un_adco_conforme_traverse_bien_le_decodage():
    """⚖️ LE TÉMOIN du décodeur. Sans lui, un `garder_adco` qui ne poserait JAMAIS la
    clé passerait le cas précédent — et le boîtier ne résoudrait plus jamais son PDL."""
    labels = {}
    m.garder_adco("  021861000000 ", labels)     # blancs autour : même compteur
    assert labels["ADCO"] == "021861000000"


@cas
def un_ADCO_non_conforme_CONDAMNE_TOUTE_la_trame():
    """🚨 LE CŒUR DE L'ARBITRAGE, et il va CONTRE l'intuition « un groupe fautif coûte
    son groupe ».

    On n'arrive ici que par un groupe ADCO ayant passé LA PARITÉ ET LE CHECKSUM sans
    avoir la forme d'un ADCO — donc par l'amputation dans l'angle mort du checksum
    (caractères retirés sommant à un multiple de 64). Cet angle mort est le MÊME pour
    tous les groupes : un index ou un PAPP raccourci a pu passer de la même façon, sans
    qu'aucune forme ne permette de le voir, puisqu'un nombre raccourci reste un nombre.

    ⭐ L'ADCO est le seul champ de la TIC dont la forme soit connue d'avance : c'est le
       seul témoin que nous ayons de cet angle mort. Garder la trame reviendrait à le
       jeter — et à écrire en base des mesures dont on a la PREUVE que la ligne a perdu
       des caractères pendant leur transmission.
    """
    labels = {}
    m._parse_label("ADCO 00000000000A 1", labels)     # identité de forme fausse
    m._parse_label("PAPP 01230 5", labels)            # d'apparence saine…
    m._parse_label("IINST 005 7", labels)
    assert "ADCO" not in labels
    assert m._rendre(labels) is m.TRAME_CONDAMNEE, \
        "la trame a été rendue malgré un ADCO non conforme"


@cas
def une_trame_saine_est_bien_rendue():
    """⚖️ LE TÉMOIN de la condamnation : sans lui, un `_rendre` qui refuserait TOUT
    passerait le cas précédent — et le lecteur ne stockerait plus jamais rien."""
    labels = {}
    m._parse_label("ADCO 021861000000 1", labels)
    m._parse_label("PAPP 01230 5", labels)
    rendu = m._rendre(labels)
    assert rendu is not None and rendu["PAPP"] == 1230
    assert m.CONDAMNEE not in rendu, "la marque interne a fui vers l'appelant"


@cas
def un_groupe_ADCO_abime_par_le_CHECKSUM_ne_condamne_PAS_la_trame():
    """⚠️ La distinction qui fait tenir tout le raisonnement : un ADCO tombé sur le
    checksum ou la parité n'atteint JAMAIS `garder_adco` — `read_frame` a déjà jeté le
    groupe et la clé n'existe pas. Ce cas-là reste un groupe perdu, pas une trame
    perdue. Sans cette frontière, une liaison un peu bruyante ne stockerait plus rien."""
    labels = {}
    m._parse_label("PAPP 01230 5", labels)            # ADCO absent : jamais décodé
    rendu = m._rendre(labels)
    assert rendu is not None and rendu["PAPP"] == 1230


# ─── L'HISTOIRE, bout en bout : des octets du fil jusqu'au refus ────────────
#
# ⚠️ Les cas ci-dessus éprouvent chaque ORGANE séparément. Ceux-ci éprouvent les
#    JOINTURES — et c'est là que vivait le défaut : chaque pièce était juste, c'est leur
#    enchaînement qui laissait passer.

def _checksum_histo(corps: str) -> str:
    """Le checksum TIC historique de `corps` = 'ETIQ VALEUR' (cf. tic_checksum_ok)."""
    return chr((sum(ord(c) for c in corps) & 0x3F) + 0x20)


def _trame(*corps: str) -> list[int]:
    """Une trame TIC historique COMPLÈTE, octets tels qu'ils arrivent du fil."""
    octets = [sain(STX)]
    for c in corps:
        octets += ligne(f"{c} {_checksum_histo(c)}")
    return octets + [sain(ETX)]


@cas
def L_AMPUTATION_traverse_parite_ET_checksum_et_est_QUAND_MEME_arretee():
    """🚨 LE CAS QUI PORTE TOUTE LA THÈSE, et le seul qui la PROUVE.

    Les autres cas appellent `_parse_label` en direct ; celui-ci part des OCTETS et
    remonte toute la chaîne. Il établit deux choses d'un coup :

      1. l'angle mort EXISTE — « ADCO 061947000000 » amputé de quatre zéros devient
         « ADCO 06194700 » et porte LE MÊME CHECKSUM (4 × 0x30 = 192, soit 0 modulo 64),
         ce que le test vérifie au lieu de le supposer ;
      2. la parité ne le voit pas davantage : les octets survivants sont intacts, c'est
         leur NOMBRE qui a changé.

    ⇒ Les deux contrôles historiques laissent passer, et seule la FORME de l'ADCO
      arrête la trame. Sans ce cas, rien ne prouverait que le garde sert à quelque
      chose : on pourrait croire le checksum suffisant.
    """
    entier, ampute = "ADCO 061947000000", "ADCO 06194700"
    assert _checksum_histo(entier) == _checksum_histo(ampute), \
        "l'angle mort du checksum n'est pas reproduit — le cas ne prouve plus rien"

    port = FauxPort(_trame(ampute, "PAPP 01230", "IINST 005", "PTEC TH.."))
    assert m.read_frame(port, m.tic_checksum_ok, m._parse_label) is m.TRAME_CONDAMNEE, \
        "une trame dont l'ADCO a perdu des caractères a été rendue"

    # ⚖️ Le témoin : la MÊME trame, ADCO entier → rendue, avec ses mesures.
    port = FauxPort(_trame(entier, "PAPP 01230", "IINST 005", "PTEC TH.."))
    trame = m.read_frame(port, m.tic_checksum_ok, m._parse_label)
    assert trame is not None, "la trame saine a été condamnée"
    assert trame["ADCO"] == "061947000000" and trame["PAPP"] == 1230


@cas
def le_boitier_n_ecrit_RIEN_tant_qu_il_n_a_pas_resolu_son_compteur():
    """L'acte I de l'histoire : au démarrage on ne SAIT pas, donc on n'écrit pas.

    🚨 Avant, l'amorce valait `0` — l'index d'un VRAI compteur. Un boîtier déplacé puis
       redémarré écrivait ses premières mesures sous son ANCIEN compteur, en silence.
    """
    import sqlite3
    base = sqlite3.connect(":memory:")            # une base ouverte, bien vivante
    assert m.peut_stocker(base, None) is False, "écriture autorisée sans compteur résolu"
    assert m.peut_stocker(base, 0) is True, "⚖️ témoin : 0 est un pdl_index VALIDE"


@cas
def de_la_trame_au_pdl_index_le_parcours_complet():
    """L'acte II : première trame → ADCO décodé → PDL créé → écriture autorisée.

    ⭐ Les trois pièces existaient et étaient testées séparément. C'est leur ENCHAÎNEMENT
       qui n'était couvert nulle part — or c'est lui, le comportement du produit.
    """
    import pathlib
    import tempfile
    import db as vrai_db

    port = FauxPort(_trame("ADCO 021861000000", "PAPP 01230", "IINST 005", "PTEC TH.."))
    trame = m.read_frame(port, m.tic_checksum_ok, m._parse_label)
    assert trame is not None

    conn = vrai_db.connect(str(pathlib.Path(tempfile.mkdtemp()) / "m.db"))
    pdl_index = None
    assert m.peut_stocker(conn, pdl_index) is False      # avant résolution : rien

    pdl_index = vrai_db.resolve_pdl(conn, trame["ADCO"], graine=0)
    assert pdl_index == 0, "le 1er compteur d'un boîtier neuf doit être l'index 0"
    assert m.peut_stocker(conn, pdl_index) is True       # après : le boîtier peut écrire
    assert conn.execute("SELECT adco FROM pdl").fetchone()[0] == "021861000000"


@cas
def une_trame_CONDAMNEE_n_est_PAS_une_trame_ABSENTE():
    """🚨 LE DÉFAUT TROUVÉ EN REVUE, et il recréait la panne que cette PR combat.

    `read_frame` rendait `None` pour deux choses opposées : « rien n'est arrivé »
    (timeout, liaison morte) et « tout est arrivé, mais l'identité ne tient pas ». La
    boucle laisse `frame_ok` à faux sur le premier cas, donc `last_success_time`
    n'avance plus, donc le watchdog relance le process au bout de 10 min — et TOUTES
    LES 10 MINUTES tant que le compteur émet le même ADCO. C'est le mode de panne de
    pi-0.9.12, atteint par une autre porte : sortir `frame_ok` du garde de stockage ne
    couvrait QUE le cas `PDL_INDEX is None`.

    ⭐ Une trame condamnée PROUVE que la liaison est vivante : ses groupes arrivent,
       leur parité et leur checksum passent. Le watchdog surveille le FIL.

    ⚠️ La boucle principale et le watchdog vivent sous `if __name__` et ne sont pas
       importables : ce banc ne peut pas les faire tourner. Il tient donc le CONTRAT
       qui rend la panne impossible — les trois retours sont DISCERNABLES — et c'est
       `labels is None` dans la boucle qui décide du reste.
    """
    entier, ampute = "ADCO 061947000000", "ADCO 06194700"

    condamnee = m.read_frame(FauxPort(_trame(ampute, "PTEC TH..")),
                             m.tic_checksum_ok, m._parse_label)
    absente = m.read_frame(FauxPort([]), m.tic_checksum_ok, m._parse_label)
    saine = m.read_frame(FauxPort(_trame(entier, "PAPP 01230", "IINST 005", "PTEC TH..")),
                         m.tic_checksum_ok, m._parse_label)

    assert condamnee is m.TRAME_CONDAMNEE
    assert absente is None
    assert saine is not None and saine is not m.TRAME_CONDAMNEE

    # 🚨 L'ASSERTION QUI PORTE TOUT : condamnée ≠ absente. Les confondre, c'est
    #    `frame_ok` à faux, donc le watchdog, donc os.execv toutes les 10 min.
    assert condamnee is not absente, \
        "condamnée et absente confondues — le watchdog relancera le process en boucle"


@cas
def signaler_adco_refuse_ne_crie_qu_une_fois_par_valeur():
    """🚨 Sans cette garde, journald est noyé : un ADCO refusé ne met pas à jour
    `_pdl_source_adco`, donc la résolution est retentée À CHAQUE TRAME (~1/s)."""
    vus = []
    vrai_log = m.log
    m.log = MagicMock()
    m.log.error = lambda *a: vus.append(a)
    m._dernier_adco_refuse = None
    try:
        for _ in range(5):
            m.signaler_adco_refuse("\x00\x00")
        assert len(vus) == 1, f"{len(vus)} cris pour une seule valeur"
        m.signaler_adco_refuse("06194700")      # une AUTRE valeur : on veut le savoir
        assert len(vus) == 2, "une valeur refusée inédite est passée sous silence"
    finally:
        m.log = vrai_log
        m._dernier_adco_refuse = None


# ─── L'ALPHABET TIC : le troisième filtre, et son angle mort propre ─────────
#
# 🚨 CE QUE LE DÉFAUT EST VRAIMENT — et ce qu'il N'EST PAS.
#
#    Un octet hors alphabet n'était pas JETÉ, il était AJOUTÉ au groupe. Il n'y
#    avait donc PAS d'amputation ici, à la différence de la parité : le groupe
#    gardait sa longueur, un de ses caractères était simplement REMPLACÉ.
#
# ⭐ Et c'est précisément là que le checksum est aveugle : il vaut
#    `(somme & 0x3F) + 0x20`, donc il ne voit la somme que MODULO 64. Remplacer
#    un caractère par `c - 0x40` retire exactement 64 — **checksum IDENTIQUE** —
#    et le résultat est un caractère de CONTRÔLE, donc hors alphabet.
#
#        'T' = 0x54  →  0x14      'S' = 0x53  →  0x13      'I' = 0x49  →  HT
#
# 🚨 MAIS IL FAUT DEUX BITS, PAS UN, et le dire faux gonflerait la menace. Un
#    SEUL bit retourné sur le fil casse TOUJOURS la parité : le compteur a
#    calculé le bit de parité sur l'octet d'origine, donc `octet_valide` l'arrête
#    et ce contrôle-ci ne le voit jamais. Ce qui l'atteint est un nombre PAIR de
#    bits retournés dans le même octet — typiquement le bit 6 de la donnée ET le
#    bit de parité, qui redevient « juste ».
#
# ⓘ C'est exactement ce que `sain(0x14)` émet dans les cas ci-dessous : l'octet
#   `'T'` dont ces deux bits ont basculé. Le banc est donc fidèle à une
#   corruption réelle — simplement RARE, pas courante. Même arbitrage que celui
#   déjà écrit dans `tic_parite.octet_valide` à propos des deux bits.


def _ligne_substituee(corps: str, i: int, octet: int) -> list[int]:
    """`corps` + SON checksum d'origine, mais le caractère `i` remplacé par `octet`.

    Émis avec la BONNE parité, pour que seule l'appartenance à l'alphabet soit en
    cause — sinon le banc prouverait la parité une deuxième fois.
    """
    texte = f"{corps} {_checksum_histo(corps)}"
    return ([sain(LF)]
            + [sain(octet) if j == i else sain(ord(c)) for j, c in enumerate(texte)]
            + [sain(CR)])


@cas
def UN_OCTET_HORS_ALPHABET_est_arrete_LA_OU_LE_CHECKSUM_EST_AVEUGLE():
    """🚨 LE CAS QUI PORTE #10, et le seul qui PROUVE que le contrôle sert.

    « PTEC TH.. » dont le `'T'` de la valeur (0x54) devient 0x14 : un seul bit
    retourné, le 6.

      1. la PARITÉ ne le voit pas — le bit de parité est juste, l'erreur est née
         sur le fil après le calcul du compteur ;
      2. le CHECKSUM ne le voit pas non plus, et le test le VÉRIFIE au lieu de le
         supposer : −0x40 = −64, donc somme inchangée MODULO 64 ;
      3. l'ALPHABET le voit, parce que 0x14 n'est pas imprimable.

    ⇒ Sans ce troisième filtre, la période tarifaire enregistrée vaut `'\x14H..'`
      au lieu de `'TH..'` — une donnée FAUSSE acceptée, pas une donnée perdue. Et
      en historique c'est PTEC qui donne l'`index_id`.
    """
    corps = "PTEC TH.."
    i = corps.index("TH..")                 # le 'T' de la VALEUR, pas celui de l'étiquette
    abime = corps[:i] + chr(ord(corps[i]) - 0x40) + corps[i + 1:]
    assert _checksum_histo(corps) == _checksum_histo(abime), \
        "l'angle mort du checksum n'est pas reproduit — le cas ne prouve plus rien"

    flux = ([sain(STX)] + _ligne_substituee(corps, i, ord(corps[i]) - 0x40)
            + ligne(f"PAPP 00450 {_checksum_histo('PAPP 00450')}") + [sain(ETX)])
    labels = m.read_frame(FauxPort(flux), m.tic_checksum_ok, range_brut)

    assert "PTEC" not in labels, \
        f"la donnée FAUSSE est passée : PTEC={labels.get('PTEC')!r} au lieu d'un rejet"
    assert m._derniere_trame["rejetees"] == 1, \
        f"le groupe condamné ne figure pas au relevé : {m._derniere_trame}"
    assert m._derniere_trame["alphabet"] == 1, \
        f"l'octet hors alphabet n'est pas compté : {m._derniere_trame}"
    assert m._derniere_trame["groupes_alphabet"] == 1, \
        f"le groupe n'est pas imputé à l'alphabet : {m._derniere_trame}"
    assert "hors alphabet" in m._cause_rejets("PTEC"), \
        f"la cause n'est pas nommée : {m._cause_rejets('PTEC')!r}"

    # ⚖️ LE TÉMOIN : la MÊME trame intacte passe, avec sa vraie valeur.
    flux = ([sain(STX)] + ligne(f"{corps} {_checksum_histo(corps)}")
            + ligne(f"PAPP 00450 {_checksum_histo('PAPP 00450')}") + [sain(ETX)])
    labels = m.read_frame(FauxPort(flux), m.tic_checksum_ok, range_brut)
    assert labels.get("PTEC") == "TH..", \
        f"un groupe entièrement dans l'alphabet a été refusé : {labels}"
    assert m._derniere_trame["alphabet"] == 0 and m._derniere_trame["rejetees"] == 0, \
        f"une trame saine est comptée comme abîmée : {m._derniere_trame}"

    # 🚨 LE SABOTAGE, exigé par l'issue : on retire le contrôle, le banc doit ROUGIR.
    #    Avec un prédicat qui accepte tout, le checksum valide la ligne abîmée et la
    #    donnée FAUSSE entre en base. C'est ce qui prouve que c'est bien CE contrôle
    #    qui l'arrête, et pas un effet de bord d'un autre.
    flux = ([sain(STX)] + _ligne_substituee(corps, i, ord(corps[i]) - 0x40)
            + ligne(f"PAPP 00450 {_checksum_histo('PAPP 00450')}") + [sain(ETX)])
    labels = m.read_frame(FauxPort(flux), m.tic_checksum_ok, range_brut,
                          alphabet=bytes([1]) * 128)
    assert labels.get("PTEC") == "\x14H..", \
        ("sabotage sans effet : sans contrôle d'alphabet la donnée fausse devrait "
         f"passer le checksum, or on obtient {labels!r} — le cas ne prouve plus rien")


def _groupe_sans_CR(corps: str, i: int, octet: int) -> list[int]:
    """Le même groupe que `_ligne_substituee`, mais dont le CR N'ARRIVE JAMAIS."""
    return _ligne_substituee(corps, i, octet)[:-1]


@cas
def la_CAUSE_survit_a_un_groupe_qui_finit_SANS_son_CR():
    """🚨 LE SOUS-COMPTAGE QUE CE CAS A ATTRAPÉ.

    `groupes_alphabet` ne montait qu'au CR. Un groupe portant un octet hors
    alphabet dont le CR est perdu finit sur le LF suivant ou sur l'ETX : il était
    alors compté `rejeté` mais **plus imputé à l'alphabet**. La cause disparaissait
    — c'est-à-dire exactement le mensonge que ce relevé existe pour tuer, et ce
    que fausserait en silence les colonnes prévues par #5.

    ⓘ Trouvé en revue, pas par le banc : le banc ne couvrait que la sortie sur CR.
    """
    corps = "PTEC TH.."
    i = corps.index("TH..")
    mauvais = ord(corps[i]) - 0x40

    # (a) le groupe abîmé finit sur l'ETX — son CR n'est jamais arrivé
    flux = [sain(STX)] + _groupe_sans_CR(corps, i, mauvais) + [sain(ETX)]
    m.read_frame(FauxPort(flux), m.tic_checksum_ok, range_brut)
    assert m._derniere_trame["rejetees"] == 1, f"groupe non compté : {m._derniere_trame}"
    assert m._derniere_trame["groupes_alphabet"] == 1, \
        f"fin sur ETX : le groupe est compté mais la CAUSE est perdue — {m._derniere_trame}"

    # (b) le groupe abîmé finit sur le LF du groupe SUIVANT
    flux = ([sain(STX)] + _groupe_sans_CR(corps, i, mauvais)
            + ligne(f"PAPP 00450 {_checksum_histo('PAPP 00450')}") + [sain(ETX)])
    labels = m.read_frame(FauxPort(flux), m.tic_checksum_ok, range_brut)
    assert m._derniere_trame["groupes_alphabet"] == 1, \
        f"fin sur LF : la CAUSE est perdue — {m._derniere_trame}"
    assert labels.get("PAPP") == "00450", \
        f"⚖️ témoin : le groupe sain qui suit devait passer — {labels}"


@cas
def le_releve_distingue_PARITE_ALPHABET_et_CHECKSUM():
    """Trois groupes, trois causes, trois compteurs — exigence de #10.

    ⚠️ Fondre les causes dirait « ça décroche » sans dire OÙ, or le fil, le
       compteur et le montage ne se diagnostiquent pas pareil.
    """
    par_parite = ligne(f"PAPP 00450 {_checksum_histo('PAPP 00450')}")
    par_parite[7] = corrompu(par_parite[7] & 0x7F)          # dans la VALEUR
    faux_cks = chr(ord(_checksum_histo("OPTARIF BASE")) ^ 1)
    flux = ([sain(STX)]
            + par_parite
            + _ligne_substituee("IINST 005", 6, 0x04)       # hors alphabet
            + ligne(f"OPTARIF BASE {faux_cks}")             # checksum FAUX, construit
            + ligne(f"ADCO 021861000000 {_checksum_histo('ADCO 021861000000')}")
            + [sain(ETX)])
    labels = m.read_frame(FauxPort(flux), m.tic_checksum_ok, range_brut)
    r = m._derniere_trame

    assert r["rejetees"] == 3, f"3 groupes devaient tomber, relevé : {r}"
    assert r["parite_groupes"] == 1, f"la parité n'est pas comptée seule : {r}"
    assert r["alphabet"] == 1 and r["groupes_alphabet"] == 1, \
        f"l'alphabet n'est pas compté seul : {r}"
    assert r["gardees"] == 1 and labels.get("ADCO") == "021861000000", \
        f"⚖️ témoin : le groupe sain devait passer — {labels}"

    cause = m._cause_rejets()
    assert "hors parité" in cause and "hors alphabet" in cause, \
        f"les deux causes ne sont pas nommées ensemble : {cause!r}"


@cas
def HT_passe_en_STANDARD_et_condamne_en_HISTORIQUE():
    """⚖️ Le témoin du mode, et il va dans les DEUX sens.

    Refuser `HT` en standard condamnerait tous les groupes : le lecteur
    deviendrait muet, et aucun cas de REFUS ne le montrerait. L'accepter en
    historique rouvrirait le trou du modulo 64, puisque `'I'` − `HT` = 64 tout
    rond. Les deux assertions se tiennent l'une l'autre.
    """
    corps = "PTEC TH.."
    i = corps.index("TH..")
    flux = [sain(STX)] + _ligne_substituee(corps, i, 0x09) + [sain(ETX)]
    m.read_frame(FauxPort(flux), m.tic_checksum_ok, range_brut)
    assert m._derniere_trame["groupes_alphabet"] == 1, \
        f"HT accepté en historique : {m._derniere_trame}"

    # Le même octet, mode standard : c'est un séparateur légal, il doit PASSER.
    flux = [sain(STX)] + _ligne_substituee(corps, i, 0x09) + [sain(ETX)]
    m.read_frame(FauxPort(flux), m.tic_checksum_ok, range_brut,
                 alphabet=m.ALPHABET_STD)
    assert m._derniere_trame["groupes_alphabet"] == 0, \
        f"HT refusé en standard : le lecteur serait muet — {m._derniere_trame}"


if __name__ == "__main__":
    # 🚨 ON RATTRAPE TOUTE EXCEPTION, PAS SEULEMENT AssertionError.
    #
    #    Avant, un cas qui levait autre chose — un TypeError parce qu'un
    #    `read_frame` cassé rendait None — faisait REMONTER l'exception et
    #    TUAIT la suite : les cas suivants ne tournaient jamais et aucun total
    #    n'était imprimé. Trouvé en sabotant `octet_valide` : trois échecs
    #    s'affichaient, sept cas disparaissaient en silence, et on ne pouvait
    #    pas savoir lesquels.
    #
    # ⚠️ Le code de sortie était juste — la CI serait passée au rouge. C'est le
    #    RELEVÉ qui mentait, en montrant moins de dégâts qu'il n'y en avait.
    #    Un banc qui sous-estime la casse est pire qu'un banc qui plante.
    import traceback
    ko = 0
    for f in CAS:
        try:
            f()
            print(f"  ok   {f.__name__}")
        except AssertionError as e:
            ko += 1
            print(f"  ÉCHEC {f.__name__}\n        {e}")
        except Exception:
            ko += 1
            trace = traceback.format_exc().strip().splitlines()[-1]
            print(f"  PLANTE {f.__name__}\n        {trace}")
    print(f"\n{len(CAS) - ko}/{len(CAS)}")
    sys.exit(1 if ko else 0)
