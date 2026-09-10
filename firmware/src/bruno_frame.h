#pragma once
// Bruno's own parser, for the one frame type only Bruno reads.
//
// WHY THIS IS A SEPARATE FILE, and not four lines added to shepherdParse
//
// A `said` frame says "this agent finished, and here is its sentence". Nobody
// asked for it, which is what makes it different from a `deet` - the body the
// device requests and then applies unconditionally, resetting the scroll
// position and restarting the type-out animation. An unsolicited `deet`
// reaching the Cardputer over BLE would swap the screen out from under
// someone mid-read.
//
// Giving the announcement its own `t` fixes that, but only if the SHARED
// parser stays ignorant of it. Teach `shepherdParse` about `said` and it
// returns a value `shepherdUiApply` does not handle, which falls through to
// the snapshot path and touches state it should not - so Shepherd would need
// an edit and therefore a reflash, and the promise that the pocket device
// keeps working untouched would be broken to add a feature it does not use.
//
// Left alone, `shepherdParse` returns SHEPHERD_NOT_MINE for an unrecognised
// `t` (shepherd_frame.h), `shepherdUiApply` returns false, and data.h passes
// the line to upstream's handlers, which find nothing they recognise either.
// Shepherd ignores these frames because it genuinely does not know them,
// which is a stronger guarantee than remembering not to send them.
//
// So: this header is compiled into the Bruno environment and nothing else.
// It shares shepherd_frame.h for the protocol version, the pane length and
// the truncating copy - the things that MUST agree across both devices - and
// shares no parsing.

#include <ArduinoJson.h>
#include <string.h>

#include "shepherd_frame.h"

// Mirrors SAID_MAX in plugin/shepherd/frame.py, plus a NUL.
//
// Deliberately far smaller than SHEPHERD_BODY_LEN (1024), which is sized for
// a scrollable detail screen on a device with keys. Bruno has no keys and a
// speech bubble cannot scroll, so anything past what fits on screen is weight
// on the wire for text nobody can reach. If the host's SAID_MAX changes, this
// changes with it; a host sending more is truncated here rather than
// overflowing, and the truncation is visible on screen rather than silent.
#define BRUNO_SAID_MAX 120
#define BRUNO_SAID_LEN (BRUNO_SAID_MAX + 1)

enum BrunoParse {
  BRUNO_NOT_MINE = 0,     // not an announcement; somebody else's frame
  BRUNO_SAID = 1,
  BRUNO_BAD_VERSION = 2,  // a relay newer than this firmware
  BRUNO_MALFORMED = 3,    // ours by type, but unusable
};

struct BrunoSaid {
  char pane[SHEPHERD_PANE_LEN];
  char body[BRUNO_SAID_LEN];
  // True when the host's text did not fit. Bruno can then show that the
  // sentence is cut rather than implying the agent stopped mid-word.
  bool truncated;

  void clear() {
    pane[0] = 0;
    body[0] = 0;
    truncated = false;
  }

  bool empty() const { return body[0] == 0; }
};

// Parse one line as an announcement.
//
// An announcement with no body is VALID and not an error: the relay publishes
// one as soon as an agent finishes, and fills in the sentence only if reading
// the pane worked. A finished agent with nothing to say still finished.
inline BrunoParse brunoParseSaid(const char* line, BrunoSaid* out) {
  if (!line || !out) return BRUNO_MALFORMED;
  // Cheap reject before spending a parse on somebody else's frame, the same
  // shape shepherdParse uses.
  if (!strstr(line, "\"t\"")) return BRUNO_NOT_MINE;

  JsonDocument doc;
  if (deserializeJson(doc, line)) return BRUNO_NOT_MINE;

  const char* t = doc["t"] | (const char*)nullptr;
  if (!t || strcmp(t, "said") != 0) return BRUNO_NOT_MINE;

  // Version is checked BEFORE anything is written, so a frame from a relay
  // this firmware does not understand cannot half-populate the struct and
  // leave Bruno showing a sentence it parsed by luck.
  const int version = doc["v"] | 0;
  if (version != SHEPHERD_PROTOCOL_VERSION) return BRUNO_BAD_VERSION;

  const char* pane = doc["i"] | (const char*)nullptr;
  if (!pane || !*pane) return BRUNO_MALFORMED;

  out->clear();
  _shCopy(out->pane, sizeof(out->pane), pane);

  // `b` absent and `b` empty mean the same thing here, and both are fine.
  const char* body = doc["b"] | (const char*)nullptr;
  if (body) {
    out->truncated = strlen(body) > BRUNO_SAID_MAX;
    _shCopy(out->body, sizeof(out->body), body);
  }
  return BRUNO_SAID;
}
