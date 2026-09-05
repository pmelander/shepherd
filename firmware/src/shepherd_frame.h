#pragma once
// Shepherd's wire frame, device side.
//
// Deliberately free of Arduino, M5 and BLE dependencies so it compiles and
// runs in PlatformIO's `native` environment. The parsing rules here are the
// mirror of plugin/shepherd/frame.py, and getting them out of step is the
// most likely way this project breaks quietly — so they are pinned by tests
// that need no board.
//
// Host -> device:
//   {"t":"snap","v":1,"ts":"...","a":[
//      {"i":"w9:p1","n":"elasmigr","s":"blocked","e":"...",
//       "q":"Allow Bash(...)?","r":"a1b2c3d4e5f6","x":true}],
//    "more":2,"why":"herdr unreachable"}
//
//   `e` is absent when the host cannot honestly claim to know how long the
//   agent has been in this state. `x` marks a question that was truncated for
//   the screen, and the host refuses to approve those — the device must not
//   offer approve for them either.
//
// Device -> host:
//   {"t":"act","i":"w9:p1","k":"approve","r":"a1b2c3d4e5f6"}

#include <ArduinoJson.h>
#include <stdio.h>
#include <string.h>

// Must match PROTOCOL_VERSION in plugin/shepherd/frame.py. A mismatch is
// reported on screen rather than rendered, because drawing fields you do not
// understand is how a glance device lies.
// v2 added `d`, the per-agent recap shown on the detail screen.
#define SHEPHERD_PROTOCOL_VERSION 2

// The host caps a frame at 12 agents (its MAX_AGENTS) for the sake of this
// device's 2048-byte receive ring. Matching it here means an over-long frame
// is truncated rather than overflowing.
#define SHEPHERD_MAX_AGENTS 12

#define SHEPHERD_PANE_LEN 16
#define SHEPHERD_ALIAS_LEN 12
#define SHEPHERD_STATUS_LEN 10
#define SHEPHERD_QUESTION_LEN 128
#define SHEPHERD_DECISION_LEN 16
#define SHEPHERD_WHY_LEN 84
#define SHEPHERD_TS_LEN 24
#define SHEPHERD_MAC_LEN 17   // 16 hex chars + NUL
// RECAP_MAX in frame.py is 64 characters; the host cuts with ASCII "..." so
// this is 64 bytes, not 64 codepoints. Rounded up for headroom.
#define SHEPHERD_RECAP_LEN 72

struct ShepherdAgent {
  char pane[SHEPHERD_PANE_LEN];
  char alias[SHEPHERD_ALIAS_LEN];
  char status[SHEPHERD_STATUS_LEN];
  char question[SHEPHERD_QUESTION_LEN];
  char decision[SHEPHERD_DECISION_LEN];
  // The agent's own one-line summary of what it is doing, from Claude Code's
  // terminal title by way of Herdr. Empty when it has not said anything.
  char recap[SHEPHERD_RECAP_LEN];
  // When it entered this state, absolute and UTC. Empty when the host cannot
  // honestly claim to know - see _Seen in frame.py. Absolute rather than a
  // count so the device never ticks its own clock.
  char since[SHEPHERD_TS_LEN];
  bool truncated;
  bool hasQuestion;

  bool isBlocked() const { return strcmp(status, "blocked") == 0; }
  bool isDone() const { return strcmp(status, "done") == 0; }
  bool isUnknown() const { return strcmp(status, "unknown") == 0; }

  // Only a blocked agent with a question the host is willing to stand behind
  // can be answered from the device. Everything else is "open the laptop".
  bool answerable() const {
    return isBlocked() && hasQuestion && !truncated && decision[0] != 0;
  }
};

enum ShepherdParse {
  SHEPHERD_NOT_MINE = 0,   // not a Shepherd frame; let upstream have it
  SHEPHERD_OK = 1,
  SHEPHERD_BAD_VERSION = 2,
  SHEPHERD_MALFORMED = 3,
};

struct ShepherdFrame {
  ShepherdAgent agents[SHEPHERD_MAX_AGENTS];
  // The frame's own timestamp, echoed back in every action so the relay can
  // bind a signature to a frame it actually sent. Without it, `focus` - which
  // carries no decision id - would be replayable forever.
  char ts[SHEPHERD_TS_LEN];
  int count;
  int more;              // agents the host had but did not send
  int version;
  char why[SHEPHERD_WHY_LEN];   // set when the host could not reach Herdr
  bool degraded;

  void clear() {
    ts[0] = 0;
    count = 0;
    more = 0;
    version = 0;
    why[0] = 0;
    degraded = false;
  }

