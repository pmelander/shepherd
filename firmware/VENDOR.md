# Vendored upstream

`firmware/` is a vendored fork, not a submodule. The tree is committed pristine first so that
every Shepherd change shows up as a reviewable diff against untouched upstream.

| | |
|---|---|
| Upstream | https://github.com/y88huang/claude-desktop-buddy-cardputer |
| Pinned commit | `257d3d84435c11a98e9dc376257acffda2a633cb` |
| Commit date | 2026-04-21 |
| Vendored on | 2026-09-05 |
| License | MIT, Copyright 2026 Anthropic, PBC (see `LICENSE`) |
| Ancestry | fork of `anthropics/claude-desktop-buddy`, ported to Cardputer ADV |

## Why this fork

It is the only firmware that already targets the **Cardputer ADV specifically** — there is a
`cardputer-adv` PlatformIO environment, and the board's peripherals (ST7789 240x135, BMI270,
56-key matrix, WS2812B on GPIO 21) are handled in `src/hal.cpp`. It also already implements
Nordic UART Service BLE with on-device approve/deny, which is the mechanism Shepherd needs.

Choosing it over `walcew/herdr-assist`'s Cardputer port retired the two largest risks in the
plan: ADV bring-up, and writing a BLE stack from scratch.

## What upstream gives us for free

- **Bluetooth is Bluedroid** (`<BLEDevice.h>`), not NimBLE. The NimBLE 1.4.x-to-2.x version
  trap the design doc worried about does not apply here.
- **`bleWrite()` already chunks to `mtu - 3`**, capped at 180, using the live negotiated MTU
  from `onMtuChanged`. That is exactly the chunking rule Shepherd specified.
- **LE Secure Connections bonding with encrypted-only characteristics** — TX, its CCCD, and RX
  all carry `ESP_GATT_PERM_*_ENCRYPTED`.
- **`bleClearBonds()`** already enumerates and removes stored LTKs from NVS.
- **A 2048-byte RX ring buffer**, comfortably larger than a Shepherd snapshot frame.
- `scripts/merge_bin.py` and a `no_ota.csv` partition scheme.

## Known upstream quirks, carried as-is

- **`-DBOARD_HAS_PSRAM` was set on the `cardputer-adv` env — removed, with evidence.** esptool
  against the real board reports `Chip is ESP32-S3 (QFN56) (revision v0.2)` and
  `Features: WiFi, BLE, Embedded Flash 8MB (XMC)` — no PSRAM. The flag is wrong for ADV
  hardware. It is **not** fatal: the board boots and advertises with it set, so it was not the
  cause of the first-flash silence (that was needing a power cycle out of download mode). It is
  removed because it is untrue, not because it broke anything.
- Upstream's counterpart is the **Claude desktop app's BLE API**, which speaks a semantic
  approve/deny. Shepherd's counterpart is a relay injecting keystrokes into a Herdr pane, so
  the data model diverges — upstream's protocol is aggregate-only
  (`total`/`running`/`waiting` plus a single `prompt` object) and cannot express per-agent state.
- `characters/` and `src/buddies/` are ~2MB of pixel-art the queue-first UI does not use. Kept
  in the pristine drop so future upstream merges stay clean; strip later if it ever matters.

## Shepherd's changes

Each is a separate commit after the pristine drop, so `git log firmware/` reads as a change
list rather than a wall.

1. **BLE security mode made a build flag, defaulting to Just Works.** Upstream is DisplayOnly
   with MITM required (`ESP_IO_CAP_OUT`, `ESP_LE_AUTH_REQ_SC_MITM_BOND`,
   `ESP_BLE_SEC_ENCRYPT_MITM`) and renders a 6-digit passkey. That cannot pair with `bleak`,
   whose WinRT backend hardcodes `DevicePairingKinds.CONFIRM_ONLY` and accepts unconditionally
   (`bleak/backends/winrt/client.py:523-524`). The passkey path is preserved behind
   `SHEPHERD_BLE_PASSKEY` rather than deleted, so moving the central to .NET/WinRT later is
   a flag flip instead of a re-implementation.
2. **Advertised name.** Upstream advertises `Claude-XXXX`, which risks the Claude desktop app
   claiming the device. Shepherd advertises under its own name.
3. **Deleted `.github/workflows/release.yml`.** Upstream's release workflow, carried in
   pristine. It worked in upstream's repo because that repo's root is what is now this
   `firmware/` subdirectory — but GitHub Actions only reads `.github/workflows/` at the
   *repository* root, so vendored here it was inert: it had never run, and could not have
   (`git tag` was empty, so its `push: tags: v*` trigger had never fired). Stripped rather
   than kept pristine because a workflow file that looks like it publishes releases and
   silently cannot is worse than no file — a stranger would reasonably trust it. Replaced by
   `/.github/workflows/release.yml` at the repo root, which does the same job with
   `working-directory: firmware` and firmware-relative paths, plus
   `/.github/workflows/ci.yml`, which runs both test suites on every push. Found by
   `/plan-eng-review` on 2026-09-08.

## Bruno's board: M5Stack Core Basic v2.7, as measured

Probed on arrival, 2026-09-22, before a line of Bruno firmware was written.
The design said to record what the board reports rather than what the product
page claims, because this project has already had `-DBOARD_HAS_PSRAM` taken
from a spec sheet and falsified by esptool on the Cardputer. It happened
again here.

