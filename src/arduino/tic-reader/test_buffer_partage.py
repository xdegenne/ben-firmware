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

# 🚨 TOUTE ASSERTION DE STRUCTURE SE FAIT SUR `CODE`, JAMAIS SUR `SRC`. Ce fichier parle
#    abondamment de son propre code dans ses commentaires — `msg_count++`, `curveN > 1`,
#    `curveActive` — et TROIS versions successives de ce banc s'y sont fait prendre : une
#    assertion satisfaite par un commentaire ne vérifie rien, et elle le fait en silence.
#    `SRC` ne sert plus qu'à chercher du texte qu'on veut VRAIMENT brut.
CODE_LIGNES = [l.split("//")[0] for l in LIGNES]
CODE = "\n".join(CODE_LIGNES)

CAS = []


def cas(f):
    CAS.append(f)
    return f


def _appels_sendBootFrame():
    """Les numéros de ligne (1-indexés) des APPELS à sendBootFrame, pas sa définition."""
    return [i + 1 for i, l in enumerate(CODE_LIGNES)
            if "sendBootFrame(" in l and not l.lstrip().startswith("bool ")]


@cas
def le_tampon_est_bien_PARTAGE_sinon_ce_banc_n_a_plus_d_objet():
    """⚖️ Le témoin de tous les autres cas. Si un jour la trame de boot reçoit son propre
    tampon, l'invariant disparaît — et ce banc doit alors être retiré, pas contourné."""
    assert re.search(r"uint8_t\*\s+buf\s*=\s*curveBuf\s*;", CODE), \
        "sendBootFrame n'écrit plus dans curveBuf : l'invariant a changé, revoir ce banc"
    assert re.search(r"writeHeader\(curveBuf,\s*TYPE_CURVE\)", CODE), \
        "curveStart n'écrit plus son en-tête dans curveBuf : revoir ce banc"
    # ⚠️ On compte les OCCURRENCES dans le CODE, commentaires retirés. Deux versions
    #    précédentes de ce banc se sont trompées ici : la première comptait les `msg_count++`
    #    cités dans les commentaires (ce fichier en parle beaucoup), la seconde comptait les
    #    LIGNES — et laissait donc passer deux incréments écrits sur la même ligne.
    n = len(re.findall(r"msg_count\s*\+\+", CODE))
    assert n == 2, (
        f"{n} incréments de msg_count dans le code au lieu de 2 — chaque trame scellée doit "
        f"consommer UN nonce, et un seul : un de plus et deux trames partagent le leur")


@cas
def CHAQUE_appel_a_sendBootFrame_traite_le_lot_en_cours():
    """🚨 LE CŒUR DU BANC. Trois appels existent ; il en suffit d'UN sans garde pour rouvrir
    la réutilisation de nonce.

    ⚠️ UNE GARDE DERRIÈRE UN `return` NE GARDE RIEN, et une première version de ce banc s'y
       est fait prendre : pour l'appel du premier boot, elle acceptait le
       `if (bootAcked && curveActive) curveFlush();` de la branche de tension basse, située
       vingt lignes plus haut — alors que cette branche fait `return` avant d'atteindre
       l'appel. Le banc passait donc par accident. La fenêtre amont est désormais TRONQUÉE au
       dernier `return`.

    ⭐ Et UN appel est légitimement NON gardé : celui du premier boot, qui tourne sur la
       PREMIÈRE trame, avant que le moindre lot ait pu démarrer — `curveActive` y est
       nécessairement faux. On ne lui ajoute pas une garde morte pour faire plaisir au banc ;
       on l'identifie, on dit pourquoi, et on exige qu'il reste LE SEUL.

    ⓘ Il est reconnu à ses arguments : lui seul passe `v0`, la trame lue par la discovery.
    """
    appels = _appels_sendBootFrame()
    assert len(appels) == 3, (
        f"{len(appels)} appels à sendBootFrame au lieu de 3 — si c'en est un NOUVEAU, il doit "
        f"traiter le lot en cours comme les autres")

    FENETRE = 30          # lignes de contexte amont : les commentaires de ce fichier sont longs
    sans_garde = []
    for n in appels:
        amont = CODE_LIGNES[max(0, n - 1 - FENETRE):n - 1]
        # 🚨 tout ce qui précède un `return` est hors du chemin qui mène à l'appel
        derniers_return = [i for i, l in enumerate(amont) if re.search(r"\breturn\b", l)]
        if derniers_return:
            amont = amont[derniers_return[-1] + 1:]
        bloc = "\n".join(amont)
        flushe = re.search(r"curveActive\s*\)\s*curveFlush\(\)", bloc)
        jete = re.search(r"curveActive\s*=\s*false\s*;\s*curveN\s*=\s*0\s*;", bloc)
        if not (flushe or jete):
            sans_garde.append(n)

    assert len(sans_garde) == 1, (
        f"{len(sans_garde)} appel(s) à sendBootFrame sans flush ni rejet du lot, lignes "
        f"{sans_garde} — un seul a le droit de l'être, celui du premier boot (#30)")
    ligne = CODE_LIGNES[sans_garde[0] - 1]
    assert "v0." in ligne, (
        f"l'appel non gardé (ligne {sans_garde[0]}) n'est PAS celui du premier boot : "
        f"{ligne.strip()!r}. Un lot peut donc être actif, et son tampon serait écrasé.")


