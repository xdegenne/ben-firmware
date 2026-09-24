#!/usr/bin/env python3
"""Banc du magasin d'accès — les garde-fous, pas les accesseurs.

Chaque cas correspond à une décision du chantier `docs/chantier-acces-multi-utilisateur.md`,
et à un défaut qu'on aurait pu écrire sans s'en apercevoir :

  · une INVITATION en attente ne doit JAMAIS ouvrir /live. Elle a `token_hash = NULL`,
    et `NULL = ?` est faux en SQL — le garde-fou est gratuit, ce test vérifie qu'il l'est
    RESTÉ (une future requête en `OR invitation_hash = ?` le détruirait en silence) ;
  · frapper ne révoque pas : un nouveau téléphone laisse l'ancien vivant, c'est VOULU
    (deux étages), et c'est ce qui rend le ménage nécessaire ;
  · révoquer une PERSONNE doit emporter ses jetons — sinon on a l'illusion d'avoir coupé ;
  · l'owner est en ÉCRITURE UNIQUE, sans chemin de secours ;
  · une ligne née localement part à `sent = 0`, et n'est marquée qu'une fois ACQUITTÉE.

    python3 src/pi/store/test_access.py
"""
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import access  # noqa: E402

CAS = []


def cas(fn):
    CAS.append(fn)
    return fn


def neuf():
    return access.connect(str(pathlib.Path(tempfile.mkdtemp()) / "access.db"))


# ── Le chemin chaud ──────────────────────────────────────────────────────────

@cas
def un_jeton_frappe_ouvre_avec_son_role():
    c = neuf()
    t = access.mint(c, uid="uid_marc", label="iPhone", role=access.ROLE_OWNER)
    assert access.role_of(c, t) == access.ROLE_OWNER
    assert access.role_of(c, "n'importe quoi") is None
    assert access.role_of(c, None) is None
    assert access.role_of(c, "") is None


@cas
def le_clair_n_est_jamais_stocke():
    c = neuf()
    t = access.mint(c, uid="uid_marc", label="iPhone")
    brut = "".join(str(r) for r in c.execute("SELECT * FROM token").fetchall())
    assert t not in brut, "le jeton EN CLAIR se retrouve dans la base"


# ── L'invitation : le garde-fou qui doit rester gratuit ──────────────────────

@cas
def une_invitation_en_attente_n_ouvre_rien():
    c = neuf()
    inv = access.create_invitation(c, role=access.ROLE_MEMBER)
    assert access.role_of(c, inv) is None, \
        "une invitation est acceptée comme Bearer — le NULL ne protège plus"


@cas
def consommer_une_invitation_frappe_un_vrai_jeton():
    c = neuf()
    inv = access.create_invitation(c, role=access.ROLE_MEMBER)
    t = access.consume_invitation(c, inv, uid="uid_claire", label="Pixel")
    assert t is not None and t != inv
    assert access.role_of(c, t) == access.ROLE_MEMBER
    assert access.role_of(c, inv) is None, "l'invitation reste utilisable après coup"
    assert [a["uid"] for a in access.list_access(c)] == ["uid_claire"]


@cas
def une_invitation_ne_se_consomme_qu_une_fois():
    c = neuf()
    inv = access.create_invitation(c)
    assert access.consume_invitation(c, inv, uid="uid_claire") is not None
    assert access.consume_invitation(c, inv, uid="uid_intrus") is None


@cas
def une_invitation_perimee_est_refusee():
    c = neuf()
    inv = access.create_invitation(c, ttl_sec=-1)
    assert access.consume_invitation(c, inv, uid="uid_claire") is None


@cas
def le_role_vient_de_l_invitation_pas_du_demandeur():
    c = neuf()
    inv = access.create_invitation(c, role=access.ROLE_MEMBER)
    t = access.consume_invitation(c, inv, uid="uid_claire")
    assert access.role_of(c, t) == access.ROLE_MEMBER
    assert not access.has_owner(c), "l'invitée s'est promue owner"


# ── Deux étages ──────────────────────────────────────────────────────────────

@cas
def frapper_ne_revoque_pas():
    c = neuf()
    vieux = access.mint(c, uid="uid_marc", label="iPhone 15")
    neufj = access.mint(c, uid="uid_marc", label="iPhone 16")
    assert access.role_of(c, vieux) is not None, \
        "l'ancien jeton est tombé — la révocation ciblée devient impossible"
    assert access.role_of(c, neufj) is not None
    assert len(access.list_tokens(c)) == 2


