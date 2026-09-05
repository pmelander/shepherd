# Vendored upstream

`firmware/` is a vendored fork, not a submodule. The tree is committed pristine first so that
every Bellwether change shows up as a reviewable diff against untouched upstream.

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
Nordic UART Service BLE with on-device approve/deny, which is the mechanism Bellwether needs.

Choosing it over `walcew/herdr-assist`'s Cardputer port retired the two largest risks in the
plan: ADV bring-up, and writing a BLE stack from scratch.

## What upstream gives us for free

- **Bluetooth is Bluedroid** (`<BLEDevice.h>`), not NimBLE. The NimBLE 1.4.x-to-2.x version
  trap the design doc worried about does not apply here.
- **`bleWrite()` already chunks to `mtu - 3`**, capped at 180, using the live negotiated MTU
  from `onMtuChanged`. That is exactly the chunking rule Bellwether specified.
- **LE Secure Connections bonding with encrypted-only characteristics** — TX, its CCCD, and RX
  all carry `ESP_GATT_PERM_*_ENCRYPTED`.
- **`bleClearBonds()`** already enumerates and removes stored LTKs from NVS.
- **A 2048-byte RX ring buffer**, comfortably larger than a Bellwether snapshot frame.
- `scripts/merge_bin.py` and a `no_ota.csv` partition scheme.

## Known upstream quirks, carried as-is

- **`-DBOARD_HAS_PSRAM` is set on the `cardputer-adv` env.** The ADV's StampS3A is an
  **ESP32-S3FN8** — 8MB flash, no PSRAM (PSRAM parts carry an R2/R8 suffix). This flag looks
  wrong. Left untouched in the pristine drop; verify on first flash before changing it, since
  the board may boot fine either way and a blind edit would be guessing.
- Upstream's counterpart is the **Claude desktop app's BLE API**, which speaks a semantic
  approve/deny. Bellwether's counterpart is a relay injecting keystrokes into a Herdr pane, so
  the data model diverges — upstream's protocol is aggregate-only
  (`total`/`running`/`waiting` plus a single `prompt` object) and cannot express per-agent state.
- `characters/` and `src/buddies/` are ~2MB of pixel-art the queue-first UI does not use. Kept
  in the pristine drop so future upstream merges stay clean; strip later if it ever matters.

## Bellwether's changes

Each is a separate commit after the pristine drop, so `git log firmware/` reads as a change
list rather than a wall.

1. **BLE security mode made a build flag, defaulting to Just Works.** Upstream is DisplayOnly
   with MITM required (`ESP_IO_CAP_OUT`, `ESP_LE_AUTH_REQ_SC_MITM_BOND`,
   `ESP_BLE_SEC_ENCRYPT_MITM`) and renders a 6-digit passkey. That cannot pair with `bleak`,
   whose WinRT backend hardcodes `DevicePairingKinds.CONFIRM_ONLY` and accepts unconditionally
   (`bleak/backends/winrt/client.py:523-524`). The passkey path is preserved behind
   `BELLWETHER_BLE_PASSKEY` rather than deleted, so moving the central to .NET/WinRT later is
   a flag flip instead of a re-implementation.
2. **Advertised name.** Upstream advertises `Claude-XXXX`, which risks the Claude desktop app
   claiming the device. Bellwether advertises under its own name.
