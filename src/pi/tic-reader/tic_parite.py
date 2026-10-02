"""Les contrôles d'un octet TIC — parité et alphabet. Isolés exprès, sans dépendance.

⭐ POURQUOI UN FICHIER À PART. `main_uart.py` importe `RPi.GPIO` au chargement :
   il ne s'importe donc pas ailleurs que sur un Pi. Tant que ces fonctions y
   vivaient, l'argument « le contrôle logiciel se teste sans boîtier » était FAUX.
   Ici, il est vrai — et le banc tourne sur n'importe quelle machine.

⭐ ET POURQUOI L'ALPHABET ATTERRIT ICI plutôt que dans un `tic_alphabet.py` neuf :
   un fichier NEUF doit être copié par l'`update.sh` de la version qui le livre, et
   s'il est oublié le lecteur meurt sur `ImportError` — en boucle, puisque systemd le
   relance. C'est le mode de défaillance qui a brûlé pi-0.9.0 (paho oublié). Ajouter
   une fonction à un fichier DÉJÀ déployé n'a pas ce risque. Le nom du module parle
   donc des contrôles d'un octet, pas de la seule parité.
"""
from __future__ import annotations

# Parité d'un octet, précalculée : PARITE[x] vaut 1 si x a un nombre IMPAIR de
# bits à 1. Une table plutôt qu'un `bin().count()` — mesuré sur Pi Zero :
# 2,24 µs/octet contre 5,82, soit 0,22 % de CPU à 9600 bauds au lieu de 0,56 %.
PARITE = bytes(bin(i).count("1") & 1 for i in range(256))


def octet_valide(octet: int) -> bool:
    """La TIC est en 7E1 : le 8e bit porte la parité PAIRE des sept autres.

    🚨 CE BIT ÉTAIT REÇU PUIS JETÉ. Le port est ouvert en 8N1, donc l'UART nous
       remet fidèlement le bit calculé par le compteur — et `& 0x7F` l'effaçait
       sans jamais le regarder. L'information de détection d'erreur arrivait
       jusqu'à nous, et on la mettait à la poubelle.

    ⭐ POURQUOI ÇA COMPTE : le checksum TIC vaut `(somme & 0x3F) + 0x20`, il ne
       voit donc la somme que MODULO 64. Basculer le bit 6 ajoute exactement 64
       — et le masque jette cette retenue. Par construction, cette erreur lui est
       INVISIBLE :

           ADCO 061947000000  -> checksum '2'
           ADCO p61947000000  -> checksum '2'   ← identique

       C'est ce trou qui a fabriqué les quatre PDL fantômes de ben-0004 :
       `0`→`p`, `1`→`q`, à chaque fois le bit 6. Quatre corruptions d'un seul
       caractère, prises pour quatre compteurs.

    ⚠️ Calculé exhaustivement sur les corruptions de 1 ou 2 bits :
           1 bit  → AUCUNE ne passe les deux contrôles réunis
           2 bits → 17 cas sur 612 passent encore

    🚨 MAIS CE COMPTE PORTE SUR LE CARACTÈRE, PAS SUR LA LIGNE — et la première
       version du lecteur confondait les deux. Elle jetait le caractère fautif et
       gardait le reste de la ligne, en supposant que le checksum rattraperait
       l'amputation. Il ne peut pas : aveugle modulo 64, il valide toute ligne
       dont les caractères retirés somment à un multiple de 64.

           2 espaces  2 × 0x20 = 64   ← et l'espace est le SÉPARATEUR
           4 zéros    4 × 0x30 = 192  ← et le zéro est dans tous les INDEX
           1 arobase  1 × 0x40 = 64

       « 001000000 » amputé de quatre zéros devient « 00100 », même checksum,
       index FAUX enregistré. L'affirmation ci-dessus ne vaut donc QUE parce que
       `read_frame` condamne désormais la ligne ENTIÈRE dès qu'un de ses
       caractères échoue à la parité — sans consulter le checksum, qui ne peut
       pas trancher. Voir `test_read_frame.py` :
       `une_ligne_amputee_est_REJETEE_meme_si_le_checksum_la_valide`.
       La parité ne rend pas les erreurs impossibles ; elle supprime l'angle
       MORT SYSTÉMATIQUE du checksum. On passe d'un événement simple et fréquent
       qui traverse, à une coïncidence rare — deux bits dans le même caractère,
       sur les ~7 ms de sa transmission à 1200 bauds.

    ⚠️ Et pourquoi en logiciel plutôt qu'en `termios` : mesuré sur ben-0003,
       `pyserial` n'active PAS `INPCK` même avec `PARITY_EVEN`. Le noyau reçoit
       le bit et ne le vérifie pas — passer le port en 7E1 n'aurait donc RIEN
       changé, tout en donnant l'air d'un correctif. Le contrôle logiciel, lui,
       ne dépend d'aucun drapeau, marche aussi sur mini-UART, se teste sans
       boîtier — et laisse COMPTER les rejets, ce que le noyau fait en silence.
    """
    return PARITE[octet & 0x7F] == (octet >> 7) & 1


