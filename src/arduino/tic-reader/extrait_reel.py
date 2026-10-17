#!/usr/bin/env python3
"""Extrait du `.ino` le TEXTE RÉEL à éprouver — il n'en recopie jamais une ligne.

🚨 C'EST LA PIÈCE QUI FAIT LA DIFFÉRENCE ENTRE UN BANC ET UNE ILLUSION. Un banc
   qui recopierait le parseur testerait sa copie : il resterait vert le jour où
   le `.ino` diverge, c'est-à-dire exactement le jour où il servirait.

⭐ Les deux autres bancs du sketch (`test_version`, `test_buffer_partage`) LISENT
   la source sans l'exécuter. Celui-ci est le premier à la FAIRE TOURNER, parce
   que la question posée — « additionne-t-on deux IINST1, ou l'IINST1 d'une autre
   trame ? » — ne se répond pas en lisant.
"""
import pathlib
import sys

INO = pathlib.Path(__file__).with_name("tic-reader.ino")


def bloc(src: str, debut: str) -> str:
    """Le bloc qui commence à `debut` et finit à sa première accolade en colonne 0."""
    i = src.index(debut)
    j = src.rfind("\n", 0, i) + 1          # inclut un éventuel `static`
    reste = src[j:]
    return reste[: reste.index("\n}\n") + 3]


def ecrire(dest: pathlib.Path) -> None:
    ino = INO.read_text(encoding="utf-8")
    morceaux = [
        "\n".join(l for l in ino.splitlines() if l.startswith("#define TIC_SEEN")),
        "\n".join(l for l in ino.splitlines() if l.startswith("#define CF_")),
        bloc(ino, "bool adcoValide"),
        ino[ino.index("struct TICValues") :][
            : ino[ino.index("struct TICValues") :].index("\n};") + 3
        ],
        bloc(ino, "static void parseTICLine(char* line"),
    ]
    dest.write_text("\n\n".join(morceaux) + "\n", encoding="utf-8")


if __name__ == "__main__":
    cible = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "reel.h")
    ecrire(cible)
    print(f"  extrait du .ino → {cible}")
