"""T10 coverage: prompt parsing and key planning.

The fixtures below are verbatim captures from Claude Code v2.1.260, taken off
a live pane during probe 2. Where a test asserts a refusal, the refusal is the
feature: this is the one module that can act on a work repo, and "open the
laptop" is always an acceptable answer.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))

from shepherd.prompt import (  # noqa: E402
    DENY_KEYS,
    PromptError,
    normalize,
    parse_prompt,
    plan_approve,
    plan_deny,
)

# ---------------------------------------------------------------- fixtures
# Captured verbatim. The apostrophe in option 2 is U+2019, deliberately.

BASH_PROMPT = """
 Bash command
 Tip: auto mode handles these prompts for you — choose "switch to auto mode" below

   git --version
   Check git version

 This command requires approval

 Do you want to proceed?
 \u276f 1. Yes
   2. Yes, and don\u2019t ask again for: git *
   3. Yes, and switch to auto mode \u00b7 auto mode handles these prompts for you
   4. No

 Esc to cancel \u00b7 Tab to amend
"""

TRUST_GATE = """
 Accessing workspace:

 C:\\Users\\x\\scratch

 Quick safety check: Is this a project you created or one you trust?

 Claude Code'll be able to read, edit, and execute files here.

 \u276f No, exit
   Yes, I trust this folder

 Enter to confirm \u00b7 Esc to cancel
"""

IDLE_PANE = """
\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
\u276f
\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
  \u23f5\u23f5 auto mode on (shift+tab to cycle) \u00b7 \u2190 1 agent
"""


# -------------------------------------------------------------- normalize


def test_normalize_folds_typographic_punctuation():
    # The real option 2 uses U+2019. A predicate assuming ASCII would fail to
    # recognise the option it most needs to reject.
    assert normalize("Yes, and don\u2019t ask again for: git *").startswith("yes,")


def test_normalize_strips_trailing_decoration():
    assert normalize(
        "Yes, and switch to auto mode \u00b7 auto mode handles these prompts"
    ) == "yes, and switch to auto mode"


# ------------------------------------------------------------------ parse


def test_parses_the_bash_prompt():
    p = parse_prompt(BASH_PROMPT)
    assert p is not None
    assert p.numbered
    assert [o.number for o in p.options] == [1, 2, 3, 4]
    assert [o.cursored for o in p.options] == [True, False, False, False]
    assert p.cursor_index == 0
    assert "proceed" in p.question.lower()


def test_parses_the_unnumbered_trust_gate():
    p = parse_prompt(TRUST_GATE)
    assert p is not None
    assert not p.numbered
    assert [o.normalized for o in p.options] == ["no, exit", "yes, i trust this folder"]
    # Cursor defaults to the REFUSING option here — the opposite of the Bash
    # prompt. This is why Enter-on-default is never safe.
    assert p.cursor_index == 0


def test_no_prompt_in_an_idle_pane():
    assert parse_prompt(IDLE_PANE) is None
    assert parse_prompt("") is None
    assert parse_prompt(None) is None
    assert parse_prompt("just some output\nand more\n") is None


# ------------------------------------------------------- option classification


def test_only_the_bare_yes_counts_as_a_safe_approve():
    opts = parse_prompt(BASH_PROMPT).options
    assert [o.is_plain_yes for o in opts] == [True, False, False, False]
    # The two dangerous affirmatives are recognised as affirmative, and
    # rejected precisely because they are qualified.
    assert opts[1].is_qualified_yes   # persistent glob permission
    assert opts[2].is_qualified_yes   # disables prompting entirely
    assert opts[3].is_no


def test_trust_gate_has_no_safe_approve_by_design():
    # "Yes, I trust this folder" grants a whole workspace. A device in a
    # pocket should not be able to do that, so the same predicate that blocks
    # the glob grant blocks this too. Not a gap — the intended behaviour.
    opts = parse_prompt(TRUST_GATE).options
    assert not any(o.is_plain_yes for o in opts)
    assert any(o.is_qualified_yes for o in opts)


# ------------------------------------------------------------------- deny


def test_deny_is_one_key_and_needs_no_parsing():
    # Verified live on both prompt shapes: esc took the agent blocked -> done.
    assert plan_deny() == ["esc"]
    assert DENY_KEYS == ("esc",)


# ---------------------------------------------------------------- approve


def test_approve_on_the_bash_prompt_is_just_enter():
    # Cursor already sits on "1. Yes", so no movement is needed.
    assert plan_approve(parse_prompt(BASH_PROMPT)) == ["enter"]


def test_approve_moves_the_cursor_when_yes_is_not_first():
    moved = BASH_PROMPT.replace(" \u276f 1. Yes", "   1. Yes").replace(
        "   4. No", " \u276f 4. No"
    )
    p = parse_prompt(moved)
    assert p.cursor_index == 3
    # Three ups to climb from "No" back to "Yes", then confirm. Never a digit:
    # the trust gate has no numbers, so index selection is not universal.
    assert plan_approve(p) == ["up", "up", "up", "enter"]


def test_approve_refuses_the_trust_gate_and_says_why():
    with pytest.raises(PromptError) as e:
        plan_approve(parse_prompt(TRUST_GATE))
    assert "no plain 'Yes'" in str(e.value)
    assert "I trust this folder" in str(e.value)


def test_approve_refuses_when_only_qualified_variants_exist():
    # A future Claude Code that drops the bare "Yes" must make Shepherd refuse
    # rather than fall through to "Yes, and don't ask again".
    text = BASH_PROMPT.replace(" \u276f 1. Yes\n", " \u276f 1. Yes, always\n")
    with pytest.raises(PromptError) as e:
        plan_approve(parse_prompt(text))
    assert "refusing qualified variants" in str(e.value)


def test_approve_refuses_an_ambiguous_double_yes():
    text = BASH_PROMPT.replace("   4. No", "   4. Yes")
    with pytest.raises(PromptError) as e:
        plan_approve(parse_prompt(text))
    assert "ambiguous" in str(e.value)


def test_approve_refuses_when_the_cursor_cannot_be_located():
    from shepherd.prompt import Prompt, PromptOption

    p = Prompt(
        question="Do you want to proceed?",
        options=(
            PromptOption(0, 1, "Yes", cursored=False),
            PromptOption(1, 2, "No", cursored=False),
        ),
    )
    with pytest.raises(PromptError) as e:
        plan_approve(p)
    assert "cursor" in str(e.value)


def test_planned_keys_are_always_known_logical_names():
    # Herdr validates key names before writing bytes, so an unexpected token
    # would fail loudly - but the planner should never emit one anyway.
    allowed = {"up", "down", "enter", "esc"}
    for text in (BASH_PROMPT,
                 BASH_PROMPT.replace(" \u276f 1. Yes", "   1. Yes").replace("   4. No", " \u276f 4. No")):
        assert set(plan_approve(parse_prompt(text))) <= allowed
    assert set(plan_deny()) <= allowed


# ------------------------------------------------- regression: the write prompt
# Captured live on 2026-09-05 during T10 validation. This exact buffer broke
# the parser: "(shift+tab)" inside option 2's label matched a footer pattern,
# the option-block walk stopped after one option, and parse_prompt returned
# None. On a real prompt Shepherd would have seen nothing at all.

WRITE_PROMPT = """
 ● Write(hello.txt)

