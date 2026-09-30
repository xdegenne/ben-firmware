#!/usr/bin/env python3
"""Ménage des PDL fantômes et des époques tarifaires bidon (issue #7).

DEUX défauts, une seule cause : un caractère de la trame TIC dont le **bit 6** s'est mis
à 1 (`0`→`p`, `1`→`q`, `.`→`n`). 0x40 vaut 64, et le checksum TIC est `(somme & 0x3F) +
0x20` — **aveugle à tout multiple de 64**. `HC..` et `HCn.` ont le même checksum, 0x47.

Constaté sur ben-0004 : trois ADCO fantômes (`p61961403012`, `061961403p12`,
`0619614030q2`) et quatre époques `HCn.`. Le contrôle de parité de 0.9.18 ferme la porte ;
ce script répare ce qui est déjà entré.

    python3 menage_fantomes.py --a-blanc    # dit ce qu'il ferait, ne touche à rien
    python3 menage_fantomes.py              # agit
"""
import sqlite3
import sys

sys.path[:0] = [__file__.rsplit("/", 1)[0]]
import db  # noqa: E402

# Les mesures sont RÉ-ATTRIBUÉES (jamais supprimées) : elles viennent du vrai compteur,
# seul leur classement était faux. Les autres tables ne portent que des dérivés d'une
# trame mal lue — rien à sauver.
A_REATTRIBUER = ("measurements", "lora_link")
A_SUPPRIMER = ("curve_rollup", "tariff_labels", "level_profile", "contract_epoch", "event")


def pdls_fantomes(conn) -> list:
    """Tout `pdl_index` dont l'ADCO n'a pas la forme d'un ADCO.

    ⚠️ Le `sorted` n'est pas cosmétique : `SELECT pdl_index, adco FROM pdl` se résout
    ENTIÈREMENT depuis l'index UNIQUE sur `adco` (index couvrant), donc les lignes
    reviennent en ordre d'ADCO, pas d'index. Le journal de l'OTA est la seule chose
    qu'on relira d'un boîtier injoignable : il doit être déterministe."""
    return sorted(i for i, a in conn.execute("SELECT pdl_index, adco FROM pdl")
                  if not db.adco_valide(a or ""))


def epoques_bidon(conn, pdl_index: int) -> list:
    """⭐ LA RÈGLE, en une ligne : **la première époque est la référence ; toute autre
    qui lui ressemble sur les DEUX PREMIERS CARACTÈRES part.**

    Elle décrit le mécanisme directement. Un bit 6 retourné (`.`→`n`, `0`→`p`, `1`→`q`,
    0x40 = 64, invisible au checksum TIC qui est `(somme & 0x3F) + 0x20`) n'abîme QU'UN
    caractère : le mot reste reconnaissable, donc le préfixe concorde toujours.

    ⭐ Pourquoi le préfixe et pas une durée : une époque **terminale** n'a pas de
      successeur, donc aucune durée mesurable — et c'est précisément celle qui compte,
      c'est elle que lisent `/registers` et tout calcul de coût.

    ⭐ Pourquoi « ressemble » et non « diffère » : les abîmées naissent en PAIRE. Le
      lecteur rouvre une époque en revenant au vrai contrat 1 à 4 s plus tard, et cette
      jumelle-là est identique à la référence. La même condition emporte les deux, et
      il ne reste qu'une ligne là où cinq disaient la même chose.

    ⚠️ CE QU'ELLE NE FAIT PAS, et c'est assumé :
      · un RETOUR au contrat d'origine (`BASE`→`TEMPO`→`BASE`) serait effacé lui aussi,
        et le boîtier resterait cru en TEMPO. Aucun boîtier du parc n'est dans ce cas —
        BASE, TEMPO, HC.. et BBR( diffèrent tous dès les deux premiers caractères ;
      · le `'3'` vu sur un boîtier radio lui échappe : il vient d'une trame TRONQUÉE,
        pas d'un bit retourné, et n'a aucun préfixe commun avec `BASE`. Autre maladie ;
      · elle suppose la PREMIÈRE ligne saine. Si c'était elle la corrompue, la règle
        s'inverserait et détruirait la vérité.
    """
    lignes = list(conn.execute(
        "SELECT ts_start, ngtf FROM contract_epoch WHERE pdl_index=? ORDER BY ts_start",
        (pdl_index,)))
    if not lignes:
        return []
    ref = lignes[0][1]
    return [(ts, n) for ts, n in lignes[1:] if n[:2] == ref[:2]]


