#pragma once
// What Bruno should be showing, decided without reference to a screen.
//
// One sheep, one speech bubble, ten agents. Two can finish within a second of
// each other while a third sits blocked waiting for an answer, and there is
// no way to show all three. So something has to give way, and the rule has to
// be the same rule every time or the device is just unpredictable.
//
// THE RULE: blocked holds the bubble, completions queue behind it.
//
// Chosen to agree with `shepherd_seen.h`, which already decided this for the
// pocket device - "Blocked outranks done: someone waiting ON you beats
// something waiting FOR you". Two devices in one system disagreeing about
// what is urgent would be a worse cost than either rule's downside, and a
// blocked agent is the only state where the herd cannot proceed without a
// human.
//
// What that rule does NOT do is lose you a completion. Every finish still
// bleats, because the sound is the notification and it is cheap; only the
// bubble is contended. A finish behind a prompt is delayed, never dropped.
//
// Kept free of M5 types on purpose, like shepherd_lock.h, shepherd_seen.h and
// line_buf.h before it. The policy is the part with decisions in it, so it is
// the part worth testing off the board - a device on a desk gives no
// assertion output and needs a flash to ask it anything.

#include <stdint.h>
#include <string.h>

#include "bruno_frame.h"
#include "shepherd_frame.h"

// How Bruno should look. The design's mapping: working -> busy, blocked ->
// attention, done -> celebrate, nothing happening -> potter.
enum BrunoMood : uint8_t {
  BRUNO_MOOD_POTTER = 0,    // nothing wants anything; graze
  BRUNO_MOOD_BUSY,          // at least one agent working, none needing a human
  BRUNO_MOOD_CELEBRATE,     // an agent finished and this is its moment
  BRUNO_MOOD_ATTENTION,     // an agent is blocked and cannot proceed without you
  BRUNO_MOOD_STALE,         // no frame recently; the relay is not talking
  BRUNO_MOOD_BAD_VERSION,   // a relay newer than this firmware
};

// How long a completion keeps the bubble once it gets it. Short enough that a
// queue of them drains at a watchable pace, long enough to read a sentence.
// The design said this needs living with rather than deciding, so it is one
// constant in one place.
#define BRUNO_CELEBRATE_MS 6000

// How many finishes can be waiting their turn. Past this the oldest is
// dropped: a backlog longer than this is not a queue any more, it is history,
// and the newest news is the news worth showing.
#define BRUNO_QUEUE_MAX 8

// What to put on screen right now.
struct BrunoView {
  BrunoMood mood;
  // Who the bubble is about. Empty when the mood is not about one agent.
  char pane[SHEPHERD_PANE_LEN];
  // What the bubble says. A question when blocked, a sentence when
  // celebrating, empty otherwise - a finished agent with nothing to say still
  // finished, and still gets its moment.
  char text[BRUNO_SAID_LEN];
  // Herd totals, for the strip along the bottom.
  int working;
  int blocked;
  int done;

  void clear() {
    mood = BRUNO_MOOD_POTTER;
    pane[0] = 0;
    text[0] = 0;
    working = blocked = done = 0;
  }
};

// One finish waiting its turn.
struct BrunoPending {
  char pane[SHEPHERD_PANE_LEN];
  char text[BRUNO_SAID_LEN];
};

// The queue of completions, and the one currently holding the bubble.
struct BrunoQueue {
  BrunoPending items[BRUNO_QUEUE_MAX];
  int count = 0;

  // The completion currently on screen, and when it took the bubble.
  BrunoPending showing;
  bool holding = false;
  uint32_t since = 0;

  void clear() {
    count = 0;
    holding = false;
    since = 0;
    showing.pane[0] = 0;
    showing.text[0] = 0;
  }

  // An announcement arrived. Queued whether or not anything else is on
  // screen: the bleat happens at the call site regardless, and this decides
  // only when the words get their turn.
  void push(const char* pane, const char* text) {
    if (!pane || !*pane) return;
    // A second announcement for the same agent replaces the first rather than
    // queueing twice - it is the same news, said better.
    for (int i = 0; i < count; i++) {
      if (strcmp(items[i].pane, pane) == 0) {
        _set(items[i], pane, text);
        return;
      }
    }
    if (count >= BRUNO_QUEUE_MAX) {
      // Drop the oldest. A backlog this long is history, and the newest news
      // is what is worth the screen.
      for (int i = 1; i < count; i++) items[i - 1] = items[i];
      count--;
    }
    _set(items[count], pane, text);
    count++;
  }