@cas
def revoquer_un_jeton_laisse_les_autres():
    c = neuf()
    tel = access.mint(c, uid="uid_marc", label="iPhone")
    tab = access.mint(c, uid="uid_marc", label="iPad")
    perdu = [t for t in access.list_tokens(c) if t["label"] == "iPhone"][0]
    assert access.revoke_token(c, perdu["id"]) == 1
    assert access.role_of(c, tel) is None
    assert access.role_of(c, tab) is not None


@cas
def revoquer_une_personne_emporte_ses_jetons():
    c = neuf()
    access.mint(c, uid="uid_marc", label="iPhone de Marc", role=access.ROLE_OWNER)
    a = access.mint(c, uid="uid_claire", label="Pixel")
    b = access.mint(c, uid="uid_claire", label="tablette")
    access.revoke_person(c, "uid_claire")
    assert access.role_of(c, a) is None and access.role_of(c, b) is None, \
        "un jeton survit à la révocation de la personne — illusion d'avoir coupé"
    # ⚠️ La ligne de Claire RESTE, marquée. Elle disparaissait avant le 24/09 —
    #    et une ligne supprimée ne remonte jamais au cloud, qui gardait donc son
    #    droit indéfiniment. C'est le drapeau qui porte la nouvelle au hello.
    lignes = {x["uid"]: x for x in access.list_access(c)}
    assert set(lignes) == {"uid_marc", "uid_claire"}
    assert lignes["uid_claire"]["revoked_ts"] is not None
    assert lignes["uid_marc"]["revoked_ts"] is None


# ── L'owner ──────────────────────────────────────────────────────────────────

@cas
def l_owner_est_en_ecriture_unique():
    c = neuf()
    access.mint(c, uid="uid_marc", role=access.ROLE_OWNER)
    assert access.has_owner(c)
    try:
        access.grant(c, "uid_intrus", access.ROLE_OWNER)
    except ValueError:
        pass
    else:
        raise AssertionError("un second owner a été posé")
    assert [x["uid"] for x in access.list_access(c)] == ["uid_marc"]


@cas
def un_owner_ne_se_fait_pas_retrograder():
    c = neuf()
    access.mint(c, uid="uid_marc", role=access.ROLE_OWNER)
    access.grant(c, "uid_marc", access.ROLE_MEMBER)
    assert access.list_access(c)[0]["role"] == access.ROLE_OWNER


# ── Une intégration n'est pas une personne ───────────────────────────────────

@cas
def une_integration_n_a_pas_de_ligne_access():
    c = neuf()
    t = access.mint(c, label="Home Assistant", role=access.ROLE_VIEWER)
    assert access.role_of(c, t) == access.ROLE_VIEWER
    assert access.list_access(c) == [], \
        "une intégration a fabriqué une ligne sans titulaire dans device_access"


# ── La révocation ────────────────────────────────────────────────────────────
#
# 🚨 Ces trois cas gardent la seule chose qui compte : qu'une personne coupée le
#    soit VRAIMENT. Un drapeau écrit et pas lu serait pire que pas de drapeau —
#    il donnerait l'illusion d'avoir coupé quelqu'un.

@cas
def revoquer_tue_les_jetons_ET_marque_la_personne():
    c = neuf()
    j = access.mint(c, uid="uid_claire", role=access.ROLE_MEMBER)
    assert access.role_of(c, j) == access.ROLE_MEMBER
    access.revoke_person(c, "uid_claire")
    # Le jeton ne vaut plus rien…
    assert access.role_of(c, j) is None
    # …et la LIGNE survit, marquée : c'est elle qui portera la nouvelle au cloud.
    ligne = [a for a in access.list_access(c) if a["uid"] == "uid_claire"]
    assert ligne and ligne[0]["revoked_ts"] is not None, \
        "la ligne doit rester pour que la révocation remonte au hello"


