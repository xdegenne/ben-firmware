#!/usr/bin/env python3
"""Banc de la VERSION de l'émetteur — elle existe sous deux formes, elle ne doit en avoir qu'une.

⭐ POURQUOI CE BANC EXISTE. Depuis 0.1.10 la version part dans la trame de boot (TLV `T_FW`,
trois octets) ET s'imprime au banner série (une chaîne). Deux représentations, donc deux
occasions de divergence — et une divergence ici ne casse RIEN : le boîtier démarre, émet,
s'enregistre, et annonce simplement une version fausse. Elle ne se découvrirait qu'en
cherchant autre chose.

🚨 La parade est STRUCTURELLE : les nombres sont la source, la chaîne est fabriquée par le
préprocesseur. Ce banc vérifie que cette structure est TOUJOURS en place — pas que les deux
valeurs s'accordent aujourd'hui, mais qu'il reste IMPOSSIBLE qu'elles s'écartent demain.

⭐ Et c'est le premier banc du sketch Arduino : il lit la SOURCE, il ne compile rien. Le coût
d'un banc qui ne demande ni AVR ni matériel est nul, et la CI le ramasse comme les autres.

Lancement :  python3 test_version.py
"""
import pathlib
import re
import sys

INO = pathlib.Path(__file__).with_name("tic-reader.ino")
SRC = INO.read_text(encoding="utf-8")

CAS = []


def cas(f):
    CAS.append(f)
    return f


def _define(nom: str):
    """La valeur d'un `#define <nom> <valeur>`, telle qu'elle est ÉCRITE dans la source."""
    m = re.search(rf"^#define\s+{re.escape(nom)}\s+(.+?)\s*(?://.*)?$", SRC, re.M)
    assert m, f"#define {nom} introuvable dans {INO.name}"
    return m.group(1).strip()


@cas
def les_trois_nombres_sont_declares_et_sont_des_nombres():
    """Le TLV en émet trois octets : chacun doit tenir dans un octet."""
    for nom in ("FW_MAJOR", "FW_MINOR", "FW_PATCH"):
        v = _define(nom)
        assert v.isdigit(), f"{nom} = {v!r} n'est pas un entier décimal"
        assert 0 <= int(v) <= 255, f"{nom} = {v} ne tient pas dans un octet (TLV T_FW)"


@cas
def la_CHAINE_est_DERIVEE_des_nombres_et_jamais_ecrite_a_la_main():
    """🚨 LE CŒUR DU BANC. Si `FW_VERSION` redevient un littéral, les deux formes peuvent
    divergent en silence — le banner dirait une version et la trame une autre."""
    v = _define("FW_VERSION")
    assert '"0' not in v and not re.match(r'^"\d', v), (
        f"FW_VERSION est écrit à la main ({v!r}) : les nombres ne sont plus la source de "
        f"vérité, et rien n'empêche plus le banner et le TLV de s'écarter")
    for nom in ("FW_MAJOR", "FW_MINOR", "FW_PATCH"):
        assert nom in v, f"FW_VERSION ne dérive pas de {nom} : {v!r}"


@cas
def le_TLV_emet_EXACTEMENT_ces_trois_nombres_la():
    """⚖️ Le témoin du cas précédent : la chaîne peut bien dériver des nombres, si le TLV
    émettait autre chose la version annoncée en radio serait quand même fausse."""
    # ⓘ Écrit directement dans le tampon de trame, sans tableau local : celui-ci coûterait
    #   de la PILE dans sendBootFrame, là où le ChaCha a déjà débordé en 0.1.3.
    m = re.search(r"buf\[pos\+\+\]\s*=\s*T_FW;\s*buf\[pos\+\+\]\s*=\s*(\d+);"
                  r"\s*(?:\n\s*)?buf\[pos\+\+\]\s*=\s*([A-Z_]+);"
                  r"\s*buf\[pos\+\+\]\s*=\s*([A-Z_]+);"
                  r"\s*buf\[pos\+\+\]\s*=\s*([A-Z_]+);", SRC)
    assert m, "l'écriture du TLV T_FW est introuvable ou a changé de forme"
    assert m.group(1) == "3", f"le TLV T_FW annonce une longueur de {m.group(1)}, pas 3"
    membres = [m.group(2), m.group(3), m.group(4)]
    assert membres == ["FW_MAJOR", "FW_MINOR", "FW_PATCH"], (
        f"le TLV émet {membres} au lieu des trois constantes — une valeur recopiée ici "
        f"serait une troisième vérité")


@cas
def le_tag_T_FW_ne_collisionne_avec_AUCUN_autre():
    """Deux tags égaux, et le récepteur attribue une valeur au mauvais champ."""
    tags = {}
    for nom, val in re.findall(r"^#define\s+(T_[A-Z0-9_]+)\s+(0x[0-9a-fA-F]+)", SRC, re.M):
        v = int(val, 16)
        assert v not in tags, f"{nom} et {tags[v]} partagent le tag {val}"
        tags[v] = nom
    assert tags.get(0x07) == "T_FW", f"T_FW n'est pas 0x07 : {tags.get(0x07)}"


@cas
def la_trame_de_boot_tient_sous_son_plafond_ANNONCE():
    """🚨 Le pire cas se CALCULE, il ne se suppose pas. Mode standard, NGTF de 16 caractères.

    ⓘ `BOOT_MAX_LEN` est documentaire — il n'est référencé nulle part, le tampon réel étant
    `curveBuf`. Mais un plafond annoncé qui serait déjà dépassé ne documenterait plus rien,
    et c'est ce que ce cas empêche.
    """
    plafond = int(_define("BOOT_MAX_LEN"))
    pire = (7                     # header clair : ver/type + boot_count(3) + msg_count(3)
            + 2 + 12              # T_ADCO
            + 2 + 3               # T_FW
            + 2 + 1               # T_ISOUSC
            + 2 + 1               # T_PREF
            + 2 + 16              # T_CONTRAT (NGTF, 16 au plus)
            + 2 + 3               # T_PAPP   (int24)
            + 2 + 2               # T_IINST  (uint16)
            + 8)                  # MAC tronqué
    assert pire <= plafond, (
        f"pire cas calculé {pire} o > BOOT_MAX_LEN annoncé {plafond} — relever le plafond "
        f"ET écrire pourquoi, ou retirer un champ")
    buf = int(_define("CURVE_BUF_LEN"))
    assert plafond <= buf, f"BOOT_MAX_LEN {plafond} dépasse le tampon réel curveBuf {buf}"


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
