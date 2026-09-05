"""Pulling an agent's last answer out of its terminal.

Herdr hands back the tail of a pane as plain text. Claude Code's transcript
has a small, stable grammar in there, and it is worth reading rather than
guessing at:

    ● Done.                          <- an assistant message starts here
                                    (blank lines are part of the block)
      Task #175078 "PM Client 2"    <- continuation, indented two spaces
    ✻ Crunched for 1m 9s              <- status; the block has ended
    ※ recap: We're making the ...     <- Claude Code's own running summary
    ❯ create the task and open PR     <- something the human typed

So a block runs from a marker glyph in column zero until the next non-blank
line in column zero. That single rule is what separates "the last thing the
agent said" from the box furniture around it.

The complication live panes showed, and fixtures would not have: a TOOL CALL
uses the same ● glyph as a message. `● Write(src/thing.py)` followed by an
indented `⎿ Wrote 114 lines` is not an answer, and taking it as one filled
the screen with a file listing. Those are skipped, and the scan keeps walking
back for something that was actually said.

What this deliberately does NOT do is try to be clever about content. It
returns text for a human to read on a 240px screen; nothing is parsed out of
it, nothing is matched against it, and no decision is taken on it. The
prompt parser in prompt.py is the one that has to be exact.
"""

from __future__ import annotations

# Column-zero glyphs that begin a transcript block. Everything else in column
# zero (a rule, a box edge) also ends a block, which is why the block scan
# tests for "not indented" rather than for these.
ANSWER_MARKER = "●"    # a filled circle: the assistant speaking
RECAP_MARKER = "※"     # a reference mark: Claude Code's running summary

# The tool-result gutter. Its presence in a block is the reliable tell that
# the ● above it was a tool call rather than a sentence.
_TOOL_GUTTER = "⎿"   # the box-drawing corner Claude Code uses

# Box-drawing runs. A table rendered for an 80-column terminal reflows into
# noise at 38 columns, so the borders are dropped and the cells kept.
_BOX = "─│┌┐└┘├┤┬┴┼"

# Chrome that Claude Code appends to its own recap line. It is an instruction
# to the person at the keyboard, not part of what the agent said, and it eats
# a third of the screen.
_RECAP_CHROME = "(disable recaps in /config)"

# How much of an answer to ship. Long enough that a real answer survives
# nearly intact, bounded so one chatty agent cannot crowd the device's line
# buffer. Scrolling means the device can show more than fits at once.
BODY_MAX = 900


def _looks_like_a_tool_call(first_line: str, block: str) -> bool:
    """Whether this ● block is a tool invocation rather than a sentence.

    Two tells, either sufficient: the result gutter appears in the block, or
    the first line has the shape `Name(argument)` with no sentence around it.
    Both are needed — a tool call whose output scrolled off the top of the
    read has no gutter left to find.
    """
    if _TOOL_GUTTER in block:
        return True
    head = first_line.strip()
    if "(" not in head or not head.endswith(")"):
        return False
    name = head[: head.index("(")]
    return bool(name) and name.replace("_", "").isalnum() and " " not in name


def _strip_box(text: str) -> str:
    """Drop box-drawing borders, keep what was inside them."""
    if not any(c in text for c in _BOX):
        return text
    return "".join(" " if c in _BOX else c for c in text)


def _is_continuation(line: str) -> bool:
    """Whether `line` belongs to the block above it."""
    return not line.strip() or line.startswith(" ")


def _block_at(lines: list[str], start: int) -> str:
    """The block beginning at `start`, joined into a paragraph.

    Interior blank lines become a single space rather than a newline: the
    device wraps to 38 columns and reflows anyway, so preserving the
    transcript's vertical rhythm would only waste rows.
    """
    out = [lines[start].strip()]
    for line in lines[start + 1:]:
        if not _is_continuation(line):
            break
        stripped = line.strip()
        if stripped:
            out.append(stripped)
    return " ".join(out)


def _last_block(lines: list[str], marker: str, skip_tools: bool = False) -> str:
    for i in range(len(lines) - 1, -1, -1):
        if not lines[i].startswith(marker):
            continue
        block = _block_at(lines, i)
        if skip_tools and _looks_like_a_tool_call(lines[i][len(marker):], block):
            continue
        return block
    return ""


def truncate_body(text: str, limit: int = BODY_MAX) -> str:
    """Keep the END, drop the beginning.

    The opposite of how truncation usually goes, and deliberately. An agent's
    answer opens with what it did and closes with what it concluded and what
    it needs next — "no PR opened, say the word", "tell me when to stop the
    servers". On a screen that can only hold part of it, the part worth
    carrying is the part you would have scrolled to.

    Cut on a word boundary when one is close by, and marked with ASCII "..."
    because the device's font has no ellipsis glyph.
    """
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    cut = collapsed[-(limit - 3):]
    space = cut.find(" ")
    if 0 <= space < 40:         # only if it does not cost most of a line
        cut = cut[space + 1:]
    return "..." + cut.lstrip()


def extract_answer(pane_text: str | None) -> str:
    """The agent's last answer, or its running recap, or nothing.

    The answer is preferred because it is what the reader asked to see. The
    recap is the fallback rather than the other way round: it is a summary
    Claude Code writes about the conversation, which is useful when there is
    no recent message but is not "the last thing this agent said".

    Empty when neither is present — a pane mid-tool-call, or an agent that
    has not spoken yet. The caller shows that as "nothing to show", which is
    honest; inventing a summary from the terminal title would not be.
    """
    if not pane_text:
        return ""
    lines = [_strip_box(l) for l in pane_text.splitlines()]
    body = _last_block(lines, ANSWER_MARKER, skip_tools=True)
    if not body:
        body = _last_block(lines, RECAP_MARKER)
        # "recap: " reads as a label the device would have to explain.
        if body.startswith(RECAP_MARKER):
            body = body[len(RECAP_MARKER):].strip()
        if body.lower().startswith("recap:"):
            body = body[len("recap:"):].strip()
    else:
        body = body[len(ANSWER_MARKER):].strip()
    if body.endswith(_RECAP_CHROME):
        body = body[: -len(_RECAP_CHROME)].rstrip()
    return truncate_body(body)