@cas
def un_jeton_survivant_ne_vaut_RIEN_si_la_personne_est_revoquee():
    """⭐ Le refus est STRUCTUREL, pas conditionné à la réussite du ménage.

    `revoke_person` supprime les jetons — mais faire dépendre la sécurité d'un
    ménage réussi, c'est parier. Ici on simule un jeton qui a survécu."""
    c = neuf()
    j = access.mint(c, uid="uid_claire", role=access.ROLE_MEMBER)
    c.execute("UPDATE access SET revoked_ts = 1 WHERE uid = 'uid_claire'")
    c.commit()
    assert access.role_of(c, j) is None


@cas
def reinviter_quelqu_un_leve_le_drapeau():
    c = neuf()
    access.mint(c, uid="uid_claire", role=access.ROLE_MEMBER)
    access.revoke_person(c, "uid_claire")
    j2 = access.mint(c, uid="uid_claire", role=access.ROLE_MEMBER)
    assert access.role_of(c, j2) == access.ROLE_MEMBER, \
        "sans lever le drapeau, un jeton tout neuf serait refusé sans raison visible"


# ── Le code d'invitation, tel qu'il est TAPÉ ─────────────────────────────────
#
# ⭐ Un code qu'on épelle au téléphone est retapé de travers : minuscules, tiret
#    oublié, « O » dit pour un zéro. Un code refusé sans raison visible coûte
#    plus cher qu'un code plus long.

@cas
def le_code_emis_ne_contient_aucun_caractere_ambigu():
    c = neuf()
    for _ in range(200):
        code = access.create_invitation(c)
        assert not (set(code) & set("ILO")), f"caractère ambigu émis : {code}"


@cas
def un_code_tape_de_travers_marche_quand_meme():
    c = neuf()
    code = access.create_invitation(c)
    # Tel qu'on le montre à l'écran, retapé en minuscules avec des espaces.
    saisi = " " + access.formater_code(code).lower() + " "
    j = access.consume_invitation(c, saisi, uid="uid_claire")
    assert j is not None, "le code affiché doit être acceptable tel qu'on le lit"


@cas
def les_confusions_classiques_sont_rattrapees():
    """O dit pour zéro, I ou L pour un. On ne les émet jamais, on les rattrape."""
    # "O0-I1L" → majuscules → O→0, I→1, L→1 → tiret jeté → "00111"
    assert access.normaliser_code("o0-i1l") == "00111"
    assert access.normaliser_code("4k7-qmx") == "4K7QMX"
    assert access.normaliser_code("4K7QMX") == "4K7QMX"
    # Un caractère hors alphabet est jeté, pas traduit au hasard.
    assert access.normaliser_code("4K7@QMX ") == "4K7QMX"


@cas
def revoquee_puis_reinvitee_le_cycle_complet():
    """⭐ La question qui a révélé le trou : que se passe-t-il si on révoque
    quelqu'un puis qu'on le réinvite ?

    🚨 Le piège est la FENÊTRE : le hello est quotidien, donc pendant 24 h le
    cloud répond encore l'ancien rôle. `est_revoquee` est ce qui empêche le
    boîtier de se laisser contredire sur sa propre décision.
    """
    c = neuf()
    access.mint(c, uid="uid_marc", role=access.ROLE_OWNER)
    j1 = access.mint(c, uid="uid_claire", role=access.ROLE_MEMBER)

    access.revoke_person(c, "uid_claire")
    assert access.est_revoquee(c, "uid_claire"), \
        "sans ce drapeau, le boîtier croirait le cloud, qui est en retard de 24 h"
    assert access.role_of(c, j1) is None

    # Marc la réinvite : le QR lève le drapeau, et SEULEMENT lui.
    inv = access.create_invitation(c, role=access.ROLE_MEMBER)
    j2 = access.consume_invitation(c, inv, uid="uid_claire", label="Pixel neuf")
    assert j2 is not None
    assert not access.est_revoquee(c, "uid_claire")
    assert access.role_of(c, j2) == access.ROLE_MEMBER


@cas
def un_jeton_d_integration_SANS_uid_reste_valable():
    """⚠️ Home Assistant n'a pas d'uid, donc aucune ligne `access`. La jointure
    de `role_of` doit être un LEFT JOIN — un JOIN le ferait disparaître."""
    c = neuf()
    j = access.mint(c, label="Home Assistant", role=access.ROLE_VIEWER)
    assert access.role_of(c, j) == access.ROLE_VIEWER


