// Bruno, drawn.
//
// A sheep, a speech bubble, and a one-line strip of herd totals. Nothing here
// decides anything - bruno_view.h has already worked out the mood, whose news
// it is and what the bubble says, and this turns that into pixels.
//
// Three notes on how rather than what.
//
// The whole SCREEN is redrawn only when the herd changes. The loop calls this
// at ~15Hz and a full 320x240 repaint at that rate makes a desk toy flicker
// like a fault rather than sit there being alive.
//
// HE is redrawn much more often, in a 108x96 patch drawn OFF-SCREEN and
// pushed in one go, because a pet that only moves when an agent finishes is a
// picture of a pet. The blink and the occasional mouthful of grass happen on
// the early-return path where nothing about the herd has changed at all.
// Drawing that patch straight to the panel meant wiping the background and
// then drawing over it with the display watching, which is a visible flash
// per redraw; a sprite costs 20KB of heap and removes the seam entirely.
//
// The animation is timed in MILLISECONDS, not frames. Counting frames makes
// the frame rate a secret parameter of every behaviour, so raising it speeds
// him up like a wind-up toy; on a clock he grazes for the same four seconds
// whatever the rate.
//
// There is no sheep in `src/buddies/`. Twenty species were vendored with the
// fork - axolotl through turtle - and not one of them is a sheep, so Bruno is
// drawn from primitives here rather than loaded from a GIF. That also
// sidesteps the filesystem, which is the right call on this project: the
// Cardputer's LittleFS is corrupt and its GIF characters stopped loading
// because of it.

#include <M5Unified.h>

#include "bruno_ui.h"

