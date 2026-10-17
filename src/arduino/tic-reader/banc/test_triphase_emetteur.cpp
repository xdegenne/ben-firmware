// test_triphase_emetteur.cpp — le parseur TIC de l'émetteur, EXÉCUTÉ sur l'hôte.
//
// 🚨 IL COMPILE LE TEXTE RÉEL DU `.ino` (via extrait_reel.py), il n'en recopie
//    pas une ligne. Un banc qui testerait une copie resterait vert le jour où le
//    sketch diverge — c'est-à-dire exactement le jour où il servirait.
//
// ⭐ C'est le PREMIER banc du sketch qui exécute quoi que ce soit : les deux
//    autres (`test_version`, `test_buffer_partage`) lisent la source. Il a été
//    écrit pour répondre à une question qui ne se répond pas en lisant :
//    « additionne-t-on deux IINST1 d'une même trame, ou l'IINST1 d'une autre ? »
//
// Lancement :  ./run-banc-triphase.sh
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <cstdlib>
typedef uint8_t byte;
#include "reel.h"

static int echecs = 0;

static void verifie(const char* nom, long obtenu, long attendu) {
  if (obtenu == attendu) printf("  ✓ %s\n", nom);
  else { printf("  ✗ %s\n      attendu %ld, obtenu %ld\n", nom, attendu, obtenu); echecs++; }
}

// 🚨 LE GROUPE SE FORGE AVEC SON CHECKSUM, et avec SES DEUX ESPACES.
//    `parseTICLine` cherche le PREMIER et le DERNIER espace et exige
//    `ls > fs` : une ligne « LABEL VALEUR » sans checksum n'en a qu'un, et la
//    fonction rend la main SANS RIEN FAIRE.
// ⚠️ La première version de ce banc l'ignorait : elle rendait 0 partout, y
//    compris sur le monophasé, et j'ai d'abord accusé le code. Un harnais qui
//    ne parle pas à la fonction qu'il teste est vert sur n'importe quoi.
static void forge(char* out, const char* label, const char* valeur) {
  int n = snprintf(out, 48, "%s %s", label, valeur);
  unsigned s = 0;
  for (int i = 0; i < n; i++) s += (unsigned char)out[i];
  out[n] = ' ';
  out[n + 1] = (char)((s & 0x3F) + 0x20);
  out[n + 2] = 0;
}

// Une TRAME = un memset, puis N lignes. C'est le contrat de `readAndParseTIC`,
// dont la toute première instruction est `memset(&v, 0, sizeof(v))` — AVANT même
// d'attendre le STX. C'est ce qui rend impossible qu'une phase franchisse une
// frontière de trame, et ce banc le reproduit fidèlement.
static void trame(TICValues& v, const char* const* lignes, int n) {
  memset(&v, 0, sizeof(v));
  for (int i = 0; i < n; i++) {
    const char* sp = strchr(lignes[i], ' ');
    char lab[24];
    size_t k = (size_t)(sp - lignes[i]);
    memcpy(lab, lignes[i], k); lab[k] = 0;
    char buf[64];
    forge(buf, lab, sp + 1);
    parseTICLine(buf, (uint8_t)strlen(buf), v);
  }
}

int main() {
  TICValues v;

  printf("── triphasé ────────────────────────────────────────────────\n");
  const char* tri[] = {"IINST1 012", "IINST2 004", "IINST3 007", "PAPP 05290"};
  trame(v, tri, 4);
  verifie("IINST = 12 + 4 + 7 (⚖️ DOIT TOMBER sans le correctif)", v.iinst, 23);
  verifie("   le drapeau IINST est posé", (v.fields_seen & TIC_SEEN_IINST) != 0, 1);
  verifie("   les trois phases sont vues (bitmap 0x07)", v.ph_vues, 0x07);
  verifie("   230 × IINST ≈ PAPP", 230 * (long)v.iinst, 5290);

  printf("── les deux questions qui ont motivé ce banc ───────────────\n");
  // 🚨 ① Une trame corrompue qui RÉPÈTE un groupe.
  const char* deuxfois[] = {"IINST1 012", "IINST1 012", "IINST2 004", "IINST3 007"};
  trame(v, deuxfois, 4);
  verifie("① IINST1 répété n'est PAS compté deux fois", v.iinst, 23);

  // 🚨 ② Une phase venue d'une AUTRE trame. Le memset doit tout effacer.
  const char* t1[] = {"IINST1 012", "IINST2 004"};
  const char* t2[] = {"IINST3 007", "IINST1 012", "IINST2 004"};
  trame(v, t1, 2);
  verifie("   trame 1 incomplète (2/3) → AUCUN IINST", v.iinst, 0);
  verifie("   et le drapeau n'est PAS posé", (v.fields_seen & TIC_SEEN_IINST) != 0, 0);
  trame(v, t2, 3);
  verifie("② la trame 2 ne récupère RIEN de la trame 1", v.iinst, 23);

  printf("── une somme partielle est une donnée FAUSSE ───────────────\n");
  const char* manque[] = {"IINST1 012", "IINST3 007", "PAPP 05290"};
  trame(v, manque, 3);
  verifie("IINST2 perdu → AUCUN IINST (surtout pas 19)", v.iinst, 0);
  verifie("   et PAPP survit : on perd le courant, pas la mesure", v.papp, 5290);

  const char* zero[] = {"IINST1 000", "IINST2 000", "IINST3 000"};
  trame(v, zero, 3);
  verifie("trois phases à 0 → IINST = 0 et drapeau POSÉ (0 ≠ absent)",
          (v.fields_seen & TIC_SEEN_IINST) != 0, 1);

  printf("── ⚖️ CONTRE-TÉMOINS monophasés — le parc en est fait ──────\n");
  const char* mono[] = {"IINST 009", "PAPP 02070"};
  trame(v, mono, 2);
  verifie("IINST seul = 9, inchangé", v.iinst, 9);
  verifie("   aucune phase vue, donc pas de bit TRIPHASÉ", v.ph_vues, 0);
  const char* mono0[] = {"IINST 000", "PAPP 00000"};
  trame(v, mono0, 2);
  verifie("IINST 000 pose quand même son drapeau",
          (v.fields_seen & TIC_SEEN_IINST) != 0, 1);

  printf("── étiquettes voisines : on ne mord pas sur le préfixe ─────\n");
  // ⚠️ `strncmp(name, "IINST", 5)` attrape tout ce qui commence par IINST.
  //    Ces cas gardent la discrimination sur le 6e caractère.
  const char* bizarre[] = {"IINST4 012", "IINST12 005", "IINSTX 009", "PAPP 01000"};
  trame(v, bizarre, 4);
  verifie("IINST4 / IINST12 / IINSTX sont IGNORÉS", v.iinst, 0);
  verifie("   aucune phase comptée", v.ph_vues, 0);
  verifie("   et PAPP passe toujours", v.papp, 1000);

  printf("\n");
  if (echecs) { printf("❌ %d cas en échec\n", echecs); return 1; }
  printf("✅ tous les cas passent\n");
  return 0;
}
