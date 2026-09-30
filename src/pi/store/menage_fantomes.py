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
import json
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
               "supprimees": {}, "epoques": [], "evenements": [], "ngtf_recale": None,
               "refus": None}

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
        purge = epoques_bidon(conn, pdl)
        # 🚨 PAS `{n for _, n in purge}` : la purge emporte DEUX sortes de lignes — les
        #    ABÎMÉES (`HCn.`) et leurs JUMELLES, identiques à la référence (`HC..`). Le
        #    contrat de la jumelle est le BON : le mettre dans `abimes` ferait supprimer
        #    le `tariff_labels` légitime en croyant retirer le chimère. Seules les valeurs
        #    qui DIFFÈRENT de la référence sont abîmées.
        ref = conn.execute("SELECT ngtf FROM contract_epoch WHERE pdl_index=? "
                           "ORDER BY ts_start LIMIT 1", (pdl,)).fetchone()
        ref = ref[0] if ref is not None else None
        abimes = {n for _, n in purge if n != ref}
        for ts, ngtf in purge:
            rapport["epoques"].append((pdl, ts, ngtf))
            if not a_blanc:
                conn.execute("DELETE FROM contract_epoch WHERE pdl_index=? AND ts_start=?",
                             (pdl, ts))
        if not purge:
            continue

        # ⚠️ Une époque abîmée ne vit pas seule : `record_ngtf` écrit AUSSI un événement
        #    `changement_offre` et une ligne `tariff_labels`, et mémorise le contrat dans
        #    `level_profile.ngtf`. Supprimer la seule époque laisserait « votre contrat est
        #    passé de HC.. à HCn. » dans la cloche de l'app, et un libellé chimère.
        for eid, donnees in list(conn.execute(
                "SELECT id, donnees FROM event WHERE type='changement_offre' AND pdl_index=?",
                (pdl,))):
            try:
                d = json.loads(donnees or "{}")
            except Exception:
                continue
            # 🚨 CIBLÉ, jamais « tous les changement_offre » : un vrai changement d'offre
            #    est un FAIT que l'utilisateur a vu passer, il ne se réécrit pas.
            if (d.get("avant") or "") in abimes or (d.get("apres") or "") in abimes:
                rapport["evenements"].append((pdl, eid, d))
                if not a_blanc:
                    conn.execute("DELETE FROM event WHERE id=?", (eid,))

        for ngtf in sorted(abimes):
            n = conn.execute("SELECT count(*) FROM tariff_labels WHERE pdl_index=? AND ngtf=?",
                             (pdl, ngtf)).fetchone()[0]
            if n:
                rapport["supprimees"]["tariff_labels"] = \
                    rapport["supprimees"].get("tariff_labels", 0) + n
                if not a_blanc:
                    conn.execute("DELETE FROM tariff_labels WHERE pdl_index=? AND ngtf=?",
                                 (pdl, ngtf))

        # ⭐ LE GESTE QU'ON OUBLIERAIT : `record_ngtf` est write-on-change contre
        #    `level_profile.ngtf`, PAS contre `contract_epoch`. Le laisser à `HCn.` ferait
        #    émettre à la trame suivante un FAUX « Changement d'offre » et rouvrirait une
        #    époque datée d'AUJOURD'HUI — on réécrirait l'histoire en croyant la réparer.
        row = conn.execute("SELECT ngtf FROM level_profile WHERE pdl_index=?", (pdl,)).fetchone()
        if row is not None and (row[0] or "") in abimes:
            reste = conn.execute("SELECT ngtf FROM contract_epoch WHERE pdl_index=? "
                                 "ORDER BY ts_start DESC LIMIT 1", (pdl,)).fetchone()
            if reste is not None:
                rapport["ngtf_recale"] = (pdl, row[0], reste[0])
                if not a_blanc:
                    conn.execute("UPDATE level_profile SET ngtf=? WHERE pdl_index=?",
                                 (reste[0], pdl))
    if not a_blanc:
        conn.commit()
    return rapport


