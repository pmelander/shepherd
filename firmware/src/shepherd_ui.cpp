#include "shepherd_ui.h"

#include <Arduino.h>
#include <mbedtls/md.h>

#include "ble_bridge.h"

// ---------------------------------------------------------------- signing
//
// The BLE bond proves the peer is a device Windows once paired with. It does
// not prove it is this firmware, because Just Works gives no MITM protection
// — bleak cannot run a passkey ceremony. So every action carries a MAC over
// the canonical message, keyed by a secret baked in at build time.
//
// The primitive is mbedtls, which ships with the ESP32 core and is far better
// tested than anything worth hand-rolling here. What IS worth testing is the
// message being signed, and that lives in shepherd_frame.h where the native
// suite can reach it — the two sides disagreeing about the message is the
// realistic failure, not a broken SHA-256.
//
// Without -DSHEPHERD_SECRET the device sends unsigned frames, which a relay
// holding a secret refuses. That is the bring-up path, not a fallback.
#ifndef SHEPHERD_SECRET
#define SHEPHERD_SECRET ""
#endif

static bool hexToBytes(const char* hex, uint8_t* out, size_t outCap, size_t* outLen) {
  size_t n = strlen(hex);
  if (n == 0 || n % 2 || n / 2 > outCap) return false;
  for (size_t i = 0; i < n; i += 2) {
    char pair[3] = {hex[i], hex[i + 1], 0};
    char* end = nullptr;
    long v = strtol(pair, &end, 16);
    if (end != pair + 2) return false;
    out[i / 2] = (uint8_t)v;
  }
  *outLen = n / 2;
  return true;
}

// Writes SHEPHERD_MAC_LEN-1 lowercase hex characters plus a NUL. Returns
// false when no secret is configured, which the caller reports rather than
// silently sending an unsigned frame.
static bool signMessage(const char* msg, size_t msgLen, char* out, size_t cap) {
  static uint8_t key[32];
  static size_t keyLen = 0;
  static bool tried = false;
  if (!tried) {
    tried = true;
    if (!hexToBytes(SHEPHERD_SECRET, key, sizeof(key), &keyLen)) keyLen = 0;
  }
  if (keyLen == 0 || cap < SHEPHERD_MAC_LEN) return false;

  uint8_t digest[32];
  const mbedtls_md_info_t* info = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
  if (!info) return false;
  if (mbedtls_md_hmac(info, key, keyLen, (const uint8_t*)msg, msgLen, digest) != 0)
    return false;

  // Truncated to 64 bits for the wire: an attacker must already hold a BLE
  // bond and guess against a relay that logs every refusal, and the link
  // sometimes negotiates a 20-byte payload.
  // Not named HEX: Arduino's Print.h does `#define HEX 16`, and the collision
  // reports as "invalid types 'int[int]' for array subscript", which points
  // nowhere near the actual cause.
  static const char* HEXDIGITS = "0123456789abcdef";
  for (int i = 0; i < (SHEPHERD_MAC_LEN - 1) / 2; i++) {
    out[i * 2] = HEXDIGITS[digest[i] >> 4];
    out[i * 2 + 1] = HEXDIGITS[digest[i] & 0x0F];
  }
  out[SHEPHERD_MAC_LEN - 1] = 0;
  return true;
}

// ---------------------------------------------------------------- state

static ShepherdFrame g_frame;
static ShepherdLock g_lock;
static uint32_t g_lastFrameMs = 0;
static bool g_everReceived = false;
static bool g_badVersion = false;
static int g_cursor = 0;          // which agent the queue is showing
static char g_note[40] = {0};     // transient feedback line
static uint32_t g_noteUntil = 0;

// Which screen is up.
//
// Auto follows the herd: the queue when something is blocked, the list when
// nothing is. List and Detail are places you went deliberately, and they
// stick — a keepalive arriving every ten seconds must not yank the screen
// out from under someone reading it. Only a genuinely NEW prompt does that.
enum class ShepherdView : uint8_t { Auto, List, Detail };
static ShepherdView g_view = ShepherdView::Auto;

static int g_listSel = 0;         // highlighted row in the herd list
static int g_listTop = 0;         // first visible row, for scrolling
static uint32_t g_detailMs = 0;   // when the body started spelling itself out
static ShepherdDetail g_detail;   // the fetched answer, for one agent
static bool g_detailPending = false;   // asked the relay, nothing back yet
static int g_scroll = 0;          // first visible line of the body
static bool g_typedOut = false;   // reveal finished, or skipped by scrolling