  // Take the next finish, if one is waiting. Returns false when empty.
  bool take(uint32_t now) {
    if (count == 0) return false;
    showing = items[0];
    for (int i = 1; i < count; i++) items[i - 1] = items[i];
    count--;
    holding = true;
    since = now;
    return true;
  }

  // Whether the current celebration has had its moment. Uses a subtraction so
  // it survives the millis() wrap a device left on a shelf crosses every ~49
  // days - the same care shepherd_lock.h takes.
  bool expired(uint32_t now) const {
    return holding && (uint32_t)(now - since) >= BRUNO_CELEBRATE_MS;
  }

  void release() {
    holding = false;
    showing.pane[0] = 0;
    showing.text[0] = 0;
  }

 private:
  static void _set(BrunoPending& slot, const char* pane, const char* text) {
    strncpy(slot.pane, pane, SHEPHERD_PANE_LEN - 1);
    slot.pane[SHEPHERD_PANE_LEN - 1] = 0;
    if (text) {
      strncpy(slot.text, text, BRUNO_SAID_LEN - 1);
      slot.text[BRUNO_SAID_LEN - 1] = 0;
    } else {
      slot.text[0] = 0;
    }
  }
};

// Decide what to show.
//
// `haveFrame` is false before the first frame ever arrives and after the relay
// has gone quiet; `badVersion` when a frame was refused for its version. Both
// outrank everything else, because a device that shows a calm herd it cannot
// actually see is lying.
inline void brunoDecide(const ShepherdFrame& f, BrunoQueue& q, uint32_t now,
                        bool haveFrame, bool badVersion, BrunoView* out) {
  out->clear();
  if (badVersion) {
    out->mood = BRUNO_MOOD_BAD_VERSION;
    return;
  }
  if (!haveFrame) {
    out->mood = BRUNO_MOOD_STALE;
    return;
  }

  out->working = f.workingCount();
  out->done = f.doneCount();
  for (int i = 0; i < f.count; i++)
    if (f.agents[i].isBlocked()) out->blocked++;

  // A celebration that has had its moment steps aside, whatever is behind it.
  if (q.expired(now)) q.release();

  // BLOCKED FIRST, and it takes the bubble off a celebration in progress.
  // Something that arrived while you were reading a finish is still the thing
  // that cannot proceed without you.
  const int blocked = f.firstShowable();
  if (blocked >= 0) {
    if (q.holding) q.release();
    out->mood = BRUNO_MOOD_ATTENTION;
    strncpy(out->pane, f.agents[blocked].pane, SHEPHERD_PANE_LEN - 1);
    strncpy(out->text, f.agents[blocked].question, BRUNO_SAID_LEN - 1);
    out->text[BRUNO_SAID_LEN - 1] = 0;
    return;
  }

  // Nothing is stuck, so the queue gets its turn.
  if (!q.holding) q.take(now);
  if (q.holding) {
    out->mood = BRUNO_MOOD_CELEBRATE;
    strncpy(out->pane, q.showing.pane, SHEPHERD_PANE_LEN - 1);
    strncpy(out->text, q.showing.text, BRUNO_SAID_LEN - 1);
    out->text[BRUNO_SAID_LEN - 1] = 0;
    return;
  }

  // An agent can be `done` in the frame with no announcement queued - the
  // relay publishes the snapshot and the sentence separately, and a restart
  // loses the queue but not the herd. Still worth celebrating; it just has
  // nothing to say.
  const int done = f.firstDone();
  if (done >= 0) {
    out->mood = BRUNO_MOOD_CELEBRATE;
    strncpy(out->pane, f.agents[done].pane, SHEPHERD_PANE_LEN - 1);
    return;
  }

  out->mood = out->working > 0 ? BRUNO_MOOD_BUSY : BRUNO_MOOD_POTTER;
}
