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

**Key rotation: still open.** The HMAC secret is still a build-time flag in
plaintext, so rotating it means the BtnG0+BtnRST dance and a reflash. The one real
credential in the system remains unrotatable in practice.

The idea worth trying first is a signed re-key over the link itself: the relay
sends a new secret in a frame signed with the *current* one, the device verifies,
stores it in NVS and acks, and the relay only writes the new secret to its config
once the ack lands. Rotation then requires possession of the current key, which is
exactly the right property, and it needs no SD card and no reflash. The new key
does cross the link — acceptable under LE Secure Connections, whose ECDH defeats a
*passive* eavesdropper even in Just Works mode; the residual risk is an active MITM
present at rotation time, who would have had to MITM the original pairing too.

**Effort:** M
**Priority:** P3

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
