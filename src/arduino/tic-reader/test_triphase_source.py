#!/usr/bin/env python3
"""Banc d'inspection : l'émetteur LÈVE-T-IL le bit TRIPHASÉ ? (#51)

🚨 CE BANC COMBLE UN TROU QUE LE HARNAIS HÔTE NE PEUT PAS COUVRIR. Celui-ci
   n'extrait que `parseTICLine` : il prouve que les trois phases sont agrégées,
   mais PAS que le drapeau part dans la trame. Or les deux moitiés sont
   indépendantes — on peut parfaitement sommer juste et n'en rien dire.

⚠️ Inspection de SOURCE, donc on lit le code et pas les commentaires : la prose
   de ce `.ino` cite abondamment `CF_TRIPHASE`, et un `grep` naïf passerait au
   vert sur un sketch qui ne l'assemble plus.

Lancement :  python3 test_triphase_source.py
"""
import pathlib
import re
import sys

INO = pathlib.Path(__file__).with_name("tic-reader.ino")
echecs = []


def verifie(nom, ok):
    print(f"  {'✓' if ok else '✗'} {nom}")
    if not ok:
        echecs.append(nom)


src = INO.read_text(encoding="utf-8")
# ⚠️ On retire les commentaires (// et /* */) AVANT de chercher.
nu = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
nu = re.sub(r"//[^\n]*", " ", nu)

verifie("CF_TRIPHASE est défini", "#define CF_TRIPHASE" in src)

m = re.search(r"#define\s+CF_TRIPHASE\s+0x([0-9a-fA-F]+)", src)
verifie("… et vaut le bit 3 (0x08)", bool(m) and int(m.group(1), 16) == 0x08)

# 🚨 Le bit ne doit mordre sur aucun autre drapeau du même octet.
pris = 0
for nom in ("CF_SRC_STANDARD", "CF_TS_VALID", "CF_HAS_IINST"):
    mm = re.search(rf"#define\s+{nom}\s+0x([0-9a-fA-F]+)", src)
    if mm:
        pris |= int(mm.group(1), 16)
verifie("… et ne recouvre aucun bit déjà pris", bool(m) and not (int(m.group(1), 16) & pris))

# ⭐ LE CŒUR : l'octet `flags` de la trame courbe doit le porter.
bloc = re.search(r"uint8_t\s+flags\s*=(.*?);", nu, flags=re.S)
verifie("l'octet `flags` est assemblé dans le CODE", bool(bloc))
verifie("… et il inclut CF_TRIPHASE", bool(bloc) and "CF_TRIPHASE" in bloc.group(1))
verifie("… conditionné aux TROIS phases (ph_vues == 0x07)",
        bool(bloc) and re.search(r"ph_vues\s*==\s*0x07", bloc.group(1)) is not None)

# ⚠️ La carte des bits en tête de fichier a DÉJÀ été périmée une fois : elle
#    décrivait DEMAIN/ADPS/PEJP alors que le code utilisait tout autre chose.
#    Un lecteur cherchant un bit libre s'y fierait.
verifie("la carte des bits en tête mentionne triphase", "bit3 triphase" in src)
verifie("… et ne décrit plus l'ancien format (DEMAIN/ADPS en bits 0-3)",
        "bit0-1 DEMAIN" not in src)

print()
if echecs:
    print(f"❌ {len(echecs)} cas en échec : " + ", ".join(echecs))
    sys.exit(1)
print("✅ tous les cas passent")
