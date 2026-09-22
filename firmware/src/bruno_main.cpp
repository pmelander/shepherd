// Bruno's own entry point.
//
// Deliberately NOT a branch inside Shepherd's main.cpp. That file plus data.h
// hold all sixteen unguarded `ble*` calls and twenty board conditionals, and
// the way to stop those being Bruno's problem is to not compile them - which
// also means Shepherd's main cannot regress because of a change made for
// Bruno. `build_src_filter` in platformio.ini is the whole mechanism.
//
// The probes at the top are what is left of the bring-up build, and they stay.
// The design's step 5 was "when the board lands, probe before you code" -
// Serial reach, chip and flash, then buttons, speaker, IMU - and every answer
// is recorded in VENDOR.md. They report at every boot rather than once,
// because a fact you can re-check beats a fact you wrote down.
//
// What HAS gone is the probe's screen: the banner, the counters, the big
// yellow letter on a keypress, the "alive N" tick. Each existed to make
// something observable before the real UI could show it, each filled a
// rectangle with black to do so, and each would now be a hole punched in a
// field. Liveness moved into the picture instead - the clouds drift.

#include <Arduino.h>
#include <M5Unified.h>
#include <Wire.h>

#include "baa_wav.h"     // generated at build time from assets/bruno/baa.wav
#include "bruno_frame.h"
#include "bruno_ui.h"
#include "bruno_view.h"
#include "line_buf.h"
#include "shepherd_frame.h"

// The queue of finishes waiting their turn at the bubble, and the decision
// about what is on screen right now. The policy is in bruno_view.h where the
// native suite can reach it; this file only feeds it and draws the answer.
static BrunoQueue g_queue;
static BrunoView g_view;

// When a frame last arrived. A device showing a calm herd it cannot see is
// lying, so past this it says NO SIGNAL instead. Matches Shepherd's own
// threshold.
#define BRUNO_STALE_MS 30000
static uint32_t g_lastFrameMs = 0;
static bool g_everReceived = false;
static bool g_badVersion = false;

// One line at a time off the serial port, the same buffer Shepherd uses. Sized
// by SHEPHERD_LINE_MAX for the same reason: the largest frame the host can
// build is ~6.4KB and a line that does not fit is DROPPED rather than
// truncated, because handing a parser JSON you already know is incomplete can
// only fail quietly.
static LineBuf<SHEPHERD_LINE_MAX> g_line;

// Both frame types live in .bss, not on the stack. ShepherdFrame is 6604
// bytes and a local one blew the 8192-byte loopTask stack on the Cardputer -
// a crash that presented as a reboot loop with nothing pointing at it.
static ShepherdFrame g_frame;
static BrunoSaid g_said;

static uint32_t g_frames = 0;
static uint32_t g_saids = 0;
static uint32_t g_rejected = 0;

// ---------------------------------------------------------------- probes

static void probeChip() {
  Serial.printf("[bruno] chip      : %s rev %d, %d core(s) @ %d MHz\n",
                ESP.getChipModel(), ESP.getChipRevision(), ESP.getChipCores(),
                (int)getCpuFrequencyMhz());
  Serial.printf("[bruno] flash     : %u bytes, %u MHz\n",
                (unsigned)ESP.getFlashChipSize(),
                (unsigned)(ESP.getFlashChipSpeed() / 1000000));
  // Expected to be zero. The board's own factory boot reported
  // "PSRAM ID read error", and -DBOARD_HAS_PSRAM is deliberately not set.
  // Printed anyway, because a silent assumption is how the Cardputer ended up
  // carrying that flag wrongly for months.
  Serial.printf("[bruno] psram     : %u bytes %s\n",
                (unsigned)ESP.getPsramSize(),
                ESP.getPsramSize() ? "(UNEXPECTED - see VENDOR.md)" : "(none, as measured)");
  Serial.printf("[bruno] heap free : %u bytes\n", (unsigned)ESP.getFreeHeap());
  Serial.printf("[bruno] display   : %dx%d\n", M5.Display.width(),
                M5.Display.height());
}