| | |
|---|---|
| Chip | ESP32-D0WDQ6-V3, revision v3.1 |
| Features | WiFi, BT, Dual Core, 240MHz, VRef calibration in efuse |
| Crystal | 40MHz |
| Flash | **16MB**, device `4018`, `mode:DIO`, `clock div:2` (40MHz) |
| PSRAM | **NONE** |
| MAC | f4:2d:c9:d0:80:1c |
| USB bridge | WCH **CH9102**, `1A86:55D4`, enumerates as its own COM port |

### `Serial` reaches USB, and needs no flag

This was bench probe #1 and the one with no fallback: Bruno has no BLE, so a
board whose `Serial` does not reach the host has no way to say so. It passes,
and unlike the Cardputer it passes for free.

The Cardputer ADV is an ESP32-S3 with native USB, whose board definition sets
`ARDUINO_USB_MODE=1` without `ARDUINO_USB_CDC_ON_BOOT`, so `Serial` landed on
a UART0 that is not wired to the connector while ESP-IDF logs still reached
the host - an asymmetry that cost three wrong hypotheses in one session. The
Core Basic is a classic ESP32 behind an EXTERNAL bridge on UART0, which is
the same UART the ROM bootloader uses. So if esptool can talk to it, `Serial`
can too, and esptool can.

Confirmed directly by holding the port open and pulsing EN via RTS, which
captures the whole boot. That capture is itself a difference worth knowing:
the bridge stays enumerated across a chip reset, so boot output can be caught
in one go. On the S3's native USB the device re-enumerates and a capture
script has to reopen in a loop.

```
ets Jul 29 2019 12:21:46
rst:0x1 (POWERON_RESET),boot:0x17 (SPI_FAST_FLASH_BOOT)
mode:DIO, clock div:2
E (56) psram: PSRAM ID read error: 0xffffffff
[W][esp32-hal-psram.c:30] psramInit(): PSRAM init failed!
M5Stack initializing...
OK
```

`[W][esp32-hal-psram.c:30]` is Arduino-HAL and `M5Stack initializing...` is
sketch level, so Arduino-level output arrives without any CDC flag.

### The board definition to use is `m5stack-grey`

Not the obvious one, and picking by name would have been wrong twice over.
Measured against what the board actually reports:

| definition | flash | mode | f_flash | PSRAM flag | |
|---|---|---|---|---|---|
| `m5stack-core-esp32` | 4MB | qio | - | no | wrong size AND mode |
| `m5stack-core-esp32-16M` | 16MB | qio | 80MHz | no | wrong mode and clock |
| `m5stack-fire` | 16MB | dio | 40MHz | **yes** | PSRAM flag is false here |
| **`m5stack-grey`** | **16MB** | **dio** | **40MHz** | no | **matches** |

One caveat carried with it: `m5stack-grey` declares
`maximum_ram_size: 532480`, which is more DRAM than an ESP32 without PSRAM
has. PlatformIO uses that only for the percentage in its build summary, so
Bruno's reported RAM usage will read LOWER than it really is. The Cardputer's
board declares 327680 and its percentages are honest. Do not compare the two
numbers directly, and do not trust a comfortable-looking RAM percentage on
this target.

### The rest, answered by Bruno's own probe build

`src/bruno_main.cpp` reports these at boot, so they are re-checkable rather
than a one-off note:

```
[bruno] chip      : ESP32-D0WDQ6-V3 rev 3, 2 core(s) @ 240 MHz
[bruno] flash     : 16777216 bytes, 40 MHz
[bruno] psram     : 0 bytes (none, as measured)
[bruno] heap free : 316068 bytes
[bruno] display   : 320x240
[bruno] imu       : M5Unified reports NONE
[bruno] i2c scan  : 0x75
[bruno] speaker   : tone sent (enabled)
```

- **No IMU.** This was an open question in the Bruno design and the answer is
  no. M5Unified reports none, and the only device answering on the internal
  I2C bus is `0x75` - the IP5306 power-management IC. An MPU6886 would sit at
  0x68 or 0x69 and nothing is there. Anything in the design that assumed a
  shake or face-down gesture needs another input.
- **Display is 320x240**, as the design assumed. Worth having confirmed rather
  than inherited: `shepherd_ui.cpp` is already resolution-agnostic, so its
  staleness and version screens render here unchanged.
- **316,068 bytes of heap free**, which puts a real number on the caveat
  above: `m5stack-grey` claims 532,480 bytes of RAM, the chip has roughly
  320KB, and PlatformIO's percentage is computed against the claim. Bruno's
  build reports 7.6% used; against what is actually there it is nearer 11%.
- **Speaker is enabled and accepts a tone.** Whether it is AUDIBLE is not
  something a probe can answer - that needs an ear in the room.
- **Buttons A/B/C report over serial** when pressed, but no press has been
  confirmed yet. The handler is in `loop()`; pressing them is a human step.
- **SD slot present and empty** - the factory firmware's mount failed with
  `sdCommand(): no token received` / `f_mount failed: (3)`. Expected with no
  card in; it does confirm the slot is wired. Bruno does not use it.

### The ingest path works on this board, with real frames

Not a unit test: real lines taken from the relay's own `frames.ndjson` and
written down the wire.

- A **951-byte, 10-agent snapshot** reassembled by `line_buf.h` and parsed by
  `shepherd_frame.h`, with every alias and status correct.
- A real **`said` frame** parsed by `bruno_frame.h`, em dash intact - so UTF-8
  survives relay, file, serial and device.
- A **`key` frame ignored**, which is the device-side half of the guard that
  keeps the shared secret off the stream. It works independently of the
  publisher refusing to write one.
- A **v99 frame refused** rather than guessed at.