// The pane the queue was showing last frame. Used to tell "the same prompt is
// still there" from "a new one arrived", which is the only thing allowed to
// pull the screen back from wherever the reader navigated to.
static char g_lastShowable[SHEPHERD_PANE_LEN] = {0};

// Rows the list can show at once: from y=18 down to the footer rule, at 11px
// a row. Fewer than MAX_AGENTS, so the window has to scroll.
#define SH_LIST_ROWS 9

// Milliseconds per character of the recap. ~25 c/s reads as deliberate
// rather than slow; a full 64-character recap lands in about two and a half
// seconds, which is roughly how long it takes to focus on the screen anyway.
#define SH_TYPE_MS 40

// Colours chosen for a 240x135 IPS at arm's length: high contrast, few hues,
// and status carried by colour AND text so it survives being glanced at.
static const uint16_t C_BG      = 0x0000;
static const uint16_t C_TEXT    = 0xFFFF;
static const uint16_t C_DIM     = 0x8410;
static const uint16_t C_BLOCKED = 0xFB40;   // amber: someone is waiting
static const uint16_t C_DONE    = 0x07E0;   // green: finished, unseen
static const uint16_t C_WORKING = 0x3D7F;   // blue
static const uint16_t C_STALE   = 0xF800;   // red: we cannot see
static const uint16_t C_LOCK    = 0x7BEF;   // grey: keys are inert, not broken

static uint16_t statusColour(const ShepherdAgent& a) {
  if (a.isBlocked()) return C_BLOCKED;
  if (a.isDone()) return C_DONE;
  if (a.isUnknown()) return C_STALE;
  if (strcmp(a.status, "working") == 0) return C_WORKING;
  return C_DIM;
}

static void note(const char* text) {
  strncpy(g_note, text, sizeof(g_note) - 1);
  g_note[sizeof(g_note) - 1] = 0;
  g_noteUntil = millis() + 2500;
}

static bool stale() {
  return !g_everReceived || (millis() - g_lastFrameMs) > SHEPHERD_STALE_MS;
}

// ---------------------------------------------------------------- input

bool shepherdUiApply(const char* line) {
  ShepherdFrame parsed;
  ShepherdParse r = shepherdParse(line, &parsed);
  if (r == SHEPHERD_NOT_MINE) return false;

  if (r == SHEPHERD_DETAIL) {
    // Deliberately does NOT touch g_lastFrameMs: a detail reply is something
    // we asked for, so counting it as proof of life would let the staleness
    // rule be satisfied by our own curiosity rather than by the relay still
    // watching the herd.
    ShepherdDetail d;
    if (shepherdParseDetail(line, &d)) {
      g_detail = d;
      g_detailPending = false;
      g_detailMs = millis();
      g_scroll = 0;
      g_typedOut = false;
    }
    return true;
  }

  g_lastFrameMs = millis();
  g_everReceived = true;

  if (r == SHEPHERD_BAD_VERSION) {
    g_badVersion = true;
    return true;   // ours, and deliberately not rendered as data
  }
  if (r != SHEPHERD_OK) return true;

  g_badVersion = false;
  // Keep both cursors on the same agent across frames where possible, so a
  // refresh arriving as you reach for a key does not move the answer — or
  // the row you were reading — out from under your thumb. The frame is
  // re-sorted by the host on every build, so index alone is not stable.
  char keep[SHEPHERD_PANE_LEN] = {0};
  if (g_cursor >= 0 && g_cursor < g_frame.count)
    strncpy(keep, g_frame.agents[g_cursor].pane, sizeof(keep) - 1);
  char keepSel[SHEPHERD_PANE_LEN] = {0};
  if (g_listSel >= 0 && g_listSel < g_frame.count)
    strncpy(keepSel, g_frame.agents[g_listSel].pane, sizeof(keepSel) - 1);

  g_frame = parsed;
  g_cursor = 0;
  g_listSel = 0;
  for (int i = 0; i < g_frame.count; i++) {
    if (keep[0] && strcmp(g_frame.agents[i].pane, keep) == 0) g_cursor = i;
    if (keepSel[0] && strcmp(g_frame.agents[i].pane, keepSel) == 0) g_listSel = i;
  }

  // A new prompt beats whatever the reader was doing. The same prompt still
  // sitting there does not — otherwise every keepalive would bounce them out
  // of a recap they were halfway through.
  int showable = g_frame.firstShowable();
  char nowShowable[SHEPHERD_PANE_LEN] = {0};
  if (showable >= 0)
    strncpy(nowShowable, g_frame.agents[showable].pane, sizeof(nowShowable) - 1);
  if (nowShowable[0] && strcmp(nowShowable, g_lastShowable) != 0) {
    g_view = ShepherdView::Auto;
    g_cursor = showable;
  }
  strncpy(g_lastShowable, nowShowable, sizeof(g_lastShowable) - 1);
  g_lastShowable[sizeof(g_lastShowable) - 1] = 0;
  return true;
}