namespace {

// Named because "which green" gets adjusted a lot and hunting RGB565 literals
// through drawing code is miserable. RGB565 is ((R>>3)<<11)|((G>>2)<<5)|(B>>3).
constexpr uint16_t C_SKY_HI  = 0x3C1B;   // deep blue overhead
constexpr uint16_t C_SKY_LO  = 0xA69E;   // pale at the horizon
constexpr uint16_t C_SUN     = 0xFF50;
constexpr uint16_t C_CLOUD   = 0xFFFF;
constexpr uint16_t C_CLOUD_S = 0xE75E;   // its underside
constexpr uint16_t C_HILL_FAR = 0x5D48;  // sunlit, further off
constexpr uint16_t C_HILL_MID = 0x3C66;
constexpr uint16_t C_GRASS    = 0x2B65;  // the field he stands in

constexpr uint16_t C_WOOL    = 0xEF7D;   // warm off-white
constexpr uint16_t C_WOOL_SH = 0xB596;   // its shadow
constexpr uint16_t C_FACE    = 0x31A6;   // near-black, but not the background

constexpr uint16_t C_BUBBLE  = 0xFFDD;   // cream, so dark text sits on it
constexpr uint16_t C_INK     = 0x1905;
constexpr uint16_t C_BG      = TFT_BLACK;  // the error screens only
constexpr uint16_t C_TEXT    = TFT_WHITE;
constexpr uint16_t C_DIM     = 0x8410;
constexpr uint16_t C_ALERT   = 0xC1A0;   // amber, dark enough to read on cream
constexpr uint16_t C_HAPPY   = 0x0460;   // green, likewise

// Where the land starts. The sheep's feet land in it rather than on the line.
constexpr int kHorizon = 168;

// What is already on screen, so a repaint only happens when it must.
BrunoMood g_lastMood = (BrunoMood)0xFF;
char g_lastPane[SHEPHERD_PANE_LEN] = {0};
char g_lastText[BRUNO_SAID_LEN] = {0};
int g_lastWorking = -1, g_lastBlocked = -1, g_lastDone = -1;

// Where the sheep stands, and where his head is. Derived in one place and
// used by both the sheep and the bubble's tail, because the first version had
// the tail hard-coded near the bubble's left edge while the head is on the
// right - so it pointed at nothing. Two hand-placed numbers that have to
// agree will eventually not agree.
int sheepCx() { return M5.Display.width() / 2 - 10; }
constexpr int kHeadDx = 34;                     // head offset from the body
int sheepHeadX() { return sheepCx() + kHeadDx; }

// ------------------------------------------------------------------- scene

// Clouds drift. This is not decoration: it is the liveness indicator, and it
// replaces the "alive N" counter the bring-up build printed in a corner. A
// frozen screen and a quiet herd look identical on a device whose whole job
// is to sit still looking calm, so SOMETHING has to keep moving - and a cloud
// belongs in the picture in a way that a debug counter does not.
int g_drift = 0;

void skyBand(int y0, int y1) {
  // Vertical gradient by horizontal lines. Cheap, and only redrawn when the
  // clouds move or the whole view changes.
  const int w = M5.Display.width();
  const int span = kHorizon > 0 ? kHorizon : 1;
  for (int y = y0; y < y1; y++) {
    const int t = (y * 255) / span;              // 0 at the top, 255 at land
    const uint8_t r = (uint8_t)((0x3C * (255 - t) + 0xA6 * t) / 255);
    const uint8_t g = (uint8_t)((0x78 * (255 - t) + 0xD2 * t) / 255);
    const uint8_t b = (uint8_t)((0xD8 * (255 - t) + 0xF5 * t) / 255);
    M5.Display.drawFastHLine(0, y, w, M5.Display.color565(r, g, b));
  }
  (void)C_SKY_HI; (void)C_SKY_LO;   // the endpoints the gradient interpolates
}

void drawCloud(int cx, int cy, int scale) {
  M5.Display.fillCircle(cx, cy, 6 * scale / 10, C_CLOUD_S);
  M5.Display.fillCircle(cx - 9 * scale / 10, cy + 2, 7 * scale / 10, C_CLOUD);
  M5.Display.fillCircle(cx, cy - 4, 9 * scale / 10, C_CLOUD);
  M5.Display.fillCircle(cx + 10 * scale / 10, cy + 1, 7 * scale / 10, C_CLOUD);
}

// The band the clouds live in, redrawn on its own so drifting costs a strip
// rather than a whole screen.
constexpr int kCloudY0 = 0, kCloudY1 = 30;

void drawClouds() {
  const int w = M5.Display.width();
  skyBand(kCloudY0, kCloudY1);
  // The sun sits behind them, top right.
  M5.Display.fillCircle(w - 34, 14, 13, C_SUN);
  // Two clouds at different speeds, so the sky does not look like a
  // conveyor belt.
  drawCloud((g_drift % (w + 80)) - 40, 16, 12);
  drawCloud(((g_drift * 2 / 3 + 170) % (w + 80)) - 40, 10, 9);
}

// The box he occupies, generously. Animating repaints only this, because a
// full 320x240 redraw five times a second is how a calm pet turns into a
// flickering fault.
constexpr int kSheepBoxY0 = 112, kSheepBoxY1 = 208;
int sheepBoxX0() { return sheepCx() - 52; }
constexpr int kSheepBoxW = 108;

void drawScene() {
  const int w = M5.Display.width(), h = M5.Display.height();
  skyBand(0, kHorizon);
  drawClouds();

  // Rolling pasture: overlapping ellipses, furthest and palest first.
  M5.Display.fillEllipse(w / 4, kHorizon + 26, w / 2 + 30, 34, C_HILL_FAR);
  M5.Display.fillEllipse(w - 30, kHorizon + 30, w / 2, 32, C_HILL_FAR);
  M5.Display.fillEllipse(w / 2 + 40, kHorizon + 34, w / 2, 30, C_HILL_MID);
  // And the field he actually stands in.
  M5.Display.fillRect(0, kHorizon + 22, w, h - kHorizon - 22, C_GRASS);
}

uint16_t moodColour(BrunoMood m) {
  switch (m) {
    case BRUNO_MOOD_ATTENTION:   return C_ALERT;
    case BRUNO_MOOD_CELEBRATE:   return C_HAPPY;
    case BRUNO_MOOD_STALE:
    case BRUNO_MOOD_BAD_VERSION: return TFT_RED;
    default:                     return C_DIM;
  }
}

// ------------------------------------------------------------------- sheep

// How he is standing this instant, as opposed to what mood he is in. Split
// out so the animation can move him without the mood changing - which is the
// whole point: the herd can be quiet for an hour and he still has to look
// like something that is alive rather than a drawing of a sheep.
struct SheepPose {
  int lift;        // off the ground, for celebrating
  int headDy;      // 0 grazing-height ... negative is head up
  bool eyesShut;   // a blink, or grazing
  bool earsUp;
};

void drawSheep(LovyanGFX* g, int cx, int cy, BrunoMood mood,
               const SheepPose& p) {
  const bool alarmed = p.earsUp;
  const int by = cy - p.lift;

  // Legs first, so the body sits over them.
  for (int i = 0; i < 4; i++) {
    const int lx = cx - 26 + i * 17;
    g->fillRect(lx, by + 18, 5, 18, C_FACE);
  }

  // Body: overlapping circles make wool without needing a bitmap.
  g->fillCircle(cx - 20, by + 2, 18, C_WOOL_SH);
  g->fillCircle(cx + 12, by + 2, 18, C_WOOL_SH);
  g->fillCircle(cx - 8,  by - 8, 20, C_WOOL);
  g->fillCircle(cx + 12, by - 4, 17, C_WOOL);
  g->fillCircle(cx - 24, by - 2, 16, C_WOOL);

  // Head, turned to the right. headDy is where the animation lives: it nods
  // gently while he is standing and drops to the grass when he grazes.
  const int hx = cx + kHeadDx;
  const int hy = by - 6 + p.headDy;
  // A neck, so the head does not detach from the body when it drops.
  g->fillEllipse(hx - 12, (by - 6 + hy) / 2, 8, 11, C_WOOL);
  g->fillEllipse(hx, hy, 13, 11, C_FACE);
  // Ears. Up and forward when something wants attention.
  g->fillEllipse(hx - 9, hy - (alarmed ? 12 : 8), 4, alarmed ? 8 : 5,
                         C_FACE);
  g->fillEllipse(hx + 7, hy - (alarmed ? 12 : 8), 4, alarmed ? 8 : 5,
                         C_FACE);

  if (p.eyesShut) {
    g->drawFastHLine(hx - 6, hy - 1, 6, C_WOOL);
    g->drawFastHLine(hx + 2, hy - 1, 6, C_WOOL);
  } else {
    const int r = alarmed ? 3 : 2;
    g->fillCircle(hx - 4, hy - 2, r, C_WOOL);
    g->fillCircle(hx + 5, hy - 2, r, C_WOOL);
  }
  (void)mood;
}

// ---------------------------------------------------------------- aliveness

// Everything that makes him move. Driven at 5Hz from brunoUiDraw, which is
// called on a timer and returns early when the VIEW has not changed - so this
// is what happens on all those early returns, and it is the difference
// between a pet and a picture of one.
//
// He only potters when nothing wants you. Grazing contentedly in front of a
// blocked agent would undercut the whole attention rule, so ATTENTION gets a
// still, ears-up sheep and nothing else.
// Everything below is in MILLISECONDS rather than in frames. The first
// version counted ticks, which meant the nod, the blink and the grazing were
// all secretly defined by the draw rate - so raising the frame rate sped him
// up like a wind-up toy and every constant needed retuning. On a clock, the
// frame rate is free to change and he still grazes for the same four seconds.
constexpr uint32_t kNodStepMs   = 110;               // one step of the nod
constexpr uint32_t kGrazeMinMs  = 2500, kGrazeVarMs = 3500;
constexpr uint32_t kGapMinMs    = 9000, kGapVarMs   = 16000;
constexpr uint32_t kBlinkMs     = 120;
constexpr uint32_t kBlinkGapMin = 2500, kBlinkGapVar = 9000;
constexpr uint32_t kBounceMs    = 90;                // celebration bounce step

uint32_t g_now = 0;
bool g_grazing = false;
uint32_t g_grazeEnds = 0;
uint32_t g_nextGraze = 6000;
uint32_t g_blinkEnds = 0;
uint32_t g_nextBlink = 4000;

SheepPose poseFor(BrunoMood mood) {
  SheepPose p{};
  p.earsUp = (mood == BRUNO_MOOD_ATTENTION);

  if (mood == BRUNO_MOOD_CELEBRATE) {
    // A real bounce, eight steps of it, so he springs rather than blinking
    // between two heights.
    static const int kBounce[8] = {0, 5, 9, 11, 11, 9, 5, 1};
    p.lift = kBounce[(g_now / kBounceMs) % 8];
    p.headDy = -5;
    return p;
  }
  if (mood == BRUNO_MOOD_ATTENTION) {
    p.headDy = -8;                     // head up, watching, dead still
    return p;
  }

  // No nod. It was tried and removed: it changed every 110ms, and every
  // change repaints his patch, so it turned the redraw seam into a permanent
  // shimmer. The flicker is fixed below by drawing the patch off-screen, but
  // the nod is not coming back on that account - three pixels of travel was
  // never worth a redraw ten times a second. Blinking and grazing carry the
  // aliveness between them, and they happen rarely enough to be events
  // rather than texture.
  p.headDy = 0;
  if (g_grazing) {
    p.headDy = 22;                     // down in the grass
    p.eyesShut = true;
  } else {
    p.eyesShut = g_now < g_blinkEnds;
  }
  return p;
}

void advanceAnimation(BrunoMood mood, uint32_t now) {
  g_now = now;
  const bool canPotter = (mood == BRUNO_MOOD_POTTER || mood == BRUNO_MOOD_BUSY);

  if (!canPotter) {
    // He does not graze while something wants you. Contented munching in
    // front of a blocked agent would undercut the whole attention rule.
    g_grazing = false;
    g_nextGraze = now + kGapMinMs + (uint32_t)random(kGapVarMs);
    return;
  }
  if (g_grazing) {
    if ((int32_t)(now - g_grazeEnds) >= 0) {
      g_grazing = false;
      g_nextGraze = now + kGapMinMs + (uint32_t)random(kGapVarMs);
    }
  } else if ((int32_t)(now - g_nextGraze) >= 0) {
    g_grazing = true;
    g_grazeEnds = now + kGrazeMinMs + (uint32_t)random(kGrazeVarMs);
  }
  if ((int32_t)(now - g_nextBlink) >= 0) {
    g_blinkEnds = now + kBlinkMs;
    g_nextBlink = now + kBlinkGapMin + (uint32_t)random(kBlinkGapVar);
  }
}

// Repaint his patch of field and then him, so a nodding head does not smear
// and a dropped one does not leave the old one behind.
// The patch, drawn OFF-SCREEN and pushed in one operation.
//
// The first version drew straight to the display: wipe the background, then
// draw the sheep. That is two passes over the same pixels with the panel
// showing both, so every redraw was a visible flash - and with a nod changing
// every 110ms it became a permanent shimmer that read as corruption. A sprite
// costs 108*96*2 = 20,736 bytes out of 314KB of free heap, and the seam
// disappears because the panel only ever sees a finished picture.
//
// It degrades rather than fails: if the allocation does not happen, the old
// clipped path still draws, flicker and all.
M5Canvas g_patch(&M5.Display);
bool g_patchReady = false;
bool g_patchTried = false;

void paintPatchInto(LovyanGFX* g, int ox, int oy, BrunoMood mood) {
  const int w = M5.Display.width(), h = M5.Display.height();
  // NOT the whole scene. Clipping would stop the pixels but not the work: the
  // full scene issues 168 gradient rows plus the clouds and the sun, all
  // outside this box. Only what is actually inside his patch is drawn - about
  // fifty sky rows, the hills, and the grass - with every coordinate shifted
  // into the patch's own space.
  const int span = kHorizon > 0 ? kHorizon : 1;
  const int skyTo = kSheepBoxY1 < kHorizon ? kSheepBoxY1 : kHorizon;
  for (int y = kSheepBoxY0; y < skyTo; y++) {
    const int t = (y * 255) / span;
    g->drawFastHLine(0, y - oy, kSheepBoxW,
                     g->color565((uint8_t)((0x3C * (255 - t) + 0xA6 * t) / 255),
                                 (uint8_t)((0x78 * (255 - t) + 0xD2 * t) / 255),
                                 (uint8_t)((0xD8 * (255 - t) + 0xF5 * t) / 255)));
  }
  g->fillEllipse(w / 4 - ox, kHorizon + 26 - oy, w / 2 + 30, 34, C_HILL_FAR);
  g->fillEllipse(w - 30 - ox, kHorizon + 30 - oy, w / 2, 32, C_HILL_FAR);
  g->fillEllipse(w / 2 + 40 - ox, kHorizon + 34 - oy, w / 2, 30, C_HILL_MID);
  g->fillRect(0, kHorizon + 22 - oy, kSheepBoxW, h - kHorizon - 22, C_GRASS);
  // Feet land IN the grass rather than on the horizon line.
  drawSheep(g, sheepCx() - ox, kHorizon - 14 - oy, mood, poseFor(mood));
}

void drawSheepPatch(BrunoMood mood) {
  const int ox = sheepBoxX0(), oy = kSheepBoxY0;
  if (!g_patchTried) {
    g_patchTried = true;
    g_patch.setColorDepth(16);
    g_patchReady = g_patch.createSprite(kSheepBoxW, kSheepBoxY1 - kSheepBoxY0)
                   != nullptr;
    Serial.printf("[bruno] sprite    : %s (%d bytes, heap left %u)\n",
                  g_patchReady ? "allocated" : "FAILED - falling back to "
                                               "direct draw",
                  kSheepBoxW * (kSheepBoxY1 - kSheepBoxY0) * 2,
                  (unsigned)ESP.getFreeHeap());
  }
  if (g_patchReady) {
    paintPatchInto(&g_patch, ox, oy, mood);
    g_patch.pushSprite(&M5.Display, ox, oy);
    return;
  }
  M5.Display.setClipRect(ox, oy, kSheepBoxW, kSheepBoxY1 - kSheepBoxY0);
  paintPatchInto(&M5.Display, 0, 0, mood);
  M5.Display.clearClipRect();
}

// ------------------------------------------------------------------ bubble

// Wrap on spaces into the given box. The device's font is fixed-width at size
// 1, so a character count is an honest measure of a line.
void bubbleText(const char* text, int x, int y, int w, int maxLines) {
  if (!text || !*text) return;
  const int cols = w / 6;                 // 6px per glyph at size 1
  int line = 0, i = 0;
  const int len = (int)strlen(text);
  while (i < len && line < maxLines) {
    int take = cols;
    if (i + take < len) {
      int brk = -1;
      for (int k = take; k > cols / 2; k--) {
        if (text[i + k] == ' ') { brk = k; break; }
      }
      if (brk > 0) take = brk;
    } else {
      take = len - i;
    }
    char buf[64];
    if (take > (int)sizeof(buf) - 1) take = sizeof(buf) - 1;
    memcpy(buf, text + i, take);
    buf[take] = 0;
    M5.Display.drawString(buf, x, y + line * 12);
    i += take;
    while (i < len && text[i] == ' ') i++;
    line++;
  }
}

void drawBubble(const BrunoView& v) {
  // Nothing to say: no bubble at all, and the sky shows through. The old
  // version filled the area black whether or not it had anything in it, which
  // over a scene would be a hole rather than an absence.
  if (!v.text[0] && !v.pane[0]) return;

  const int w = M5.Display.width();
  const int bx = 10, by = 28, bw = w - 20, bh = 74;
  const uint16_t edge = moodColour(v.mood);
  M5.Display.fillRoundRect(bx, by, bw, bh, 8, C_BUBBLE);
  M5.Display.drawRoundRect(bx, by, bw, bh, 8, edge);
  // The tail, on the RIGHT, leaning the same way the head does and pointing
  // down at it. Anchored to sheepHeadX() rather than to the bubble, so it
  // follows the sheep if either moves.
  const int tx = sheepHeadX();
  const int base = by + bh;
  M5.Display.fillTriangle(tx - 16, base, tx - 2, base, tx + 4, base + 12,
                          C_BUBBLE);
  M5.Display.drawLine(tx - 16, base, tx + 4, base + 12, edge);
  M5.Display.drawLine(tx + 4, base + 12, tx - 2, base, edge);
  // Erase the bubble's own border between the tail's feet so it reads as one
  // shape rather than a triangle stuck to a box.
  M5.Display.drawFastHLine(tx - 15, base, 13, C_BUBBLE);

  M5.Display.setTextSize(1);
  if (v.pane[0]) {
    M5.Display.setTextColor(edge, C_BUBBLE);
    char who[40];
    snprintf(who, sizeof(who), "%s %s", v.pane,
             v.mood == BRUNO_MOOD_ATTENTION ? "is asking" : "finished");
    M5.Display.drawString(who, bx + 10, by + 8);
  }
  M5.Display.setTextColor(C_INK, C_BUBBLE);
  bubbleText(v.text, bx + 10, by + 24, bw - 20, 4);
}

// ------------------------------------------------------------------- strip

void drawStrip(const BrunoView& v) {
  // A band of shade along the bottom of the field, rather than a black bar
  // cut out of it.
  const int w = M5.Display.width(), h = M5.Display.height();
  M5.Display.fillRect(0, h - 17, w, 17, 0x1A43);
  M5.Display.drawFastHLine(0, h - 18, w, 0x4B0A);
  M5.Display.setTextSize(1);
  char s[48];
  snprintf(s, sizeof(s), "%d working   %d blocked   %d done", v.working,
           v.blocked, v.done);
  M5.Display.setTextColor(0xCE79, 0x1A43);
  M5.Display.drawString(s, 8, h - 13);
}

// A device showing a calm herd it cannot actually see is lying. These two are
// the same rule Shepherd draws, said in Bruno's own hand rather than shared -
// shepherd_ui.cpp is not compiled into this environment, and dragging it in
// would bring hal.h, the keyboard and the whole Cardputer with it.
void drawNoSignal() {
  const int w = M5.Display.width(), h = M5.Display.height();
  M5.Display.fillScreen(C_BG);
  M5.Display.setTextDatum(middle_center);
  M5.Display.setTextColor(TFT_RED, C_BG);
  M5.Display.setTextSize(2);
  M5.Display.drawString("NO SIGNAL", w / 2, h / 2 - 12);
  M5.Display.setTextSize(1);
  M5.Display.setTextColor(C_DIM, C_BG);
  M5.Display.drawString("no frame from the relay", w / 2, h / 2 + 14);
  M5.Display.setTextDatum(top_left);
}

void drawBadVersion() {
  const int w = M5.Display.width(), h = M5.Display.height();
  M5.Display.fillScreen(C_BG);
  M5.Display.setTextDatum(middle_center);
  M5.Display.setTextColor(TFT_RED, C_BG);
  M5.Display.setTextSize(2);
  M5.Display.drawString("VERSION", w / 2, h / 2 - 12);
  M5.Display.setTextSize(1);
  M5.Display.setTextColor(C_DIM, C_BG);
  char s[48];
  snprintf(s, sizeof(s), "relay speaks past v%d", SHEPHERD_PROTOCOL_VERSION);
  M5.Display.drawString(s, w / 2, h / 2 + 14);
  M5.Display.drawString("reflash this device", w / 2, h / 2 + 30);
  M5.Display.setTextDatum(top_left);
}

}  // namespace