  int blockedCount() const {
    int n = 0;
    for (int i = 0; i < count; i++)
      if (agents[i].isBlocked()) n++;
    return n;
  }

  // First answerable agent, or -1. The queue is one prompt at a time: you can
  // only answer one question anyway, and keeping it to one keeps the
  // inherited input model intact.
  int firstAnswerable(int from = 0) const {
    for (int i = from; i < count; i++)
      if (agents[i].answerable()) return i;
    return -1;
  }

  // First agent worth putting on the queue screen, answerable or not.
  //
  // Distinct from firstAnswerable on purpose. A blocked agent whose question
  // was truncated cannot be approved, but it still deserves the screen and an
  // explanation — driving the queue off answerability alone sent those to the
  // herd list instead, where they read as "blocked" with no reason and the
  // approve key silently did nothing.
  int firstShowable(int from = 0) const {
    for (int i = from; i < count; i++)
      if (agents[i].isBlocked() && agents[i].hasQuestion) return i;
    return -1;
  }
};

// Copy a JSON string field into a fixed buffer, always NUL-terminated.
inline void _shCopy(char* dst, size_t cap, const char* src) {
  if (!src) { dst[0] = 0; return; }
  strncpy(dst, src, cap - 1);
  dst[cap - 1] = 0;
}

inline ShepherdParse shepherdParse(const char* line, ShepherdFrame* out) {
  if (!line || !out) return SHEPHERD_MALFORMED;
  // Cheap reject before spending a parse on somebody else's frame.
  if (!strstr(line, "\"t\"")) return SHEPHERD_NOT_MINE;

  JsonDocument doc;
  if (deserializeJson(doc, line)) return SHEPHERD_NOT_MINE;

  const char* t = doc["t"] | (const char*)nullptr;
  if (!t || strcmp(t, "snap") != 0) return SHEPHERD_NOT_MINE;

  out->clear();
  _shCopy(out->ts, sizeof(out->ts), doc["ts"] | (const char*)nullptr);
  out->version = doc["v"] | 0;
  if (out->version != SHEPHERD_PROTOCOL_VERSION) return SHEPHERD_BAD_VERSION;

  JsonArrayConst arr = doc["a"];
  if (arr.isNull()) return SHEPHERD_MALFORMED;

  for (JsonObjectConst row : arr) {
    if (out->count >= SHEPHERD_MAX_AGENTS) {
      // Count the overflow rather than dropping it silently; the strip has to
      // be able to say "there are more of these".
      out->more++;
      continue;
    }
    ShepherdAgent& a = out->agents[out->count];
    memset(&a, 0, sizeof(a));
    _shCopy(a.pane, sizeof(a.pane), row["i"] | (const char*)nullptr);
    _shCopy(a.alias, sizeof(a.alias), row["n"] | (const char*)nullptr);
    _shCopy(a.status, sizeof(a.status), row["s"] | (const char*)nullptr);
    if (a.pane[0] == 0 || a.status[0] == 0) continue;   // unusable row

    const char* q = row["q"] | (const char*)nullptr;
    if (q && *q) {
      _shCopy(a.question, sizeof(a.question), q);
      a.hasQuestion = true;
    }
    _shCopy(a.decision, sizeof(a.decision), row["r"] | (const char*)nullptr);
    _shCopy(a.recap, sizeof(a.recap), row["d"] | (const char*)nullptr);
    _shCopy(a.since, sizeof(a.since), row["e"] | (const char*)nullptr);
    a.truncated = row["x"] | false;
    out->count++;
  }

  out->more += (int)(doc["more"] | 0);
  const char* why = doc["why"] | (const char*)nullptr;
  if (why && *why) {
    _shCopy(out->why, sizeof(out->why), why);
    out->degraded = true;
  }
  return SHEPHERD_OK;
}

// ------------------------------------------------------------- elapsed
//
// The frame carries two absolute UTC timestamps — its own `ts` and each
// agent's `e` — and the device subtracts them. That is the whole reason the
// host sends absolutes: this board has no battery-backed RTC, its system
// clock is whatever the last time-sync said, and a device ticking its own
// "waiting 4m" counter would drift away from the truth between frames.
//
// Both strings are "YYYY-MM-DDTHH:MM:SSZ", written by strftime on the host,
// so this parses that exact shape and refuses anything else rather than
// guessing.