bool shepherdUiActive() {
  // Own the screen once a frame has ever arrived. Standing aside when stale
  // would hand the display back to upstream's buddy, which would cheerfully
  // draw an idle pet while the herd's real state is unknown.
  return g_everReceived;
}

bool shepherdUiNeedsAttention() {
  // Deliberately blind to the lock. The lock governs what a key may DO, not
  // whether you get told — a locked device that stayed quiet about a blocked
  // agent would be a worse device, not a safer one.
  if (stale() || g_badVersion) return false;
  // Showable, not answerable: an agent whose question was truncated still
  // stopped and still wants you. "You must open the laptop" is attention too.
  return g_frame.firstShowable() >= 0;
}

// ---------------------------------------------------------------- output

static void drawHeader(M5Canvas& spr, int W) {
  spr.setTextSize(1);
  spr.setTextDatum(TL_DATUM);
  spr.setTextColor(C_DIM, C_BG);
  spr.drawString("SHEPHERD", 4, 3);
  // The lock is stated, always. Its whole job is to make keys do nothing,
  // and an unexplained dead key is the exact failure this project already
  // spent a session chasing once.
  if (g_lock.locked(millis())) {
    spr.setTextColor(C_LOCK, C_BG);
    spr.drawString("LOCKED", 58, 3);
  }

  char right[40];
  if (stale()) {
    snprintf(right, sizeof(right), "NO SIGNAL");
    spr.setTextColor(C_STALE, C_BG);
  } else {
    int blocked = g_frame.blockedCount();
    if (g_frame.more)
      snprintf(right, sizeof(right), "%d blk  %d+%d", blocked,
               g_frame.count, g_frame.more);
    else
      snprintf(right, sizeof(right), "%d blk  %d", blocked, g_frame.count);
    spr.setTextColor(blocked ? C_BLOCKED : C_DIM, C_BG);
  }
  spr.setTextDatum(TR_DATUM);
  spr.drawString(right, W - 4, 3);
  spr.drawFastHLine(0, 13, W, C_DIM);
}

// Word-wrap into the detail area. Returns the next free y.
static int drawWrapped(M5Canvas& spr, const char* text, int x, int y,
                       int cols, int maxLines, int lineH) {
  int len = (int)strlen(text);
  int pos = 0, line = 0;
  char buf[96];
  while (pos < len && line < maxLines) {
    int take = len - pos;
    if (take > cols) {
      take = cols;
      // Break on the last space so words survive the wrap.
      int sp = -1;
      for (int i = take; i > cols / 2; i--) {
        if (text[pos + i] == ' ') { sp = i; break; }
      }
      if (sp > 0) take = sp;
    }
    if (take >= (int)sizeof(buf)) take = (int)sizeof(buf) - 1;
    memcpy(buf, text + pos, take);
    buf[take] = 0;
    spr.drawString(buf, x, y + line * lineH);
    pos += take;
    while (pos < len && text[pos] == ' ') pos++;
    line++;
  }
  return y + line * lineH;
}

static void drawStale(M5Canvas& spr, int W, int H) {
  spr.setTextDatum(MC_DATUM);
  spr.setTextColor(C_STALE, C_BG);
  spr.setTextSize(2);
  spr.drawString("NO SIGNAL", W / 2, H / 2 - 12);
  spr.setTextSize(1);
  spr.setTextColor(C_DIM, C_BG);
  spr.drawString("no frame for 30s", W / 2, H / 2 + 10);
  // Never leave the last known herd on screen here: a device that shows
  // four idle agents because it stopped being able to ask is worse than one
  // that admits it cannot see.
}

