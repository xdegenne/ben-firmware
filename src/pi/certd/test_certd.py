#!/usr/bin/env python3
"""Banc des GARDES de ben-certd — ce qui décide qu'un boîtier vit ou devient muet.

🚨 Le garde n°2 (clé publique du certificat == clé privée locale) est le seul qui
empêche la BRIQUE DÉFINITIVE : un certificat qui ne correspond pas à la clé
locale rend le boîtier muet, et sans mTLS il ne peut même plus signaler qu'il est
cassé — sur un appareil potentiellement injoignable.

⭐ Chaque cas est une panne réelle qu'on aurait pu livrer. Le cas « conforme »
seul ne prouverait rien : ce sont les REFUS qui valident les gardes.

    python3 src/pi/certd/test_certd.py
"""
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import ben_certd as certd  # noqa: E402

CAS = []


def cas(fn):
    CAS.append(fn)
    return fn


# ⭐ L'AGENT tourne avec OpenSSL 3 sur le Pi. Le banc doit donc forger ses
#    certificats avec le MÊME outil — sinon il valide un monde qui n'existe pas.
#    (macOS livre LibreSSL, qui refuse `-not_after` et les durées négatives.)
OSSL = next((p for p in ("/opt/homebrew/opt/openssl@3/bin/openssl",
                         "/usr/local/opt/openssl@3/bin/openssl")
             if shutil.which(p)), "openssl")


def ossl(*a, entree=None):
    return subprocess.run([OSSL, *a], input=entree,
                          capture_output=True, check=True).stdout


class Atelier:
    """Une CA et de quoi forger des certificats aux propriétés voulues."""

    def __init__(self, d: pathlib.Path, nom="BEN Root CA"):
        self.d = d
        self.ca_key = d / f"{nom}.key"
        self.ca_crt = d / f"{nom}.crt"
        ossl("ecparam", "-genkey", "-name", "prime256v1", "-out", str(self.ca_key))
        # ⚠️ Un FICHIER de configuration, pas `-addext` : le banc tourne sur
        #    macOS (LibreSSL) alors que l'agent tourne sur le Pi (OpenSSL 3), et
        #    `-addext` n'est pas portable entre les deux.
        cnf = d / f"{nom}.cnf"
        cnf.write_text(
            "[req]\ndistinguished_name=dn\nx509_extensions=v3_ca\nprompt=no\n"
            f"[dn]\nCN={nom}\nO=BEN\n"
            "[v3_ca]\nbasicConstraints=critical,CA:TRUE\n"
            "keyUsage=critical,keyCertSign,cRLSign\n")
        ossl("req", "-x509", "-new", "-key", str(self.ca_key), "-days", "3650",
             "-config", str(cnf), "-extensions", "v3_ca", "-out", str(self.ca_crt))

    def cle(self, nom: str) -> pathlib.Path:
        p = self.d / f"{nom}.key"
        ossl("ecparam", "-genkey", "-name", "prime256v1", "-out", str(p))
        return p

    def certificat(self, nom: str, cle: pathlib.Path, cn: str,
                   jours: int = 180, expire: bool = False) -> pathlib.Path:
        csr = self.d / f"{nom}.csr"
        crt = self.d / f"{nom}.crt"
        ext = self.d / f"{nom}.ext"
        ext.write_text(f"subjectAltName=DNS:{cn}\n"
                       "extendedKeyUsage=serverAuth,clientAuth\n"
                       "basicConstraints=critical,CA:FALSE\n")
        ossl("req", "-new", "-key", str(cle), "-subj", f"/CN={cn}/O=BEN",
             "-out", str(csr))
        dates = ["-days", str(jours)]
        if expire:
            # Deux ans en arrière, expiré depuis un an : le cas du boîtier
            # revenu après une longue absence.
            hier = time.gmtime(time.time() - 365 * 86400)
            avant = time.gmtime(time.time() - 730 * 86400)
            dates = ["-not_before", time.strftime("%Y%m%d%H%M%SZ", avant),
                     "-not_after", time.strftime("%Y%m%d%H%M%SZ", hier)]
        ossl("x509", "-req", "-in", str(csr), "-CA", str(self.ca_crt),
             "-CAkey", str(self.ca_key), "-CAcreateserial", *dates,
             "-extfile", str(ext), "-out", str(crt))
        return crt