def _q(v) -> str:
    """Cite une valeur pour SQLite.

    ⚠️ `repr()` de Python N'EST PAS une citation SQL : une chaîne contenant `'` sort
    entre guillemets DOUBLES (acceptée seulement par le repli hérité de SQLite), et une
    chaîne contenant les deux sort avec un `\'` que SQLite REJETTE — la réinjection
    entière échouerait sur une erreur de syntaxe. `event.corps` porte du français avec
    apostrophes (« C'est fait : votre contrat est passé de… ») et `donnees` du JSON.
    """
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def sauvegarde(conn, chemin: str) -> int:
    """Écrit le RETOUR ARRIÈRE des lignes visées. Rend le nombre d'instructions.

    🚨 Les bases embarquées ne sont PAS sauvegardées : une ligne perdue l'est pour de
    bon. On ne copie pas la base entière (des centaines de Mo) mais exactement de quoi
    défaire ce qu'on s'apprête à faire.

    ⭐ Ce ne sont PAS que des `INSERT`. Les mesures sont DÉPLACÉES, pas supprimées, et
    `measurements`/`lora_link` n'ont aucune clé primaire : réinsérer leurs lignes après
    coup les mettrait en DOUBLE — l'exemplaire déplacé sous le vrai PDL, plus
    l'exemplaire restauré sous le fantôme. Le retour arrière d'un `UPDATE` est un
    `UPDATE`, ciblé par `rowid`, et il restaure aussi le `sent` d'origine — que le
    ménage remet à 0.
    """
    fantomes = pdls_fantomes(conn)
    sains = [i for i, a in conn.execute("SELECT pdl_index, adco FROM pdl")
             if db.adco_valide(a or "")]
    n = 0
    with open(chemin, "w", encoding="utf-8") as f:
        f.write("-- menage_fantomes : RETOUR ARRIERE. A rejouer tel quel pour defaire.\n")
        for pdl in fantomes:
            for table in A_REATTRIBUER:          # déplacées → on les REMET
                for rowid, sent in conn.execute(
                        f"SELECT rowid, sent FROM {table} WHERE pdl_index=?", (pdl,)):
                    f.write(f"UPDATE {table} SET pdl_index={pdl}, sent={sent} "
                            f"WHERE rowid={rowid};\n")
                    n += 1
            for table in ("pdl",) + A_SUPPRIMER:  # supprimées → on les REINSERE
                cols = [c[1] for c in conn.execute(f"PRAGMA table_info({table})")]
                for ligne in conn.execute(f"SELECT * FROM {table} WHERE pdl_index=?", (pdl,)):
                    f.write(f"INSERT OR REPLACE INTO {table}({','.join(cols)}) VALUES("
                            + ",".join(_q(v) for v in ligne) + ");\n")
                    n += 1
            for addr, adco, pi in conn.execute(   # ré-orientée → on la REORIENTE
                    "SELECT lora_addr, adco, pdl_index FROM emitter WHERE pdl_index=?", (pdl,)):
                f.write(f"UPDATE emitter SET adco={_q(adco)}, pdl_index={pi} "
                        f"WHERE lora_addr={addr};\n")
                n += 1
        for pdl in sains:
            purge = epoques_bidon(conn, pdl)
            for ts, ngtf in purge:
                f.write(f"INSERT OR REPLACE INTO contract_epoch VALUES({pdl},{ts},{_q(ngtf)});\n")
                n += 1
            if purge:
                row = conn.execute("SELECT ngtf FROM level_profile WHERE pdl_index=?",
                                   (pdl,)).fetchone()
                if row is not None:
                    f.write(f"UPDATE level_profile SET ngtf={_q(row[0])} "
                            f"WHERE pdl_index={pdl};\n")
                    n += 1
                for eid, in conn.execute(
                        "SELECT id FROM event WHERE type='changement_offre' AND pdl_index=?",
                        (pdl,)):
                    pass   # les events sont sauvés ci-dessous, avec leurs colonnes
                cols = [c[1] for c in conn.execute("PRAGMA table_info(event)")]
                for ligne in conn.execute(
                        "SELECT * FROM event WHERE type='changement_offre' AND pdl_index=?",
                        (pdl,)):
                    f.write(f"INSERT OR REPLACE INTO event({','.join(cols)}) VALUES("
                            + ",".join(_q(v) for v in ligne) + ");\n")
                    n += 1
                cols = [c[1] for c in conn.execute("PRAGMA table_info(tariff_labels)")]
                for ligne in conn.execute(
                        "SELECT * FROM tariff_labels WHERE pdl_index=?", (pdl,)):
                    f.write(f"INSERT OR REPLACE INTO tariff_labels({','.join(cols)}) VALUES("
                            + ",".join(_q(v) for v in ligne) + ");\n")
                    n += 1
    return n