static void drawBadVersion(M5Canvas& spr, int W, int H) {
  spr.setTextDatum(MC_DATUM);
  spr.setTextColor(C_STALE, C_BG);
  spr.setTextSize(2);
  spr.drawString("VERSION", W / 2, H / 2 - 20);
  spr.setTextSize(1);
  spr.setTextColor(C_TEXT, C_BG);
  char buf[48];
  snprintf(buf, sizeof(buf), "relay v%d, firmware v%d",
           g_frame.version, SHEPHERD_PROTOCOL_VERSION);
  spr.drawString(buf, W / 2, H / 2 + 4);
  spr.setTextColor(C_DIM, C_BG);
  spr.drawString("reflash the device", W / 2, H / 2 + 20);
}

// Draw `text` wrapped, but only its first `reveal` characters.
//
// The wrap is computed over the WHOLE string and the reveal happens inside
// that fixed layout. Wrapping the revealed prefix instead would be simpler
// and much worse: words would jump between lines as the typewriter caught up
// with them, and the reader would be chasing the text.
static int drawTyped(M5Canvas& spr, const char* text, int x, int y,
                     int cols, int maxLines, int lineH, int reveal,
                     int skipLines = 0) {
  int len = (int)strlen(text);
  int pos = 0, line = 0, drawn = 0;
  char buf[96];
  while (pos < len) {
    int take = len - pos;
    if (take > cols) {
      take = cols;
      int sp = -1;
      for (int i = take; i > cols / 2; i--) {
        if (text[pos + i] == ' ') { sp = i; break; }
      }
      if (sp > 0) take = sp;
    }
    if (take >= (int)sizeof(buf)) take = (int)sizeof(buf) - 1;

    int show = reveal - drawn;
    if (show > take) show = take;
    const int row = line - skipLines;
    if (show > 0 && row >= 0 && row < maxLines) {
      memcpy(buf, text + pos, show);
      buf[show] = 0;
      spr.drawString(buf, x, y + row * lineH);

      // A caret at the write head, while there is still text to come. It is
      // what makes the reveal read as typing rather than as a slow redraw.
      if (drawn + show < len)
        spr.drawString("_", x + show * 6, y + row * lineH);
    }

    drawn += take;                 // advance by the whole line, not the part
    pos += take;                   // shown, so the layout stays put
    while (pos < len && text[pos] == ' ') { pos++; drawn++; }
    line++;
    // Keep counting lines past the window so the caller knows how far it can
    // scroll, but stop once the reveal has run out - there is nothing below.
    if (show <= 0 && reveal <= drawn) break;
  }
  return line;
}

// Nothing needs answering: a scrollable list of the herd, with a cursor.
static void drawHerd(M5Canvas& spr, int W, int H) {
  spr.setTextSize(1);
  spr.setTextDatum(TL_DATUM);
  const int lineH = 11;

  if (g_frame.count == 0) {
    spr.setTextDatum(MC_DATUM);
    spr.setTextColor(C_DIM, C_BG);
    spr.drawString("no agents", W / 2, H / 2);
    spr.setTextDatum(TL_DATUM);
    return;
  }

  // Keep the selection inside the window, scrolling by the minimum needed so
  // the list does not jump a page when you step off the edge of it.
  if (g_listSel < g_listTop) g_listTop = g_listSel;
  if (g_listSel >= g_listTop + SH_LIST_ROWS) g_listTop = g_listSel - SH_LIST_ROWS + 1;
  if (g_listTop > g_frame.count - SH_LIST_ROWS) g_listTop = g_frame.count - SH_LIST_ROWS;
  if (g_listTop < 0) g_listTop = 0;

  int y = 18;
  for (int i = g_listTop; i < g_frame.count && i < g_listTop + SH_LIST_ROWS; i++) {
    const ShepherdAgent& a = g_frame.agents[i];
    if (i == g_listSel) {
      spr.fillRect(0, y - 1, W, lineH, 0x2104);   // a band, not an inversion:
      spr.drawString(">", 1, y);                  // status colour must survive
    }
    spr.setTextColor(statusColour(a), C_BG);
    spr.drawString(a.alias, 8, y);
    spr.setTextDatum(TR_DATUM);
    spr.drawString(a.status, W - 6, y);
    spr.setTextDatum(TL_DATUM);
    y += lineH;
  }

  // Only claim there is more when there is; a permanent "..." teaches people
  // to ignore it.
  if (g_frame.count > SH_LIST_ROWS) {
    char pos[16];
    snprintf(pos, sizeof(pos), "%d/%d", g_listSel + 1, g_frame.count);
    spr.setTextDatum(TR_DATUM);
    spr.setTextColor(C_DIM, C_BG);
    spr.drawString(pos, W - 6, H - 11);
    spr.setTextDatum(TL_DATUM);
  }

  spr.drawFastHLine(0, H - 14, W, C_DIM);
  spr.setTextColor(C_DIM, C_BG);
  if (g_frame.degraded) {
    spr.setTextColor(C_STALE, C_BG);
    spr.drawString(g_frame.why, 6, H - 11);
  } else {
    spr.drawString("[;/.] move  [enter] recap", 6, H - 11);
  }
}

