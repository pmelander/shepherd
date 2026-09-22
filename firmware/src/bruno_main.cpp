// Bruno's own entry point.
//
// Deliberately NOT a branch inside Shepherd's main.cpp. That file plus data.h
// hold all sixteen unguarded `ble*` calls and twenty board conditionals, and
// the way to stop those being Bruno's problem is to not compile them - which
// also means Shepherd's main cannot regress because of a change made for
// Bruno. `build_src_filter` in platformio.ini is the whole mechanism.
//
// THIS FIRST VERSION IS A PROBE, and says so on the screen. The design's step
// 5 is "when the board lands, probe before you code": Serial reach, chip and
// flash, then buttons, speaker, IMU. The first two were answered from the ROM
// bootloader and are recorded in VENDOR.md. The rest need code running on the
// board, so they live here rather than in a throwaway sketch - the probe
// becomes the skeleton instead of being deleted.
//
// It also exercises the real ingest path end to end: line_buf.h reassembling
// bytes off the wire, then shepherd_frame.h and bruno_frame.h parsing them.
// Pipe real frames at it from frames.ndjson and it will say what it saw.

#include <Arduino.h>
#include <M5Unified.h>
#include <Wire.h>

#include "bruno_frame.h"
#include "line_buf.h"
#include "shepherd_frame.h"

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

static void probeSpeaker() {
  // Short and quiet. SFX_VOLUME on Shepherd is 80 of 255 because the full
  // volume was, in the owner's words, a bit loud.
  M5.Speaker.setVolume(80);
  M5.Speaker.tone(880, 120);
  Serial.printf("[bruno] speaker   : tone sent (%s)\n",
                M5.Speaker.isEnabled() ? "enabled" : "NOT ENABLED");
}

// ---------------------------------------------------------------- screen

static void banner(const char* status) {
  M5.Display.fillScreen(TFT_BLACK);
  M5.Display.setTextColor(TFT_WHITE, TFT_BLACK);
  M5.Display.setTextDatum(top_left);
  M5.Display.setTextSize(2);
  M5.Display.drawString("BRUNO", 8, 8);
  M5.Display.setTextSize(1);
  M5.Display.drawString("probe build - not the real UI yet", 8, 34);
  M5.Display.drawString(status, 8, 52);
}

static void showCounts() {
  char line[64];
  snprintf(line, sizeof(line), "snap=%lu  said=%lu  rejected=%lu  dropped=%lu",
           (unsigned long)g_frames, (unsigned long)g_saids,
           (unsigned long)g_rejected, (unsigned long)g_line.dropped);
  M5.Display.fillRect(0, 70, M5.Display.width(), 20, TFT_BLACK);
  M5.Display.setTextColor(TFT_WHITE, TFT_BLACK);
  M5.Display.drawString(line, 8, 70);
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
      M5.Display.fillRect(0, 100, M5.Display.width(), 60, TFT_BLACK);
      M5.Display.setTextColor(TFT_GREEN, TFT_BLACK);
      M5.Display.drawString(g_said.pane, 8, 100);
      M5.Display.setTextColor(TFT_WHITE, TFT_BLACK);
      M5.Display.drawString(g_said.body, 8, 116);
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
  probeChip();
  probeImu();
  probeI2C();
  probeSpeaker();
  Serial.printf("[bruno] buffers   : line=%d bytes, worst frame=%d\n",
                SHEPHERD_LINE_MAX, SHEPHERD_WORST_FRAME);
  Serial.printf("[bruno] ready. Pipe frames.ndjson at me.\n");
  Serial.printf("[bruno] press A / B / C to report the buttons.\n");

  banner("waiting for frames on serial");
  showCounts();
}

void loop() {
  M5.update();

  // The remaining probe from the design's step 5. Reported rather than acted
  // on: Bruno is a tamagotchi, and what the buttons should DO is a decision
  // that belongs with the real UI.
  if (M5.BtnA.wasPressed()) Serial.printf("[bruno] button    : A\n");
  if (M5.BtnB.wasPressed()) Serial.printf("[bruno] button    : B\n");
  if (M5.BtnC.wasPressed()) Serial.printf("[bruno] button    : C\n");

  while (Serial.available()) {
    if (g_line.push((char)Serial.read())) {
      // The `{` check is the caller's policy, not the buffer's, exactly as in
      // data.h. line_buf.h reassembles lines and does not care what is in them.
      if (g_line.buf[0] == '{') {
        handleLine(g_line.buf);
        showCounts();
      }
    }
  }

  delay(5);
}
