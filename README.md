# Shepherd

A pocket herd radio for [Herdr](https://herdr.dev). An M5Stack **Cardputer ADV** shows
what every Claude Code agent on your machine is doing, and lets you approve or deny a
blocked one from the sofa — over Bluetooth LE, without touching the laptop.

Five agents run concurrently here across five Herdr workspaces. When one stops and waits
for a human, nothing physical notices. It can sit blocked for minutes while your
attention is elsewhere, and finding out means alt-tabbing through workspaces. This is the
thing that watches them.

```
 SHEPHERD                 1 blk  6      SHEPHERD  LOCKED         0 blk  5
 ─────────────────────────────────      ─────────────────────────────────
 signtest              BLOCKED         > elasmigr                    done
                                         agenstac                    idle
 Do you want to create signed.txt?      newprcl                   working
                                         tempcuad                    idle
                                         herdremo                 working
 ─────────────────────────────────      ─────────────────────────────────
 [y]ok [n]no [>]next [del]list          [;/.] move  [enter] recap
```

## What it does

- **Shows the herd.** Every agent Herdr knows about, with its status, ordered so whoever
  is waiting on you comes first.
- **Puts a blocked agent's question on screen** the moment it arrives, ahead of whatever
  you were looking at.
- **Approves or denies from the device.** One keypress sends a signed action; the relay
  re-reads the pane, confirms the prompt is still the one you were shown, and answers it.
- **Reads back what an agent last said**, fetched on demand and scrollable, so you can
  tell "finished and fine" from "finished and you should look".
- **Refuses to lie.** If it cannot reach Herdr, it says so. If it has not heard from the
  relay in 30 seconds it says `NO SIGNAL` rather than leaving a calm-looking herd on
  screen. If a prompt was too long to show in full, it will not offer to approve it.

## How it fits together

```
 Herdr  ──CLI+named pipe──►  relay (python)  ──BLE / Nordic UART──►  Cardputer ADV
   ▲                                                                       │
   └────────────── send_keys, only for a prompt still on screen ◄──────────┘
```

**The relay** (`plugin/`) is a Herdr plugin. It polls `herdr agent list` for membership,
subscribes to Herdr's named pipe for status transitions, and pushes a JSON snapshot to
the device on change or every 10 seconds. It is the only component that can talk to
Herdr.

**The firmware** (`firmware/`) is a vendored fork of
[`y88huang/claude-desktop-buddy-cardputer`](https://github.com/y88huang/claude-desktop-buddy-cardputer)
(MIT, Anthropic PBC). Upstream already targeted the ADV specifically and already spoke
Nordic UART, which retired the two biggest risks in the plan. See
[`firmware/VENDOR.md`](firmware/VENDOR.md) for the pin and the diff boundary. Shepherd
owns the screen when it has something to say and stands aside otherwise, so a device that
loses the relay degrades to the thing it was rather than to a blank panel.

## Requirements

| | |
|---|---|
| Hardware | M5Stack **Cardputer ADV** (ESP32-S3FN8, 8MB flash, no PSRAM, BLE 5 only) |
| Host | Windows — the relay uses Herdr's named-pipe IPC and `bleak`'s WinRT backend |
| Herdr | 0.8.0+ (developed against 0.8.2, socket protocol 20) |
| Python | 3.12+, with `bleak` |
| Firmware | PlatformIO, plus a host `g++` for the off-device tests |

## Getting it running

### 1. Pair the device

Flash once, then pair from **Windows Settings → Bluetooth**, not from code. `bleak` on
Windows cannot create a bond — it fails at every protection level and then returns
`ERROR_CANCELLED` on encrypted operations — but it reuses a bond Windows already holds.
This is a one-time manual step and there is no way around it.

### 2. Generate the shared secret, in this order

The relay owns the secret; the firmware is built with it. One command generates it on
first run and prints the exact line the build needs:

```sh
cd plugin && py -3 start.py --secret-flag
# -DSHEPHERD_SECRET=\"<64 hex chars>\"
```

Paste that into `firmware/secret.ini` (gitignored; see `secret.ini.example`):

```ini
[env:cardputer-adv]
build_flags = ${common.cardputer_flags} -DSHEPHERD_SECRET=\"<64 hex chars>\"
```

Use the command rather than writing the flag yourself. Two things about it are easy to
get wrong and both fail the same way — a device that pairs, connects, draws the herd
perfectly, and has every single action refused as a bad signature:

- **The path.** The relay reads its secret from `HERDR_PLUGIN_CONFIG_DIR` when Herdr
  injected one and from `~/.shepherd` when it did not. Generating a key in the wrong
  place gives you two secrets. `--secret-flag` resolves the path exactly the way the
  relay does.
- **The escaping.** PlatformIO eats plain quotes out of an ini, and the define then
  reaches the compiler as a bare numeric token rather than a string. `\"` is required.

### 3. Flash

```sh
cd firmware
pio run -e cardputer-adv -t upload --upload-port COM5
```

Flashing puts the board into download mode on its own; only coming *back* out needs the
physical reset button.

### 4. Install the plugin

```sh
herdr plugin link C:\path\to\herdr-remote\plugin
```

The relay then starts with each Herdr session and holds the BLE link for as long as the
session lasts. Two things make that safe to leave on:

- **One relay at a time.** It takes an OS file lock, so a copy left running by hand does
  not fight the session-started one for the single Cardputer. The loser exits 0 and says
  why. This matters more than it sounds: a second relay does not fail loudly, it sits in
  the reconnect backoff insisting the device is not advertising — because the first one
  is holding the link — and that message points at the radio, the pairing, the firmware,
  everywhere except the other copy of itself.
- **It exits with the session.** Herdr does *not* kill plugin startup processes when a
  session stops, verified by stopping one and finding the relay still running afterwards.
  So the relay watches `HERDR_SOCKET_PATH` and stops itself. An incoming relay waits up
  to 20s for a departing one to let go, because the new session's hook fires before the
  old relay's watchdog has noticed.

To run one by hand instead — for debugging — stop the Herdr-started one first, or the
lock will (correctly) refuse you.

## Using it

| Screen | Keys |
|---|---|
| Queue (something is blocked) | `y` approve · `n` deny · `>` next · `del` herd list |
| Herd list | `;` `.` move · `enter` recap · `del` back to queue |
| Recap | `;` `.` scroll · `,` `/` next agent · `del` back |
| Anywhere | **`Fn`+`Del`** lock / unlock |

The device is **locked at boot** and re-locks after 30 seconds without a key. `Fn` is the
modifier because the keyboard scanner suppresses every ordinary key event while it is
held, which makes `Fn`+key the one input shape a single point of pressure cannot produce
— a key *sequence* would be easier and worse, because cloth has all day and will
eventually type any sequence.

The screen stays on when locked. This is a glance device; reading the herd without
touching it is the whole point.

## Security model

The threat is not a determined attacker with a radio. It is that this thing can press
`Enter` on a permission prompt, so the interesting question is whether it can ever press
it on the wrong one.

- **The prompt you approve is the prompt you saw.** Every approve re-reads the pane at
  send time and compares a fingerprint of the parsed question and options against what
  was actually rendered on the device. An agent that moved on between the frame and your
  thumb gets a refusal, not an approval.
- **A closed action set.** `approve`, `deny`, `focus`, `detail` — a literal dict. There is
  no path that builds a Herdr command from device-supplied text, and no raw `pane.run`.
- **A pane allowlist** rebuilt from every frame, so an action can only target a pane that
  was actually on screen.
- **Approve is refused for a truncated prompt.** Deny always works. The device can always
  say no; it may only say yes to something it showed you in full.
- **Everything is signed.** HMAC-SHA256 over `ts|pane|action|decision`, truncated to 64
  bits for a link that sometimes negotiates a 20-byte payload. The bond proves the peer is
  a device Windows once paired with; it does not prove it is *this* Cardputer, because
  Just Works gives no MITM protection and `bleak` cannot run a passkey ceremony.
- **Signatures bind to a frame we actually sent**, which is the only replay protection
  `focus` has, since it carries no decision id. Decision ids are one-shot.
- **Verification happens before anything is read.** An unauthenticated peer should not be
  able to make the relay do work, including the work of reading a pane.
- **Every attempt is audited**, refusals included, to `HERDR_PLUGIN_STATE_DIR` — outside
  any repo, because the log holds full prompt text.

What this does not defend against: anyone who can read the firmware image or the relay's
config file. Both hold the secret in the clear. See [`TODOS.md`](TODOS.md) for the
bond-hardening and key-rotation work that is still open.

## Tests

```sh
py -3 -m pytest tests/          # 178 — relay, protocol, gate, parser
cd firmware && pio test -e native   # 41 — device-side, no board required
```

The native suite matters more than it looks. The device's parsing rules mirror
`plugin/shepherd/frame.py`, and a board on a desk is the worst place to discover they
have drifted: it needs a flash, a physical reset, and gives no assertion output.

A recurring lesson is recorded through the commit history and worth stating once here:
**live validation caught what fixtures structurally could not.** Two prompt-parser bugs,
a 25× MTU throughput loss, a crash that escaped to the event loop, an orphaned relay
process, a logging failure that would have blocked a *deny*, a receive buffer that
silently truncated, and an answer extractor that passed every fixture and then returned a
file listing when pointed at a real pane. Fixtures encode what you already thought of.

## Layout

```
plugin/shepherd/     the relay
  herdr.py           the seam: how Herdr is reached (CLI today, pipe later)
  events.py          named-pipe subscriptions
  models.py          Herdr's vocabulary, verbatim
  frame.py           what goes on the wire
  transport.py       BLE, chunking, reconnect
  prompt.py          reading a Claude Code permission prompt
  recap.py           pulling an agent's last answer out of its terminal
  actions.py         the gate: verify, re-verify, dispatch, audit
  auth.py            the shared secret and the canonical message
  runner.py          the composition — where the design decisions become behaviour
firmware/src/
  shepherd_frame.h   device-side protocol, free of Arduino so it tests natively
  shepherd_lock.h    the key lock, same reason
  shepherd_ui.cpp    the three screens
  ble_bridge.cpp     Nordic UART, pairing policy
docs/designs/        the design doc, including what turned out to be wrong
TODOS.md             what is deliberately not done yet
```

## Status

The core loop works end to end and has been exercised against real agents: an agent
blocks, the device shows the question, a keypress from across the room answers it, and
the file the agent wanted to write appears.

**It does not yet get your attention.** `shepherdUiNeedsAttention()` exists, is correct,
and is called by nothing — the board's speaker and its WS2812B are still driven only by
upstream's own pet state, which Shepherd does not feed. So today the device shows you a
blocked agent; it does not tell you about one. Until that is wired, this is a glance
device rather than a notifier, which is half of what it is for.

The rest of what is deliberately not done is in [`TODOS.md`](TODOS.md).
