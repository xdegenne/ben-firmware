#!/usr/bin/env python3
"""Banc du contrat de `/claim` — le TICKET, et jamais un jeton d'identité.

🚨 POURQUOI CE FICHIER EXISTE. Le contrat de `/claim` a été entièrement changé le
   05/10 — `{"firebase_token": …}` + `Authorization` sont devenus un `ticket` dans
   le corps — et LES 44 BANCS EXISTANTS SONT RESTÉS VERTS. Aucun ne couvrait ce
   contrat : `test_routage_ports` vérifie seulement que la garde laisse passer la
   route, pas ce qu'elle fait.

⭐ Un changement de contrat qui ne fait tomber aucun banc est un contrat que rien
   ne défend. Ce fichier défend les quatre invariants qui peuvent régresser en
   SILENCE — aucun ne produirait d'erreur visible, et les trois premiers
   renverraient le boîtier à l'état qu'on vient précisément de quitter.

Lancer : `cd src/pi/store && python3 test_claim_ticket.py`
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import access  # noqa: E402
import local_api  # noqa: E402

_ECHECS: list[str] = []


def cas(fn):
    try:
        fn()
        print(f"  ✅ {fn.__name__.replace('_', ' ')}")
    except AssertionError as e:
        print(f"  ❌ {fn.__name__.replace('_', ' ')}\n     {e}")
        _ECHECS.append(fn.__name__)
    return fn


def _serveur():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), local_api.Handler)
    srv.chiffre = True            # /claim n'est servi qu'en chiffré
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _post(base, corps: dict, entetes: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(base + "/claim", data=json.dumps(corps).encode(),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in (entetes or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:  # noqa: BLE001
            return e.code, {}


def _base_neuve(avec_owner: bool = True):
    """Un magasin d'accès vierge, dans un répertoire temporaire.

    ⭐ `avec_owner=True` PAR DÉFAUT, et c'est l'état réaliste : un boîtier en service
    a un propriétaire. Sans lui, le verrou du premier propriétaire refuse tout
    `/claim` sans rôle — ce qui est son travail, mais masquerait ce que les autres
    cas veulent éprouver.

    🚨 ON PRÉ-REMPLIT LE SINGLETON, on ne réassigne pas `ACCESS_PATH`. `session()`
    déclare `path: str = ACCESS_PATH` : ce défaut est lié À LA DÉFINITION, donc
    changer le module après l'import n'a AUCUN effet — la première tentative de ce
    banc s'est écrasée sur `/var/lib/ben-firmware` (permission refusée), ce qui est
    la bonne nouvelle : elle a échoué FRANCHEMENT au lieu d'écrire quelque part.

    ⭐ Les autres bancs passent `conn` à la main et n'ont pas ce problème. Celui-ci
    appelle par HTTP, donc c'est `local_api` qui ouvre la session : il faut agir sur
    le singleton lui-même.
    """
    chemin = str(pathlib.Path(tempfile.mkdtemp()) / "access.db")
    access._shared = access.connect(chemin)
    # ⚠️ ET LE FREIN, qui est un état GLOBAL du module : sans cette remise à zéro,
    #    le deuxième cas de ce fichier recevait un 429 et le banc accusait F1 d'un
    #    défaut qui n'existait pas. Un banc qui échoue pour la mauvaise raison coûte
    #    autant qu'un banc qui passe pour la mauvaise raison.
    local_api._claim_dernier = 0.0
    if avec_owner:
        with access.session() as conn:
            access.mint(conn, uid="firebase:bob", label="pixel", role=access.ROLE_OWNER)
    return chemin


# ── ① Le jeton d'identité est REFUSÉ, franchement ────────────────────────────
#
# 🚨 C'est l'invariant central du correctif du 04-05/10 : le boîtier ne doit
#    JAMAIS recevoir d'`ID token` Firebase — un porteur valable ~1 h auprès de
#    tout le projet BEN, et l'attaquant réaliste est le propriétaire d'un boîtier
#    rooté contre ses propres invités.
#
# ⚠️ Refuser ne « dé-reçoit » pas le jeton : s'il arrive, il est déjà passé. Ce que
#    le refus garantit, c'est qu'une app ancienne échoue VISIBLEMENT au lieu de
#    croire que ça marche — et qu'on ne relaie rien.
@cas
def un_en_tete_Authorization_est_refuse_sans_rien_relayer():
    _base_neuve()
    relaye = []
    local_api._demander_au_cloud = lambda *a, **k: relaye.append(a) or ("x", "member")
    srv, base = _serveur()
    try:
        code, corps = _post(base, {"ticket": "peu-importe"},
                            {"Authorization": "Bearer un-jwt-quelconque"})
    finally:
        srv.shutdown()
    assert code == 400, f"statut {code}, attendu 400"
    assert corps.get("error") == "identity_not_accepted", f"erreur {corps!r}"
    # ⭐ LA MOITIÉ QUI COMPTE : rien n'est parti vers le cloud.
    assert not relaye, "le boîtier a relayé malgré l'en-tête refusé"


# ── ② Un /claim sans ticket ne relaie rien ───────────────────────────────────
@cas
def sans_ticket_rien_ne_part_vers_le_cloud():
    _base_neuve()
    relaye = []
    local_api._demander_au_cloud = lambda *a, **k: relaye.append(a) or ("x", "member")
    srv, base = _serveur()
    try:
        code, corps = _post(base, {"label": "iPhone"})
    finally:
        srv.shutdown()
    assert code == 400, f"statut {code}, attendu 400"
    assert corps.get("error") == "missing_ticket", f"erreur {corps!r}"
    assert not relaye, "un aller-retour mTLS pour une requête sans ticket"


# ── ③ LA CHARGE ENVOYÉE AU CLOUD — testée pour de vrai cette fois ────────────
#
# ⚠️ PREMIÈRE VERSION DE CE CAS : il interceptait `_demander_au_cloud` et vérifiait
#    ses ARGUMENTS. Il ne voyait donc jamais la charge JSON, construite À
#    L'INTÉRIEUR — et remettre `{"firebase_token": …}` dans le corps laissait le
#    banc VERT. Mesuré. Son propre commentaire décrivait le trou qu'il ne fermait
#    pas.
#
# ⭐ D'où `_charge_claim`, sortie de `_demander_au_cloud` pour être pure et donc
#    éprouvable sans rien simuler. C'est l'invariant central du chantier : le
#    boîtier transporte un TICKET, jamais une clé d'identité.
@cas
def la_charge_envoyee_au_cloud_ne_porte_QUE_le_ticket():
    charge = local_api._charge_claim("TICKET-ABC")
    assert charge == {"ticket": "TICKET-ABC", "fonder": False}, (
        f"charge {charge!r} — hors invitation : le ticket, et `fonder` TOUJOURS "
        f"présent (cf. le banc dédié)")
    # 🔒 Aucune clé qui puisse porter une identité, sous aucun nom.
    for interdit in ("firebase_token", "id_token", "token", "jwt", "authorization"):
        assert interdit not in charge, (
            f"la charge porte {interdit!r} : le boîtier transporterait une identité")


# ── ③ter 🚨 `fonder` EST TOUJOURS SÉRIALISÉ, MÊME À FALSE ────────────────────
#
# Et ce n'est pas du style : c'est ce qui rend l'ordre de livraison sûr DANS LES DEUX
# SENS. `ben-api` refuse les champs inconnus — mais cette garde ne se déclenche QUE SI
# LE CHAMP EST PRÉSENT.
#
# ⚠️ L'omettre quand il vaut `False`, comme on le fait pour `role`, laisserait un
#    firmware récent passer SANS BRUIT devant un cloud ancien : celui-ci accepterait
#    la charge, fonderait un premier propriétaire sans ordre, et le TOFU resterait
#    ouvert. C'est précisément le cas dangereux — l'intrus sur le LAN avec un compte
#    Google et un `deviceId` que mDNS diffuse.
#
# ⭐ Toujours présent, le champ fait échouer le cloud ancien en 400, donc
#    `contract_mismatch` (502) non réessayable : une incompatibilité VISIBLE au lieu
#    d'un trou de sécurité muet.
@cas
def fonder_est_toujours_present_dans_la_charge():
    for role_invit in ("", access.ROLE_MEMBER):
        for fonder in (False, True):
            charge = local_api._charge_claim("T", role_invit, fonder)
            assert "fonder" in charge, (
                f"`fonder` ABSENT (role_invit={role_invit!r}, fonder={fonder}) — un "
                f"cloud ancien accepterait la charge en silence, et le TOFU resterait "
                f"ouvert")
            assert charge["fonder"] is fonder, f"fonder={charge['fonder']!r}"
    # ⓘ L'asymétrie avec `role` est VOULUE : son absence ne crée aucun droit, sa
    #    présence est ce qui demande quelque chose.
    assert "role" not in local_api._charge_claim("T", "", False)


@cas
def le_role_part_SEULEMENT_sur_le_chemin_de_l_invitation():
    assert local_api._charge_claim("T", access.ROLE_MEMBER) == {
        "ticket": "T", "fonder": False, "role": access.ROLE_MEMBER}, \
        "le rôle de l'invitation n'est pas transmis"
    assert "role" not in local_api._charge_claim("T", ""), (
        "un rôle part hors invitation — le boîtier déclarerait un droit que "
        "personne ne lui a donné")


# ── ③bis Et le relais passe bien le ticket reçu, sans le réécrire ────────────
@cas
def le_ticket_recu_est_transporte_tel_quel():
    _base_neuve()
    vus = []

    def faux_cloud(ticket, role_invitation=""):
        vus.append((ticket, role_invitation))
        return "firebase:claire", access.ROLE_MEMBER

    local_api._demander_au_cloud = faux_cloud
    srv, base = _serveur()
    try:
        code, corps = _post(base, {"ticket": "TICKET-ABC", "label": "iPhone"})
    finally:
        srv.shutdown()
    assert code == 200, f"statut {code}, corps {corps!r}"
    assert corps.get("token"), "aucun jeton local rendu"
    assert vus == [("TICKET-ABC", "")], (
        f"le boîtier a transporté {vus!r} — attendu le ticket seul")


# ── ④ F1 — une divergence d'owner ne bloque plus à vie ───────────────────────
#
# 🚨 Constat F1 de la revue 01. `grant` refuse un second owner en LEVANT. Si le
#    cloud dit `owner` pour cet uid alors qu'un AUTRE est owner ici, `mint`
#    propageait l'exception ⇒ 409 à CHAQUE tentative, lu « injoignable » par
#    l'app, et le seul recours était de désappairer le boîtier.
#
# ⚠️ Et avec le ticket, le cloud rend TOUJOURS un rôle : le chemin fautif serait
#    devenu systématique au lieu d'être un cas limite.
#
# ⭐ On retombe sur `member` : la personne entre, sans droit d'administration.
@cas
def un_owner_cloud_divergent_ne_bloque_plus_la_revendication():
    _base_neuve()
    with access.session() as conn:
        assert access.has_owner(conn), "le banc n'a pas posé d'owner — témoin MORT"

    local_api._demander_au_cloud = lambda *a, **k: ("firebase:claire", access.ROLE_OWNER)
    srv, base = _serveur()
    try:
        code, corps = _post(base, {"ticket": "T", "label": "iPhone"})
    finally:
        srv.shutdown()
    assert code == 200, (
        f"statut {code} ({corps!r}) — une divergence d'owner bloque encore la "
        f"revendication, c'est le défaut F1")
    assert corps.get("role") == access.ROLE_MEMBER, (
        f"rôle {corps.get('role')!r}, attendu member : on ne promeut pas un second "
        f"owner, on retombe")


# ── ⑤ L'invitation n'est PAS consommée si le cloud refuse ────────────────────
#
# ⚠️ Le rôle de l'invitation est lu AVANT l'aller-retour, pour être envoyé au
#    cloud. La consommer à ce moment-là brûlerait le bon de droit d'un tiers sur un
#    échec qui ne le concerne pas.
@cas
def une_invitation_survit_a_un_refus_du_cloud():
    _base_neuve()
    with access.session() as conn:
        code_invit = access.create_invitation(conn, role=access.ROLE_MEMBER)
        assert access.role_invitation(conn, code_invit) == access.ROLE_MEMBER, \
            "l'invitation n'est pas lisible — témoin MORT"

    def cloud_en_panne(*a, **k):
        raise local_api._CloudInjoignable("banc")

    local_api._demander_au_cloud = cloud_en_panne
    srv, base = _serveur()
    try:
        code, _ = _post(base, {"ticket": "T", "invitation": code_invit})
    finally:
        srv.shutdown()
    assert code == 503, f"statut {code}, attendu 503 (panne, pas refus)"
    with access.session() as conn:
        assert access.role_invitation(conn, code_invit) == access.ROLE_MEMBER, (
            "l'invitation a été consommée alors que le cloud était en panne — le "
            "bon de droit est perdu pour rien")


# ── ⑥ Le frein, testé pour lui-même ─────────────────────────────────────────
#
# ⭐ Il s'est fait remarquer en faisant tomber deux autres cas de ce fichier. Un
#    comportement qu'on découvre par accident mérite son propre banc : `/claim` est
#    NON AUTHENTIFIÉ par construction — il faut bien un chemin pour obtenir un
#    jeton — donc c'est le frein qui empêche un inconnu sur le LAN de marteler le
#    cloud à travers le boîtier.
#
# ⚠️ Non bloquant, délibérément : on refuse franchement plutôt que d'empiler des
#    threads en attente, ce qui serait exactement la panne qu'on veut éviter.
@cas
def le_frein_refuse_un_second_claim_immediat():
    _base_neuve()
    local_api._demander_au_cloud = lambda *a, **k: ("firebase:claire", access.ROLE_MEMBER)
    srv, base = _serveur()
    try:
        premier, _ = _post(base, {"ticket": "T1", "label": "iPhone"})
        second, corps = _post(base, {"ticket": "T2", "label": "iPad"})
    finally:
        srv.shutdown()
    assert premier == 200, f"le premier /claim a échoué ({premier})"
    assert second == 429, f"le second a rendu {second}, attendu 429 — le frein ne freine plus"
    assert corps.get("error") in ("too_many", "busy"), f"erreur {corps!r}"


# ── ⑦ 🚨 LA FAILLE : une invitation BIDON annulait une révocation ────────────
#
# Reproduite le 05/10. La garde testait la CHAÎNE BRUTE envoyée par l'app :
#
#     sans invitation      → 403 revoked
#     invitation BIDON "x" → 200 {"role": "member"}, et revoked_ts remis à NULL
#
# ⚠️ N'importe quel caractère sautait la garde. `role_invit` restait vide, donc le
#    code partait dans la branche « sans invitation » et `grant` levait le drapeau.
#
# 🚨 ET LE CONTOURNEMENT ÉTAIT PERMANENT : la ligne repart avec `sent=0`, donc le
#    battement suivant la RÉINSCRIT dans le cloud. Une révocation effacée pour de bon
#    par une chaîne de trois octets.
@cas
def une_invitation_bidon_n_annule_pas_une_revocation():
    _base_neuve()
    with access.session() as conn:
        access.mint(conn, uid="firebase:claire", label="iPhone", role=access.ROLE_MEMBER)
        access.revoke_person(conn, "firebase:claire")
        assert access.est_revoquee(conn, "firebase:claire"), "témoin MORT : pas révoquée"

    local_api._demander_au_cloud = lambda *a, **k: ("firebase:claire", access.ROLE_MEMBER)
    srv, base = _serveur()
    try:
        code, corps = _post(base, {"ticket": "T", "invitation": "x", "label": "iPhone"})
    finally:
        srv.shutdown()
    assert code == 403 and corps.get("error") == "revoked", (
        f"statut {code} ({corps!r}) — une invitation BIDON a levé la révocation")
    with access.session() as conn:
        assert access.est_revoquee(conn, "firebase:claire"), (
            "la révocation a été effacée — et elle repartirait au cloud au battement "
            "suivant, donc définitivement")


# ── ⑧ Mais une invitation VALABLE, elle, lève bien la révocation ─────────────
#
# ⭐ Le témoin symétrique, et il n'est pas décoratif : sans lui, on pourrait
#    « corriger » la faille en refusant TOUTE réinvitation, ce qui casserait le seul
#    chemin de retour prévu — une réinvitation est un geste DÉLIBÉRÉ de l'owner.
@cas
def une_invitation_valable_leve_la_revocation():
    _base_neuve()
    with access.session() as conn:
        access.mint(conn, uid="firebase:claire", label="iPhone", role=access.ROLE_MEMBER)
        access.revoke_person(conn, "firebase:claire")
        code_invit = access.create_invitation(conn, role=access.ROLE_MEMBER)

    local_api._demander_au_cloud = lambda *a, **k: ("firebase:claire", access.ROLE_MEMBER)
    srv, base = _serveur()
    try:
        code, corps = _post(base, {"ticket": "T", "invitation": code_invit})
    finally:
        srv.shutdown()
    assert code == 200, f"statut {code} ({corps!r}) — la réinvitation ne marche plus"
    with access.session() as conn:
        assert not access.est_revoquee(conn, "firebase:claire"), \
            "l'invitation valable n'a pas levé le drapeau"


# ── ⑨ 🚨 LE VERROU DU PREMIER PROPRIÉTAIRE ───────────────────────────────────
#
# « Fusionner ne livre rien » est vrai de cette PR, PAS du prochain tag tiré de
# `main`. Un ticket SANS rôle sur un boîtier SANS owner fait un owner — depuis le
# LAN, avec un compte Google et le `deviceId` que mDNS DIFFUSE. C'est le TOFU rejeté
# le 19/09.
#
# ⚠️ Ni le ticket ni le cloud ne peuvent le fermer : l'attaquant frappe le ticket
#    pour SON propre uid, et le cloud ne voit pas par quel canal il est arrivé. Seul
#    le boîtier le sait.
#
# ⭐ Ce verrou ne bloque aucun chemin légitime : la remise du ticket en BLE (⑤) n'est
#    pas livrée. Il rend `main` TAGGABLE en attendant, et c'est tout son objet.
@cas
def un_boitier_sans_owner_refuse_de_fonder_un_proprietaire():
    _base_neuve(avec_owner=False)
    relaye = []
    local_api._demander_au_cloud = lambda *a, **k: relaye.append(a) or ("x", "owner")
    srv, base = _serveur()
    try:
        code, corps = _post(base, {"ticket": "T", "label": "iPhone"})
    finally:
        srv.shutdown()
    assert code == 403, f"statut {code} ({corps!r}) — le TOFU est ouvert"
    assert corps.get("error") == "first_owner_locked", f"erreur {corps!r}"
    # ⭐ Et le ticket de la personne n'est PAS brûlé : on refuse AVANT l'aller-retour.
    assert not relaye, "le boîtier a relayé — le ticket a été consommé pour rien"


# ── ⑩ Une invitation ne doit pas être brûlée par qui a déjà un droit ─────────
@cas
def un_droit_existant_ne_consomme_pas_l_invitation():
    _base_neuve()
    with access.session() as conn:
        access.mint(conn, uid="firebase:claire", label="iPhone", role=access.ROLE_MEMBER)
        code_invit = access.create_invitation(conn, role=access.ROLE_MEMBER)

    local_api._demander_au_cloud = lambda *a, **k: ("firebase:claire", access.ROLE_MEMBER)
    srv, base = _serveur()
    try:
        code, _ = _post(base, {"ticket": "T", "invitation": code_invit, "label": "iPad"})
    finally:
        srv.shutdown()
    assert code == 200, f"statut {code}"
    with access.session() as conn:
        assert access.role_invitation(conn, code_invit) == access.ROLE_MEMBER, (
            "l'invitation a été consommée par quelqu'un qui avait déjà un droit — "
            "le bon de droit d'un tiers est perdu")


# ── ⑫ 🚨 UNE RÉINVITATION DOIT CONSOMMER L'INVITATION ───────────────────────
#
# ⚠️ Régression reproduite le 05/10. `role_personne` rend le rôle de la LIGNE, et une
#    révocation ne supprime pas la ligne. Une personne révoquée comptait donc comme
#    « droit existant » :
#
#      claim avec invitation valable    : 200 {'role': 'member'}
#      invitation encore valable après ? 'member'      ← réutilisable par un TIERS
#
# 🚨 Et la règle « on ne change un rôle que par révocation puis réinvitation » ne
#    tenait plus : la réinvitation rendait l'ancien rôle sans consommer le code.
@cas
def une_reinvitation_consomme_l_invitation_et_applique_son_role():
    _base_neuve()
    with access.session() as conn:
        access.mint(conn, uid="firebase:claire", label="iPhone", role=access.ROLE_MEMBER)
        access.revoke_person(conn, "firebase:claire")
        code_invit = access.create_invitation(conn, role=access.ROLE_MEMBER)

    local_api._demander_au_cloud = lambda *a, **k: ("firebase:claire", access.ROLE_MEMBER)
    srv, base = _serveur()
    try:
        code, corps = _post(base, {"ticket": "T", "invitation": code_invit})
    finally:
        srv.shutdown()
    assert code == 200, f"statut {code} ({corps!r})"
    with access.session() as conn:
        assert access.role_invitation(conn, code_invit) == "", (
            "l'invitation est encore valable après une réinvitation — un TIERS peut "
            "s'en servir jusqu'à son expiration")
        assert not access.est_revoquee(conn, "firebase:claire"), \
            "la réinvitation n'a pas levé le drapeau"


# ── ⑪bis LA CLASSIFICATION DES RÉPONSES, testée pour elle-même ──────────────
#
# ⚠️ Le cas ⑪ ci-dessous lève `_BoitierRefuse` depuis un faux cloud : il n'exécute
#    donc JAMAIS la classification. Confondre `device_revoked` et `no_access`
#    laissait tous les bancs verts — mesuré. ⇒ On éprouve la fonction PURE.
@cas
def chaque_reponse_du_cloud_a_son_verdict():
    import json as _json
    v = local_api._verdict_cloud
    cas_attendus = [
        (200, {},                            None),
        (403, {"error": "bad_ticket"},       local_api._TicketInvalide),
        (403, {"error": "no_access"},        local_api._AucunDroitCloud),
        (403, {"error": "device_mismatch"},  local_api._BoitierRefuse),
        (403, {"error": "device_revoked"},   local_api._BoitierRefuse),
        # ⓘ Motif inconnu : on ne devine pas, et surtout on ne dit pas à la personne
        #    que ça vient d'elle.
        (403, {"error": "quelque_chose"},    local_api._BoitierRefuse),
        (403, {},                            local_api._BoitierRefuse),
        # ⚠️ 400 n'est PAS réessayable : typiquement un cloud plus ancien que le
        #    boîtier, qui ne connaît pas le ticket.
        (400, {"error": "bad_body"},         local_api._ContratRompu),
        (500, {},                            local_api._CloudInjoignable),
        (503, {},                            local_api._CloudInjoignable),
    ]
    for statut, corps, attendu in cas_attendus:
        got = v(statut, _json.dumps(corps).encode())
        if attendu is None:
            assert got is None, f"{statut} {corps} → {got!r}, attendu aucun verdict"
        else:
            assert isinstance(got, attendu), (
                f"{statut} {corps} → {type(got).__name__}, attendu {attendu.__name__}")
    # ⚖️ Et un corps illisible ne doit pas faire exploser la classification.
    assert isinstance(v(403, b"pas du json"), local_api._BoitierRefuse)
    # 🚨 LE SEUL 403 QUI DOIT DIRE « c'est vous » est `no_access`. Ce banc-ci est le
    #    seul qui le garantisse : les motifs du boîtier et les inconnus partagent
    #    désormais UNE branche, donc c'est l'inverse qu'il faut défendre — qu'aucun
    #    autre motif ne devienne un `no_access`.
    for motif in ("device_mismatch", "device_revoked", "inconnu", ""):
        got = v(403, _json.dumps({"error": motif}).encode())
        assert not isinstance(got, local_api._AucunDroitCloud), (
            f"403 {motif!r} classé « aucun droit » — la personne irait réclamer une "
            f"invitation pour un problème qui ne la concerne pas")


# ── ⑪ Un refus du BOÎTIER ne se dit pas comme un refus de la PERSONNE ────────
@cas
def un_boitier_refuse_par_le_cloud_ne_dit_pas_aucun_droit():
    _base_neuve()

    def cloud_refuse_le_boitier(*a, **k):
        raise local_api._BoitierRefuse("device_revoked")

    local_api._demander_au_cloud = cloud_refuse_le_boitier
    srv, base = _serveur()
    try:
        code, corps = _post(base, {"ticket": "T"})
    finally:
        srv.shutdown()
    assert code == 403, f"statut {code}"
    assert corps.get("error") == "device_rejected", (
        f"erreur {corps!r} — « aucun droit » enverrait la personne réclamer une "
        f"invitation pour un problème de certificat du boîtier")


if __name__ == "__main__":
    print("── contrat de /claim : le ticket ──")
    print(f"\n{len(_ECHECS)} échec(s)" if _ECHECS else "\ntout vert")
    sys.exit(1 if _ECHECS else 0)