// Does a v2.7 Core Basic carry an IMU? Still an open question in the design.
// Asked two ways, because they can disagree: M5Unified only reports an IMU it
// has a driver for, while a raw scan reports anything that acknowledges.
static void probeImu() {
  const auto type = M5.Imu.getType();
  Serial.printf("[bruno] imu       : M5Unified reports %s\n",
                type == m5::imu_none ? "NONE"
                : type == m5::imu_mpu6050 ? "MPU6050"
                : type == m5::imu_mpu6886 ? "MPU6886"
                : type == m5::imu_sh200q  ? "SH200Q"
                : type == m5::imu_bmi270  ? "BMI270"
                                          : "something it has a driver for");
}

static void probeI2C() {
  // The internal bus. Whatever answers here is physically present, driver or
  // no driver.
  Serial.printf("[bruno] i2c scan  :");
  int found = 0;
  for (uint8_t addr = 0x08; addr < 0x78; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) {
      Serial.printf(" 0x%02X", addr);
      found++;
    }
  }
  Serial.printf("%s\n", found ? "" : " (nothing answered)");
}

// WHICH board M5Unified thinks this is. Everything below depends on it: the
// button pins, the speaker pin, and the display driver all come from this
// answer. It was missing from the first probe, which is why "buttons and
// speaker do nothing" had no obvious first suspect.
static void probeBoard() {
  const char* name = "unknown";
  switch (M5.getBoard()) {
    case m5::board_t::board_M5Stack:      name = "M5Stack (Core Basic/Grey)"; break;
    case m5::board_t::board_M5StackCore2: name = "Core2"; break;
    case m5::board_t::board_M5StackCoreS3: name = "CoreS3"; break;
    case m5::board_t::board_M5StickC:     name = "StickC"; break;
    case m5::board_t::board_M5StickCPlus: name = "StickC Plus"; break;
    case m5::board_t::board_M5Cardputer:  name = "Cardputer"; break;
    default: break;
  }
  Serial.printf("[bruno] board     : %s (enum %d)\n", name, (int)M5.getBoard());
}

static void probeSpeaker() {
  // Reports, and no longer makes a noise. This used to play two loud tones at
  // boot, which was the right thing when the question was whether the speaker
  // worked at all - a probe nobody can hear proves nothing. It is confirmed
  // working now, so the tones are just a device that announces itself every
  // time the power blinks. The state is still printed, because that is the
  // half that costs nothing.
  Serial.printf("[bruno] speaker   : enabled=%d volume=%d (silent at boot "
                "by choice)\n",
                (int)M5.Speaker.isEnabled(), (int)M5.Speaker.getVolume());
}

// Read the button GPIOs directly, beside what M5Unified reports. If the raw
// pin moves and M5.BtnX does not, the fault is in board detection or pin
// mapping. If neither moves, the pins themselves are wrong for this hardware.
// One probe distinguishes two very different problems.
static constexpr int BTN_A_PIN = 39;
static constexpr int BTN_B_PIN = 38;
static constexpr int BTN_C_PIN = 37;

static void probeButtonPins() {
  pinMode(BTN_A_PIN, INPUT);
  pinMode(BTN_B_PIN, INPUT);
  pinMode(BTN_C_PIN, INPUT);
  Serial.printf("[bruno] btn pins  : raw A(39)=%d B(38)=%d C(37)=%d "
                "(1 = released, these are active-low)\n",
                digitalRead(BTN_A_PIN), digitalRead(BTN_B_PIN),
                digitalRead(BTN_C_PIN));
}

// ---------------------------------------------------------------- ingest