def menage(conn, *, a_blanc: bool = True) -> dict:
    """Rend le rapport de ce qui a été fait — ou de ce qui le serait."""
    sains = sorted(i for i, a in conn.execute("SELECT pdl_index, adco FROM pdl")
                   if db.adco_valide(a or ""))
    fantomes = pdls_fantomes(conn)
    rapport = {"sains": sains, "fantomes": fantomes, "deplacees": {},
               "supprimees": {}, "epoques": [], "refus": None}

    # 🚨 LE GARDE. Ré-attribuer suppose de savoir VERS QUI. Avec zéro ou plusieurs PDL
    #    sains, la destination est indécidable — on ne devine pas, on ne touche à rien.
    if fantomes and len(sains) != 1:
        rapport["refus"] = f"{len(sains)} PDL sains : destination indécidable"
        return rapport
    vrai = sains[0] if sains else None

    for pdl in fantomes:
        for t in A_REATTRIBUER:
            n = conn.execute(f"UPDATE {t} SET pdl_index=?, sent=0 WHERE pdl_index=?"
                             if not a_blanc else
                             f"SELECT count(*) FROM {t} WHERE pdl_index=?",
                             (vrai, pdl) if not a_blanc else (pdl,))
            n = n.fetchone()[0] if a_blanc else n.rowcount
            if n:
                rapport["deplacees"][t] = rapport["deplacees"].get(t, 0) + n
        for t in A_SUPPRIMER:
            n = conn.execute(f"DELETE FROM {t} WHERE pdl_index=?" if not a_blanc else
                             f"SELECT count(*) FROM {t} WHERE pdl_index=?", (pdl,))
            n = n.fetchone()[0] if a_blanc else n.rowcount
            if n:
                rapport["supprimees"][t] = rapport["supprimees"].get(t, 0) + n
        if not a_blanc:
            # L'émetteur ne se supprime pas : sans lui, une trame de courbe n'aurait
            # plus où se ranger tant qu'aucune trame de boot n'est repassée.
            conn.execute("UPDATE emitter SET pdl_index=?, adco=(SELECT adco FROM pdl "
                         "WHERE pdl_index=?) WHERE pdl_index=?", (vrai, vrai, pdl))
            conn.execute("DELETE FROM pdl WHERE pdl_index=?", (pdl,))
            rapport["supprimees"]["pdl"] = rapport["supprimees"].get("pdl", 0) + 1

    for pdl in sains:
        for ts, ngtf in epoques_bidon(conn, pdl):
            rapport["epoques"].append((pdl, ts, ngtf))
            if not a_blanc:
                conn.execute("DELETE FROM contract_epoch WHERE pdl_index=? AND ts_start=?",
                             (pdl, ts))
    if not a_blanc:
        conn.commit()
    return rapport


def sauvegarde(conn, chemin: str) -> int:
    """Écrit les lignes VISÉES en `INSERT` rejouables. Rend le nombre de lignes.

    🚨 Les bases embarquées ne sont PAS sauvegardées : une ligne supprimée par erreur est
    perdue définitivement. On ne copie pas la base entière (des centaines de Mo) mais
    exactement ce qu'on s'apprête à toucher — quelques Ko, qu'on garde jusqu'à validation.
    """
    fantomes = pdls_fantomes(conn)
    sains = [i for i, a in conn.execute("SELECT pdl_index, adco FROM pdl")
             if db.adco_valide(a or "")]
    n = 0
    with open(chemin, "w", encoding="utf-8") as f:
        f.write("-- menage_fantomes : lignes visees, rejouables tel quel\n")
        for pdl in fantomes:
            for table in ("pdl", "emitter") + A_REATTRIBUER + A_SUPPRIMER:
                for ligne in conn.execute(f"SELECT * FROM {table} WHERE pdl_index=?", (pdl,)):
                    vals = ",".join("NULL" if v is None else
                                    repr(v) if isinstance(v, str) else str(v) for v in ligne)
                    f.write(f"INSERT INTO {table} VALUES({vals});\n")
                    n += 1
        for pdl in sains:
            for ts, ngtf in epoques_bidon(conn, pdl):
                f.write(f"INSERT INTO contract_epoch VALUES({pdl},{ts},{ngtf!r});\n")
                n += 1
    return n


def conforme(conn) -> list:
    """L'INVARIANT du contrôle d'effet — vrai AVANT comme APRÈS sur un boîtier sain.
    ⭐ Le succès n'est PAS « j'ai supprimé quelque chose » : sur six boîtiers du parc il
       n'y a rien à faire, et c'est un succès. Exiger un effet ferait échouer l'update
       partout ailleurs, donc rejouer toutes les 10 min — la mécanique de pi-0.9.12."""
    ko = []
    if pdls_fantomes(conn):
        ko.append(f"PDL non conformes restants : {pdls_fantomes(conn)}")
    for i, a in conn.execute("SELECT pdl_index, adco FROM pdl"):
        if db.adco_valide(a or "") and epoques_bidon(conn, i):
            ko.append(f"epoques bidon restantes sur pdl {i}")
    connus = {i for i, in conn.execute("SELECT pdl_index FROM pdl")}
    for t in A_REATTRIBUER + A_SUPPRIMER:
        # ⚠️ `event.pdl_index` est NULLABLE — un événement sans PDL n'est pas une
        #    orpheline, et le compter en ferait échouer l'update sur un boîtier sain.
        orph = [i for i, in conn.execute(f"SELECT DISTINCT pdl_index FROM {t}")
                if i is not None and i not in connus]
        if orph:
            ko.append(f"{t} : pdl_index orphelins {orph}")
    return ko


if __name__ == "__main__":
    a_blanc = "--a-blanc" in sys.argv
    with sqlite3.connect(db.DB_PATH) as c:
        r = menage(c, a_blanc=a_blanc)
        tete = "MARCHE A BLANC — rien n'est touche" if a_blanc else "MENAGE"
        print(f"[{tete}] pdl sains={r['sains']} fantomes={r['fantomes']}")
        if r["refus"]:
            print(f"  REFUS : {r['refus']}")
            sys.exit(1)
        for t, n in sorted(r["deplacees"].items()):
            print(f"  reattribue  {n:>7} lignes  {t}")
        for t, n in sorted(r["supprimees"].items()):
            print(f"  supprime    {n:>7} lignes  {t}")
        for pdl, ts, ngtf in r["epoques"]:
            print(f"  epoque purgee  pdl {pdl}  ts={ts}  ngtf={ngtf!r}")
        ko = conforme(c)
        print("  invariant : " + ("OK" if not ko else " / ".join(ko)))
        sys.exit(0 if a_blanc or not ko else 1)
