#pragma once
// The shared secret, and how it changes.
//
// It used to be one thing: a build flag, in plaintext, fixed until somebody
// found a USB cable. That made the only real credential in the system the
// only part of it that could not be rotated, which is the wrong way round.
//
// Now there are two sources, in order:
//
//   1. NVS, if a rotation has ever succeeded.
//   2. -DSHEPHERD_SECRET, the value flashed with the firmware.
//
// So a fresh device works with the key it was built with, and a rotated one
// keeps working across reboots. A factory reset clears the NVS copy and drops
// back to the build flag — which is the honest behaviour for "reset to how it
// left the workshop", and does mean the relay must be told to rotate again.

#include <stddef.h>
#include <stdint.h>

#include "shepherd_frame.h"

// Load the active secret. Call once at boot, before anything signs.
void shepherdSecretBegin();

// Sign `msg` with the active secret. Writes SHEPHERD_MAC_LEN-1 lowercase hex
// characters plus a NUL. False when no secret is configured at all, which the
// caller reports rather than quietly sending an unsigned frame.
bool shepherdSign(const char* msg, size_t msgLen, char* out, size_t cap);

// Sign with an explicit key rather than the active one. Needed for exactly
// one thing: proving possession of a new key while the old one is still the
// active one.
bool shepherdSignWith(const uint8_t* key, size_t keyLen, const char* msg,
                      size_t msgLen, char* out, size_t cap);

// Apply a verified rekey. Checks the MAC against the CURRENT secret, then
// stores the new one and makes it active. Returns false and changes nothing
// if the signature does not verify.
//
// On success `ackOut` receives the proof the relay needs — a MAC over a
// different action, signed with the NEW key, so that a replayed rekey frame
// cannot itself pass as an acknowledgement of one.
bool shepherdApplyRekey(const ShepherdRekey& req, char* ackOut, size_t ackCap);

// Whether the active secret came from NVS (rotated) rather than the build
// flag. Shown on the info screen: "which key is this thing using" is
// otherwise unanswerable without a debugger.
bool shepherdSecretRotated();

// Forget any rotated key and fall back to the build flag. Part of factory
// reset.
void shepherdSecretForget();
