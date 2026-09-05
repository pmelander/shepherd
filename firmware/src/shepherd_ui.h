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

// Handle one key. Returns true when Shepherd consumed it.
bool shepherdUiKey(HalKey k);

// True while the device is showing a question the host will accept an answer
// for — used to decide whether to chirp.
bool shepherdUiNeedsAttention();
