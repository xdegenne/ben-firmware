#!/usr/bin/env python3
"""Banc de la cadence d'envoi — le rattrapage, et le retour au calme.

Ce n'est pas un banc de réglage : chaque cas correspond à un comportement qu'on ne peut
pas observer sans attendre des heures sur un boîtier.

  · un boîtier à **courte fenêtre de connectivité** ne rattrapait JAMAIS son retard. Il
    envoyait un lot de **500 points** toutes les 60 s pour une production radio de
    64,5/min — un débit NET de 435/min, donc **plus de 3 h 30 de connectivité par jour**
    rien que pour ne pas reculer. En dessous, l'écart grandissait chaque jour ;
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


# ── Le garde autour de la lecture du retard ──────────────────────────────────

class _BaseQuiTombe:
    """Une base qui refuse de répondre — verrou tenu, carte SD fatiguée."""

    def execute(self, *a, **k):
        import sqlite3
        raise sqlite3.OperationalError("database is locked")


@cas
def une_base_illisible_ne_tue_pas_le_publisher():
    """🚨 LE DÉFAUT TROUVÉ EN REVUE. `pending_approx()` interroge la base, et son
    appelant est HORS du `try` de la boucle. L'ancien `_sleep(PERIOD)` ne pouvait rien
    lever ; celui-ci si. Sans garde, l'exception remonte hors de `main()` : le process
    meurt sans passer par « arrêté proprement », systemd le relance, et on perd le
    backoff exponentiel."""
    assert pub.cadence_sure(_BaseQuiTombe()) == pub.PERIOD


@cas
def ne_pas_savoir_mesurer_le_retard_ne_fait_pas_ACCELERER():
    """⭐ Le sens du repli, et c'est lui qui compte : une base illisible retombe sur la
    CROISIÈRE, pas sur le rattrapage. L'inverse ferait marteler l'API précisément quand
    le boîtier va mal."""
    assert pub.cadence_sure(_BaseQuiTombe()) != pub.PERIOD_RETARD


@cas
def une_base_qui_repond_garde_la_decision_normale():
    """⚖️ LE TÉMOIN du garde : sans lui, un `cadence_sure` qui rendrait TOUJOURS
    `PERIOD` passerait les deux cas ci-dessus — et le rattrapage ne marcherait jamais."""

    class _BaseEnRetard:
        def execute(self, *a, **k):
            class R:
                def fetchone(self_inner):
                    return (10 * pub.BATCH, 1)      # max(rowid), min(rowid non envoyé)
            return R()

    assert pub.cadence_sure(_BaseEnRetard()) == pub.PERIOD_RETARD


# ── Le niveau de journalisation d'un échec ───────────────────────────────────

@cas
def un_echec_qui_PERSISTE_est_une_ERREUR():
    """🚨 LE DÉFAUT QUI A COÛTÉ UN DIAGNOSTIC ENTIER. Tous les échecs de publication étaient
    en `warning`, donc en priorité syslog 4 — un cran sous le `-p 3` de l'instantané de santé.

    Un boîtier du parc MESURAIT (une trame toutes les 38 s), était EN LIGNE (hello en 17 ms,
    WiFi à -40 dBm), son publisher TOURNAIT (0 redémarrage), il avait **566 248 points en
    attente** et n'envoyait RIEN. Le diagnostic à distance ne pouvait pas voir POURQUOI :
    la seule ligne qui l'expliquait était sous le seuil (#18).

    ⭐ Monter le niveau coûte zéro et rend la panne visible — à la sonde comme à qui lit le
       journal directement."""
    import logging
    assert pub.niveau_echec(pub.ECHECS_ERREUR) == logging.ERROR
    assert pub.niveau_echec(pub.ECHECS_ERREUR + 50) == logging.ERROR


@cas
def un_echec_ISOLE_reste_un_avertissement():
    """⚖️ LE TÉMOIN, et il n'est pas décoratif : un réseau cligne. Passer en `error` dès le
    premier échec remplirait le journal d'erreurs pour des coupures de quelques secondes — et
    un niveau qui crie tout le temps ne garde plus rien, on cesse de le regarder. C'est le même
    raisonnement que la gigue totale."""
    import logging
    assert pub.niveau_echec(1) == logging.WARNING
    assert pub.niveau_echec(pub.ECHECS_ERREUR - 1) == logging.WARNING


@cas
def le_seuil_d_erreur_est_FRANCHI_avant_le_plafond_du_backoff():
    """⚠️ Si le seuil était au-delà du plafond de backoff, il ne servirait à rien : le boîtier
    atteint `BACKOFF_MAX` à partir de ~9 échecs et n'« avance » plus. Un seuil à 20 ne se
    verrait donc jamais plus tôt qu'un seuil à 9 — mais il retarderait la visibilité de
    plusieurs dizaines de minutes."""
    import math
    plafond = math.ceil(math.log2(pub.BACKOFF_MAX))     # ~9 : 2**9 = 512 > 300
    assert pub.ECHECS_ERREUR < plafond, (
        f"seuil {pub.ECHECS_ERREUR} >= {plafond} : la panne ne serait visible qu'après "
        "des dizaines de minutes de silence")


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
