#!/usr/bin/env python3
"""Banc de la DÉCLARATION réconciliée — « le cloud sait-il quelle version tourne ? »

Ce qu'on éprouve ici n'est pas un réglage, c'est un défaut MESURÉ le 04/10 sur le parc :
`devices.sw_version` annonçait `0.9.27` pour les 8 boîtiers alors que deux tournaient en
`0.9.28`, et la vue parc serait restée fausse INDÉFINIMENT. La cause était un ÉVÉNEMENT —
un drapeau que chaque `update.sh` devait penser à poser, et qu'un seul script sur 37 a
posé. Le remplaçant est une CONDITION : version mémorisée ≠ version installée.

🚨 Un banc d'une condition doit prouver les DEUX sens. Une `motif_declaration` qui
   rendrait TOUJOURS un motif fermerait le défaut d'origine et passerait tous les cas
   « il faut déclarer » — en faisant redéclarer sept boîtiers toutes les 60 s pour
   toujours. Les témoins NÉGATIFS sont donc la moitié du banc, pas sa décoration.

    python3 src/pi/publisher/test_declaration_version.py
"""
import json
import logging
import os
import pathlib
import sys
import tempfile

R = pathlib.Path(__file__).resolve().parent
sys.path[:0] = [str(R.parent), str(R)]

import ben_publisher as pub  # noqa: E402

# ⓘ LE JOURNAL DU PUBLISHER EST MUET PENDANT LE BANC. Plusieurs cas lui passent EXPRÈS un
#    fichier illisible ou un répertoire inscriptible — c'est l'objet du contrôle — et leurs
#    avertissements atterriraient dans le journal de l'OTA, ce banc étant exécuté en préflight
#    par `update.sh`. On les y lirait comme une panne du boîtier. Les assertions sont l'oracle,
#    pas le journal.
logging.getLogger("ben-publisher").setLevel(logging.CRITICAL)

CAS = []


def cas(fn):
    CAS.append(fn)
    return fn


# `motif_declaration` est pure : on lui passe un temps, jamais l'horloge.
JAMAIS = float("-inf")
PLANCHER = pub.DECLARATION_PLANCHER_S
LONG = pub.DECLARATION_PLANCHER_REFUS_S


def motif(installee="0.10.0", declaree="", manquants=(), refuses=(),
          dernier=JAMAIS, maintenant=0.0):
    return pub.motif_declaration(installee, declaree, list(manquants), set(refuses),
                                 dernier, maintenant)


# ── LES QUATRE ÉTATS DE LA MÉMOIRE ────────────────────────────────────────────

@cas
def memoire_absente_vaut_jamais_declaree_donc_une_declaration():
    """⭐ LE CAS QUI RECALE TOUT LE PARC. Au premier battement après l'OTA qui livre ce
    code, aucun boîtier n'a de mémoire : chacun doit déclarer une fois."""
    m = motif(installee="0.10.0", declaree="")
    assert m, "mémoire absente doit déclarer"
    assert "0.10.0" in m and "jamais" in m, m


@cas
def memoire_egale_a_la_version_installee_ne_declare_RIEN():
    """⚖️ LE TÉMOIN, et sans lui tout le reste du banc passerait sur une fonction qui
    déclare toujours — sept boîtiers déclarant toutes les 60 s, pour toujours."""
    assert motif(installee="0.10.0", declaree="0.10.0") == ""


@cas
def memoire_differente_declare_et_le_journal_dit_les_deux_versions():
    m = motif(installee="0.10.0", declaree="0.9.28")
    assert m, "une version neuve doit se déclarer"
    assert "0.10.0" in m and "0.9.28" in m, (
        f"le motif doit nommer les DEUX versions, sinon le journal ne permet pas de "
        f"trancher ce qui a été comparé : {m!r}")


@cas
def un_RETOUR_ARRIERE_declare_aussi():
    """🚨 C'est une ÉGALITÉ qu'on teste, pas un ORDRE — et c'est voulu. Le drapeau
    comparait un ordre (`installée >= attendue`) ; ici, redescendre en 0.9.28 après un
    retour arrière est un fait que le cloud doit apprendre comme un autre."""
    assert motif(installee="0.9.28", declaree="0.10.0")


# ── CE QU'ON NE DÉCLARE PAS ───────────────────────────────────────────────────

@cas
def une_version_installee_VIDE_ne_se_declare_jamais():
    """⚠️ `device.json` illisible ou amputé apprendrait au cloud un `sw` VIDE — pire que
    périmé : plus rien ne dirait que la valeur est à retrouver."""
    assert motif(installee="", declaree="") == ""
    assert motif(installee="", declaree="0.10.0") == ""


# ── LE PLANCHER : un refus du cloud ne doit pas faire une rafale ──────────────

@cas
def un_refus_du_cloud_est_rejoue_AU_TOUR_SUIVANT_mais_pas_avant_le_plancher():
    """⭐ LA PROPRIÉTÉ QUI REMPLACE LE DRAPEAU. Le cloud refuse ⇒ la mémoire n'est pas
    écrite ⇒ la condition reste VRAIE, donc rien n'est perdu. Mais elle reste vraie à
    CHAQUE tour : sans plancher le boîtier redéclarerait toutes les 10 s quand le retard
    est gros — la rafale déjà constatée avec le drapeau."""
    # Juste après une tentative refusée : la condition est toujours vraie…
    assert motif(installee="0.10.0", declaree="", dernier=0.0,
                 maintenant=PLANCHER - 1) == "", "rafale : le plancher est sauté"
    # …et elle repart dès le plancher écoulé, sans mémoire d'événement à conserver.
    assert motif(installee="0.10.0", declaree="", dernier=0.0,
                 maintenant=PLANCHER + 1)


