// tic-relay — relaie la téléinformation BRUTE vers le FTDI, octet par octet.
//
// POURQUOI : le Pro Mini n'a qu'UN seul UART, et c'est celui qui lit la TIC (RX relié à la
// sortie du front-end optocoupleur). Impossible d'ouvrir un second port pour observer.
// Mais l'UART est FULL DUPLEX : on peut réémettre sur TX ce qu'on reçoit sur RX, sans
// toucher au câblage. Le FTDI, branché sur TX, voit alors la trame telle que l'Arduino la
// reçoit — y compris les champs que le firmware de production jette en silence (STGE).
//
// À LIRE côté PC avec les MÊMES paramètres : 9600 bauds, 7 bits, parité paire, 1 stop.
//   python3 tic-sniff.py --baud 9600
//
// ⚠️ Ce sketch REMPLACE tic-reader : plus d'émission LoRa tant qu'il tourne. Reflasher le
//    firmware de production après la session. L'upload n'efface PAS l'EEPROM, donc la clé
//    ChaCha20 et le mode persisté survivent.
//
// ⚠️ DÉCONNECTER LA SUPERCAP avant tout flash (elle maintient l'alim et empêche le reset
//    auto par DTR — l'upload échoue ou part en vrille).
//
// Historique au lieu de standard : passer BAUD à 1200. Le format 7E1 est le même.

#include <avr/wdt.h>

static const long BAUD = 9600;      // 9600 = TIC standard · 1200 = TIC historique

void setup() {
  wdt_disable();                    // pas de chien de garde : on ne fait que relayer
  Serial.begin(BAUD, SERIAL_7E1);
}

void loop() {
  // Relais nu, sans tampon ni filtre. Toute mise en forme se ferait au prix d'un risque
  // de perte : à 9600 bauds une trame standard peut dépasser 1 ko, très au-dessus des
  // 2 ko de RAM de l'ATmega328. On ne stocke rien, on transmet au fil de l'eau.
  while (Serial.available()) {
    Serial.write(Serial.read());
  }
}