static void handleLine(const char* line) {
  // Bruno's own frame type first. It is a different `t` precisely so the
  // shared parser does not know it - which is what lets Shepherd ignore these
  // without an edit or a reflash.
  switch (brunoParseSaid(line, &g_said)) {
    case BRUNO_SAID:
      g_saids++;
      Serial.printf("[bruno] said      : %s -> \"%s\"%s\n", g_said.pane,
                    g_said.body, g_said.truncated ? " (truncated)" : "");
      // Queue the words, and bleat NOW. The sound is the notification and it
      // is cheap; only the bubble is contended. So a finish that lands behind
      // a blocked agent is delayed on screen but never silent.
      g_queue.push(g_said.pane, g_said.body);
      M5.Speaker.setVolume(160);
      M5.Speaker.playWav(baa_wav, sizeof(baa_wav));
      return;
    case BRUNO_BAD_VERSION:
      g_rejected++;
      Serial.printf("[bruno] said      : REFUSED, protocol newer than v%d\n",
                    SHEPHERD_PROTOCOL_VERSION);
      return;
    case BRUNO_MALFORMED:
      g_rejected++;
      Serial.printf("[bruno] said      : malformed\n");
      return;
    case BRUNO_NOT_MINE:
      break;   // fall through to the shared parser
  }

  switch (shepherdParse(line, &g_frame)) {
    case SHEPHERD_OK:
      g_frames++;
      // Freshness is recorded here and nowhere else, so NO SIGNAL means "the
      // relay stopped talking" rather than "nothing interesting happened".
      g_lastFrameMs = millis();
      g_everReceived = true;
      g_badVersion = false;
      Serial.printf("[bruno] snap      : %d agent(s), ts=%s\n", g_frame.count,
                    g_frame.ts);
      for (int i = 0; i < g_frame.count; i++) {
        Serial.printf("[bruno]             %-8s %-9s %s\n",
                      g_frame.agents[i].pane, g_frame.agents[i].status,
                      g_frame.agents[i].alias);
      }
      return;
    case SHEPHERD_BAD_VERSION:
      g_rejected++;
      g_badVersion = true;
      Serial.printf("[bruno] snap      : REFUSED, wrong protocol version\n");
      return;
    case SHEPHERD_MALFORMED:
      g_rejected++;
      Serial.printf("[bruno] snap      : malformed\n");
      return;
    default:
      // NOT_MINE covers `deet` and `key`, which Bruno has no business
      // rendering, and anything else on the wire.
      Serial.printf("[bruno] ignored   : %.60s\n", line);
      return;
  }
}

// ---------------------------------------------------------------- lifecycle

void setup() {
  auto cfg = M5.config();
  M5.begin(cfg);

  Serial.begin(115200);
  delay(200);   // let the bridge settle before the first line

  Serial.printf("\n[bruno] ===== probe build, %s %s =====\n", __DATE__, __TIME__);
  probeBoard();
  probeChip();
  probeImu();
  probeI2C();
  probeButtonPins();
  probeSpeaker();
  Serial.printf("[bruno] buffers   : line=%d bytes, worst frame=%d\n",
                SHEPHERD_LINE_MAX, SHEPHERD_WORST_FRAME);
  Serial.printf("[bruno] ready. Pipe frames.ndjson at me.\n");
  Serial.printf("[bruno] press A / B / C to report the buttons.\n");

}

// A device that only speaks when something happens cannot be told apart from
// a dead one. Shepherd carries the same heartbeat for the same reason, and it
// is what caught a silent 25x throughput bug there. This one carries the RAW
// pin states beside M5Unified's view, so a press is visible even if the
// library's mapping is wrong.
static void heartbeat() {
  static uint32_t next = 0;
  const uint32_t now = millis();
  if ((int32_t)(now - next) < 0) return;
  next = now + 2000;

  Serial.printf("[bruno] HB %lus  m5=%c%c%c  raw=%d%d%d  bright=%d  "
                "snap=%lu said=%lu dropped=%lu  heap=%u\n",
                (unsigned long)(now / 1000),
                M5.BtnA.isPressed() ? 'A' : '-',
                M5.BtnB.isPressed() ? 'B' : '-',
                M5.BtnC.isPressed() ? 'C' : '-',
                digitalRead(BTN_A_PIN), digitalRead(BTN_B_PIN),
                digitalRead(BTN_C_PIN),
                M5.Display.getBrightness(),
                (unsigned long)g_frames, (unsigned long)g_saids,
                (unsigned long)g_line.dropped, (unsigned)ESP.getFreeHeap());

  // Liveness, and it has moved into the picture. The bring-up build printed
  // an "alive N" counter in a corner, which meant filling a rectangle with
  // black - fine on a black screen, a hole punched in the sky on this one.
  // Drifting clouds do the same job and belong where they are.
  brunoUiTick();
}