──────────────────────────────
 Create file
 hello.txt
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
  1 hi
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 Do you want to create hello.txt?
 ❯ 1. Yes
   2. Yes, and switch to accept edits (auto-approve file edits and common file commands) for this session (shift+tab)
   3. No

 Esc to cancel · Tab to amend
"""


def test_write_prompt_parses_despite_shift_tab_inside_an_option_label():
    p = parse_prompt(WRITE_PROMPT)
    assert p is not None, "regression: parser returned None on a real prompt"
    assert [o.number for o in p.options] == [1, 2, 3]
    assert p.cursor_index == 0


def test_write_prompt_session_wide_auto_approve_is_rejected():
    # A third qualified-yes variant, found only by live validation: it
    # auto-approves file edits for the whole session.
    opts = parse_prompt(WRITE_PROMPT).options
    assert opts[0].is_plain_yes
    assert opts[1].is_qualified_yes
    assert "accept edits" in opts[1].label
    assert opts[2].is_no


def test_write_prompt_approve_selects_only_the_bare_yes():
    assert plan_approve(parse_prompt(WRITE_PROMPT)) == ["enter"]


# ------------------------------------- regression: input echo above the prompt
# The real buffer carries the user's own submitted text echoed with the SAME
# cursor glyph the option list uses. Anchoring on the first cursor line latched
# onto the echo, found no options beside it, and returned None. Only a capture
# of the whole buffer shows this — a fixture written from the visible tail
# cannot.

WRITE_PROMPT_WITH_ECHO = """
 ▐▛███▛█   Claude Code v2.1.260
▝▜██████▀  Opus 5 (1M context)

❯ Create a file called hello.txt in this folder containing the word hi.

● Write(hello.txt)

──────────────────────────────
 Create file
 hello.txt
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
  1 hi
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 Do you want to create hello.txt?
 ❯ 1. Yes
   2. Yes, and switch to accept edits (auto-approve file edits and common file commands) for this session (shift+tab)
   3. No

 Esc to cancel · Tab to amend
"""


def test_input_echo_above_the_prompt_does_not_hijack_the_anchor():
    p = parse_prompt(WRITE_PROMPT_WITH_ECHO)
    assert p is not None, "regression: echoed input captured the anchor"
    assert [o.number for o in p.options] == [1, 2, 3]
    assert p.options[0].is_plain_yes
    assert plan_approve(p) == ["enter"]


def test_echo_alone_with_no_option_block_is_still_not_a_prompt():
    echo_only = "\n".join(WRITE_PROMPT_WITH_ECHO.splitlines()[:6])
    assert parse_prompt(echo_only) is None
