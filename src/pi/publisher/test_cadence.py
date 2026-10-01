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


# ── Le niveau Python doit ATTEINDRE le journal ───────────────────────────────

@cas
def chaque_ligne_porte_sa_PRIORITE_SYSLOG():
    """🚨 LE DÉFAUT QUI A FAIT ÉCHOUER TOUT UN DIAGNOSTIC, ET QUE LE BANC PRÉCÉDENT NE VOYAIT
    PAS — il n'éprouvait que `niveau_echec`, une fonction juste et sans effet.

    `logging.basicConfig()` écrit du texte brut sur stderr. systemd capte stderr et range TOUT
    à une priorité FIXE (`SyslogLevel=6` par défaut, vérifié sur l'unité). Le niveau Python ne
    changeait donc que le TEXTE. Mesuré sur un vrai échec, sur un boîtier du parc :

        [2026-09-23 03:22:51][WARNING] échec n°1 ([Errno -3] Temporary failure…
                              ↑ le texte dit WARNING        →  PRIORITY=6

    ⇒ `health.errors()` interroge `journalctl -p 3` : il ne voyait RIEN des pannes du
      publisher, et passer `warning` en `error` n'y aurait rien changé.

    ⭐ Le préfixe `<N>` est le mécanisme de systemd (`SyslogLevelPrefix=yes`, actif par
       défaut) : vérifié sur la cible, `<4>` donne `PRIORITY=4` et `<3>` donne `PRIORITY=3`,
       et systemd retire le préfixe du message."""
    import io
    import logging
    racine = pub.installer_journal()
    tampon = io.StringIO()
    racine.handlers[0].stream = tampon
    logging.getLogger("essai").warning("un avertissement")
    logging.getLogger("essai").error("une erreur")
    lignes = [l for l in tampon.getvalue().splitlines() if l]
    assert len(lignes) == 2, lignes
    assert lignes[0].startswith("<4>"), f"WARNING doit porter <4> : {lignes[0]!r}"
    assert lignes[1].startswith("<3>"), f"ERROR doit porter <3> : {lignes[1]!r}"


@cas
def la_table_des_priorites_est_celle_de_SYSLOG():
    """⚖️ LE TÉMOIN de la table : une correspondance décalée d'un cran rendrait les erreurs
    invisibles à `-p 3` tout en donnant l'illusion que le correctif est en place. Ce sont les
    valeurs de `sd_journal_print`, pas un choix."""
    import logging
    assert pub.PRIORITE_SYSLOG[logging.ERROR] == 3     # err
    assert pub.PRIORITE_SYSLOG[logging.WARNING] == 4   # warning
    assert pub.PRIORITE_SYSLOG[logging.INFO] == 6      # info
    # 🚨 C'est `-p 3` que la sonde interroge : ERROR doit être <= 3, et WARNING au-dessus.
    assert pub.PRIORITE_SYSLOG[logging.ERROR] <= 3 < pub.PRIORITE_SYSLOG[logging.WARNING]


@cas
def installer_journal_ne_DOUBLE_pas_les_lignes():
    """⚠️ Appelé deux fois — un redémarrage logique, un banc — il ne doit pas empiler les
    gestionnaires : chaque ligne apparaîtrait deux fois dans le journal, avec le même
    horodatage, et on croirait à un bégaiement du service."""
    pub.installer_journal()
    pub.installer_journal()
    import logging
    assert len(logging.getLogger().handlers) == 1


@cas
def l_explication_du_serveur_VOYAGE_avec_l_echec():
    """🚨 L'explication d'un refus ne doit pas rester dans un `warning` que la sonde ne voit
    pas. Première version : seul `log.warning("HTTP %d %s", status, body)` la portait, et la
    ligne escaladée en `error` au bout de cinq échecs ne disait que « HTTP 400 ».

    ⚠️ Or un 400 ou un 413 ne se résout PAS en réessayant — le lot est malformé ou trop gros,
    et le boîtier bouclera dessus. Le corps est la seule chose qui dira laquelle des deux.

    ⚖️ Cas STRUCTUREL : on vérifie que le corps entre dans l'exception, parce que c'est elle
    qui est journalisée au niveau escaladé."""
    import pathlib as _p
    import re
    src = _p.Path(pub.__file__).read_text()
    m = re.search(r'raise RuntimeError\(f"HTTP \{status\}([^"]*)"\)', src)
    assert m, "la levée sur HTTP non-2xx a changé de forme — le cas doit être revu"
    assert "body" in m.group(1), (
        "le corps de la réponse ne voyage pas avec l'exception : la ligne escaladée en "
        f"`error` ne dira que le code : {m.group(0)}")


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


