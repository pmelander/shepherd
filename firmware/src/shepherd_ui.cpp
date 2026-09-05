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

  g_lastFrameMs = millis();
  g_everReceived = true;

  if (r == SHEPHERD_BAD_VERSION) {
    g_badVersion = true;
    return true;   // ours, and deliberately not rendered as data
  }
  if (r != SHEPHERD_OK) return true;

  g_badVersion = false;
  // Keep the cursor on the same agent across frames where possible, so a
  // refresh arriving as you reach for a key does not move the answer out
  // from under your thumb.
  char keep[SHEPHERD_PANE_LEN] = {0};
  if (g_cursor >= 0 && g_cursor < g_frame.count)
    strncpy(keep, g_frame.agents[g_cursor].pane, sizeof(keep) - 1);

  g_frame = parsed;
  g_cursor = 0;
  if (keep[0]) {
    for (int i = 0; i < g_frame.count; i++) {
      if (strcmp(g_frame.agents[i].pane, keep) == 0) { g_cursor = i; break; }
    }
  }
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

// Nothing needs answering: a compact list of the herd.
static void drawHerd(M5Canvas& spr, int W, int H) {
  spr.setTextSize(1);
  spr.setTextDatum(TL_DATUM);
  int y = 18;
  const int lineH = 11;
  int shown = 0;
  for (int i = 0; i < g_frame.count && y < H - 12; i++) {
    const ShepherdAgent& a = g_frame.agents[i];
    spr.setTextColor(statusColour(a), C_BG);
    spr.drawString(a.alias, 6, y);
    spr.setTextDatum(TR_DATUM);
    spr.drawString(a.status, W - 6, y);
    spr.setTextDatum(TL_DATUM);
    y += lineH;
    shown++;
  }
  if (shown == 0) {
    spr.setTextDatum(MC_DATUM);
    spr.setTextColor(C_DIM, C_BG);
    spr.drawString("no agents", W / 2, H / 2);
    spr.setTextDatum(TL_DATUM);
  }
  if (g_frame.degraded) {
    spr.setTextColor(C_STALE, C_BG);
    spr.drawString(g_frame.why, 6, H - 10);
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
    spr.drawString("locked - Fn+Enter to unlock", 6, H - 11);
  } else if (!canApprove) {
    spr.drawString("[n] deny   [>] next", 6, H - 11);
  } else {
    spr.drawString("[y] approve  [n] deny  [>] next", 6, H - 11);
  }
}

void shepherdUiDraw(M5Canvas& spr, int W, int H) {
  spr.fillSprite(C_BG);
  drawHeader(spr, W);

  if (g_badVersion) {
    drawBadVersion(spr, W, H);
  } else if (stale()) {
    drawStale(spr, W, H);
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

bool shepherdUiKey(HalKey k) {
  if (!shepherdUiActive()) return false;

  const uint32_t now = millis();

  // The lock is checked before the version and staleness gates, so the chord
  // still works on a screen that is refusing to render anything else. Being
  // unable to unlock a NO SIGNAL device would mean waiting out a reconnect
  // with a dead keyboard.
  if (k == HalKey::Unlock) {
    note(g_lock.toggle(now) ? "locked" : "unlocked");
    return true;
  }
  // accept() both tests the lock and, when it passes, resets the idle timer:
  // using the device is what keeps it awake.
  if (!g_lock.accept(now)) {
    // Consumed, not passed on. While Shepherd owns the screen a locked key
    // must not reach the buddy's own approve path either. And it says so,
    // because a silent no-op is how the last bug presented.
    note("locked - Fn+Enter");
    return true;
  }
  if (g_badVersion || stale()) return false;

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

    default:
      return false;
  }
}
