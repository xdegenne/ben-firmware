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
print("── ⚠️ défauts trouvés en revue de la PR #57 ───────────────")

# ① 🚨 UNE TRAME SANS IINST COMPLET DOIT ÊTRE PERDUE, PAS PUBLIÉE À 0.
#    `required` n'a JAMAIS contenu TIC_SEEN_IINST : une ligne perdue au checksum
#    laissait `v.iinst` à 0, et `curveAdd` encodait ce 0 comme un POINT RÉEL —
#    « 30, 30, 0, 30 » sur un compteur qui tire 30 A. Antérieur au triphasé, mais
#    le triphasé le rend TROIS fois plus probable.
m = re.search(r"uint16_t\s+required\s*=([^;]*);", nu)
verifie("la garde histo est assemblée dans le CODE", bool(m))
verifie("① … et elle EXIGE TIC_SEEN_IINST (sinon un 0 inventé entre dans la courbe)",
        bool(m) and "TIC_SEEN_IINST" in m.group(1))
verifie("   … toujours avec TIC_SEEN_PAPP", bool(m) and "TIC_SEEN_PAPP" in m.group(1))

# ② 🚨 LE BIT TRIPHASÉ EST UNE PROPRIÉTÉ DU BATCH, PAS DE SON 1er ÉCHANTILLON.
#    Lu dans `curveStart` seul, une trame qui perdait une phase faisait partir
#    ~55 s et jusqu'à 130 points marqués « monophasé ».
verifie("② un accumulateur de batch existe", "curveTriphase" in nu)
verifie("   … levé par N'IMPORTE QUELLE trame complète, dans curveAdd",
        re.search(r"if\s*\(\s*v\.ph_vues\s*==\s*0x07\s*\)\s*curveTriphase\s*=\s*true", nu)
        is not None)
verifie("   … et l'octet de drapeaux est rectifié au flush",
        re.search(r"curveBuf\s*\[\s*7\s*\]\s*\|=\s*CF_TRIPHASE", nu) is not None)

print("── ⚖️ le harnais hôte ne doit PAS casser la compilation AVR ─")
# 🚨 DÉFAUT RÉEL, découvert en tentant un flash : `arduino-cli` compile TOUT
#    fichier .cpp/.c posé à côté du `.ino`. Le harnais hôte y était, et le
#    compilateur AVR échouait sur `<cstdint>` — donc AUCUN flash n'était possible.
#    Les bancs passaient tous : seul un flash révélait le défaut.
# ⭐ Arduino ne compile pas les sous-répertoires autres que `src/`, d'où `banc/`.
rep = INO.parent
intrus = sorted(p.name for p in rep.glob("*") if p.suffix in (".cpp", ".c", ".S"))
verifie(f"aucun .cpp/.c/.S à côté du sketch (trouvés : {intrus or 'aucun'})", not intrus)
verifie("le harnais vit bien dans banc/", (rep / "banc" / "test_triphase_emetteur.cpp").exists())

print()
if echecs:
    print(f"❌ {len(echecs)} cas en échec : " + ", ".join(echecs))
    sys.exit(1)
print("✅ tous les cas passent")
