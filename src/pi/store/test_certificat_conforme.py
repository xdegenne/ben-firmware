"""`:8088` ne doit s'ouvrir qu'avec un certificat qu'un TÉLÉPHONE accepte.

═══ POURQUOI CE BANC EXISTE ═══════════════════════════════════════════════════

Deux correctifs justes, pris ensemble, produisaient une régression que ni l'un
ni l'autre n'avait :

  · l'app ne se replie plus en clair quand la poignée de main échoue — il le
    fallait, sinon un tiers pouvait provoquer le repli ;
  · `_ecoute_tls` ouvrait `:8088` avec N'IMPORTE QUEL certificat.

⇒ Un boîtier du parc encore sous certificat d'origine (« CN seul, zéro
   extension, 3650 jours ») qui reçoit pi-0.9.17 ouvre une écoute que l'iPhone
   REFUSE, et l'app n'a plus de repli : boîtier INJOIGNABLE. Un firmware qui
   casse une app — ce que la règle cardinale du projet interdit.

⭐ Ne pas ouvrir `:8088` n'est pas une panne : `:8087` sert, et l'app y retombe.

⚠️ Ce banc éprouve la DÉCISION (conforme / pas conforme), pas TLS lui-même.
   Le plafond des ~398 jours, lui, a été mesuré sur un vrai iPhone le 23/09.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import local_api  # noqa: E402

CAS = []


def cas(fn):
    CAS.append(fn)
    return fn


def _atelier() -> pathlib.Path:
    """Fabrique les quatre certificats du banc. ⚠️ Recréés à chaque exécution :
    un certificat figé dans le dépôt finirait par expirer et ferait rougir le
    banc pour une raison sans rapport."""
    d = pathlib.Path(tempfile.mkdtemp())
    script = d / "gen.sh"
    script.write_text(_GEN, encoding="utf-8")
    subprocess.run(["bash", str(script), str(d)], check=True,
                   capture_output=True)
    return d


_GEN = r"""
set -e
d=$1; cd "$d"
openssl genrsa -out ca.key 2048 2>/dev/null
openssl req -x509 -new -key ca.key -sha256 -days 3650 -out ca.crt -subj "/CN=BANC CA" 2>/dev/null
gen() {
  openssl genrsa -out "$1.key" 2048 2>/dev/null
  openssl req -new -key "$1.key" -out "$1.csr" -subj "/CN=ben-0001" 2>/dev/null
  printf '%s\n' "$3" > "$1.ext"
  openssl x509 -req -in "$1.csr" -CA ca.crt -CAkey ca.key -CAcreateserial \
    -out "$1.crt" -days "$2" -sha256 $([ -s "$1.ext" ] && echo "-extfile $1.ext") 2>/dev/null
}
gen conforme 180 "subjectAltName=DNS:ben-0001
extendedKeyUsage=serverAuth,clientAuth"
gen trop_long 3650 "subjectAltName=DNS:ben-0001"
gen sans_san 180 ""
gen mauvais_san 180 "subjectAltName=DNS:ben-0003"
"""


@cas
def un_certificat_conforme_est_accepte():
    """⚖️ LE TÉMOIN. Sans lui, une garde qui refuserait TOUT satisferait tous
    les autres cas — et `:8088` ne s'ouvrirait jamais, en silence."""
    d = _atelier()
    ok, raison = local_api._certificat_conforme(str(d / "conforme.crt"), "ben-0001")
    assert ok, f"un certificat conforme est refusé : {raison}"
    assert "180" in raison, raison


@cas
def trop_long_est_refuse():
    """3650 jours : c'est le parc tel qu'il est. Mesuré refusé par iOS le 23/09."""
    d = _atelier()
    ok, raison = local_api._certificat_conforme(str(d / "trop_long.crt"), "ben-0001")
    assert not ok, "un certificat de 10 ans est accepté"
    assert "398" in raison, raison


@cas
def sans_subjectAltName_est_refuse():
    d = _atelier()
    ok, raison = local_api._certificat_conforme(str(d / "sans_san.crt"), "ben-0001")
    assert not ok, "un certificat sans SAN est accepté"
    assert "subjectAltName" in raison, raison


@cas
def un_SAN_qui_designe_un_AUTRE_boitier_est_refuse():
    """⚠️ Le cas vicieux : le certificat est parfaitement formé et signé, mais
    il ne parle pas de CE boîtier. Un téléphone le refusera au nom."""
    d = _atelier()
    ok, raison = local_api._certificat_conforme(str(d / "mauvais_san.crt"), "ben-0001")
    assert not ok, "un certificat émis pour un autre boîtier est accepté"
    assert "ben-0003" in raison, raison


@cas
def un_certificat_illisible_ne_fait_PAS_ouvrir():
    """🚨 L'ignorance n'est pas une raison de parier. Ne pas savoir doit fermer
    `:8088`, pas l'ouvrir : le pire cas devient le comportement d'avant 0.9.17,
    l'app parle en clair."""
    d = _atelier()
    faux = d / "pas_un_certificat.crt"
    faux.write_text("ceci n'est pas un certificat", encoding="utf-8")
    ok, raison = local_api._certificat_conforme(str(faux), "ben-0001")
    assert not ok, "un fichier illisible fait ouvrir l'écoute chiffrée"
    assert "illisible" in raison, raison


@cas
def sans_device_id_connu_le_SAN_n_est_pas_exige_nominatif():
    """`device.json` illisible : on ne peut pas comparer au nom. On exige quand
    même un SAN — c'est lui qui manque sur les certificats d'origine."""
    d = _atelier()
    ok, _ = local_api._certificat_conforme(str(d / "mauvais_san.crt"), "")
    assert ok, "sans deviceId, un certificat par ailleurs conforme est refusé"
    ok2, _ = local_api._certificat_conforme(str(d / "sans_san.crt"), "")
    assert not ok2, "sans deviceId, l'absence de SAN devrait rester refusée"


if __name__ == "__main__":
    ko = 0
    for fn in CAS:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            ko += 1
            print(f"  KO   {fn.__name__} : {e}")
    print(f"\n{len(CAS) - ko}/{len(CAS)}")
    sys.exit(1 if ko else 0)
