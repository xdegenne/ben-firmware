#!/usr/bin/env python3
"""Banc du bit TRIPHASÉ dans le codec LoRa (#51) — et surtout de sa NON-RÉGRESSION.

🚨 CE FICHIER EST PARTAGÉ AVEC LES SEPT BOÎTIERS DU PARC. Le risque de ce
   chantier n'est pas de rater le triphasé — personne n'en a — c'est de casser
   le monophasé en le réparant. D'où un témoin à l'OCTET contre une référence
   figée, capturée sur la version d'AVANT le changement.

⭐ `triphase` a un défaut à False, donc tout appelant existant produit
   exactement les mêmes octets. Ce banc est là pour que ça reste vrai.

Lancement :  python3 test_triphase_codec.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import frame_codec as fc  # noqa: E402

CLE = bytes(range(32))

# 🚨 RÉFÉRENCE CAPTURÉE SUR LA VERSION D'AVANT (main, 2026-10-17). Si cette ligne
#    doit changer un jour, ce n'est pas le banc qu'on corrige : c'est qu'on a
#    modifié le format d'une trame que sept boîtiers émettent déjà.
GOLDEN = "050700002a000000d2040340e20100dc050003140228024f2df0ec2f9ad4b713"

echecs = []


def verifie(nom, obtenu, attendu):
    if obtenu == attendu:
        print(f"  ✓ {nom}")
    else:
        print(f"  ✗ {nom}\n      attendu : {attendu!r}\n      obtenu  : {obtenu!r}")
        echecs.append(nom)


def trame(**kw):
    base = dict(boot_count=7, msg_count=42, batch_seq=1234, index_id=3,
                index_value=123456, papp=[1500, 1520, 1480], period_ds=20)
    base.update(kw)
    return fc.encode_curve(CLE, **base)


print("── ⚖️ NON-RÉGRESSION : le parc est monophasé ───────────────")
verifie("une trame SANS triphase est identique à l'octet près",
        trame().hex(), GOLDEN)
verifie("   et `triphase=False` explicite donne la même chose",
        trame(triphase=False).hex(), GOLDEN)

print("── le bit ─────────────────────────────────────────────────")
verifie("CF_TRIPHASE vaut bien le bit 3", fc.CF_TRIPHASE, 0x08)
verifie("il ne mord sur aucun bit déjà pris",
        fc.CF_TRIPHASE & (fc.CF_SRC_STANDARD | fc.CF_TS_VALID | fc.CF_HAS_IINST), 0)

avec = trame(triphase=True)
verifie("⚖️ avec triphase, les octets DIFFÈRENT", avec.hex() != GOLDEN, True)
# ⚠️ NEUF octets changent, pas un — et c'est la bonne réponse. La trame porte un
#    MAC de 8 octets qui COUVRE les drapeaux : modifier le bit en change
#    forcément l'empreinte. Attendre « un seul octet » était mon erreur, et elle
#    aurait fait passer ce banc pour un défaut du code.
sans = trame()
diff = [i for i, (a, b) in enumerate(zip(sans, avec)) if a != b]
verifie("un seul octet de CORPS change, plus les 8 du MAC", len(diff), 9)
# ⚠️ `diff[0]` et non `diff and diff[0]` levait un IndexError quand la mutation
#    rendait les deux trames identiques : le banc PLANTAIT au lieu de rendre son
#    relevé, et les cas suivants n'étaient jamais évalués. Un banc doit tomber,
#    pas s'interrompre — sinon il cache les défauts qu'il n'a pas encore testés.
verifie("   et c'est bien l'octet des flags", diff[0] if diff else None, 7)

print("── aller-retour ───────────────────────────────────────────")
d_sans = fc.decode(sans, CLE)
d_avec = fc.decode(avec, CLE)
verifie("sans le bit → triphase = False", d_sans["triphase"], False)
verifie("avec le bit → triphase = True", d_avec["triphase"], True)
verifie("⚖️ le reste de la trame est INCHANGÉ",
        {k: v for k, v in d_sans.items() if k not in ("triphase", "flags")},
        {k: v for k, v in d_avec.items() if k not in ("triphase", "flags")})

print("── ⚖️ un vieil émetteur, lu par un récepteur à jour ───────")
# 🚨 LE CAS DU PARC MIXTE. Les sept boîtiers émettent sans le bit ; leur
#    récepteur, lui, sera à jour. Il doit lire « monophasé », jamais « inconnu ».
verifie("un flags à 0x04 (histo, pas de bit 3) → triphase False",
        bool(0x04 & fc.CF_TRIPHASE), False)
verifie("   et has_iinst reste vrai", bool(0x04 & fc.CF_HAS_IINST), True)

print()
if echecs:
    print(f"❌ {len(echecs)} cas en échec : " + ", ".join(echecs))
    sys.exit(1)
print("✅ tous les cas passent")
