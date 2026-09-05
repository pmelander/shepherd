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
#define SHEPHERD_PROTOCOL_VERSION 1

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

struct ShepherdAgent {
  char pane[SHEPHERD_PANE_LEN];
  char alias[SHEPHERD_ALIAS_LEN];
  char status[SHEPHERD_STATUS_LEN];
  char question[SHEPHERD_QUESTION_LEN];
  char decision[SHEPHERD_DECISION_LEN];
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
  int count;
  int more;              // agents the host had but did not send
  int version;
  char why[SHEPHERD_WHY_LEN];   // set when the host could not reach Herdr
  bool degraded;

  void clear() {
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

// Build a device -> host action frame. Returns the length written, or 0 if it
// would not fit or the inputs are unusable.
inline size_t shepherdBuildAct(char* buf, size_t cap, const char* pane,
                               const char* action, const char* decision) {
  if (!buf || !pane || !action || !*pane || !*action) return 0;
  int n;
  if (decision && *decision) {
    n = snprintf(buf, cap, "{\"t\":\"act\",\"i\":\"%s\",\"k\":\"%s\",\"r\":\"%s\"}\n",
                 pane, action, decision);
  } else {
    n = snprintf(buf, cap, "{\"t\":\"act\",\"i\":\"%s\",\"k\":\"%s\"}\n",
                 pane, action);
  }
  if (n < 0 || (size_t)n >= cap) return 0;
  return (size_t)n;
}
