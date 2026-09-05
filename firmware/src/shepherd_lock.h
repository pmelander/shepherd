#pragma once
// The key lock, for carrying the thing in a pocket.
//
// The acceptance test is "device in a pocket", and all 56 keys are live all
// the time. The queue screen shows a real prompt with `y` bound to approve,
// so a fold of cloth can approve a genuine permission request before anyone
// has read it. Nothing else in the system defends against that: the relay's
// send-time re-verification proves the prompt was really on screen, which is
// exactly what a pocket press satisfies.
//
// So: locked at boot, locked again after a spell of no keys, and unlocked
// only by a chord a pocket cannot produce.
//
// What the lock deliberately does NOT do is blank the screen. This is a
// glance device — being able to look at the herd without touching it is the
// entire product — and upstream's own 30s idle sleep already handles the
// backlight. The lock governs what keys may DO, not what the screen shows.
//
// Free of Arduino and M5 so it compiles and runs under `pio test -e native`.
// Time comes in as a millis()-style uint32_t; every comparison is written to
// survive the ~49.7 day wrap.

#include <stdint.h>

// How long without a key before the device locks itself again. Short on
// purpose: the realistic sequence is pull it out, answer, put it away, and
// the window that matters is the one between putting it away and the next
// prompt arriving.
#define SHEPHERD_LOCK_IDLE_MS 30000

struct ShepherdLock {
  // Locked at boot. A device that comes back from a reset in someone's
  // pocket — which is how it will come back from a flat battery — must not
  // wake up able to answer.
  bool _locked = true;
  uint32_t _lastKeyMs = 0;
  uint32_t idleMs = SHEPHERD_LOCK_IDLE_MS;

  void begin(uint32_t now) {
    _locked = true;
    _lastKeyMs = now;
  }

  // Current state, after applying the idle re-lock.
  //
  // Not const, and asking is what advances the timer. That is deliberate:
  // the draw path asks every frame, so a device left alone locks itself
  // whether or not anybody presses anything. Hanging the re-lock off a key
  // event instead would mean an untouched device stayed unlocked forever,
  // which is precisely the pocket case.
  bool locked(uint32_t now) {
    if (!_locked && (uint32_t)(now - _lastKeyMs) >= idleMs) _locked = true;
    return _locked;
  }

  // The unlock chord arrived. Toggles rather than unlocks, so the same
  // gesture is also how you deliberately put it away instead of waiting out
  // the idle timer.
  bool toggle(uint32_t now) {
    bool wasLocked = locked(now);
    _locked = !wasLocked;
    _lastKeyMs = now;
    return _locked;
  }

  // An ordinary key arrived. True when it may act; false means the caller
  // must swallow it AND say so on screen — a key that does nothing silently
  // is indistinguishable from a broken device.
  bool accept(uint32_t now) {
    if (locked(now)) return false;
    _lastKeyMs = now;   // using it keeps it awake
    return true;
  }
};
