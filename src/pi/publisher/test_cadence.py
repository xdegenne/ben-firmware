#!/usr/bin/env python3
"""Banc de la cadence d'envoi — le rattrapage, et le retour au calme.

Ce n'est pas un banc de réglage : chaque cas correspond à un comportement qu'on ne peut
pas observer sans attendre des heures sur un boîtier.

  · un boîtier à **courte fenêtre de connectivité** ne rattrapait JAMAIS son retard. Il
    envoyait un lot toutes les 60 s pour une production radio de 64,5 points/min — un
    débit NET de 935/min, donc **plus de 3 h 30 de connectivité par jour** rien que pour
    ne pas reculer. En dessous, l'écart grandissait chaque jour ;
  · ⭐ le goulot n'était PAS la taille du lot, et la mesure l'a tranché : un aller-retour
    coûte **~400 ms pour 1000 points** (mesuré sur un Pi Zero du parc), contre 60 s de
    sommeil. Le service travaillait **0,7 % du temps** ;
  · ⚠️ mais la cadence de rattrapage ne doit pas devenir la cadence permanente — sinon on
    passerait de « trop lent quand il faut rattraper » à « inutilement bavard tout le
    reste du temps », et sept boîtiers marteleraient l'API en continu.

    python3 src/pi/publisher/test_cadence.py
"""
import pathlib
import sys

R = pathlib.Path(__file__).resolve().parent
sys.path[:0] = [str(R.parent), str(R)]

import ben_publisher as pub  # noqa: E402

CAS = []


def cas(fn):
    CAS.append(fn)
    return fn


# ── La décision ──────────────────────────────────────────────────────────────

@cas
def a_jour_on_garde_la_cadence_de_croisiere():
    """⚖️ LE TÉMOIN, et il n'est pas décoratif : sans lui, une `cadence` qui rendrait
    TOUJOURS la valeur de rattrapage passerait tous les autres cas — et sept boîtiers
    interrogeraient l'API toutes les 10 s en permanence, pour rien."""
    assert pub.cadence(0) == pub.PERIOD
    assert pub.cadence(1) == pub.PERIOD


@cas
def sous_un_lot_plein_on_n_accelere_pas():
    """Le seuil est `BATCH`, pas zéro. En dessous d'un lot plein il ne reste au plus
    qu'un lot partiel : accélérer pour lui ne gagnerait rien, et ferait osciller la
    cadence à chaque tour."""
    assert pub.cadence(pub.BATCH - 1) == pub.PERIOD


@cas
def des_qu_un_lot_plein_attend_on_accelere():
    """Le cas qui motive tout le chantier."""
    assert pub.cadence(pub.BATCH) == pub.PERIOD_RETARD
    assert pub.cadence(pub.BATCH * 100) == pub.PERIOD_RETARD


@cas
def la_cadence_de_rattrapage_est_PLUS_RAPIDE_que_la_croisiere():
    """🚨 Le sens de l'inégalité est le cœur du correctif. Inverser les deux constantes
    produirait un service qui RALENTIT quand il prend du retard — et rien d'autre dans
    ce banc ne le verrait, puisque les deux valeurs resteraient distinctes."""
    assert pub.PERIOD_RETARD < pub.PERIOD


# ── Ce que ça donne sur le terrain ───────────────────────────────────────────

@cas
def une_journee_de_retard_se_resorbe_en_moins_de_vingt_minutes():
    """⭐ Le vrai critère : pas « la constante vaut 10 » mais « le boîtier rattrape ».

    Un boîtier radio produit 43 points toutes les ~40 s, soit ~93 000 par jour. Un
    aller-retour de 1000 points a été mesuré à ~0,40 s sur un Pi Zero du parc.

    ⚠️ Ce cas tomberait si quelqu'un remontait `PERIOD_RETARD` sans regarder l'effet —
    c'est précisément ce qu'on veut qu'il voie.
    """
    JOUR, ALLER_RETOUR = 93_000, 0.40
    lots = -(-JOUR // pub.BATCH)                       # arrondi au lot supérieur
    secondes = lots * (ALLER_RETOUR + pub.PERIOD_RETARD)
    assert secondes < 20 * 60, f"{secondes / 60:.0f} min pour rattraper une journée"


@cas
def la_charge_parc_reste_bornee():
    """⚠️ L'autre bord, celui qu'un sommeil nul ferait sauter : sept boîtiers qui
    rattrapent ENSEMBLE après une panne d'opérateur. La VM est une DEV1-S au budget
    mémoire déjà tendu — la cadence doit plafonner la charge, pas seulement accélérer."""
    PARC, ALLER_RETOUR = 7, 0.40
    par_boitier = pub.BATCH / (ALLER_RETOUR + pub.PERIOD_RETARD)
    assert par_boitier * PARC < 2000, f"{par_boitier * PARC:.0f} points/s pour le parc"


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