@cas
def elaguer_ne_supprime_JAMAIS_le_jeton_qu_on_vient_de_rendre():
    """🚨 LE CAS QUI CASSAIT `MAX(id)`. `consume_invitation` TRANSFORME la ligne
    de l'invitation : son numéro date de la frappe du code, pas de son usage.
    Un jeton obtenu entre les deux porte un numéro PLUS GRAND — et l'élagage
    aurait supprimé celui qu'on vient tout juste d'accorder."""
    c = neuf()
    access.grant(c, "uid_claire", access.ROLE_MEMBER)
    code = access.create_invitation(c, role=access.ROLE_MEMBER)   # ligne n
    entre_temps = access.mint(c, uid="uid_claire", label="iPhone",
                              role=access.ROLE_MEMBER)            # ligne n+1
    frais = access.consume_invitation(c, code, uid="uid_claire", label="iPhone")
    assert frais is not None

    access.elaguer_doublons(c, "uid_claire", "iPhone", garder=frais)
    assert access.role_of(c, frais) == access.ROLE_MEMBER, \
        "le jeton tout juste accordé a été supprimé"
    assert access.role_of(c, entre_temps) is None, "l'ancien survit"


@cas
def sans_le_jeton_a_garder_on_ne_supprime_RIEN():
    """⚖️ TÉMOIN. Mieux vaut une ligne en trop qu'un accès coupé : si on ne
    sait pas lequel garder, on ne touche à rien."""
    c = neuf()
    a = access.mint(c, uid="uid_xav", label="iPhone", role=access.ROLE_OWNER)
    access.mint(c, uid="uid_xav", label="iPhone", role=access.ROLE_OWNER)
    assert access.elaguer_doublons(c, "uid_xav", "iPhone", garder=None) == 0
    assert access.elaguer_doublons(c, "uid_xav", "iPhone", garder="inconnu") == 0
    assert access.role_of(c, a) is not None


@cas
def desappairer_efface_TOUT_et_rouvre_la_place_a_un_owner():
    """🚨 `grant` est en écriture unique pour `owner`. Un boîtier désapparié qui
    garderait sa ligne refuserait le propriétaire SUIVANT, définitivement."""
    c = neuf()
    j_xav = access.mint(c, uid="uid_xav", label="iPhone", role=access.ROLE_OWNER)
    access.mint(c, uid="uid_claire", label="Pixel", role=access.ROLE_MEMBER)
    integration = access.mint(c, label="Home Assistant", role=access.ROLE_MEMBER)

    acces, jetons = access.tout_effacer(c)
    assert (acces, jetons) == (2, 3), (acces, jetons)
    assert access.list_access(c) == [] and access.list_tokens(c) == []
    assert access.role_of(c, j_xav) is None
    assert access.role_of(c, integration) is None, "l'intégration a survécu"

    # ⭐ CE QUI COMPTE VRAIMENT : la place est libre.
    access.grant(c, "uid_nouveau", access.ROLE_OWNER)
    assert [r["uid"] for r in access.list_access(c)] == ["uid_nouveau"]


@cas
def sans_effacement_le_prochain_proprietaire_est_REFUSE():
    """⚖️ LE TÉMOIN. Sans lui, le test ci-dessus ne prouve pas que l'effacement
    sert à quelque chose — un `grant` qui accepterait toujours donnerait le même
    résultat."""
    c = neuf()
    access.grant(c, "uid_xav", access.ROLE_OWNER)
    try:
        access.grant(c, "uid_nouveau", access.ROLE_OWNER)
    except ValueError:
        return
    raise AssertionError("grant a accepté un SECOND owner")


# ── Le prénom reste au boîtier ───────────────────────────────────────────────

@cas
def nommer_puis_relire():
    c = neuf()
    access.grant(c, "uid_claire", access.ROLE_MEMBER)
    assert access.nommer(c, "uid_claire", "  Claire  ") is True
    ligne = [r for r in access.list_access(c) if r["uid"] == "uid_claire"][0]
    assert ligne["nom"] == "Claire", ligne["nom"]


@cas
def un_nom_vide_efface_au_lieu_de_stocker_du_vide():
    """Sinon l'écran affiche une chaîne invisible au lieu de retomber sur
    « Membre », et la ligne paraît sans libellé du tout."""
    c = neuf()
    access.grant(c, "uid_claire", access.ROLE_MEMBER)
    access.nommer(c, "uid_claire", "Claire")
    access.nommer(c, "uid_claire", "   ")
    assert [r for r in access.list_access(c) if r["uid"] == "uid_claire"][0]["nom"] is None


