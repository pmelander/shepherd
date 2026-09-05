"""Pulling the last answer out of a pane.

The fixtures here are shaped like real panes because the first version of
this passed everything a hand-written fixture could throw at it and then
returned a file listing, a session notice and a mangled table when pointed at
the five agents actually running on this machine. Every case below is one of
those.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))

from shepherd.recap import (  # noqa: E402
    BODY_MAX,
    extract_answer,
    truncate_body,
)

ANSWER = """\
❯ create the task and open PR

  Made 1 scratchpad edit +119, ran 2 shell commands

● Done.

  Task #175078 "PM Client 2" — child of #175070, same area and iteration.

  Both commits are on the branch and the tree is clean.

✻ Crunched for 1m 9s · done Thursday 1:26 PM

※ recap: We're making the ported PriceManager screens visually consistent
  under story #175070. Next: your review of the PR. (disable recaps in /config)

────────
❯
────────
  auto mode on (shift+tab to cycle)
"""

TOOL_CALL_LAST = """\
● Done with the first pass.

  The parser now handles both shapes.

● Write(plugin/shepherd/recap.py)
  ⎿ Wrote 114 lines to plugin/shepherd/recap.py
     1 \"\"\"Pulling an agent's last answer out of its terminal.
     2
     … +104 lines
"""


def test_the_last_thing_the_agent_said_wins():
    body = extract_answer(ANSWER)
    assert body.startswith("Done.")
    assert "Task #175078" in body
    assert "tree is clean" in body


def test_the_transcript_furniture_is_not_the_answer():
    body = extract_answer(ANSWER)
    assert "create the task and open PR" not in body, "that is what the human typed"
    assert "Crunched for" not in body, "that is a status line"
    assert "auto mode on" not in body, "that is the input box"
    assert "─" not in body


def test_a_tool_call_is_not_an_answer():
    # Both tells: the result gutter, and the Name(argument) shape. Taking the
    # Write() block as the answer filled the screen with a file listing.
    body = extract_answer(TOOL_CALL_LAST)
    assert body.startswith("Done with the first pass")
    assert "Wrote 114 lines" not in body
    assert "recap.py" not in body


def test_the_recap_line_is_the_fallback_when_nothing_was_said():
    only_recap = (
        "● Bash(git status)\n"
        "  ⎿ On branch main\n"
        "\n"
        "※ recap: Halfway through the migration. (disable recaps in /config)\n"
    )
    body = extract_answer(only_recap)
    assert body == "Halfway through the migration."
    assert "disable recaps" not in body, "that is an instruction to the laptop"


def test_a_pane_with_nothing_readable_returns_nothing():
    # Honest emptiness. The device says so; inventing a summary from the
    # terminal title would be worse than admitting there is nothing.
    assert extract_answer("") == ""
    assert extract_answer(None) == ""
    assert extract_answer("❯ just a prompt\n✻ Working...\n") == ""


def test_a_table_loses_its_borders_but_keeps_its_cells():
    table = (
        "● Headline numbers:\n"
        "  ┌────┐┬────┐\n"
        "  │ DP │ 15-23 days │\n"
        "  └────┘┴────┘\n"
    )
    body = extract_answer(table)
    assert "DP" in body and "15-23 days" in body
    for glyph in "─│┌┐└┘├┤┬┴┼":
        assert glyph not in body


def test_blank_lines_inside_a_block_do_not_end_it():
    # Claude Code separates paragraphs with blank lines and keeps the same
    # indent. Ending the block there would return only the first sentence.
    assert "Both commits" in extract_answer(ANSWER)


def test_a_long_answer_is_cut_on_a_word_boundary():
    long = "● " + " ".join(["migration"] * 200)
    body = extract_answer(long)
    assert len(body) <= BODY_MAX
    assert body.endswith("...")
    assert "migratio..." not in body, "cut mid-word"

    # ...unless honouring the word boundary would cost most of a line.
    assert truncate_body("a" * 300, 100).endswith("...")
    assert len(truncate_body("a" * 300, 100)) == 100
