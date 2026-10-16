#!/usr/bin/env python3
"""Banc de la présentation du ticket — le chemin qui rend un boîtier NEUF revendicable.

🚨 POURQUOI CE FICHIER. `presenter_le_ticket` a été ajoutée et LES 21 FICHIERS DE BANCS
   SONT RESTÉS VERTS : un chemin entier, et rien ne le couvrait. C'est le quatrième
   avertissement du même genre dans ce chantier.

⭐ CE QU'IL DÉFEND, et chaque cas coûte cher s'il casse :

     pas de fichier        → AUCUN appel au cloud (99,99 % des démarrages)
     200                   → `fonder: true` envoyé · droit local écrit · fichier EFFACÉ
     403 bad_ticket        → fichier EFFACÉ   (sinon rejoué à chaque démarrage, pour rien)
     panne réseau          → fichier CONSERVÉ (sinon le boîtier est condamné par une
                              coupure WiFi au premier démarrage)
     500                   → fichier CONSERVÉ
     200 mal formé         → fichier CONSERVÉ (un bug de NOTRE côté ne doit pas
                              condamner le boîtier)

⚠️ L'asymétrie « effacer sur refus, garder sur panne » est tout l'enjeu : l'un se
   répare par un nouveau déballage, l'autre par un peu de patience.

Lancer : `cd src/pi/publisher && python3 test_presenter_ticket.py`
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile

# 🚨 AVANT L'IMPORT : `claim_ticket.CHEMIN` est lu à l'import. Sans ça le banc
#    écrirait dans /var/lib/ben-firmware — et échouerait en permission, ce qui est la
#    bonne sorte d'échec, mais pas celle qu'on veut ici.
_TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["BEN_CLAIM_TICKET_PATH"] = str(_TMP / "claim_ticket")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import ben_publisher  # noqa: E402
from store import access, claim_ticket  # noqa: E402

_ECHECS: list[str] = []


def cas(fn):
    try:
        fn()
        print(f"  ✅ {fn.__name__.replace('_', ' ')}")
    except AssertionError as e:
        print(f"  ❌ {fn.__name__.replace('_', ' ')}\n     {e}")
        _ECHECS.append(fn.__name__)
    return fn


class FauxClient:
    """Un cloud en bocal : il enregistre ce qu'on lui envoie et rend ce qu'on veut."""

    def __init__(self, reponse=None, leve=None):
        self.reponse, self.leve, self.vus = reponse, leve, []

    def post(self, path, payload):
        self.vus.append((path, payload))
        if self.leve:
            raise self.leve
        return self.reponse


def _neuf(ticket: str | None = "TICKET-DU-DEBALLAGE"):
    """Un magasin d'accès vierge, et un fichier de ticket au choix."""
    access._shared = access.connect(str(pathlib.Path(tempfile.mkdtemp()) / "a.db"))
    claim_ticket.effacer()
    if ticket:
        claim_ticket.poser(ticket)


# ── Le cas de tous les jours : il n'y a rien à faire ────────────────────────
@cas
def sans_fichier_aucun_appel_au_cloud():
    _neuf(ticket=None)
    cli = FauxClient(reponse=(200, "{}"))
    ben_publisher.presenter_le_ticket(cli)
    assert not cli.vus, (
        "le publisher a appelé le cloud sans ticket à présenter — c'est 99,99 % des "
        "démarrages, et ça coûterait un aller-retour mTLS à chacun")


