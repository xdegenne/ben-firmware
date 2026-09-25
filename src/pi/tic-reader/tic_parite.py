"""Le contrôle de parité d'un octet TIC. Isolé exprès, sans aucune dépendance.

⭐ POURQUOI UN FICHIER À PART. `main_uart.py` importe `RPi.GPIO` au chargement :
   il ne s'importe donc pas ailleurs que sur un Pi. Tant que cette fonction y
   vivait, l'argument « le contrôle logiciel se teste sans boîtier » était FAUX.
   Ici, il est vrai — et le banc tourne sur n'importe quelle machine.
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

           ADCO 061947720012  -> checksum '>'
           ADCO p61947720012  -> checksum '>'   ← identique

       C'est ce trou qui a fabriqué les quatre PDL fantômes de ben-0004 :
       `0`→`p`, `1`→`q`, à chaque fois le bit 6. Quatre corruptions d'un seul
       caractère, prises pour quatre compteurs.

    ⚠️ Calculé exhaustivement sur les corruptions de 1 ou 2 bits :
           1 bit  → AUCUNE ne passe les deux contrôles réunis
           2 bits → 17 cas sur 612 passent encore
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