@cas
def le_chemin_automatique_n_ecrase_PAS_la_correction_du_proprietaire():
    """🚨 Un téléphone se re-revendique tout seul. S'il réécrivait le prénom à
    chaque fois, « ffff » corrigé en « Claire » redeviendrait « ffff » dans les
    secondes qui suivent."""
    c = neuf()
    access.grant(c, "uid_claire", access.ROLE_MEMBER)
    access.nommer(c, "uid_claire", "ffff", seulement_si_vide=True)
    access.nommer(c, "uid_claire", "Claire")          # le propriétaire corrige
    access.nommer(c, "uid_claire", "ffff", seulement_si_vide=True)  # re-claim
    assert [r for r in access.list_access(c)
            if r["uid"] == "uid_claire"][0]["nom"] == "Claire"


@cas
def le_chemin_automatique_remplit_quand_c_est_vide():
    """⚖️ TÉMOIN de la précédente : un garde qui bloque TOUT serait invisible
    dans le test ci-dessus, puisqu'il donnerait le même résultat."""
    c = neuf()
    access.grant(c, "uid_xav", access.ROLE_OWNER)
    assert access.nommer(c, "uid_xav", "Xavier", seulement_si_vide=True) is True
    assert [r for r in access.list_access(c)][0]["nom"] == "Xavier"


@cas
def nommer_un_inconnu_ne_cree_AUCUNE_ligne():
    """⚖️ TÉMOIN. Nommer ne doit pas être une porte d'écriture détournée sur la
    table des DROITS."""
    c = neuf()
    assert access.nommer(c, "uid_fantome", "Pirate") is False
    assert access.list_access(c) == []


@cas
def le_hello_ne_transporte_JAMAIS_le_prenom():
    """🔒 LE TÉMOIN QUI COMPTE. Le prénom est une donnée personnelle qui n'a
    aucune raison de monter au cloud : il ne sert qu'à rendre un écran lisible.
    On vérifie le contenu EXACT, pas seulement l'absence du mot « nom » — une
    clé ajoutée par distraction doit faire rougir le banc."""
    c = neuf()
    access.grant(c, "uid_claire", access.ROLE_MEMBER)
    access.nommer(c, "uid_claire", "Claire")
    charge = access.pour_le_hello(c)
    assert charge == [{"uid": "uid_claire", "role": access.ROLE_MEMBER,
                       "revoked": False}], charge
    assert "Claire" not in repr(charge)


# ── Réinstallation : la clé qui traîne ───────────────────────────────────────

@cas
def reinstaller_ne_laisse_pas_le_jeton_precedent_vivant():
    """Une réinstallation emporte le coffre du téléphone : l'app n'a plus de
    jeton et se revendique. L'ancien resterait VALIDE, détenu par personne."""
    c = neuf()
    vieux = access.mint(c, uid="uid_xav", label="Xiaomi 24094RAD4G",
                        role=access.ROLE_OWNER)
    neuf_ = access.mint(c, uid="uid_xav", label="Xiaomi 24094RAD4G",
                        role=access.ROLE_OWNER)
    assert access.role_of(c, vieux) is not None, "témoin : le vieux vit encore"

    assert access.elaguer_doublons(c, "uid_xav", "Xiaomi 24094RAD4G",
                                   garder=neuf_) == 1
    assert access.role_of(c, vieux) is None, "le vieux jeton ouvre ENCORE"
    assert access.role_of(c, neuf_) == access.ROLE_OWNER, "le neuf a sauté"


