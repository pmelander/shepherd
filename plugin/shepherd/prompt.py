"""Reading a Claude Code permission prompt, and deciding which keys to send.

This is the most safety-critical module in Shepherd. Everything else can fail
by showing the wrong thing; this one can fail by *doing* the wrong thing on a
work repo.

The design here is forced by what the prompt actually looks like, captured
live from Claude Code v2.1.260. Two real shapes, both from the same session:

  Startup trust gate — unnumbered, cursor defaults to the REFUSING option

      ❯ No, exit
        Yes, I trust this folder

      Enter to confirm · Esc to cancel

  Bash tool permission — numbered, cursor defaults to the affirmative

      Do you want to proceed?
      ❯ 1. Yes
        2. Yes, and don't ask again for: git *
        3. Yes, and switch to auto mode · auto mode handles these prompts
        4. No

      Esc to cancel · Tab to amend

Consequences that shape every rule below:

* Neither numbering nor default cursor position is stable across prompt
  kinds, so selecting by index or pressing Enter blind are both unsafe. On
  the trust gate, Enter exits the agent.
* Three of the four Bash options begin with "Yes". Option 2 writes a
  persistent glob permission; option 3 turns off permission prompting for
  that agent entirely. Approving the wrong one silently widens what an agent
  may do, forever, on a work repo.
* `Esc` cancels on every shape seen, so deny needs no parsing at all.
* A third shape turned up during live validation, for file writes:

      Do you want to create hello.txt?
      ❯ 1. Yes
        2. Yes, and switch to accept edits (...) for this session (shift+tab)
        3. No

  Note the "(shift+tab)" inside an option label. An earlier footer pattern
  matched that and truncated the option block, so the parser returned None on
  a real prompt and Shepherd would have seen nothing. Option-shaped lines are
  now checked before chrome patterns.

The approve predicate is therefore deliberately narrow: the option whose
label is *exactly* "Yes" and nothing more. That one rule excludes the glob
grant, the auto-mode switch, and — by design rather than by accident — the
trust gate's "Yes, I trust this folder". A device in a pocket should not be
able to grant a whole workspace; that prompt renders as "open laptop".
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Deny is a single key on every prompt shape observed, verified end to end
# (agent_status went blocked -> done). It needs no parsing, which is why the
# device can always say no even when it cannot safely say yes.
DENY_KEYS: tuple[str, ...] = ("esc",)

CURSOR_GLYPHS = "❯>▶➤"

# "  ❯ 2. Yes, and don't ask again"  ->  cursor, "2", label
_OPTION_RE = re.compile(
    r"^\s*(?P<cursor>[" + CURSOR_GLYPHS + r"])?\s*"
    r"(?:(?P<num>\d{1,2})[.)]\s*)?"
    r"(?P<label>\S.*?)\s*$"
)

# Trailing decoration after a middot: "Yes, and switch to auto mode · auto
# mode handles these prompts for you". Stripped before matching so the
# decoration cannot smuggle text past the predicate.
_DECORATION_RE = re.compile(r"\s+[·|]\s+.*$")

# Lines that are chrome rather than choices.
#
# Deliberately does NOT match bare "shift+tab" or "ctrl+...". A real prompt
# put that inside an option label:
#
#   2. Yes, and switch to accept edits (...) for this session (shift+tab)
#
# and matching it here made the option-block walk stop early, leaving one
# option, which parse_prompt then rejected as not-a-prompt. Shepherd saw
# nothing at all on a live prompt. The mode lines this was meant to catch are
# already covered by the auto/manual alternatives below.
_FOOTER_RE = re.compile(
    r"(enter to confirm|esc to cancel|tab to amend|"
    r"auto mode on|manual mode on|for shortcuts)",
    re.IGNORECASE,
)

# A line carrying a cursor glyph or an "N." prefix is a choice, whatever else
# it happens to contain. Checked before footer matching so option text can
# never be mistaken for chrome again.
_LOOKS_LIKE_OPTION_RE = re.compile(
    r"^\s*(?:[" + CURSOR_GLYPHS + r"]\s*)?\d{1,2}[.)]\s+\S"
    r"|^\s*[" + CURSOR_GLYPHS + r"]\s+\S"
)


_QUESTION_RE = re.compile(
    r"(do you want to|requires approval|is this a project you)",
    re.IGNORECASE,
)


def _is_chrome(line: str) -> bool:
    """Is this line UI furniture rather than a choice?"""
    if _LOOKS_LIKE_OPTION_RE.match(line):
        return False
    return bool(_FOOTER_RE.search(line))


class PromptError(RuntimeError):
    """The prompt could not be read well enough to act on it safely."""


def normalize(label: str) -> str:
    """Fold a label to something matchable.

    NFKC first because the real prompts contain typographic punctuation — the
    apostrophe in "don't ask again" is U+2019, not ASCII. Matching on a
    prefix that assumed ASCII would quietly fail to recognise the option it
    most needs to reject.
    """
    text = unicodedata.normalize("NFKC", label)
    text = _DECORATION_RE.sub("", text)
    return " ".join(text.split()).strip().casefold()


@dataclass(frozen=True, slots=True)
class PromptOption:
    line: int          # index within the parsed option block
    number: int | None # the displayed number, when the prompt uses them
    label: str         # as shown, decoration stripped
    cursored: bool

    @property
    def normalized(self) -> str:
        return normalize(self.label)

    @property
    def is_plain_yes(self) -> bool:
        """Exactly "Yes", nothing after it. The only safe approve."""
        return self.normalized == "yes"

    @property
    def is_qualified_yes(self) -> bool:
        """A "Yes, ..." variant: a glob grant, an auto-mode switch, a trust
        grant. Affirmative, and never what a pocket device should choose."""
        n = self.normalized
        return n.startswith("yes") and n != "yes"

    @property
    def is_no(self) -> bool:
        return self.normalized == "no" or self.normalized.startswith("no,")


@dataclass(frozen=True, slots=True)
class Prompt:
    question: str
    options: tuple[PromptOption, ...]

    @property
    def cursor_index(self) -> int | None:
        for i, o in enumerate(self.options):
            if o.cursored:
                return i
        return None

    @property
    def numbered(self) -> bool:
        return any(o.number is not None for o in self.options)


def parse_prompt(text: str | None) -> Prompt | None:
    """Extract the option block from a pane's detection buffer.

    Returns None when the buffer holds no recognisable prompt, which is the
    common case — most of the time an agent is simply working.
    """
    if not text:
        return None

    lines = [ln.rstrip() for ln in text.splitlines()]

    # The cursor glyph is the one marker every prompt shape shares — but it is
    # NOT unique in the buffer. Claude Code echoes the user's own input with
    # the same glyph:
    #
    #     ❯ Create a file called hello.txt in this folder
    #
    # so anchoring on the first cursor line latches onto the echo near the top,
    # finds no options beside it, and misses the real option block at the
    # bottom. Live validation caught this; a fixture written from the part of
    # the buffer already being looked at could not.
    #
    # Candidates are therefore tried bottom-up, and the first one that yields a
    # real option block wins. A live prompt is always the last thing on screen.
    candidates = []
    for i, ln in enumerate(lines):
        if any(g in ln for g in CURSOR_GLYPHS) and not _is_chrome(ln):
            m = _OPTION_RE.match(ln)
            if m and m.group("cursor") and m.group("label"):
                candidates.append(i)

    for anchor in reversed(candidates):
        found = _block_at(lines, anchor)
        if found is not None:
            return found
    return None


def _block_at(lines: list[str], anchor: int) -> "Prompt | None":
    """Try to read an option block around one cursor line."""
    # Walk outward from the cursor line over contiguous option-shaped lines.
    start = anchor
    while start - 1 >= 0:
        prev = lines[start - 1]
        if not prev.strip() or _is_chrome(prev) or _QUESTION_RE.search(prev):
            break
        m = _OPTION_RE.match(prev)
        if not m or not m.group("label"):
            break
        # An unnumbered prose line above a numbered list is the question, not
        # an option.
        if m.group("num") is None and any(
            _OPTION_RE.match(x) and _OPTION_RE.match(x).group("num")
            for x in lines[anchor : anchor + 4]
        ):
            break
        start -= 1

    end = anchor
    while end + 1 < len(lines):
        nxt = lines[end + 1]
        if not nxt.strip() or _is_chrome(nxt):
            break
        m = _OPTION_RE.match(nxt)
        if not m or not m.group("label"):
            break
        end += 1

    options: list[PromptOption] = []
    for n, i in enumerate(range(start, end + 1)):
        m = _OPTION_RE.match(lines[i])
        if not m or not m.group("label"):
            continue
        num = m.group("num")
        options.append(
            PromptOption(
                line=n,
                number=int(num) if num else None,
                label=_DECORATION_RE.sub("", m.group("label")).strip(),
                cursored=bool(m.group("cursor")),
            )
        )

    if len(options) < 2:
        return None

    question = ""
    for ln in reversed(lines[:start]):
        if ln.strip() and not _is_chrome(ln):
            question = ln.strip()
            break

    return Prompt(question=question, options=tuple(options))


def plan_deny() -> list[str]:
    """Keys that cancel. Same on every prompt shape, so no parsing needed."""
    return list(DENY_KEYS)


def plan_approve(prompt: Prompt) -> list[str]:
    """Keys that select the plain "Yes", or raise rather than guess.

    Refusing is always an acceptable outcome here: the human can open the
    laptop. Choosing wrongly is not.
    """
    matches = [o for o in prompt.options if o.is_plain_yes]
    if not matches:
        qualified = [o.label for o in prompt.options if o.is_qualified_yes]
        raise PromptError(
            "no plain 'Yes' option"
            + (f"; refusing qualified variants: {qualified}" if qualified else "")
        )
    if len(matches) > 1:
        raise PromptError("ambiguous: more than one plain 'Yes'")

    target = prompt.options.index(matches[0])
    cursor = prompt.cursor_index
    if cursor is None:
        raise PromptError("cursor position unknown; refusing to move blind")

    delta = target - cursor
    keys = ["down"] * delta if delta > 0 else ["up"] * (-delta)
    keys.append("enter")
    return keys