// One agent, at length: what it says it is doing, and for how long.
static void drawDetail(M5Canvas& spr, int W, int H, int idx) {
  const ShepherdAgent& a = g_frame.agents[idx];

  spr.setTextSize(1);
  spr.setTextDatum(TL_DATUM);
  spr.setTextColor(statusColour(a), C_BG);
  spr.drawString(a.alias, 6, 18);

  // Status and how long it has been that way, on one line. The duration is
  // computed from two absolute timestamps in the frame, so it is the host's
  // clock talking, not this board's.
  char right[32];
  char ago[12];
  shepherdFormatElapsed(ago, sizeof(ago), shepherdElapsed(g_frame, a));
  if (ago[0]) snprintf(right, sizeof(right), "%s  %s", a.status, ago);
  else        snprintf(right, sizeof(right), "%s", a.status);
  spr.setTextDatum(TR_DATUM);
  spr.drawString(right, W - 6, 18);
  spr.setTextDatum(TL_DATUM);
  spr.drawFastHLine(0, 30, W, 0x2104);

  const bool mine = strcmp(g_detail.pane, a.pane) == 0;
  const int rows = 6;
  int total = 0;

  if (g_detailPending || !mine) {
    // The relay has been asked and has not answered yet. Say so rather than
    // showing the previous agent's text under this agent's name.
    spr.setTextColor(C_DIM, C_BG);
    spr.drawString("asking...", 6, 36);
  } else if (g_detail.body[0]) {
    int reveal = g_typedOut ? SHEPHERD_BODY_LEN
                            : (int)((millis() - g_detailMs) / SH_TYPE_MS);
    spr.setTextColor(C_TEXT, C_BG);
    total = drawTyped(spr, g_detail.body, 6, 36, 38, rows, 11, reveal, g_scroll);
  } else {
    // An empty body is an answer, not a failure: mid-tool-call, or nothing
    // said yet. Inventing a summary here would be the dishonest option.
    spr.setTextColor(C_DIM, C_BG);
    spr.drawString("nothing said yet", 6, 36);
  }

  spr.drawFastHLine(0, H - 14, W, C_DIM);
  spr.setTextColor(C_DIM, C_BG);
  if (total > rows) {
    char pos[16];
    snprintf(pos, sizeof(pos), "%d/%d", g_scroll + 1, total - rows + 1);
    spr.setTextDatum(TR_DATUM);
    spr.drawString(pos, W - 6, H - 11);
    spr.setTextDatum(TL_DATUM);
    spr.drawString("[;/.] scroll  [</>] agent", 6, H - 11);
  } else {
    spr.drawString("[del] back  [</>] next agent", 6, H - 11);
  }
}

// Something needs answering: the queue screen.
static void drawQueue(M5Canvas& spr, int W, int H, int idx) {
  const ShepherdAgent& a = g_frame.agents[idx];

  spr.setTextSize(1);
  spr.setTextDatum(TL_DATUM);
  spr.setTextColor(statusColour(a), C_BG);
  spr.drawString(a.alias, 6, 18);

  spr.setTextDatum(TR_DATUM);
  spr.setTextColor(C_BLOCKED, C_BG);
  spr.drawString("BLOCKED", W - 6, 18);
  spr.setTextDatum(TL_DATUM);

  spr.setTextColor(C_TEXT, C_BG);
  int y = drawWrapped(spr, a.question, 6, 34, 38, 5, 11);

  // Say why approve is off whenever it is off, not only for the truncated
  // case. An unexplained dead key reads as a broken device.
  const bool canApprove = a.answerable();
  if (!canApprove) {
    spr.setTextColor(C_STALE, C_BG);
    spr.drawString(a.truncated ? "truncated - open the laptop"
                               : "no decision id - open the laptop",
                   6, y + 2);
  }

  spr.drawFastHLine(0, H - 14, W, C_DIM);
  spr.setTextColor(C_DIM, C_BG);
  if (g_lock.locked(millis())) {
    // Advertise the way out rather than the keys that will not work.
    spr.setTextColor(C_LOCK, C_BG);
    spr.drawString("locked - Fn+Del to unlock", 6, H - 11);
  } else if (!canApprove) {
    spr.drawString("[n] deny  [>] next  [del] list", 6, H - 11);
  } else {
    // 40 columns at 6px, so this is the whole budget. "del" earns its place:
    // without it the herd list is unreachable exactly when the device is
    // most worth looking at.
    spr.drawString("[y]ok [n]no [>]next [del]list", 6, H - 11);
  }
}