// Raw, active-low, with a tiny debounce. Independent of M5Unified entirely,
// so a press shows up here whatever the library thinks the board is.
static bool rawPressed(int pin, bool* was) {
  const bool down = digitalRead(pin) == LOW;
  const bool edge = down && !*was;
  *was = down;
  return edge;
}

void loop() {
  M5.update();
  heartbeat();

  // The remaining probe from the design's step 5. Reported rather than acted
  // on: Bruno is a tamagotchi, and what the buttons should DO is a decision
  // that belongs with the real UI.
  if (M5.BtnA.wasPressed()) Serial.printf("[bruno] button    : A (M5Unified)\n");
  if (M5.BtnB.wasPressed()) Serial.printf("[bruno] button    : B (M5Unified)\n");
  if (M5.BtnC.wasPressed()) Serial.printf("[bruno] button    : C (M5Unified)\n");

  static bool wasA = false, wasB = false, wasC = false;
  const char* raw = rawPressed(BTN_A_PIN, &wasA) ? "A"
                  : rawPressed(BTN_B_PIN, &wasB) ? "B"
                  : rawPressed(BTN_C_PIN, &wasC) ? "C" : nullptr;
  if (raw) {
    Serial.printf("[bruno] button    : %s (RAW GPIO)\n", raw);
    // The big yellow letter is gone. It existed to make a press observable
    // during bring-up, when the only other channel was a serial monitor
    // nobody had open - and it worked, the buttons are confirmed. Drawing it
    // now would mean filling a rectangle with black over a field. The beep
    // and the serial line are enough, and C still fetches a sheep.
    M5.Speaker.setVolume(255);
    if (raw[0] == 'C') {
      // C is the sheep. The whole 64KB budget went on one good bleat rather
      // than three mediocre ones, and this is the first time it has been
      // asked to make a noise on hardware.
      const bool ok = M5.Speaker.playWav(baa_wav, sizeof(baa_wav));
      Serial.printf("[bruno] baa       : playWav(%u bytes) -> %s\n",
                    (unsigned)sizeof(baa_wav), ok ? "accepted" : "REFUSED");
    } else {
      M5.Speaker.tone(1500, 80);
    }
  }

  while (Serial.available()) {
    if (g_line.push((char)Serial.read())) {
      // The `{` check is the caller's policy, not the buffer's, exactly as in
      // data.h. line_buf.h reassembles lines and does not care what is in them.
      if (g_line.buf[0] == '{') handleLine(g_line.buf);
    }
  }

  // Decide and draw at 5Hz. brunoUiDraw is a no-op when nothing it cares
  // about changed, so this is cheap; the rate only bounds how quickly a
  // celebration can give way to the next one.
  static uint32_t nextDraw = 0;
  const uint32_t now = millis();
  if ((int32_t)(now - nextDraw) >= 0) {
    // ~15fps. brunoUiDraw is a no-op when the herd has not moved AND he is
    // mid-pose, so this is not fifteen full repaints a second - it is the
    // rate at which a nod is allowed to look like a nod rather than a jump.
    nextDraw = now + 66;
    const bool fresh = g_everReceived
                    && (uint32_t)(now - g_lastFrameMs) < BRUNO_STALE_MS;
    brunoDecide(g_frame, g_queue, now, fresh, g_badVersion, &g_view);
    brunoUiDraw(g_view);
  }

  delay(5);
}