# ─── L'alphabet légal d'un groupe TIC ───────────────────────────────────────
#
# `Enedis-NOI-CPT_54E` §6.2.1.2 : le champ « donnée » ne contient que des caractères
# ASCII IMPRIMABLES, soit 0x20 à 0x7E. L'alphabet est donc CONNU D'AVANCE, et tout
# octet hors de cet ensemble est une erreur par définition de la norme — pas une
# heuristique.
#
# 🚨 CE CONTRÔLE N'EST REDONDANT AVEC AUCUN DES DEUX AUTRES.
#
#    Pas avec la PARITÉ : la parité paire détecte un nombre IMPAIR de bits
#    retournés. DEUX bits retournés dans le même octet la traversent intacts, et
#    peuvent très bien produire un octet hors alphabet. Ce contrôle attrape donc une
#    partie de ce que la parité ne peut STRUCTURELLEMENT pas voir.
#
#    Pas avec le CHECKSUM : il vaut (somme & 0x3F) + 0x20, donc il ne voit la somme
#    que MODULO 64 — l'angle mort déjà documenté plus haut.
#
# ⭐ Trois filtres indépendants, chacun couvrant l'angle mort des deux autres, pour
#    trois comparaisons par octet.
HT = 0x09   # séparateur de champ du mode STANDARD (§5.3.6)


def octet_dans_alphabet(octet: int) -> bool:
    """Mode HISTORIQUE : ASCII imprimables 0x20-0x7E, et rien d'autre.

    🚨 `HT` EST REFUSÉ ICI, ET CE N'EST PAS UN OUBLI. En historique le séparateur
       est l'espace ; `HT` n'y est pas un caractère légal. Et l'accepter « pour
       simplifier » rouvrirait exactement le trou que ce contrôle vient fermer :
       `'I'` vaut 0x49, `HT` vaut 0x09, soit 0x40 d'écart — donc **64**, donc
       `(somme & 0x3F)` inchangé, donc **checksum IDENTIQUE**. Un seul octet abîmé
       de cette façon passerait les trois filtres.

    ⓘ Le masque `& 0x7F` est là pour que le prédicat dise la vérité sur l'octet TEL
      QU'IL ARRIVE d'un port 8N1, bit de parité compris : 0x80 est hors alphabet, et
      le reste après masque aussi s'il n'est pas imprimable.
    """
    o = octet & 0x7F
    return 0x20 <= o <= 0x7E


def octet_dans_alphabet_std(octet: int) -> bool:
    """Mode STANDARD : les mêmes, PLUS `HT` (0x09) qui y sépare légalement les champs.

    ⚠️ Refuser `HT` en standard condamnerait TOUS les groupes, donc rendrait le
       lecteur muet. C'est le témoin que le banc doit porter : un contrôle trop
       strict ne se voit pas dans les cas de refus, seulement dans les cas de
       PASSAGE.
    """
    o = octet & 0x7F
    return 0x20 <= o <= 0x7E or o == HT


# ─── Les mêmes, en TABLE — c'est la forme que `read_frame` consomme ─────────
#
# ⭐ MESURÉ SUR PI ZERO W (armv6l, Python 3.9), coût par octet et part de CPU à
#    9600 bauds, en `nice -19` :
#
#        appel de fonction  `not f(b)`     10,73 µs     1,030 %
#        table              `not T[b]`      2,97 µs     0,285 %
#        témoin : appel à vide              3,02 µs        —
#
#    Un appel de fonction Python coûte À LUI SEUL 3 µs sur cette machine, soit la
#    quasi-totalité du coût du prédicat : la table est donc ×3,6 moins chère, et
#    revient au prix d'une instruction vide. L'issue #10 exige que le contrôle ne
#    coûte « RIEN sur une ligne saine » — avec 0,036 % de CPU à 1200 bauds, c'est
#    tenu. Même raison et même forme que la table `PARITE` ci-dessus.
#
# 🚨 128 ENTRÉES, PAS 256, et ce n'est pas une économie : `read_frame` indexe avec
#    l'octet DÉJÀ masqué (`b = raw[0] & 0x7F`), après que la parité a jugé le
#    huitième bit. Une table de 256 laisserait croire qu'on peut l'indexer avec
#    l'octet brut, et donc qu'on se passe du masque.
#
# ⭐ DÉRIVÉES DES PRÉDICATS, jamais réécrites à la main : une table et un prédicat
#    qui divergent, c'est un contrôle qui dit deux choses selon l'appelant. Le banc
#    le vérifie quand même sur les 128 valeurs, comme il le fait pour `PARITE`.
ALPHABET_HISTO = bytes(1 if octet_dans_alphabet(i) else 0 for i in range(128))
ALPHABET_STD   = bytes(1 if octet_dans_alphabet_std(i) else 0 for i in range(128))