# ── 🚨 Le déballage qui réussit ─────────────────────────────────────────────
@cas
def un_deballage_reussi_fonde_l_owner_et_efface_le_fichier():
    _neuf()
    cli = FauxClient(reponse=(200, json.dumps({"uid": "firebase:xavier", "role": "owner"})))
    ben_publisher.presenter_le_ticket(cli)

    assert len(cli.vus) == 1, f"{len(cli.vus)} appel(s), attendu 1"
    chemin, charge = cli.vus[0]
    assert chemin == "/claim", f"chemin {chemin!r}"
    # 🚨 LA MOITIÉ QUI COMPTE : `fonder` est vrai, et SEULEMENT d'ici.
    assert charge.get("fonder") is True, (
        f"charge {charge!r} — sans `fonder: true`, le cloud REFUSE de créer le premier "
        f"propriétaire : le déballage échoue en silence")
    assert charge.get("ticket") == "TICKET-DU-DEBALLAGE", f"charge {charge!r}"
    assert "role" not in charge, "un rôle part hors chemin d'invitation"

    with access.session() as ac:
        assert access.role_personne(ac, "firebase:xavier") == access.ROLE_OWNER, (
            "le droit local n'a pas été écrit — le boîtier ne connaîtrait pas son "
            "propre propriétaire hors ligne")
        assert access.has_owner(ac), "has_owner reste faux"
    assert claim_ticket.lire() == "", (
        "le ticket n'est pas effacé — il serait présenté à chaque démarrage, alors "
        "qu'il est consommé")


# ── 🚨 L'asymétrie : effacer sur REFUS, garder sur PANNE ────────────────────
@cas
def un_refus_definitif_efface_le_ticket():
    _neuf()
    cli = FauxClient(reponse=(403, json.dumps({"error": "bad_ticket"})))
    ben_publisher.presenter_le_ticket(cli)
    assert claim_ticket.lire() == "", (
        "un ticket refusé est conservé — il serait rejoué à CHAQUE démarrage, pour "
        "rien, et masquerait le vrai état du boîtier dans les journaux")


@cas
def une_panne_reseau_CONSERVE_le_ticket():
    _neuf()
    cli = FauxClient(leve=OSError("réseau coupé"))
    ben_publisher.presenter_le_ticket(cli)
    assert claim_ticket.lire() == "TICKET-DU-DEBALLAGE", (
        "le ticket a été effacé sur une PANNE — une coupure WiFi au premier démarrage "
        "condamnerait le boîtier définitivement")


@cas
def un_500_CONSERVE_le_ticket():
    _neuf()
    cli = FauxClient(reponse=(500, "{}"))
    ben_publisher.presenter_le_ticket(cli)
    assert claim_ticket.lire() == "TICKET-DU-DEBALLAGE", "un 500 a effacé le ticket"


@cas
def une_reponse_200_mal_formee_CONSERVE_le_ticket():
    _neuf()
    # ⚠️ LE TROISIÈME CAS EST LE SEUL QUI ISOLE LA GARDE SUR L'uid, et il a fallu une
    #    mutation pour s'en apercevoir : `{"uid": ""}` SEUL n'a pas de rôle, donc
    #    `grant` lève sur le RÔLE avant même de regarder l'uid. Le cas passait pour la
    #    mauvaise raison.
    # 🚨 Avec un rôle VALIDE et un uid vide, rien d'autre ne protège : `grant` ne
    #    regarde pas l'uid, et insérerait un `owner` qui n'est personne — un boîtier
    #    qui se croit revendiqué et ne l'est pas.
    for mauvaise in ("pas du json",
                     json.dumps({"uid": ""}),
                     json.dumps({"uid": "", "role": "owner"}),
                     json.dumps({"uid": "firebase:x", "role": "zorglub"})):
        claim_ticket.poser("TICKET-DU-DEBALLAGE")
        cli = FauxClient(reponse=(200, mauvaise))
        ben_publisher.presenter_le_ticket(cli)
        assert claim_ticket.lire() == "TICKET-DU-DEBALLAGE", (
            f"réponse {mauvaise[:30]!r} : le ticket a été effacé — un défaut de NOTRE "
            f"côté ne doit pas condamner le boîtier")
        with access.session() as ac:
            assert not access.has_owner(ac), "un owner a été posé sur une réponse invalide"


