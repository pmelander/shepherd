#pragma once
// What the person holding this device has already been told.
//
// Herdr has its own idea of seen and it is a different one: it clears `done`
// when the TAB is focused on the laptop. Reading an answer on the Cardputer
// clears nothing there, so the notification state stayed true and the LED
// blinked at someone who already knew.
//
// This has been got wrong twice, which is why it lives in its own header
// where the native suite can reach it:
//
//   1. Marked from the DRAW path after a dwell, on the reasoning that a lit
//      screen meant somebody could have looked. But the alarm wakes the
//      screen itself, so the device kept marking its own wake-up as read and
//      the light went out with nobody in the room. A notification you might
//      never see is worse than one that will not stop.
//
//   2. Stored as a SINGLE entry, and compared against "the first blocked or
//      done agent". Frames sort those to the front, so one acknowledged
//      agent at the top masked every agent that arrived behind it.
//
// So: a set, keyed by pane AND kind, and written only when a key is pressed.
// A keypress is the one thing on this device that proves a person is there.

#include <stdint.h>
#include <string.h>

#include "shepherd_frame.h"

// What kind of attention an entry acknowledges. Mirrors ShepherdAlarm, kept
// as a plain int here so this header stays free of the UI's types.
#define SHEPHERD_SEEN_NONE 0
#define SHEPHERD_SEEN_DONE 1
#define SHEPHERD_SEEN_BLOCKED 2

struct ShepherdSeen {
  char pane[SHEPHERD_MAX_AGENTS][SHEPHERD_PANE_LEN];
  uint8_t kind[SHEPHERD_MAX_AGENTS];
  int count = 0;

  void clear() { count = 0; }

  bool has(const char* p, uint8_t k) const {
    if (!p || !*p) return false;
    for (int i = 0; i < count; i++) {
      if (kind[i] == k && strcmp(pane[i], p) == 0) return true;
    }
    return false;
  }

  void mark(const char* p, uint8_t k) {
    if (!p || !*p || has(p, k)) return;
    // Full is not worth failing over: the frame caps at twelve agents, so
    // getting here means a herd that churned through more than twelve
    // acknowledged states without a reboot. Dropping the oldest costs at
    // most one repeated notification.
    if (count >= SHEPHERD_MAX_AGENTS) {
      for (int i = 1; i < count; i++) {
        memcpy(pane[i - 1], pane[i], SHEPHERD_PANE_LEN);
        kind[i - 1] = kind[i];
      }
      count--;
    }
    strncpy(pane[count], p, SHEPHERD_PANE_LEN - 1);
    pane[count][SHEPHERD_PANE_LEN - 1] = 0;
    kind[count] = k;
    count++;
  }

  // The first agent still wanting a human, skipping anything acknowledged.
  // Blocked outranks done: someone waiting ON you beats something waiting
  // FOR you. Writes the kind through `outKind`, or SHEPHERD_SEEN_NONE.
  int firstUnseen(const ShepherdFrame& f, uint8_t* outKind) const {
    *outKind = SHEPHERD_SEEN_NONE;
    for (int i = f.firstShowable(); i >= 0; i = f.firstShowable(i + 1)) {
      if (!has(f.agents[i].pane, SHEPHERD_SEEN_BLOCKED)) {
        *outKind = SHEPHERD_SEEN_BLOCKED;
        return i;
      }
    }
    for (int i = f.firstDone(); i >= 0; i = f.firstDone(i + 1)) {
      if (!has(f.agents[i].pane, SHEPHERD_SEEN_DONE)) {
        *outKind = SHEPHERD_SEEN_DONE;
        return i;
      }
    }
    return -1;
  }
};