void shepherdUiDraw(M5Canvas& spr, int W, int H) {
  spr.fillSprite(C_BG);
  drawHeader(spr, W);

  if (g_badVersion) {
    drawBadVersion(spr, W, H);
  } else if (stale()) {
    drawStale(spr, W, H);
  } else if (g_view == ShepherdView::Detail && g_listSel < g_frame.count) {
    drawDetail(spr, W, H, g_listSel);
  } else if (g_view == ShepherdView::List) {
    drawHerd(spr, W, H);
  } else {
    // Showable, not answerable. Driving the screen off answerability sent a
    // blocked-but-truncated agent to the herd list, where it read as one more
    // amber row and the approve key did nothing with no explanation — which
    // made the queue screen's own "open the laptop" line unreachable code.
    int idx = g_frame.firstShowable(g_cursor);
    if (idx < 0) idx = g_frame.firstShowable();
    if (idx >= 0) drawQueue(spr, W, H, idx);
    else drawHerd(spr, W, H);
  }

  if (g_note[0] && (int32_t)(millis() - g_noteUntil) < 0) {
    spr.setTextDatum(BC_DATUM);
    spr.setTextColor(C_TEXT, C_BG);
    spr.drawString(g_note, W / 2, H - 1);
    spr.setTextDatum(TL_DATUM);
  }
}

// ---------------------------------------------------------------- actions

static void sendAct(const ShepherdAgent& a, const char* action) {
  // Sign against the timestamp of the frame currently on screen. The relay
  // only accepts a frame it recently sent, so a captured action stops working
  // once the herd moves on — which is the only replay protection `focus` has.
  char msg[SHEPHERD_TS_LEN + SHEPHERD_PANE_LEN + SHEPHERD_DECISION_LEN + 32];
  char mac[SHEPHERD_MAC_LEN] = {0};
  bool signedOk = false;
  size_t mlen = shepherdCanonicalMessage(msg, sizeof(msg), g_frame.ts,
                                         a.pane, action, a.decision);
  if (mlen) signedOk = signMessage(msg, mlen, mac, sizeof(mac));

  char buf[256];
  size_t n = signedOk
      ? shepherdBuildAct(buf, sizeof(buf), a.pane, action, a.decision,
                         g_frame.ts, mac)
      : shepherdBuildAct(buf, sizeof(buf), a.pane, action, a.decision);
  if (!n) { note("frame too long"); return; }
  if (!bleConnected()) { note("not connected"); return; }
  bleWrite((const uint8_t*)buf, n);
  if (!signedOk) {
    // Visible rather than silent: an unsigned frame will be refused by any
    // relay configured with a secret, and "nothing happened" is the worst
    // possible explanation for that.
    note("unsigned - check build");
  }
}

// The list and the detail screen share a selection. Moving it wraps, because
// on a nine-row window with twelve agents the alternative is a dead key at
// each end.
static void moveSelection(int delta) {
  if (g_frame.count <= 0) return;
  g_listSel = (g_listSel + delta + g_frame.count) % g_frame.count;
}

// Whether the herd list is what is currently on screen.
static bool showingList() {
  if (g_view == ShepherdView::List) return true;
  return g_view == ShepherdView::Auto && g_frame.firstShowable() < 0;
}

// Ask the relay what this agent last said. Signed exactly like an approve:
// it makes the relay read a pane, and "an unauthenticated peer cannot make
// the relay do work" covers reads too.
static void requestDetail(const ShepherdAgent& a, uint32_t now) {
  g_detail.clear();
  g_detailPending = true;
  g_detailMs = now;
  g_scroll = 0;
  g_typedOut = false;
  sendAct(a, "detail");
}