def monde():
    """Un boîtier ben-0001 avec sa CA, sa clé et son certificat en service."""
    d = pathlib.Path(tempfile.mkdtemp())
    a = Atelier(d)
    cle = a.cle("device")
    crt = a.certificat("device", cle, "ben-0001")
    shutil.copy(a.ca_crt, d / "root-ca.crt")
    certd.CA = str(d / "root-ca.crt")
    return d, a, cle, crt


# ── Le cas nominal ────────────────────────────────────────────────────────────

@cas
def un_certificat_conforme_est_accepte():
    d, a, cle, _ = monde()
    neuf = a.certificat("neuf", cle, "ben-0001")
    assert certd.controler(str(neuf), str(cle), "ben-0001") is None


# ── 🚨 Le garde qui empêche la brique ─────────────────────────────────────────

@cas
def un_certificat_pour_une_AUTRE_cle_est_refuse():
    """🚨 LE cas catastrophique. Arrive si l'archive opérateur est périmée — un
    boîtier reprovisionné depuis a une autre clé. Sans ce garde, il devient MUET
    et ne peut plus signaler qu'il est cassé."""
    d, a, cle, _ = monde()
    autre = a.cle("autre")
    trompeur = a.certificat("trompeur", autre, "ben-0001")
    motif = certd.controler(str(trompeur), str(cle), "ben-0001")
    assert motif is not None and "clé publique" in motif, motif


# ── Les autres gardes ─────────────────────────────────────────────────────────

@cas
def une_CA_etrangere_est_refusee():
    d, a, cle, _ = monde()
    pirate = Atelier(d, "CA pirate")
    faux = pirate.certificat("faux", cle, "ben-0001")
    motif = certd.controler(str(faux), str(cle), "ben-0001")
    assert motif is not None and "chaîne" in motif, motif


@cas
def le_certificat_d_un_autre_boitier_est_refuse():
    d, a, cle, _ = monde()
    voisin = a.certificat("voisin", cle, "ben-0010")
    motif = certd.controler(str(voisin), str(cle), "ben-0001")
    assert motif is not None and "CN" in motif, motif


@cas
def un_certificat_deja_expire_est_refuse():
    d, a, cle, _ = monde()
    mort = a.certificat("mort", cle, "ben-0001", expire=True)
    motif = certd.controler(str(mort), str(cle), "ben-0001")
    assert motif is not None and "expiré" in motif, motif


# ── L'écriture atomique ───────────────────────────────────────────────────────

@cas
def l_ecriture_est_atomique_et_ne_laisse_rien_derriere():
    """⚠️ Une coupure pendant l'écriture laisserait sinon un PEM tronqué — donc
    un boîtier sans identité lisible."""
    d = pathlib.Path(tempfile.mkdtemp())
    cible = d / "device.crt"
    cible.write_text("ANCIEN")
    certd.ecrire_atomique(str(cible), "NOUVEAU")
    assert cible.read_text() == "NOUVEAU"
    restes = [p.name for p in d.iterdir() if p.name.startswith(".certd-")]
    assert restes == [], f"fichiers temporaires laissés : {restes}"


@cas
def le_CN_est_lu_dans_le_certificat_pas_ailleurs():
    """⭐ Source de vérité : le certificat lui-même — c'est lui qui fait foi
    auprès du serveur, pas device.json."""
    d, a, cle, crt = monde()
    certd.CRT = str(crt)
    assert certd.device_id() == "ben-0001"


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