void brunoUiInvalidate() {
  g_lastMood = (BrunoMood)0xFF;
  g_lastPane[0] = 0;
  g_lastText[0] = 0;
  g_lastWorking = g_lastBlocked = g_lastDone = -1;
}

void brunoUiDraw(const BrunoView& v) {
  const bool same = v.mood == g_lastMood
                 && strcmp(v.pane, g_lastPane) == 0
                 && strcmp(v.text, g_lastText) == 0
                 && v.working == g_lastWorking
                 && v.blocked == g_lastBlocked
                 && v.done == g_lastDone;
  if (same) {
    // Nothing about the HERD changed, which is the normal case and used to
    // mean nothing happened at all. It is where he lives: the nod, the blink,
    // the occasional mouthful of grass. Only his patch is repainted.
    if (v.mood == BRUNO_MOOD_STALE || v.mood == BRUNO_MOOD_BAD_VERSION) return;
    const SheepPose before = poseFor(v.mood);
    advanceAnimation(v.mood, millis());
    const SheepPose after = poseFor(v.mood);
    if (before.lift != after.lift || before.headDy != after.headDy
        || before.eyesShut != after.eyesShut || before.earsUp != after.earsUp) {
      drawSheepPatch(v.mood);
    }
    return;
  }

  g_lastMood = v.mood;
  strncpy(g_lastPane, v.pane, sizeof(g_lastPane) - 1);
  g_lastPane[sizeof(g_lastPane) - 1] = 0;
  strncpy(g_lastText, v.text, sizeof(g_lastText) - 1);
  g_lastText[sizeof(g_lastText) - 1] = 0;
  g_lastWorking = v.working;
  g_lastBlocked = v.blocked;
  g_lastDone = v.done;

  if (v.mood == BRUNO_MOOD_STALE) { drawNoSignal(); return; }
  if (v.mood == BRUNO_MOOD_BAD_VERSION) { drawBadVersion(); return; }

  drawScene();
  M5.Display.setTextDatum(top_left);
  M5.Display.setTextSize(1);

  drawBubble(v);
  drawSheepPatch(v.mood);
  drawStrip(v);
}

void brunoUiTick() {
  // Called on the heartbeat. Drifts the clouds and redraws only their band,
  // which is what proves the screen is alive when the herd is quiet - the job
  // the bring-up build's "alive N" counter used to do, now done by something
  // that belongs in the picture.
  if (g_lastMood == BRUNO_MOOD_STALE || g_lastMood == BRUNO_MOOD_BAD_VERSION) {
    return;   // an error screen should not have weather
  }
  g_drift = (g_drift + 3) % 100000;
  drawClouds();
}
