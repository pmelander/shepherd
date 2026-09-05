#include "shepherd_secret.h"

#include <Arduino.h>
#include <Preferences.h>
#include <mbedtls/md.h>
#include <string.h>

// Without the flag the device sends unsigned frames, which a relay holding a
// secret refuses. That is the bring-up path, not a fallback.
#ifndef SHEPHERD_SECRET
#define SHEPHERD_SECRET ""
#endif

// Its own namespace, not upstream's "buddy". The secret has a different
// lifetime from the pet and the settings, and mixing them would mean every
// stats write shared a namespace with a credential.
static const char* NVS_NS = "shepherd";
static const char* NVS_KEY = "secret";

static uint8_t g_key[SHEPHERD_SECRET_BYTES];
static size_t g_keyLen = 0;
static bool g_rotated = false;

static bool hexToBytes(const char* hex, uint8_t* out, size_t outCap,
                       size_t* outLen) {
  size_t n = hex ? strlen(hex) : 0;
  if (n == 0 || n % 2 || n / 2 > outCap) return false;
  for (size_t i = 0; i < n; i += 2) {
    char pair[3] = {hex[i], hex[i + 1], 0};
    char* end = nullptr;
    long v = strtol(pair, &end, 16);
    if (end != pair + 2) return false;
    out[i / 2] = (uint8_t)v;
  }
  *outLen = n / 2;
  return true;
}

void shepherdSecretBegin() {
  Preferences p;
  if (p.begin(NVS_NS, true)) {
    char stored[SHEPHERD_SECRET_HEX_LEN] = {0};
    size_t got = p.getString(NVS_KEY, stored, sizeof(stored));
    p.end();
    // Validate before trusting it. A truncated or corrupt NVS entry silently
    // becoming the active key would refuse every action with no clue why.
    if (got > 0 && shepherdIsSecretHex(stored) &&
        hexToBytes(stored, g_key, sizeof(g_key), &g_keyLen)) {
      g_rotated = true;
      Serial.println("[key] using rotated secret from NVS");
      return;
    }
    if (got > 0) Serial.println("[key] NVS secret unusable; using build flag");
  }
  if (!hexToBytes(SHEPHERD_SECRET, g_key, sizeof(g_key), &g_keyLen))
    g_keyLen = 0;
  Serial.printf("[key] build-flag secret %s\n",
                g_keyLen ? "loaded" : "ABSENT - frames will be unsigned");
}

bool shepherdSecretRotated() { return g_rotated; }

void shepherdSecretForget() {
  Preferences p;
  if (p.begin(NVS_NS, false)) {
    p.remove(NVS_KEY);
    p.end();
  }
  g_rotated = false;
  if (!hexToBytes(SHEPHERD_SECRET, g_key, sizeof(g_key), &g_keyLen))
    g_keyLen = 0;
  Serial.println("[key] rotated secret forgotten; back to the build flag");
}

bool shepherdSignWith(const uint8_t* key, size_t keyLen, const char* msg,
                      size_t msgLen, char* out, size_t cap) {
  if (!key || keyLen == 0 || !msg || !out || cap < SHEPHERD_MAC_LEN)
    return false;

  uint8_t digest[32];
  const mbedtls_md_info_t* info = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
  if (!info) return false;
  if (mbedtls_md_hmac(info, key, keyLen, (const uint8_t*)msg, msgLen, digest) != 0)
    return false;

  // Truncated to 64 bits for the wire: an attacker must already hold a BLE
  // bond and guess against a relay that logs every refusal, and the link
  // sometimes negotiates a 20-byte payload.
  // Not named HEX: Arduino's Print.h does `#define HEX 16`, and the collision
  // reports as "invalid types 'int[int]' for array subscript", which points
  // nowhere near the actual cause.
  static const char* HEXDIGITS = "0123456789abcdef";
  for (int i = 0; i < (SHEPHERD_MAC_LEN - 1) / 2; i++) {
    out[i * 2] = HEXDIGITS[digest[i] >> 4];
    out[i * 2 + 1] = HEXDIGITS[digest[i] & 0x0F];
  }
  out[SHEPHERD_MAC_LEN - 1] = 0;
  return true;
}

bool shepherdSign(const char* msg, size_t msgLen, char* out, size_t cap) {
  return shepherdSignWith(g_key, g_keyLen, msg, msgLen, out, cap);
}

bool shepherdApplyRekey(const ShepherdRekey& req, char* ackOut, size_t ackCap) {
  if (g_keyLen == 0) {
    // Nothing to authorise the change with. Accepting here would let anyone
    // who can reach the characteristic set the key on a bring-up device and
    // then drive it — the one case where "no secret configured" has to mean
    // refuse rather than allow.
    Serial.println("[key] rekey refused: no current secret to verify against");
    return false;
  }
  if (!shepherdIsSecretHex(req.hex)) return false;

  // The message contains the NEW key and is signed with the OLD one, so
  // changing the key requires already holding the key.
  char msg[SHEPHERD_TS_LEN + 24 + SHEPHERD_SECRET_HEX_LEN];
  size_t mlen = shepherdCanonicalMessage(msg, sizeof(msg), req.ts, "device",
                                         "rekey", req.hex);
  if (!mlen) return false;

  char expect[SHEPHERD_MAC_LEN] = {0};
  if (!shepherdSign(msg, mlen, expect, sizeof(expect))) return false;
  // Length-safe compare; both are fixed-width lowercase hex from our own
  // formatter, so a plain strcmp is honest here.
  if (strcmp(expect, req.mac) != 0) {
    Serial.println("[key] rekey refused: signature did not verify");
    return false;
  }

  uint8_t fresh[SHEPHERD_SECRET_BYTES];
  size_t freshLen = 0;
  if (!hexToBytes(req.hex, fresh, sizeof(fresh), &freshLen)) return false;

  // Prove possession BEFORE committing anything. If this cannot be built the
  // relay would never learn the rotation happened, and the two sides would
  // end up holding different keys — the one outcome worse than not rotating.
  char proofMsg[SHEPHERD_TS_LEN + 24];
  size_t plen = shepherdCanonicalMessage(proofMsg, sizeof(proofMsg), req.ts,
                                         "device", "rekeyed", nullptr);
  char proof[SHEPHERD_MAC_LEN] = {0};
  if (!plen || !shepherdSignWith(fresh, freshLen, proofMsg, plen, proof,
                                 sizeof(proof)))
    return false;

  Preferences p;
  if (!p.begin(NVS_NS, false)) return false;
  const bool stored = p.putString(NVS_KEY, req.hex) > 0;
  p.end();
  if (!stored) {
    Serial.println("[key] rekey refused: NVS write failed");
    return false;
  }

  memcpy(g_key, fresh, freshLen);
  g_keyLen = freshLen;
  g_rotated = true;
  Serial.println("[key] rotated");

  return shepherdBuildKeyAck(ackOut, ackCap, req.ts, proof) > 0;
}