@cas
def un_echec_qui_PERSISTE_declenche_un_hello():
    """🚨 LE CAS QUI VIENT DE COÛTER NEUF JOURS. Mesuré sur ben-0012 le 2026-10-01 : le
    boîtier mesurait, le lien était debout (hello en 17 ms, HTTP 204), `ben-publisher`
    tournait sans un seul plantage — et 566 248 points n'étaient pas partis depuis le 22/09.

    La raison était écrite dans son journal à CHAQUE tentative. Elle était illisible parce
    que l'instantané de santé ne voyage qu'avec le hello, et que le hello ne part qu'une fois
    par jour. On attendait 24 h pour apprendre ce que le boîtier savait depuis la première
    minute.

    ⭐ Au franchissement du seuil d'erreur, l'échec se signale lui-même."""
    assert pub.signaler_echec(pub.ECHECS_ERREUR, float("-inf"), 1000.0)


@cas
def un_echec_ISOLE_ne_declenche_PAS_de_hello():
    """⚖️ L'autre bord, et c'est lui qui empêche le remède d'être pire que le mal : une
    coupure de quelques secondes arrive tous les jours, sur les sept boîtiers. Si le premier
    échec déclenchait un instantané, le parc entier se mettrait à battre — et `snapshot()`
    coûte jusqu'à 20 s de collecte.

    Le seuil est celui de l'ERREUR, le même que `niveau_echec()` : un seul seuil, pas deux à
    garder d'accord."""
    for n in range(1, pub.ECHECS_ERREUR):
        assert not pub.signaler_echec(n, float("-inf"), 1000.0), f"échec n°{n} a signalé"


@cas
def une_panne_CONTINUE_ne_signale_qu_une_fois_par_heure():
    """🚨 Sans plancher, un boîtier en panne persistante enverrait un hello à CHAQUE tentative
    — soit un instantané toutes les 300 s au plafond du backoff, avec jusqu'à 20 s de collecte
    à chaque fois. Un boîtier coupé du monde doit signaler, pas battre."""
    t0 = 100_000.0
    assert pub.signaler_echec(99, float("-inf"), t0), "le premier signalement doit partir"
    # Juste après, et même bien plus tard dans l'heure : silence.
    assert not pub.signaler_echec(99, t0, t0 + 1)
    assert not pub.signaler_echec(99, t0, t0 + pub.HELLO_SUR_ECHEC_S - 1)
    # À l'échéance : on signale de nouveau.
    assert pub.signaler_echec(99, t0, t0 + pub.HELLO_SUR_ECHEC_S)


@cas
def le_signalement_part_APRES_la_ligne_de_journal():
    """🚨 L'ORDRE EST LE FOND, PAS LA FORME. `snapshot()` LIT le journal. Envoyé avant le
    `log.log`, le hello partirait avec un instantané qui ne contient pas l'échec qui l'a
    déclenché : un signalement qui ne signale rien — exactement le défaut de `N_PUB = 5`,
    une sonde qui rend des données valides et vides de sens.

    ⚖️ Cas STRUCTUREL, et il n'a pas le choix de l'être : les deux ordres produisent un hello
       valide, un code de retour identique et aucune erreur. Seul le CONTENU diffère, et il
       ne diffère que sur une vraie cible. On lit donc la source."""
    # 🚨 ON VÉRIFIE CHAQUE BRANCHE, PAS LA PREMIÈRE VENUE. Première version de ce cas : elle
    #    prenait le premier `log.log(...)` et le premier `signaler_echec(` du corps de boucle.
    #    Quand la garde « erreur de base locale » a été ajoutée AVANT la branche serveur, son
    #    propre `signaler_echec` passait devant le `log.log` de l'autre branche, et le cas
    #    tombait pour une mauvaise raison. Il y a maintenant DEUX chemins qui signalent, et
    #    chacun doit journaliser d'abord.
    src = pathlib.Path(pub.__file__).read_text()
    corps = src[src.index("    while not _stop:"):]
    branches = ["        except" + b for b in corps.split("\n        except")[1:]]
    vus = 0
    for b in branches:
        if "signaler_echec(" not in b:
            continue
        vus += 1
        i_log = min((b.index(m) for m in ("log.log(", "log.error(", "log.warning(")
                     if m in b), default=-1)
        assert i_log >= 0, f"une branche signale sans rien journaliser :\n{b[:200]}"
        assert i_log < b.index("signaler_echec("), (
            "le hello de signalement est envoyé AVANT que l'échec soit journalisé : "
            f"l'instantané ne contiendra pas la raison qui l'a déclenché\n{b[:200]}")
    assert vus >= 2, (
        f"{vus} branche(s) de signalement trouvée(s) : il doit y en avoir au moins deux — "
        "l'échec SERVEUR et l'erreur de BASE LOCALE, qui ne se confondent pas")


