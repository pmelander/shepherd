#pragma once
// Shepherd's screen: a queue of prompts, one at a time.
//
// Why a queue and not the five-glyph strip the design doc drew: you can only
// answer one question at a time anyway, so a queue reaches the acceptance
// test without rewriting the inherited input model. The strip is the fun
// part and it is v2; this is the part that has to work.
//
// This module owns the screen only while it has something to say. When no
// Shepherd frame has arrived recently it stands aside and upstream's buddy UI
// draws as before, so a device that loses the relay degrades to the thing it
// was rather than to a blank panel.

#include <M5GFX.h>

#include "shepherd_frame.h"
#include "shepherd_lock.h"
#include "hal.h"

// A frame older than this is not worth drawing. The host sends a keepalive
// every 10s, so 30s of silence means the relay is gone, the laptop slept, or
// the link dropped — and a glance device that keeps showing the last good
// herd in that situation is lying.
#define SHEPHERD_STALE_MS 30000

// Feed one newline-delimited line from the link. Returns true when it was a
// Shepherd frame and upstream should not also try to parse it.
bool shepherdUiApply(const char* line);

// Should Shepherd own the screen this frame?
bool shepherdUiActive();

// Draw into the shared sprite. Caller flushes.
void shepherdUiDraw(M5Canvas& spr, int W, int H);

// Tell Shepherd whether it is what the viewer is actually looking at.
//
// shepherdUiActive() only says "a frame has arrived at some point", which is
// not the same thing: the buddy's info screen and its modals draw OVER
// Shepherd, and while one of them is up Shepherd must not be taking keys.
// Called from the draw dispatch, so this tracks whatever that decides rather
// than a second copy of the same conditions drifting out of step.
void shepherdUiOnScreen(bool visible);

// Handle one key. Returns true when Shepherd consumed it.
bool shepherdUiKey(HalKey k);

// What, if anything, currently wants a human.
//
// Two kinds, and they are not the same urgency. Blocked means an agent has
// stopped and cannot continue without you. Done means one finished while you
// were looking somewhere else — Herdr only reports `done` for a tab that has
// not been seen, so a focused pane never produces it. Both deserve telling;
// only one deserves an interruption.
enum class ShepherdAlarm : uint8_t { None = 0, Done = 1, Blocked = 2 };

ShepherdAlarm shepherdUiAttention();

// The same thing, edge-triggered: returns non-None exactly once per new
// arrival, and again every SHEPHERD_NAG_MS for as long as `unseen` holds.
//
// Edge rather than level because the caller wakes the screen and makes a
// noise with it, and "an agent is still blocked" stays true for minutes.
// Pass screenOff as `unseen`: once the display is up the reader has been
// told, and a device that keeps chirping at someone already looking at it is
// a device they will turn off.
ShepherdAlarm shepherdUiTakeAlarm(bool unseen);

// Lock the keys immediately, without waiting out the idle window. Called
// when the screen goes dark.
void shepherdUiLock();

// Whether Shepherd should stand back and let the buddy have the screen.
//
// True when the device is locked and the herd is calm — nothing blocked,
// nothing newly finished, the relay still talking. That is the moment there
// is no information worth a dense readout, and it is also the moment you
// most often look at the thing: you pick it up, the screen lights, and a
// dense grid of "idle idle idle" tells you nothing a pet does not.
//
// Locked is the right trigger rather than merely idle. Locked means nobody
// is working the device, so the display is decoration; unlocking is the act
// of asking it a question, and that is when the herd list earns its space.
// It also means a wake-for-alarm still lands on the queue, because an alarm
// is never calm.
bool shepherdUiResting();

// The herd in four numbers, for driving the buddy's mood. Upstream's
// derive() already asks exactly these questions of its own TamaState; it was
// simply never given Shepherd's answers, which is why the buddy has spent
// this whole project reacting to nothing.
struct ShepherdHerd {
  bool live;      // a recent frame, and one we understood
  int total;
  int working;
  int blocked;
  int done;
};
ShepherdHerd shepherdUiHerd();