def code_sortie(rapport: dict, invariant_ko: list) -> int:
    """🚨 AUCUN ÉTAT DE LA DONNÉE NE FAIT ÉCHOUER CETTE UPDATE. Toujours 0.

    Ce n'est pas de la complaisance, c'est la leçon de pi-0.9.12 appliquée à une migration
    de données. Un `update.sh` qui échoue laisse `device.json` non bumpé, donc l'update
    REJOUE toutes les 10 min — et ici ce serait DÉFINITIF, puisque aucune version ultérieure
    ne pourrait plus atteindre le boîtier.

    Deux états parfaitement légitimes seraient pris pour des pannes :
      · le REFUS (0 ou 2+ PDL sains) — « je ne sais pas vers qui réattribuer » est voulu ;
      · un invariant encore faux — une anomalie que ce ménage-ci ne sait pas réparer ne
        rend la base ni pire qu'avant, ni urgente.

    Ce qui DOIT faire échouer l'update vit ailleurs, dans `update.sh` : le préflight (code
    cassé — on veut retenter au prochain tag) et « un service arrêté n'est pas revenu »
    (dégât réel et réparable). Pas la donnée.
    """
    return 0


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
        c.row_factory = sqlite3.Row
        r = menage(c, a_blanc=a_blanc)
        tete = "MARCHE A BLANC — rien n'est touche" if a_blanc else "MENAGE"
        print(f"[{tete}] pdl sains={r['sains']} fantomes={r['fantomes']}")

        # 🚨 LE REFUS EST UN SUCCES, ET LE CODE DE SORTIE LE DIT.
        #
        #    « Je ne sais pas vers qui reattribuer, donc je ne touche a rien » est un etat
        #    STABLE et VOULU — pas une panne. Sortir en 1 ferait echouer l'update.sh, donc
        #    `device.json` ne serait jamais bumpe, donc l'update REJOUERAIT toutes les
        #    10 min POUR TOUJOURS, et aucune OTA ulterieure ne passerait plus jamais sur ce
        #    boitier. C'est exactement la mecanique qui a brule pi-0.9.12, et elle serait
        #    ici DEFINITIVE puisque aucune version suivante ne pourrait l'atteindre.
        if r["refus"]:
            print(f"  REFUS (volontaire, rien touche) : {r['refus']}")
            sys.exit(0)

        for t_, n in sorted(r["deplacees"].items()):
            print(f"  reattribue  {n:>7} lignes  {t_}")
        for t_, n in sorted(r["supprimees"].items()):
            print(f"  supprime    {n:>7} lignes  {t_}")
        for pdl, ts, ngtf in r["epoques"]:
            print(f"  epoque purgee   pdl {pdl}  ts={ts}  ngtf={ngtf!r}")
        for pdl, eid, don in r["evenements"]:
            print(f"  event purge     pdl {pdl}  {don.get('avant')!r} -> {don.get('apres')!r}")
        if r["ngtf_recale"]:
            pdl, avant, apres = r["ngtf_recale"]
            print(f"  level_profile   pdl {pdl}  ngtf {avant!r} -> {apres!r}")

        ko = conforme(c)
        print("  invariant : " + ("OK" if not ko else " / ".join(ko)))

        # ⚠️ Et meme ici : un invariant encore faux n'est PAS une raison de sortir en
        #    erreur. La base n'est pas pire qu'avant, la sauvegarde existe, et le journal
        #    le dit. Un etat de DONNEE ne doit jamais bloquer le parc.
        sys.exit(code_sortie(r, ko))