@cas
def le_signalement_repousse_le_battement_QUOTIDIEN():
    """⚠️ Le hello de signalement doit réarmer `prochain_hello`, sinon le battement quotidien
    vient se superposer au signalement et on paie deux collectes pour une information.

    ⚖️ Structurel pour la même raison : les deux versions marchent."""
    # 🚨 ON ANCRE SUR L'APPEL, PAS SUR LE NOM. Première version de ce cas : elle cherchait
    #    `signaler_echec(echecs`, qui matche d'abord la DÉFINITION de la fonction, deux cents
    #    lignes plus haut. Le bloc extrait englobait alors tout `main()` jusqu'au premier
    #    `cli.close()`, y compris le `prochain_hello = hello()` d'AVANT la boucle — le cas
    #    passait donc même avec le défaut en place. Vérifié par mutation : il ne tombait pas.
    src = pathlib.Path(pub.__file__).read_text()
    corps = src[src.index("    while not _stop:"):]
    bloc = corps[corps.index("if signaler_echec("):]
    bloc = bloc[:bloc.index("cli.close()")]
    assert "prochain_hello = hello()" in bloc, (
        "le signalement n'affecte pas `prochain_hello` — le hello quotidien se superposera "
        f"à l'instantané de panne. Bloc lu : {bloc!r}")


@cas
def RIEN_A_ENVOYER_s_ecrit_dans_le_journal():
    """🚨 LE SILENCE QUI REND LES DEUX PANNES INDISCERNABLES.

    La branche « rien à envoyer » était en `log.debug`. Le niveau racine est `INFO`, donc elle
    n'écrivait RIEN. Conséquence mesurée sur un boîtier du parc : un publisher qui échoue en
    boucle et un publisher qui n'a rien à envoyer laissaient exactement la même trace —
    aucune. Neuf jours de diagnostic sans pouvoir séparer les deux cas.

    ⭐ Et le retard doit voyager avec, parce que c'est la CONTRADICTION qui informe :
      « rien à envoyer · reste ~569526 » dit en une ligne que `pending` et la réalité ne
      s'accordent pas. Les deux chiffres séparés ne disaient rien.

    ⚠️ Gratuit sur un boîtier sain : à 0,74 point/s et une période de 60 s, chaque tour porte
       ~44 points — cette branche n'y est jamais atteinte.

    ⚖️ Cas STRUCTUREL : `log.debug` et `log.info` s'exécutent tous deux sans erreur et rendent
       `None`. Seul le NIVEAU diffère, et il ne se voit que dans un vrai journal."""
    src = pathlib.Path(pub.__file__).read_text()
    corps = src[src.index("    while not _stop:"):]

    # L'appel qui PORTE le message : on part du message et on remonte au `log.` le plus proche.
    i = corps.index('"rien à envoyer')
    debut = corps.rindex("log.", 0, i)
    appel = corps[debut:corps.index(")", i) + 1]

    assert appel.startswith("log.info("), (
        f"« rien à envoyer » est journalisé par {appel.split('(')[0]!r} : avec un niveau "
        "racine à INFO, un `debug` n'écrit RIEN et cette branche redevient muette")
    assert "pending_approx" in appel, (
        f"la ligne ne porte pas le retard : {appel!r} — sans lui elle ne contredit rien")


@cas
def les_DEUX_compteurs_repartent_de_zero_apres_un_lot_reussi():
    """🚨 Défaut trouvé en revue. `echecs` repartait de zéro, `echecs_base` NON — il comptait
    donc tous les incidents depuis le démarrage et non les CONSÉCUTIFS. Deux conséquences :
    la ligne « %d fois de suite » mentait, et une fois le seuil franchi un `database is
    locked` isolé — qui est NORMAL, le lecteur écrit en continu — déclenchait un signalement.

    ⭐ Un lot qui passe PROUVE que la base se lit : les deux compteurs doivent tomber.

    ⚖️ Cas STRUCTUREL : les deux versions publient, rendent le même code et ne lèvent pas.
       Seule une longue suite d'incidents espacés les distingue, et on ne peut pas la jouer
       dans un banc."""
    src = pathlib.Path(pub.__file__).read_text()
    corps = src[src.index("    while not _stop:"):]
    succes = corps[corps.index("if 200 <= status < 300:"):]
    succes = succes[:succes.index("elif status == 403:")]
    succes = "\n".join(l for l in succes.splitlines() if not l.lstrip().startswith("#"))
    assert "echecs = 0" in succes, "le compteur SERVEUR ne repart pas de zéro"
    assert "echecs_base = 0" in succes, (
        "le compteur de la BASE LOCALE ne repart pas de zéro après un lot réussi : il "
        "comptera tous les incidents depuis le démarrage, et un `database is locked` isolé "
        "finira par déclencher un signalement")


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