@cas
def le_plancher_LONG_ne_s_applique_pas_quand_une_version_attend():
    """🚨 Le plancher long (6 h) existe pour un ADS définitivement non conforme : « plus
    rien à apprendre ». Il serait FAUX ici — une version attend d'être dite. Six heures
    de vue parc fausse à cause d'un pdl fantôme serait un défaut pour un autre."""
    # Un pdl manquant ET déjà refusé : le cas qui déclenche le plancher long…
    assert motif(installee="0.10.0", declaree="0.10.0", manquants=[0], refuses=[0],
                 dernier=0.0, maintenant=PLANCHER + 1) == "", (
        "version à jour + pdl refusé : le plancher LONG doit s'appliquer")
    # …mais si en plus la version diffère, c'est le plancher COURT qui vaut.
    assert motif(installee="0.10.0", declaree="0.9.28", manquants=[0], refuses=[0],
                 dernier=0.0, maintenant=PLANCHER + 1), (
        "une version en attente doit repartir au plancher COURT")
    assert motif(installee="0.10.0", declaree="0.10.0", manquants=[0], refuses=[0],
                 dernier=0.0, maintenant=LONG + 1), (
        "le plancher long doit finir par s'écouler")


@cas
def le_pdl_sans_ref_reste_un_declencheur_a_lui_seul():
    """⚠️ CONTRE-TÉMOIN DU CHANTIER : on AJOUTE une condition, on n'en remplace pas une.
    Un compteur neuf sur un boîtier à jour doit continuer de déclarer."""
    m = motif(installee="0.10.0", declaree="0.10.0", manquants=[2])
    assert m and "pdl sans ref [2]" in m, m


@cas
def les_deux_motifs_voyagent_ENSEMBLE_dans_le_journal():
    m = motif(installee="0.10.0", declaree="0.9.28", manquants=[0, 1])
    assert "0.9.28" in m and "pdl sans ref [0, 1]" in m, m


# ── LA MÉMOIRE SUR LE DISQUE ──────────────────────────────────────────────────

@cas
def aller_retour_sur_le_disque():
    with tempfile.TemporaryDirectory() as d:
        f = os.path.join(d, "version-declaree.json")
        assert pub.version_declaree(f) == "", "absent doit valoir « jamais déclarée »"
        pub.memoriser_version_declaree("0.10.0", f)
        assert pub.version_declaree(f) == "0.10.0"
        # Et la condition devient fausse — l'aller-retour complet, pas la moitié.
        assert motif(installee="0.10.0", declaree=pub.version_declaree(f)) == ""


@cas
def l_ecriture_est_ATOMIQUE_et_ne_laisse_pas_de_temporaire():
    """⭐ Un fichier tronqué par une coupure se relirait comme une AUTRE version, donc
    ferait TAIRE la déclaration — le seul mode de défaillance silencieux de ce fichier."""
    with tempfile.TemporaryDirectory() as d:
        f = os.path.join(d, "v.json")
        pub.memoriser_version_declaree("0.10.0", f)
        restes = sorted(os.listdir(d))
        assert restes == ["v.json"], f"temporaire laissé derrière : {restes}"


@cas
def un_fichier_ILLISIBLE_vaut_jamais_declaree_et_ne_leve_pas():
    """⚠️ « On ne sait pas » et « jamais déclarée » doivent donner le MÊME comportement :
    une déclaration de trop, bornée par le plancher. L'arbitrage inverse — supposer que
    le cloud sait — laisserait la vue parc fausse pour toujours, et EN SILENCE."""
    with tempfile.TemporaryDirectory() as d:
        for contenu in ('{"version"', '', 'pas du json', '{}', '{"version": null}'):
            f = os.path.join(d, "v.json")
            pathlib.Path(f).write_text(contenu)
            assert pub.version_declaree(f) == "", f"{contenu!r} doit valoir « jamais »"


@cas
def un_repertoire_NON_INSCRIPTIBLE_ne_fait_rien_echouer():
    """⚠️ Même doctrine que le redémarrage du publisher par l'agent : on journalise et on
    continue. Lever ici tuerait le tour de boucle APRÈS un 2xx — donc après une
    déclaration réussie, pour une écriture de confort."""
    with tempfile.TemporaryDirectory() as d:
        inaccessible = os.path.join(d, "absent", "v.json")
        pub.memoriser_version_declaree("0.10.0", inaccessible)  # ne doit pas lever
        assert pub.version_declaree(inaccessible) == ""


@cas
def le_contenu_est_du_JSON_date():
    """ⓘ Daté pour qu'on puisse, sur un boîtier, distinguer « déclarée à l'instant » de
    « déclarée il y a trois mois » sans croiser avec le journal."""
    with tempfile.TemporaryDirectory() as d:
        f = os.path.join(d, "v.json")
        pub.memoriser_version_declaree("0.10.0", f)
        d2 = json.loads(pathlib.Path(f).read_text())
        assert d2["version"] == "0.10.0" and isinstance(d2["ts"], int), d2


# ── LE DRAPEAU RETIRÉ ─────────────────────────────────────────────────────────

@cas
def le_drapeau_legue_n_est_plus_un_declencheur():
    """🚨 Le drapeau ne doit plus RIEN déclencher : s'il déclenchait encore, on aurait
    gardé les deux mécanismes — donc celui qu'on retire."""
    assert not hasattr(pub, "DECLARER_FLAG"), (
        "DECLARER_FLAG survit : le mécanisme événementiel n'a pas été retiré")
    assert pub.DRAPEAU_LEGUE.endswith("declaration-requise.json")
    # Et la décision ne prend aucun drapeau en entrée : la signature le prouve.
    import inspect
    args = inspect.signature(pub.motif_declaration).parameters
    assert "drapeau" not in args and "flag" not in args, list(args)


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
