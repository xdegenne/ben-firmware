# Renouvellement de certificat — le côté BOÎTIER

Ce document décrit ce que fait **le boîtier**. La politique (quand un certificat
est jugé à remplacer, qui signe, comment on révoque) appartient au serveur et
n'est pas décrite ici : le boîtier ne la connaît pas, et c'est délibéré.

Agent : `src/pi/certd/ben_certd.py` · unité : `ben-certd.service`

---

## Ce que c'est

Chaque boîtier BEN possède un certificat X.509 signé par la CA BEN. Il lui sert
à deux choses : s'authentifier auprès du cloud (mTLS), et s'identifier auprès de
l'application mobile sur le réseau local.

Un certificat a une durée de vie. `ben-certd` est l'agent qui s'assure que
celui du boîtier reste valide, **sans intervention humaine sur place**.

## Le principe

```
réveil quotidien (+ gigue)  →  GET :8444/  →  une directive  →  on obéit
```

Le boîtier ne décide rien. Il se présente, on lui dit quoi faire, il le fait.

⭐ **Le boîtier ne se souvient de rien.** Aucun état local, aucun fichier de
progression. S'il tenait le sien, il pourrait diverger de celui du serveur — et
ce serait toujours le boîtier qui aurait tort, sans moyen de le savoir.

La gigue est **pleine** (`random.uniform(période/2, période)`), pas un intervalle
fixe : sinon toute une flotte se réveillerait ensemble après une coupure de
courant générale.

## Les directives

| directive | ce que le boîtier fait |
|---|---|
| *(rien, `204`)* | le certificat convient — on se rendort |
| `csr` | engendre une demande de signature **pour sa clé actuelle** et la dépose |
| `csr+newkey` | engendre une **clé neuve** puis la demande. Rare, jamais automatique |
| `cert` | un certificat signé l'attend : on le contrôle, on l'essaie, on bascule |
| `pending` | la demande est déposée, la signature n'est pas encore faite — on attend |

🚨 **La clé privée ne quitte JAMAIS le boîtier.** Elle est engendrée sur place et
ne transite par aucun canal. Ce qui monte est une *demande de signature* : une
clé **publique**, accompagnée d'une signature qui prouve qu'on détient la privée.

## Les gardes, avant toute bascule

Un certificat qui ne correspond pas à la clé privée locale rend le boîtier
**muet** — et sans mTLS, il ne peut même plus signaler qu'il est cassé, sur un
appareil potentiellement injoignable. D'où cinq contrôles, du moins cher au plus
cher, et l'ordre compte :

1. **pas déjà expiré** — en premier, parce que `openssl verify` contrôle les
   dates *dans* la validation de chaîne : placé après, ce contrôle ne serait
   jamais atteint et un certificat expiré serait journalisé « chaîne invalide ».
2. **chaîne valide** contre `root-ca.crt`, celui que le boîtier possède.
3. 🚨 **la clé publique du certificat correspond à `device.key`** — le seul garde
   qui évite la brique définitive.
4. **le CN est bien celui du boîtier**.
5. ⭐ **une poignée de main mTLS complète avec le certificat candidat**, pendant
   que l'ancien est toujours en place. On ne *déduit* plus qu'il marchera : on
   l'a **utilisé**.

Ce n'est qu'après ces cinq contrôles que `device.crt` est remplacé, par écriture
atomique (`os.replace`) — une coupure en cours d'écriture laisserait sinon un PEM
tronqué, donc un boîtier sans identité lisible. L'ancien est conservé à côté.

## Tout échec est sans conséquence

⭐ **Un échec laisse le boîtier exactement dans l'état où il était.** L'ancien
certificat reste valide jusqu'à son échéance, les mesures continuent de partir,
et il n'y a jamais urgence à basculer. C'est ce qui rend l'opération sûre — donc
ce qui interdit de la bâcler.

En conséquence, l'unité n'a **ni `WatchdogSec`, ni `StartLimitAction=reboot`** :
un agent de certificat qui tombe ne doit jamais redémarrer le boîtier. Un
garde-fou plus destructeur que la panne qu'il traite est un défaut, pas une
protection.

Si le boîtier refuse le certificat qu'on lui a préparé, il le dit dans son
journal et ne le remplace pas. La tâche reste visible côté serveur, ce qui
distingue « n'a pas encore récupéré » de « a récupéré et refusé ».

## Pourquoi un service séparé du publisher

Une bascule de certificat exige de **redémarrer le publisher** : celui-ci charge
le certificat une seule fois, à la construction de son contexte SSL, et tient
ensuite une connexion persistante. Sans redémarrage il continuerait indéfiniment
avec l'ancien — sans la moindre erreur, donc sans que ça se voie.

Du code qui redémarre son propre processus au milieu d'une opération est la
recette d'un état à moitié appliqué. D'où deux agents :

    ben-publisher   pousse les mesures   ← ne doit JAMAIS être bloqué par la PKI
    ben-certd       gère l'identité      ← peut échouer sans conséquence

`ben-certd` tourne sous l'utilisateur `ben`. Sa **seule** opération privilégiée
est `systemctl restart ben-publisher`, accordée par `/etc/sudoers.d/ben-certd` —
un verbe, une unité. Pas `systemctl *`, pas `ALL` : un droit plus large que le
besoin est un droit qu'on finit par utiliser pour autre chose.

## Démarrage

`ben-certd` n'a pas d'`[Install]`/`WantedBy`. Il est lancé par
`ben-network-check.service`, dans la même branche que les lecteurs — donc
uniquement si le boîtier est **provisionné et en ligne**. Hors ligne il n'aurait
personne à qui parler, et son certificat actuel reste valide.

Ce n'est pas une *capability* : entretenir son certificat n'est pas une propriété
du matériel, comme le sont une radio ou une entrée TIC. C'est une fonction de
flotte, identique sur tous les boîtiers, qu'ils lisent la TIC par un fil ou par
radio.

## Bancs

    python3 src/pi/certd/test_certd.py              les cinq gardes
    python3 src/pi/provisioner/test_network_recovery.py   quels agents démarrent

Le banc des gardes ne vérifie pas seulement qu'un certificat conforme est
accepté : ce sont les **refus** qui le valident. Chaque cas correspond à une
panne qu'on aurait pu livrer.
