#pragma once
// One line of JSON, reassembled from a byte stream.
//
// This is the third piece of device logic extracted into a dependency-free
// header so the native suite can reach it, and it is here for the same reason
// as the other two: it was wrong, and the symptom pointed nowhere.
// shepherd_lock.h and shepherd_seen.h have the same shape and the same note.
//
// What was wrong: the buffer was too small, twice over. `_usbLine` held 1024
// bytes and `_btLine` 4096, while the largest frame the host can actually
// emit is 6317 - measured by building it with the real FrameBuilder, not
// estimated. An over-long line was silently truncated and the truncated JSON
// was then handed to the parser, which rejected it as somebody else's frame.
// So the failure mode was a screen that simply stopped updating, with nothing
// logged, nothing on screen, and nothing to point at. The device looked calm
// and the herd looked frozen.
//
// It could not be tested where it lived. `data.h` pulls in Arduino.h,
// ArduinoJson.h, ble_bridge.h and xfer.h, and the feed loop called
// `_applyJson`, which calls `millis()`. Nothing about reassembling a line
// needs any of that.
//
// Two behaviour changes came with the move, both deliberate:
//
//   1. An over-long line is DROPPED, not truncated-and-delivered. Handing a
//      parser JSON you already know is incomplete can only fail, and
//      pretending otherwise is what made this invisible.
//   2. Drops are COUNTED. A non-zero count is the difference between "the
//      relay stopped talking" and "the relay is sending frames this device
//      cannot receive", which are the same symptom and different bugs.

#include <stddef.h>
#include <stdint.h>

// The largest frame plugin/shepherd/frame.py can emit, measured rather than
// reasoned about: twelve agents (its MAX_AGENTS), every one blocked, each
// carrying six options at OPTION_MAX, encodes to 6317 bytes. A realistic herd
// is nowhere near it - five agents with one blocked is 1039 bytes, and all
// five blocked is 2663 - which is exactly why the old limits held for months
// and would have failed on a busy afternoon.
//
// Re-measure if the frame grows a field. Build the worst case through the
// real FrameBuilder; do not add up the constants by hand, which is how 4096
// came to look sufficient.
#define SHEPHERD_WORST_FRAME 6317

// Rounded up to the next power of two, which leaves 1875 bytes of headroom so
// one added per-agent field does not silently put us back where we started.
// Two of these live in .bss (one per transport) for 16KB total, against
// roughly 320KB of DRAM - the build reports 31.6% used before this change.
#define SHEPHERD_LINE_MAX 8192

static_assert(SHEPHERD_LINE_MAX > SHEPHERD_WORST_FRAME + 1,
              "The line buffer cannot hold the largest frame the host can "
              "send. That does not fail loudly - it presents as a screen "
              "that stops updating. Raise SHEPHERD_LINE_MAX, and do not "
              "lower SHEPHERD_MAX_AGENTS to make this pass without saying so "
              "in shepherd_frame.h.");

template <size_t N>
struct LineBuf {
  static_assert(N >= 2, "a line buffer needs room for one byte and a NUL");

  char buf[N];
  // size_t rather than uint16_t. The old field was uint16_t, which happened
  // to be fine at 4096 and would have wrapped somewhere past 65535 - a trap
  // costing two bytes to remove.
  size_t len = 0;
  // Lines thrown away for not fitting. Nothing resets this: it is a fault
  // count for the life of the boot, and it is the only evidence this failure
  // leaves behind.
  uint32_t dropped = 0;
  // Whether the line currently being assembled has already overflowed. Kept
  // separate from `len` so the rest of an over-long line is consumed and
  // discarded rather than being mistaken for the start of the next one.
  bool poisoned = false;

  void reset() {
    len = 0;
    poisoned = false;
  }

  // Feed one byte.
  //
  // Returns true when `buf` holds a complete, NUL-terminated, non-empty line
  // that did not overflow - read it immediately, because the next push starts
  // overwriting. Returns false for every other byte, including the terminator
  // of a blank line and the terminator of a line that was too long.
  bool push(char c) {
    if (c == '\n' || c == '\r') {
      const bool ready = !poisoned && len > 0;
      if (poisoned) dropped++;
      if (ready) buf[len] = 0;
      len = 0;
      poisoned = false;
      return ready;
    }
    if (len < N - 1) {
      buf[len++] = c;
      return false;
    }
    // Full. Swallow the rest of the line; the terminator will bin it.
    poisoned = true;
    return false;
  }
};
