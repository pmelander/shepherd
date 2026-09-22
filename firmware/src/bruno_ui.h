#pragma once
// Bruno's screen. The pixels only - every decision about WHAT to show lives in
// bruno_view.h, where the native suite can hold it to account.

#include "bruno_view.h"

// Draw the whole screen for this view. Cheap to call repeatedly: it redraws
// only when something it cares about changed, because a full 320x240 repaint
// at 2Hz makes a tamagotchi flicker like a fault.
void brunoUiDraw(const BrunoView& v);

// Force the next draw to repaint everything, after something else has been on
// the display.
void brunoUiInvalidate();

// Drift the clouds. Call it on a slow timer - this is the liveness signal,
// and it matters because a frozen screen and a quiet herd look identical on a
// device whose whole job is to sit still looking calm. Redraws only the sky
// band, and does nothing on the error screens, which should not have weather.
void brunoUiTick();
