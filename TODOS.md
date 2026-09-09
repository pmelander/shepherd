# TODOS

## Firmware

### ~~Refuse further BLE bonds after the first~~ — DONE. Key rotation still open.

**Bonds: done.** `onConfirmPIN` refuses when `bleBondCount() > 0`, so the device
bonds with exactly one central. Verified live: `bonds=1 (pairing closed)` at boot,
and the already-bonded laptop still reconnects — `[ble] connected / auth ok /
mtu=517` — because a returning peer uses its stored LTK and never runs pairing.

Deliberately not an advertising whitelist, which is the obvious Bluedroid answer:
`esp_ble_bond_dev_t` carries the address but not its type, and whitelisting with the
wrong type locks out the host that is *already* paired. Refusing the bond leaves
reconnection untouched, so getting it wrong fails toward "a stranger can bond"
rather than "the owner cannot".

Escape hatch: **settings → reset → forget pairing**, tap-twice confirm, bonds only
— separate from factory reset because re-pairing after Windows drops its half
should not also cost you the pet, the stats and the settings. Without it a
one-sided bond loss would be unrecoverable short of a reflash. The info screen
gains a `paired` line, since a refusal is invisible from the outside.

Not verified: an actual second central being turned away. No second BLE host was
available. The guard is armed and the non-regression is proven; the refusal path
itself has only been read, not run.

**Key rotation: done.** `settings`-free: the plugin action
**Shepherd: rotate the shared secret** (or `start.py --rotate-key`) drops a marker
the running relay picks up within a second. The relay signs a new key with the
*current* one, the device verifies, stores it in NVS and returns a MAC computed
with the *new* key, and only then does the relay write it to disk. No USB, no SD
card, no reflash.

Ordering is the design: interrupt it anywhere before the proof verifies and both
sides still hold the old key. The failure worth engineering against is the two
halves disagreeing, because that presents as a device that pairs, connects, draws
the herd perfectly and refuses every action.

Verified live end to end: secret `7889f30f...` -> `f1b4e529...`, and a reboot
reports `[key] using rotated secret from NVS`.

Residual, and stated rather than implied: the new key crosses the link. LE Secure
Connections' ECDH defeats a *passive* eavesdropper even in Just Works mode, so the
exposure is an active MITM present at the moment of rotation - who would have had
to MITM the original pairing too. And a factory reset drops back to the build-time
key by design, so a rotated pair needs rotating again afterwards.

Left over: `firmware/secret.ini` still holds the *original* key, so a reflash
reverts the device while the relay keeps the rotated one. `start.py --secret-flag`
prints the current value to paste back. Worth automating if rotation becomes
routine rather than occasional.

### ~~Key lock / screen-off state for pocket carry~~ — DONE

Built as `src/shepherd_lock.h` plus a `HalKey::Unlock` chord. Locked at boot,
re-locked after 30s of no keys (and immediately when the screen sleeps),
unlocked by **Fn+Del**.

Del rather than Enter, which now opens an agent's recap from the herd list: a
modifier away from "show me this" is too close to "lock the device" for a key you
reach for without looking.

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

### Event loop discards the transition it was told about

**What:** `_event_loop()` (plugin/shepherd/runner.py:487) receives
`pane.agent_status_changed` carrying `agent_status`, then throws that value away
and marks the frame dirty so the next `refresh()` rereads current state. Also, a
newly created pane stays unsubscribed until some *existing* subscription happens
to fire and trigger a membership comparison.

**Why:** A state change witnessed and then discarded is worse than one never
seen, because the code looks like it handles the event. Focus the pane on the
laptop between the event arriving and the reread landing and Herdr has already
cleared `done`, so the reread returns `idle` and the transition that fired the
event is gone. On Shepherd that is a missed chirp. On Bruno it is a missed
bleat, which is the whole product.

**Context:** Found by the outside voice during /plan-eng-review on 2026-09-08.
The active decision from 2026-09-05 records why this is a poll/push hybrid:
`pane.agent_detected` does not stream, and the push payload carries only
agent, agent_status, pane_id and workspace_id. So the status IS in the event -
it is being dropped, not missing. Start by having the event write the observed
status into a short-lived per-pane record that `build()` prefers over a reread
while it is newer than the last poll. Mind the naming trap already documented in
tests/test_events.py: `events.subscribe` takes the DOTTED form, the emitted
event's `type` uses UNDERSCORES, and mixing them fails silently.

**Effort:** M
**Priority:** P2
**Depends on:** Nothing. Worth doing before Bruno's bleat depends on transitions.

### Cardputer LittleFS is corrupt; the buddy runs in ASCII mode

**What:** The Cardputer's LittleFS partition fails to mount -
`Corrupted dir pair at {0x1,0x0}` - so `firmware/characters/bufo/*.gif` never
load and the buddy falls back to ASCII rendering. Everything else on the device
works normally.

**Why:** This is currently knowledge that exists only in session transcripts,
and it is load-bearing: the Bruno design cites it as the reason `baa.wav` goes
into flash as a const array rather than onto the filesystem. Unrecorded, that
argument reads as an unsupported assertion and someone will reasonably reverse
it. It also means `xfer.h` - the character transfer feature - has no working
end-to-end path on the only device that has it.

**Context:** Noticed while investigating why the GIF characters stopped loading;
not diagnosed further because the ASCII buddy is usable and the herd screens do
not touch the filesystem. Two things to try: `LittleFS.format()` from a one-off
sketch, and whether `board_build.partitions = no_ota.csv` actually leaves a
correctly sized LittleFS region - a mismatched partition table produces exactly
this error. If a format fixes it, record that too.

**Effort:** S
**Priority:** P3
**Depends on:** Nothing. Bruno deliberately avoids the filesystem because of it.
