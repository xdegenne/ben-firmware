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
    assert [x["uid"] for x in access.list_access(c)] == ["uid_marc"]


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


# ── La boîte d'envoi ─────────────────────────────────────────────────────────

@cas
def une_ligne_nee_localement_part_a_remonter():
    c = neuf()
    access.mint(c, uid="uid_marc", role=access.ROLE_OWNER)
    assert [p["uid"] for p in access.pending(c)] == ["uid_marc"]
    access.mark_sent(c, ["uid_marc"])
    assert access.pending(c) == []


@cas
def mark_sent_ne_marque_que_les_acquittees():
    c = neuf()
    access.mint(c, uid="uid_marc", role=access.ROLE_OWNER)
    access.mint(c, uid="uid_claire", role=access.ROLE_MEMBER)
    access.mark_sent(c, ["uid_marc"])
    assert [p["uid"] for p in access.pending(c)] == ["uid_claire"]


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
    assert [p["uid"] for p in access.pending(publisher)] == ["uid_marc"]
    access.mark_sent(publisher, ["uid_marc"])
    assert access.pending(api) == []


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