// Days since 1970-01-01 for a civil date. Howard Hinnant's algorithm, valid
// for any proleptic Gregorian date; used here so the calculation needs no
// libc time support and runs identically in the native tests.
inline long _shDaysFromCivil(int y, unsigned m, unsigned d) {
  y -= m <= 2;
  const int era = (y >= 0 ? y : y - 399) / 400;
  const unsigned yoe = (unsigned)(y - era * 400);
  const unsigned doy = (153 * (m + (m > 2 ? -3 : 9)) + 2) / 5 + d - 1;
  const unsigned doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
  return (long)era * 146097 + (long)doe - 719468;
}

// Seconds since the epoch, or -1 when the string is not the expected shape.
inline long shepherdIsoSeconds(const char* iso) {
  if (!iso || !*iso) return -1;
  int y = 0, mo = 0, d = 0, h = 0, mi = 0, s = 0;
  if (sscanf(iso, "%4d-%2d-%2dT%2d:%2d:%2dZ", &y, &mo, &d, &h, &mi, &s) != 6)
    return -1;
  if (mo < 1 || mo > 12 || d < 1 || d > 31) return -1;
  if (h > 23 || mi > 59 || s > 60) return -1;
  return _shDaysFromCivil(y, (unsigned)mo, (unsigned)d) * 86400L
         + h * 3600L + mi * 60L + s;
}

// How long the agent has been in this state, in seconds, or -1 when unknown.
//
// Negative differences clamp to 0 rather than being reported: a frame whose
// `e` is a second ahead of its `ts` is a rounding artefact, and "-1s ago" on
// a glance screen reads as a bug in a way that "0s" does not.
inline long shepherdElapsed(const ShepherdFrame& f, const ShepherdAgent& a) {
  long now = shepherdIsoSeconds(f.ts);
  long then = shepherdIsoSeconds(a.since);
  if (now < 0 || then < 0) return -1;
  return now > then ? now - then : 0;
}

// "4m", "2h10m", "3d" — the coarsest unit that still says something. Always
// NUL-terminates; writes "" when the duration is unknown.
inline void shepherdFormatElapsed(char* buf, size_t cap, long secs) {
  if (!buf || cap == 0) return;
  if (secs < 0) { buf[0] = 0; return; }
  if (secs < 60)          snprintf(buf, cap, "%lds", secs);
  else if (secs < 3600)   snprintf(buf, cap, "%ldm", secs / 60);
  else if (secs < 86400)  snprintf(buf, cap, "%ldh%ldm", secs / 3600,
                                   (secs % 3600) / 60);
  else                    snprintf(buf, cap, "%ldd", secs / 86400);
}

// The exact bytes both sides sign. Mirrors canonical_message() in
// plugin/shepherd/auth.py, and the two diverging is the most likely way
// signing breaks — so it is built here, in the natively-testable header,
// rather than inline wherever the HMAC happens.
//
// An absent decision id is an EMPTY FIELD, not a missing one: `focus` carries
// no id, and dropping the separator would let it collide with a different
// action's message.
inline size_t shepherdCanonicalMessage(char* buf, size_t cap, const char* ts,
                                       const char* pane, const char* action,
                                       const char* decision) {
  if (!buf || !ts || !pane || !action || !*ts || !*pane || !*action) return 0;
  int n = snprintf(buf, cap, "%s|%s|%s|%s", ts, pane, action,
                   (decision && *decision) ? decision : "");
  if (n < 0 || (size_t)n >= cap) return 0;
  return (size_t)n;
}

// Build a device -> host action frame. Returns the length written, or 0 if it
// would not fit or the inputs are unusable.
//
// `ts` and `mac` are optional. A relay configured with a secret refuses
// frames without them; leaving them off is how a device is brought up before
// its firmware carries one.
inline size_t shepherdBuildAct(char* buf, size_t cap, const char* pane,
                               const char* action, const char* decision,
                               const char* ts = nullptr,
                               const char* mac = nullptr) {
  if (!buf || !pane || !action || !*pane || !*action) return 0;
  char rpart[SHEPHERD_DECISION_LEN + 8] = {0};
  if (decision && *decision)
    snprintf(rpart, sizeof(rpart), ",\"r\":\"%s\"", decision);
  char spart[SHEPHERD_TS_LEN + SHEPHERD_MAC_LEN + 24] = {0};
  if (ts && *ts && mac && *mac)
    snprintf(spart, sizeof(spart), ",\"ts\":\"%s\",\"mac\":\"%s\"", ts, mac);
  int n = snprintf(buf, cap, "{\"t\":\"act\",\"i\":\"%s\",\"k\":\"%s\"%s%s}\n",
                   pane, action, rpart, spart);
  if (n < 0 || (size_t)n >= cap) return 0;
  return (size_t)n;
}