@cas
def un_boot_NON_ACQUITTE_en_STREAMING_fait_retomber_en_REGISTERING():
    """🚨 LA RÉGRESSION QUE LE REJET DU LOT A FAILLI INTRODUIRE, et elle était grave.

    `lastSent*` ne bouge que sur ACK : si la trame de boot du chemin STREAMING n'est pas
    acquittée, la condition reste vraie à la trame TIC suivante (~1,7 s). Avant le rejet du
    lot, cette boucle était bornée PAR ACCIDENT — le lot continuait de se remplir, le flush
    périodique partait au bout de 40-55 s, et c'est LUI qui porte
    `if (!acked) bootAcked = false`.

    ⚠️ En jetant le lot, `curveN` repart à 1 à chaque tour, donc la condition `curveN > 1` du
       flush périodique n'est JAMAIS vraie : la seule porte vers REGISTERING se referme, et
       l'émetteur martèle une trame de boot à 20 dBm à chaque trame TIC, indéfiniment —
       brownout sur supercap, et rapport cyclique 868 MHz dépassé.

    ⇒ Le chemin STREAMING doit donc poser `bootAcked = false` lui-même quand l'envoi échoue.
    """
    assert re.search(r"curveN\s*>\s*1", CODE), (
        "la condition `curveN > 1` du flush périodique a changé — or c'est elle qui rend le "
        "flush impossible quand le lot est rejeté à chaque tour, donc elle fait partie du "
        "raisonnement de ce cas : le revoir avant de la toucher")
    # le `else` de l'appel STREAMING — celui qui passe `v.`, pas `v0.`
    m = re.search(r"if \(sendBootFrame\(v\.adco.*?\n(.*?)\n\s*\}\s*\n", CODE, re.S)
    assert m, "l'appel STREAMING à sendBootFrame est introuvable ou a changé de forme"
    apres = CODE[m.start():m.start() + 2000]
    assert re.search(r"\}\s*else\s*\{", apres), (
        "l'appel STREAMING n'a pas de branche `else` : un envoi non acquitté laisserait "
        "`bootAcked` vrai et la condition vraie → réémission à chaque trame TIC")
    bloc_else = apres[apres.index("else"):]
    assert re.search(r"bootAcked\s*=\s*false\s*;", bloc_else[:1200]), (
        "l'échec d'envoi ne pose pas `bootAcked = false` : l'émetteur resterait en STREAMING "
        "et martèlerait sa trame de boot toutes les ~1,7 s à 20 dBm")


@cas
def le_rejet_du_lot_remet_BIEN_les_deux_marqueurs():
    """Jeter un lot demande `curveActive = false` ET `curveN = 0`. Oublier le second laisse
    un compte d'échantillons qui ne correspond plus à rien dans le tampon suivant."""
    n = len(re.findall(r"curveActive\s*=\s*false\s*;\s*curveN\s*=\s*0\s*;", CODE))
    assert n >= 2, (
        f"{n} rejet(s) de lot de la forme complète trouvé(s) : le chemin STREAMING (#30) et "
        f"le batch-horloge doivent tous deux remettre les DEUX marqueurs")
    # ⚠️ Un `curveActive = false` SEUL, hors de curveFlush, laisserait curveN en place.
    # ⚠️ Hors la DÉCLARATION (`static bool curveActive = false;`), qui n'est pas un rejet —
    #    une première version de ce banc la comptait comme telle.
    seuls = [i + 1 for i, l in enumerate(CODE_LIGNES)
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
