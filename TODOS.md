# TODOS

## Firmware

### Refuse further BLE bonds after the first, and give the HMAC key a rotation path

**What:** After one successful pairing, the peripheral should stop accepting new bonds. And the shared HMAC secret should be changeable without a full USB reflash.

**Why:** Just Works bonding with bonding left open means any central can pair with the device. The app-layer HMAC is what actually blocks a stranger's frames — but it is baked into the firmware at build time in plaintext build flags, and with OTA cut, rotating it means the BtnG0+BtnRST dance and a reflash. The one real credential in the system is unrotatable in practice.

**Context:** `bleak` on Windows forces `NO_INPUT_OUTPUT` / MITM-off pairing — verified in `bleak/backends/winrt/client.py` line 524, which hardcodes `ceremony = DevicePairingKinds.CONFIRM_ONLY`. So pairing itself cannot be hardened and the app-layer MAC is the entire defence. The key is generated at relay first run and flashed at build time; that bootstrap ordering is not yet sequenced anywhere. Damage is already bounded by the send-time re-verification decided in this review (a hostile peer can only approve a prompt genuinely on screen, and only when it was shown untruncated), which is why this is defence in depth rather than an open door. Start with NimBLE's bond-count configuration; for rotation, reading the key from the SD card at boot is the cheap option but trades firmware exposure for physical-media exposure.

**Effort:** M
**Priority:** P3
**Depends on:** BLE pairing-policy probe passing; the firmware fork building

### ~~Key lock / screen-off state for pocket carry~~ — DONE

Built as `src/shepherd_lock.h` plus a `HalKey::Unlock` chord. Locked at boot,
re-locked after 30s of no keys, unlocked by **Fn+Enter**.

Fn is the chord because the keyboard scanner already suppresses every
ordinary key event while Fn is held, which makes Fn+key the one input shape a
single point of pressure cannot produce. A key *sequence* was rejected: cloth
has all day, and will eventually type any sequence.

The screen deliberately stays on. This is a glance device — reading the herd
without touching it is the product — and upstream's 30s idle sleep already
handles the backlight. The lock governs what keys may DO, not what is shown,
and `shepherdUiNeedsAttention()` is blind to it so a locked device still
chirps.

Pinned by 8 native tests, including the millis() wrap: a device left on a
shelf crosses it every ~49.7 days, and a sloppy comparison there would hold
the lock *open*.

Still open from the original note: IMU wake (shake / face-down) is upstream
code that remains unused. Worth revisiting only if the Fn chord turns out to
be annoying in daily use.

## Relay

### Named-pipe HerdrSource implementation

**What:** A second `HerdrSource` implementation that holds Herdr's named pipe open and uses `events.subscribe`, replacing per-tick `herdr` CLI subprocess spawns.

**Why:** Each CLI call spawns a Windows process at 50-100ms. At a 1Hz poll that is roughly 86,000 spawns per working day on a laptop already hosting five Claude Code agents. The pipe also enables sub-second blocked detection if `pane_agent_status_changed` fires.

**Context:** Deliberately deferred behind the `HerdrSource` seam decided in this review, so this is an added file rather than a refactor, with the existing test suite already written against the protocol. Whether it is worth writing depends on the event probe. Note the naming rule verified on this host: `events.subscribe` takes the DOTTED form in `params.subscriptions[].type` (`pane.agent_status_changed`), while `events.wait` and the emitted event's `type` const both use UNDERSCORES (`pane_agent_status_changed`). Mixing them yields a silent false negative. Windows named pipes plus asyncio plus your own JSON framing is the fiddly part.

**Effort:** M
**Priority:** P3
**Depends on:** Event probe result; relay shipping with the CLI-backed source first