# ── Le fichier lui-même ─────────────────────────────────────────────────────
@cas
def le_fichier_du_ticket_est_en_0600_et_atomique():
    _neuf(ticket=None)
    claim_ticket.poser("ABC")
    mode = oct(claim_ticket.CHEMIN.stat().st_mode & 0o777)
    assert mode == "0o600", f"mode {mode} — le seul secret en clair du boîtier"
    # ⭐ L'écriture passe par un `.tmp` renommé : aucun fichier intermédiaire ne reste.
    assert not claim_ticket.CHEMIN.with_suffix(".tmp").exists(), \
        "le fichier temporaire survit — une coupure laisserait un ticket tronqué"
    assert claim_ticket.lire() == "ABC"
    claim_ticket.effacer()
    assert claim_ticket.lire() == "", "effacer() n'efface pas"
    claim_ticket.effacer()  # ⭐ idempotent : appelé sur un refus ET sur un succès


@cas
def le_ticket_est_REPRESENTE_a_chaque_tour_de_boucle():
    """🚨 LE DÉFAUT : il n'était présenté qu'au DÉMARRAGE.

    Quatre sorties de `presenter_le_ticket` journalisent « conservé, on
    réessaiera » — cloud injoignable, 5xx, réponse sans uid, octroi local en
    échec — et RIEN ne réessayait. Aucun de ces cas ne tue le publisher, donc
    systemd ne le relançait pas : au bout de 900 s le ticket expirait et le
    boîtier restait SANS PROPRIÉTAIRE. Il fallait rouvrir une fenêtre BLE —
    exactement ce que le ticket existe pour éviter.

    ⭐ ASSERTION STRUCTURELLE, PAS TEXTUELLE. On parse l'AST et on exige que
    l'appel soit DANS la boucle `while` de `main`. Un `grep` serait satisfait
    par l'appel de démarrage, qui existait déjà et ne corrigeait rien — et les
    commentaires citent abondamment le nom de la fonction.
    """
    import ast
    import pathlib as _pl

    src = _pl.Path(__file__).with_name("ben_publisher.py").read_text()
    main = next(n for n in ast.walk(ast.parse(src))
                if isinstance(n, ast.FunctionDef) and n.name == "main")
    boucles = [n for n in ast.walk(main) if isinstance(n, ast.While)]
    assert boucles, "plus de boucle `while` dans main() — banc à revoir"

    def appelle(noeud):
        return any(isinstance(n, ast.Call)
                   and getattr(n.func, "id", "") == "presenter_le_ticket"
                   for n in ast.walk(noeud))

    assert any(appelle(b) for b in boucles), (
        "`presenter_le_ticket` n'est appelé que HORS de la boucle : un ticket "
        "conservé après un échec ne sera jamais représenté, et il expirera")


@cas
def la_representation_est_gardee_par_la_presence_du_fichier():
    """⭐ LE CONTRE-TÉMOIN : on ne doit pas appeler le cloud à chaque tour.

    Sans garde, le publisher tenterait une présentation toutes les 60 s sur TOUS
    les boîtiers du parc, alors que le fichier n'existe qu'entre un déballage BLE
    et la première connexion réussie — 99,99 % des démarrages.
    """
    import ast
    import pathlib as _pl

    src = _pl.Path(__file__).with_name("ben_publisher.py").read_text()
    main = next(n for n in ast.walk(ast.parse(src))
                if isinstance(n, ast.FunctionDef) and n.name == "main")
    boucle = next(n for n in ast.walk(main) if isinstance(n, ast.While))

    garde_ok = False
    for n in ast.walk(boucle):
        if not isinstance(n, ast.If):
            continue
        cond = ast.dump(n.test)
        corps = ast.dump(ast.Module(body=n.body, type_ignores=[]))
        if "lire" in cond and "presenter_le_ticket" in corps:
            garde_ok = True
    assert garde_ok, (
        "la représentation n'est pas gardée par `claim_ticket.lire()` — le "
        "publisher appellerait le cloud à chaque tour, sur tout le parc")


if __name__ == "__main__":
    print("── présentation du ticket de déballage ──")
    print(f"\n{len(_ECHECS)} échec(s)" if _ECHECS else "\ntout vert")
    sys.exit(1 if _ECHECS else 0)