bool shepherdUiKey(HalKey k) {
  if (!shepherdUiActive()) return false;

  const uint32_t now = millis();

  // The chord is handled before the version and staleness gates, so it still
  // works on a screen that is refusing to render anything else. Being unable
  // to unlock a NO SIGNAL device would mean waiting out a reconnect with a
  // dead keyboard.
  if (k == HalKey::Unlock) {
    note(g_lock.toggle(now) ? "locked" : "unlocked");
    return true;
  }
  // Every key, not only the two that send. Navigating the herd in a pocket
  // is harmless in itself, but it moves the selection - so you would unlock
  // to answer and find the cursor somewhere other than where you left it,
  // with the queue's y bound to whichever agent the cloth landed on.
  if (!g_lock.accept(now)) {
    // Consumed, not passed on: while Shepherd owns the screen a locked key
    // must not reach the buddy's approve path either. And it says so,
    // because a silent no-op is indistinguishable from a broken device.
    note("locked - Fn+Del");
    return true;
  }
  if (g_badVersion || stale()) return false;

  // ---- the detail screen -------------------------------------------------
  if (g_view == ShepherdView::Detail) {
    switch (k) {
      case HalKey::Back:
        g_view = ShepherdView::List;
        return true;

      case HalKey::Up:
      case HalKey::Down:
        // Scrolling is also the impatient exit from the typewriter: once you
        // have started moving through the text, watching it appear a
        // character at a time is in your way.
        g_typedOut = true;
        if (k == HalKey::Down) g_scroll++;
        else if (g_scroll > 0) g_scroll--;
        return true;

      case HalKey::Left:
      case HalKey::Right:
        // Walk the herd without surfacing to the list between agents.
        moveSelection(k == HalKey::Right ? 1 : -1);
        if (g_listSel < g_frame.count) requestDetail(g_frame.agents[g_listSel], now);
        return true;

      default:
        // Nothing on this screen sends anything, so swallow the rest rather
        // than letting y/n fall through to a queue the reader cannot see.
        return true;
    }
  }

  // ---- the herd list -----------------------------------------------------
  if (showingList()) {
    switch (k) {
      case HalKey::Up:
      case HalKey::Left:
        moveSelection(-1);
        return true;
      case HalKey::Down:
      case HalKey::Right:
        moveSelection(1);
        return true;
      case HalKey::Approve:
        if (g_frame.count == 0) return true;
        g_view = ShepherdView::Detail;
        requestDetail(g_frame.agents[g_listSel], now);
        return true;
      case HalKey::Back:
        // Back out of a list you navigated to deliberately; on the automatic
        // list there is nowhere further out to go.
        if (g_view == ShepherdView::List) g_view = ShepherdView::Auto;
        return true;
      default:
        return false;   // m for the buddy menu, etc.
    }
  }

  // ---- the queue ---------------------------------------------------------
  // Keys follow the screen: whatever drawQueue is showing is what y/n act on.
  int idx = g_frame.firstShowable(g_cursor);
  if (idx < 0) idx = g_frame.firstShowable();

  switch (k) {
    case HalKey::Approve:
      if (idx < 0) return false;
      // Belt and braces with the host: it refuses truncated approves too,
      // but the device should not offer an action it knows will be refused.
      if (!g_frame.agents[idx].answerable()) {
        note("cannot approve - laptop");
        return true;
      }
      sendAct(g_frame.agents[idx], "approve");
      note("approved");
      g_cursor = idx + 1;
      return true;

    case HalKey::Deny:
      // Deny stays available even when approve is not: escaping a prompt you
      // cannot fully read is always safe, and it is the whole point of being
      // able to answer from the sofa.
      if (idx < 0) return false;
      sendAct(g_frame.agents[idx], "deny");
      note("denied");
      g_cursor = idx + 1;
      return true;

    case HalKey::Right:
    case HalKey::Down: {
      int next = g_frame.firstShowable(idx + 1);
      g_cursor = (next >= 0) ? next : 0;
      return true;
    }

    case HalKey::Back:
      // The way to the herd list while something is queued. Without it the
      // list is unreachable exactly when the device is most interesting.
      g_view = ShepherdView::List;
      if (idx >= 0) g_listSel = idx;
      return true;

    default:
      return false;
  }
}