@cas
def elaguer_ne_touche_NI_les_autres_appareils_NI_les_integrations():
    """⚖️ LE TÉMOIN QUI COMPTE. Un élagage qui emporterait l'iPhone, l'invité ou
    Home Assistant serait pire que le désordre qu'il nettoie."""
    c = neuf()
    autre_modele = access.mint(c, uid="uid_xav", label="iPhone de Xavier",
                               role=access.ROLE_OWNER)
    invite = access.mint(c, uid="uid_claire", label="Xiaomi 24094RAD4G",
                         role=access.ROLE_MEMBER)
    integration = access.mint(c, label="Home Assistant", role=access.ROLE_MEMBER)
    access.mint(c, uid="uid_xav", label="Xiaomi 24094RAD4G", role=access.ROLE_OWNER)
    dernier = access.mint(c, uid="uid_xav", label="Xiaomi 24094RAD4G",
                          role=access.ROLE_OWNER)

    assert access.elaguer_doublons(c, "uid_xav", "Xiaomi 24094RAD4G",
                                   garder=dernier) == 1
    assert access.role_of(c, autre_modele) is not None, "autre libellé emporté"
    assert access.role_of(c, invite) is not None, "autre personne emportée"
    assert access.role_of(c, integration) is not None, "intégration emportée"


@cas
def elaguer_sur_un_seul_jeton_ne_fait_RIEN():
    """Le cas courant. Un élagage qui se déclencherait à vide finirait par
    couper le seul jeton existant — c'est exactement l'erreur d'indice à ne pas
    commettre dans le `id < MAX(id)`."""
    c = neuf()
    seul = access.mint(c, uid="uid_xav", label="iPhone", role=access.ROLE_OWNER)
    assert access.elaguer_doublons(c, "uid_xav", "iPhone", garder=seul) == 0
    assert access.role_of(c, seul) == access.ROLE_OWNER


@cas
def elaguer_sans_uid_ne_peut_pas_faucher_les_integrations():
    """⚖️ Deux intégrations peuvent porter le même nom. Aucune n'a d'uid : un
    élagage déclenché sur `uid=""` ne doit rien pouvoir faire."""
    c = neuf()
    a = access.mint(c, label="Home Assistant", role=access.ROLE_MEMBER)
    b = access.mint(c, label="Home Assistant", role=access.ROLE_MEMBER)
    assert access.elaguer_doublons(c, "", "Home Assistant", garder=a) == 0
    assert access.role_of(c, a) is not None and access.role_of(c, b) is not None


# ── L'écran d'administration ─────────────────────────────────────────────────

@cas
def la_liste_n_expose_aucune_empreinte():
    c = neuf()
    access.mint(c, uid="uid_marc", label="iPhone")
    for t in access.list_tokens(c):
        assert "token_hash" not in t and "invitation_hash" not in t
        assert isinstance(t["id"], int), "sans `id`, révoquer depuis un écran est impossible"


@cas
def une_invitation_n_apparait_pas_comme_un_appareil():
    c = neuf()
    access.create_invitation(c)
    assert access.list_tokens(c) == []


@cas
def last_used_ts_se_pose_a_l_usage():
    c = neuf()
    t = access.mint(c, uid="uid_marc", label="iPhone")
    assert access.list_tokens(c)[0]["last_used_ts"] is None
    access.role_of(c, t)
    assert access.list_tokens(c)[0]["last_used_ts"] >= int(time.time()) - 2


@cas
def une_base_verrouillee_n_empeche_pas_d_autoriser():
    """Le publisher écrit dans la même base depuis un AUTRE processus. S'il tient
    le verrou, la mise à jour cosmétique de `last_used_ts` doit être ABANDONNÉE,
    jamais propagée : une autorisation ne rate pas pour une colonne d'affichage."""
    chemin = str(pathlib.Path(tempfile.mkdtemp()) / "access.db")
    api, autre = access.connect(chemin), access.connect(chemin)
    t = access.mint(api, uid="uid_marc", label="iPhone")
    autre.execute("BEGIN EXCLUSIVE")          # simule le publisher en pleine écriture
    try:
        assert access.role_of(api, t) is not None, \
            "l'autorisation échoue quand la base est occupée"
    finally:
        autre.rollback()


@cas
def deux_connexions_voient_les_memes_lignes():
    """Le défaut que SQLite corrige : `local_api` frappe, `ben_publisher` remonte.
    Avec un JSON, l'un écrasait l'autre en silence."""
    chemin = str(pathlib.Path(tempfile.mkdtemp()) / "access.db")
    api, publisher = access.connect(chemin), access.connect(chemin)
    access.mint(api, uid="uid_marc", role=access.ROLE_OWNER)
    assert [a["uid"] for a in access.list_access(publisher)] == ["uid_marc"]
    access.revoke_person(api, "uid_marc")
    assert access.list_access(publisher)[0]["revoked_ts"] is not None


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
