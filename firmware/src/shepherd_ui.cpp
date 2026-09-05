#include "shepherd_ui.h"

#include <Arduino.h>

#include "ble_bridge.h"

// ---------------------------------------------------------------- state

static ShepherdFrame g_frame;
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
  if (stale() || g_badVersion) return false;
  return g_frame.firstAnswerable() >= 0;
}

// ---------------------------------------------------------------- output

static void drawHeader(M5Canvas& spr, int W) {
  spr.setTextSize(1);
  spr.setTextDatum(TL_DATUM);
  spr.setTextColor(C_DIM, C_BG);
  spr.drawString("SHEPHERD", 4, 3);

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

  if (a.truncated) {
    // The host will refuse this approve, so the device must not offer it.
    spr.setTextColor(C_STALE, C_BG);
    spr.drawString("truncated - open the laptop", 6, y + 2);
  }

  spr.drawFastHLine(0, H - 14, W, C_DIM);
  spr.setTextColor(C_DIM, C_BG);
  if (a.truncated) {
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
    int idx = g_frame.firstAnswerable(g_cursor);
    if (idx < 0) idx = g_frame.firstAnswerable();
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
  char buf[192];
  size_t n = shepherdBuildAct(buf, sizeof(buf), a.pane, action, a.decision);
  if (!n) { note("frame too long"); return; }
  if (!bleConnected()) { note("not connected"); return; }
  bleWrite((const uint8_t*)buf, n);
}

bool shepherdUiKey(HalKey k) {
  if (!shepherdUiActive()) return false;
  if (g_badVersion || stale()) return false;

  int idx = g_frame.firstAnswerable(g_cursor);
  if (idx < 0) idx = g_frame.firstAnswerable();

  switch (k) {
    case HalKey::Approve:
      if (idx < 0) return false;
      // Belt and braces with the host: it refuses truncated approves too,
      // but the device should not offer an action it knows will be refused.
      if (g_frame.agents[idx].truncated) { note("truncated - laptop"); return true; }
      sendAct(g_frame.agents[idx], "approve");
      note("approved");
      g_cursor = idx + 1;
      return true;

    case HalKey::Deny:
      if (idx < 0) return false;
      sendAct(g_frame.agents[idx], "deny");
      note("denied");
      g_cursor = idx + 1;
      return true;

    case HalKey::Right:
    case HalKey::Down: {
      int next = g_frame.firstAnswerable(idx + 1);
      g_cursor = (next >= 0) ? next : 0;
      return true;
    }

    default:
      return false;
  }
}
