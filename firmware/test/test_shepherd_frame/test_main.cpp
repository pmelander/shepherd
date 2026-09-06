// Device-side frame parsing, tested off the board.
//
// Runs in PlatformIO's `native` environment: no ESP32, no radio, no reset
// button. These are the rules that must stay in step with
// plugin/shepherd/frame.py, and drift between them is the most likely way
// this project breaks quietly.
//
//   pio test -e native

#include <unity.h>

#include "shepherd_frame.h"

static ShepherdFrame f;

void setUp(void) { f.clear(); }
void tearDown(void) {}

// ------------------------------------------------------------ not ours

void test_ignores_frames_that_are_not_shepherds(void) {
    // Upstream's own aggregate protocol must pass straight through.
    TEST_ASSERT_EQUAL(SHEPHERD_NOT_MINE,
        shepherdParse("{\"total\":3,\"running\":1,\"waiting\":0}", &f));
    TEST_ASSERT_EQUAL(SHEPHERD_NOT_MINE,
        shepherdParse("{\"t\":\"other\",\"v\":3,\"a\":[]}", &f));
    TEST_ASSERT_EQUAL(SHEPHERD_NOT_MINE, shepherdParse("not json", &f));
    TEST_ASSERT_EQUAL(SHEPHERD_NOT_MINE, shepherdParse("", &f));
}

void test_null_input_is_malformed_not_a_crash(void) {
    TEST_ASSERT_EQUAL(SHEPHERD_MALFORMED, shepherdParse(nullptr, &f));
}

// ------------------------------------------------------------- version

void test_version_mismatch_is_reported_not_rendered(void) {
    // Drawing fields you do not understand is how a glance device lies, so a
    // mismatch must be distinguishable from a good frame.
    TEST_ASSERT_EQUAL(SHEPHERD_BAD_VERSION,
        shepherdParse("{\"t\":\"snap\",\"v\":99,\"a\":[]}", &f));
    TEST_ASSERT_EQUAL(SHEPHERD_BAD_VERSION,
        shepherdParse("{\"t\":\"snap\",\"a\":[]}", &f));   // absent == 0
}

// --------------------------------------------------------------- agents

void test_parses_a_realistic_frame(void) {
    const char* line =
      "{\"t\":\"snap\",\"v\":3,\"ts\":\"2026-09-05T10:21:30Z\",\"a\":["
      "{\"i\":\"w9:p1\",\"n\":\"elasmigr\",\"s\":\"blocked\","
      "\"q\":\"Allow Bash(git push --force origin main)?\",\"r\":\"a1b2c3d4e5f6\"},"
      "{\"i\":\"wD:p1\",\"n\":\"agenstac\",\"s\":\"done\"},"
      "{\"i\":\"w2:p1\",\"n\":\"newprcl\",\"s\":\"idle\"}]}";
    TEST_ASSERT_EQUAL(SHEPHERD_OK, shepherdParse(line, &f));
    TEST_ASSERT_EQUAL(3, f.count);
    TEST_ASSERT_EQUAL(1, f.blockedCount());
    TEST_ASSERT_EQUAL_STRING("w9:p1", f.agents[0].pane);
    TEST_ASSERT_EQUAL_STRING("elasmigr", f.agents[0].alias);
    TEST_ASSERT_TRUE(f.agents[0].isBlocked());
    TEST_ASSERT_TRUE(f.agents[0].hasQuestion);
    TEST_ASSERT_TRUE(f.agents[1].isDone());
    TEST_ASSERT_FALSE(f.agents[2].hasQuestion);
}

void test_missing_e_field_is_normal_not_an_error(void) {
    // The host omits the duration when it cannot honestly claim to know it.
    const char* line = "{\"t\":\"snap\",\"v\":3,\"a\":["
                       "{\"i\":\"w2:p1\",\"n\":\"x\",\"s\":\"idle\"}]}";
    TEST_ASSERT_EQUAL(SHEPHERD_OK, shepherdParse(line, &f));
    TEST_ASSERT_EQUAL(1, f.count);
}

void test_rows_without_a_pane_or_status_are_dropped(void) {
    const char* line = "{\"t\":\"snap\",\"v\":3,\"a\":["
                       "{\"n\":\"nopane\",\"s\":\"idle\"},"
                       "{\"i\":\"w2:p1\"},"
                       "{\"i\":\"w5:p1\",\"n\":\"ok\",\"s\":\"idle\"}]}";
    TEST_ASSERT_EQUAL(SHEPHERD_OK, shepherdParse(line, &f));
    TEST_ASSERT_EQUAL(1, f.count);
    TEST_ASSERT_EQUAL_STRING("w5:p1", f.agents[0].pane);
}

void test_overflow_is_counted_not_silently_dropped(void) {
    char line[3072];
    int n = snprintf(line, sizeof(line), "{\"t\":\"snap\",\"v\":3,\"a\":[");
    for (int i = 0; i < SHEPHERD_MAX_AGENTS + 3; i++) {
        n += snprintf(line + n, sizeof(line) - n,
                      "%s{\"i\":\"w%d:p1\",\"n\":\"a%d\",\"s\":\"idle\"}",
                      i ? "," : "", i, i);
    }
    snprintf(line + n, sizeof(line) - n, "]}");
    TEST_ASSERT_EQUAL(SHEPHERD_OK, shepherdParse(line, &f));
    TEST_ASSERT_EQUAL(SHEPHERD_MAX_AGENTS, f.count);
    TEST_ASSERT_EQUAL(3, f.more);
}

void test_host_reported_overflow_is_added(void) {
    const char* line = "{\"t\":\"snap\",\"v\":3,\"more\":4,\"a\":["
                       "{\"i\":\"w2:p1\",\"n\":\"x\",\"s\":\"idle\"}]}";
    TEST_ASSERT_EQUAL(SHEPHERD_OK, shepherdParse(line, &f));
    TEST_ASSERT_EQUAL(4, f.more);
}

void test_degraded_frame_carries_a_reason(void) {
    const char* line = "{\"t\":\"snap\",\"v\":3,\"why\":\"cannot run herdr\",\"a\":["
                       "{\"i\":\"w2:p1\",\"n\":\"x\",\"s\":\"unknown\"}]}";
    TEST_ASSERT_EQUAL(SHEPHERD_OK, shepherdParse(line, &f));
    TEST_ASSERT_TRUE(f.degraded);
    TEST_ASSERT_EQUAL_STRING("cannot run herdr", f.why);
    TEST_ASSERT_TRUE(f.agents[0].isUnknown());
}

// ---------------------------------------------------------- answerable

void test_only_a_blocked_agent_with_an_untruncated_question_is_answerable(void) {
    const char* line =
      "{\"t\":\"snap\",\"v\":3,\"a\":["
      "{\"i\":\"w1:p1\",\"n\":\"a\",\"s\":\"idle\",\"q\":\"?\",\"r\":\"r1\"},"
      "{\"i\":\"w2:p1\",\"n\":\"b\",\"s\":\"blocked\"},"
      "{\"i\":\"w3:p1\",\"n\":\"c\",\"s\":\"blocked\",\"q\":\"?\",\"r\":\"r3\",\"x\":true},"
      "{\"i\":\"w4:p1\",\"n\":\"d\",\"s\":\"blocked\",\"q\":\"?\"},"
      "{\"i\":\"w5:p1\",\"n\":\"e\",\"s\":\"blocked\",\"q\":\"ok?\",\"r\":\"r5\"}]}";
    TEST_ASSERT_EQUAL(SHEPHERD_OK, shepherdParse(line, &f));
    TEST_ASSERT_FALSE(f.agents[0].answerable());  // not blocked
    TEST_ASSERT_FALSE(f.agents[1].answerable());  // no question
    TEST_ASSERT_FALSE(f.agents[2].answerable());  // truncated: open the laptop
    TEST_ASSERT_FALSE(f.agents[3].answerable());  // no decision id
    TEST_ASSERT_TRUE(f.agents[4].answerable());
    TEST_ASSERT_EQUAL(4, f.firstAnswerable());
}

void test_first_answerable_walks_the_queue(void) {
    const char* line =
      "{\"t\":\"snap\",\"v\":3,\"a\":["
      "{\"i\":\"w1:p1\",\"n\":\"a\",\"s\":\"blocked\",\"q\":\"?\",\"r\":\"r1\"},"
      "{\"i\":\"w2:p1\",\"n\":\"b\",\"s\":\"blocked\",\"q\":\"?\",\"r\":\"r2\"}]}";
    TEST_ASSERT_EQUAL(SHEPHERD_OK, shepherdParse(line, &f));
    TEST_ASSERT_EQUAL(0, f.firstAnswerable());
    TEST_ASSERT_EQUAL(1, f.firstAnswerable(1));
    TEST_ASSERT_EQUAL(-1, f.firstAnswerable(2));
}

void test_showable_includes_the_ones_you_cannot_answer(void) {
    // The queue screen is driven by firstShowable, not firstAnswerable. A
    // blocked agent whose question was truncated cannot be approved, but it
    // must still get the screen and the explanation — driving the UI off
    // answerability alone hid it in the herd list, where the approve key did
    // nothing and said nothing.
    const char* line =
      "{\"t\":\"snap\",\"v\":3,\"a\":["
      "{\"i\":\"w1:p1\",\"n\":\"a\",\"s\":\"working\"},"
      "{\"i\":\"w2:p1\",\"n\":\"b\",\"s\":\"blocked\"},"
      "{\"i\":\"w3:p1\",\"n\":\"c\",\"s\":\"blocked\",\"q\":\"?\",\"r\":\"r3\",\"x\":true},"
      "{\"i\":\"w4:p1\",\"n\":\"d\",\"s\":\"blocked\",\"q\":\"ok?\",\"r\":\"r4\"}]}";
    TEST_ASSERT_EQUAL(SHEPHERD_OK, shepherdParse(line, &f));
    TEST_ASSERT_EQUAL(3, f.firstAnswerable());
    TEST_ASSERT_EQUAL(2, f.firstShowable());     // the truncated one, first
    TEST_ASSERT_EQUAL(3, f.firstShowable(3));
    TEST_ASSERT_EQUAL(-1, f.firstShowable(4));
    // Blocked with no question at all is not worth a screen: there is nothing
    // to show and nothing to press, so index 1 is skipped over.
    TEST_ASSERT_EQUAL(2, f.firstShowable(1));
}

void test_done_is_found_separately_from_blocked(void) {
    // `done` means finished AND not looked at - Herdr splits it from `idle`
    // on whether the tab has been seen. It is the notification this device
    // exists for at least as much as blocked is, and for a long time the
    // alarm fired only for blocked, so a finished agent said nothing at all.
    const char* line =
      "{\"t\":\"snap\",\"v\":3,\"a\":["
      "{\"i\":\"w1:p1\",\"n\":\"a\",\"s\":\"idle\"},"
      "{\"i\":\"w2:p1\",\"n\":\"b\",\"s\":\"working\"},"
      "{\"i\":\"w3:p1\",\"n\":\"c\",\"s\":\"done\"},"
      "{\"i\":\"w4:p1\",\"n\":\"d\",\"s\":\"done\"}]}";
    TEST_ASSERT_EQUAL(SHEPHERD_OK, shepherdParse(line, &f));
    TEST_ASSERT_EQUAL(2, f.firstDone());
    TEST_ASSERT_EQUAL(3, f.firstDone(3));
    TEST_ASSERT_EQUAL(-1, f.firstDone(4));
    // Nothing to answer, so the queue stays empty and the herd list shows.
    TEST_ASSERT_EQUAL(-1, f.firstShowable());
}

void test_idle_is_not_done_and_must_never_ring(void) {
    // A focused tab never reaches `done` - Herdr reports it as idle, because
    // you were already looking. Treating the two alike would make the device
    // chirp about the pane in front of you.
    const char* line = "{\"t\":\"snap\",\"v\":3,\"a\":["
                       "{\"i\":\"w1:p1\",\"n\":\"a\",\"s\":\"idle\"}]}";
    TEST_ASSERT_EQUAL(SHEPHERD_OK, shepherdParse(line, &f));
    TEST_ASSERT_EQUAL(-1, f.firstDone());
    TEST_ASSERT_EQUAL(-1, f.firstShowable());
}

void test_blocked_and_done_are_both_found_so_the_caller_can_rank_them(void) {
    // The UI rings for blocked when both are present: someone waiting on you
    // outranks something waiting for you. Both indices have to be reachable
    // for that choice to exist at all.
    const char* line =
      "{\"t\":\"snap\",\"v\":3,\"a\":["
      "{\"i\":\"w1:p1\",\"n\":\"a\",\"s\":\"done\"},"
      "{\"i\":\"w2:p1\",\"n\":\"b\",\"s\":\"blocked\",\"q\":\"ok?\",\"r\":\"r2\"}]}";
    TEST_ASSERT_EQUAL(SHEPHERD_OK, shepherdParse(line, &f));
    TEST_ASSERT_EQUAL(0, f.firstDone());
    TEST_ASSERT_EQUAL(1, f.firstShowable());
}

void test_the_herd_can_be_counted_by_state(void) {
    // These feed the buddy. Upstream's derive() already asks exactly the
    // questions Shepherd can answer - is anything waiting, did something just
    // finish, how many are running - it was simply never given the numbers.
    const char* line =
      "{\"t\":\"snap\",\"v\":3,\"a\":["
      "{\"i\":\"w1:p1\",\"n\":\"a\",\"s\":\"working\"},"
      "{\"i\":\"w2:p1\",\"n\":\"b\",\"s\":\"working\"},"
      "{\"i\":\"w3:p1\",\"n\":\"c\",\"s\":\"done\"},"
      "{\"i\":\"w4:p1\",\"n\":\"d\",\"s\":\"idle\"},"
      "{\"i\":\"w5:p1\",\"n\":\"e\",\"s\":\"blocked\",\"q\":\"ok?\",\"r\":\"r5\"}]}";
    TEST_ASSERT_EQUAL(SHEPHERD_OK, shepherdParse(line, &f));
    TEST_ASSERT_EQUAL(5, f.count);
    TEST_ASSERT_EQUAL(2, f.workingCount());
    TEST_ASSERT_EQUAL(1, f.doneCount());
    TEST_ASSERT_EQUAL(1, f.blockedCount());
}

void test_counting_an_empty_or_degraded_herd_is_all_zeroes(void) {
    // A degraded frame marks everything unknown, which is none of the three.
    const char* line = "{\"t\":\"snap\",\"v\":3,\"why\":\"gone\",\"a\":["
                       "{\"i\":\"w1:p1\",\"n\":\"a\",\"s\":\"unknown\"}]}";
    TEST_ASSERT_EQUAL(SHEPHERD_OK, shepherdParse(line, &f));
    TEST_ASSERT_EQUAL(0, f.workingCount());
    TEST_ASSERT_EQUAL(0, f.doneCount());
    TEST_ASSERT_EQUAL(0, f.blockedCount());
}

// -------------------------------------------------------- recap + elapsed

void test_the_recap_and_its_timestamp_are_parsed(void) {
    const char* line =
      "{\"t\":\"snap\",\"v\":3,\"ts\":\"2026-09-05T14:00:00Z\",\"a\":["
      "{\"i\":\"w1:p1\",\"n\":\"a\",\"s\":\"working\","
      "\"d\":\"PriceComponentManager_Client migration to React\","
      "\"e\":\"2026-09-05T13:46:00Z\"}]}";
    TEST_ASSERT_EQUAL(SHEPHERD_OK, shepherdParse(line, &f));
    TEST_ASSERT_EQUAL_STRING("PriceComponentManager_Client migration to React",
                             f.agents[0].recap);
    TEST_ASSERT_EQUAL_STRING("2026-09-05T13:46:00Z", f.agents[0].since);
    TEST_ASSERT_EQUAL(14 * 60, shepherdElapsed(f, f.agents[0]));
}

void test_an_agent_with_no_recap_leaves_the_field_empty(void) {
    // Absent, not empty-with-a-heading: the detail screen tests recap[0].
    const char* line =
      "{\"t\":\"snap\",\"v\":3,\"a\":["
      "{\"i\":\"w1:p1\",\"n\":\"a\",\"s\":\"idle\"}]}";
    TEST_ASSERT_EQUAL(SHEPHERD_OK, shepherdParse(line, &f));
    TEST_ASSERT_EQUAL_STRING("", f.agents[0].recap);
    TEST_ASSERT_EQUAL_STRING("", f.agents[0].since);
    TEST_ASSERT_EQUAL(-1, shepherdElapsed(f, f.agents[0]));
}

void test_a_long_recap_is_truncated_not_overflowed(void) {
    char line[512];
    char d[300];
    memset(d, 'z', sizeof(d) - 1);
    d[sizeof(d) - 1] = 0;
    snprintf(line, sizeof(line),
             "{\"t\":\"snap\",\"v\":3,\"a\":["
             "{\"i\":\"w1:p1\",\"n\":\"a\",\"s\":\"idle\",\"d\":\"%s\"}]}", d);
    TEST_ASSERT_EQUAL(SHEPHERD_OK, shepherdParse(line, &f));
    TEST_ASSERT_EQUAL(SHEPHERD_RECAP_LEN - 1, (int)strlen(f.agents[0].recap));
}

void test_iso_seconds_parses_the_hosts_exact_shape_and_nothing_else(void) {
    TEST_ASSERT_EQUAL(0, shepherdIsoSeconds("1970-01-01T00:00:00Z"));
    TEST_ASSERT_EQUAL(86400, shepherdIsoSeconds("1970-01-02T00:00:00Z"));
    // A leap day, because the civil-date maths is the part worth doubting.
    TEST_ASSERT_EQUAL(shepherdIsoSeconds("2024-02-28T00:00:00Z") + 86400,
                      shepherdIsoSeconds("2024-02-29T00:00:00Z"));
    TEST_ASSERT_EQUAL(shepherdIsoSeconds("2024-02-29T00:00:00Z") + 86400,
                      shepherdIsoSeconds("2024-03-01T00:00:00Z"));
    // A year boundary, and the century rule 1900 gets wrong.
    TEST_ASSERT_EQUAL(shepherdIsoSeconds("2025-12-31T23:59:59Z") + 1,
                      shepherdIsoSeconds("2026-01-01T00:00:00Z"));

    for (const char* bad : {"", "not a date", "2026-09-05", "2026-13-05T00:00:00Z",
                            "2026-09-32T00:00:00Z", "2026-09-05T25:00:00Z"})
        TEST_ASSERT_EQUAL_MESSAGE(-1, shepherdIsoSeconds(bad), bad);
    TEST_ASSERT_EQUAL(-1, shepherdIsoSeconds(nullptr));
}

void test_a_clock_that_disagrees_reads_as_zero_not_as_negative(void) {
    // The host stamps `ts` and `e` from the same clock, but rounding can put
    // them a second apart. "-1s ago" on a glance screen reads as a bug.
    const char* line =
      "{\"t\":\"snap\",\"v\":3,\"ts\":\"2026-09-05T14:00:00Z\",\"a\":["
      "{\"i\":\"w1:p1\",\"n\":\"a\",\"s\":\"idle\",\"e\":\"2026-09-05T14:00:05Z\"}]}";
    TEST_ASSERT_EQUAL(SHEPHERD_OK, shepherdParse(line, &f));
    TEST_ASSERT_EQUAL(0, shepherdElapsed(f, f.agents[0]));
}

void test_elapsed_is_formatted_at_the_coarsest_useful_unit(void) {
    char b[12];
    shepherdFormatElapsed(b, sizeof(b), 5);        TEST_ASSERT_EQUAL_STRING("5s", b);
    shepherdFormatElapsed(b, sizeof(b), 59);       TEST_ASSERT_EQUAL_STRING("59s", b);
    shepherdFormatElapsed(b, sizeof(b), 60);       TEST_ASSERT_EQUAL_STRING("1m", b);
    shepherdFormatElapsed(b, sizeof(b), 3599);     TEST_ASSERT_EQUAL_STRING("59m", b);
    shepherdFormatElapsed(b, sizeof(b), 7800);     TEST_ASSERT_EQUAL_STRING("2h10m", b);
    shepherdFormatElapsed(b, sizeof(b), 90000);    TEST_ASSERT_EQUAL_STRING("1d", b);
    // Unknown must be empty, not "0s" - the whole point of omitting `e` is
    // that the host refuses to assert a duration it does not know.
    shepherdFormatElapsed(b, sizeof(b), -1);       TEST_ASSERT_EQUAL_STRING("", b);
}

// ------------------------------------------------------------- detail

void test_a_detail_reply_is_recognised_but_not_parsed_as_a_snapshot(void) {
    // shepherdParse hands it back as SHEPHERD_DETAIL rather than filling a
    // frame, because the body is a kilobyte and does not belong on every
    // snapshot's stack.
    const char* line = "{\"t\":\"deet\",\"v\":3,\"i\":\"w9:p1\",\"b\":\"Done.\"}";
    TEST_ASSERT_EQUAL(SHEPHERD_DETAIL, shepherdParse(line, &f));

    ShepherdDetail d;
    TEST_ASSERT_TRUE(shepherdParseDetail(line, &d));
    TEST_ASSERT_EQUAL_STRING("w9:p1", d.pane);
    TEST_ASSERT_EQUAL_STRING("Done.", d.body);
}

void test_an_empty_body_is_an_answer_not_a_failure(void) {
    // "this agent has not said anything readable" is a real reply. Rejecting
    // it would leave the screen saying "asking..." forever.
    const char* line = "{\"t\":\"deet\",\"v\":3,\"i\":\"w9:p1\",\"b\":\"\"}";
    ShepherdDetail d;
    TEST_ASSERT_TRUE(shepherdParseDetail(line, &d));
    TEST_ASSERT_EQUAL_STRING("w9:p1", d.pane);
    TEST_ASSERT_EQUAL_STRING("", d.body);
}

void test_a_detail_reply_needs_a_pane_and_a_matching_version(void) {
    ShepherdDetail d;
    TEST_ASSERT_FALSE(shepherdParseDetail(
        "{\"t\":\"deet\",\"v\":3,\"b\":\"orphan\"}", &d));
    TEST_ASSERT_FALSE(shepherdParseDetail(
        "{\"t\":\"deet\",\"v\":99,\"i\":\"w9:p1\",\"b\":\"x\"}", &d));
    TEST_ASSERT_FALSE(shepherdParseDetail(
        "{\"t\":\"snap\",\"v\":3,\"a\":[]}", &d));
    TEST_ASSERT_FALSE(shepherdParseDetail("not json", &d));
    TEST_ASSERT_FALSE(shepherdParseDetail(nullptr, &d));
}

void test_a_long_body_is_truncated_not_overflowed(void) {
    char line[2048];
    char b[1400];
    memset(b, 'w', sizeof(b) - 1);
    b[sizeof(b) - 1] = 0;
    snprintf(line, sizeof(line),
             "{\"t\":\"deet\",\"v\":3,\"i\":\"w9:p1\",\"b\":\"%s\"}", b);
    ShepherdDetail d;
    TEST_ASSERT_TRUE(shepherdParseDetail(line, &d));
    TEST_ASSERT_EQUAL(SHEPHERD_BODY_LEN - 1, (int)strlen(d.body));
}

void test_the_option_block_is_parsed_with_its_classification(void) {
    const char* line =
      "{\"t\":\"snap\",\"v\":3,\"a\":["
      "{\"i\":\"w9:p1\",\"n\":\"a\",\"s\":\"blocked\","
      "\"q\":\"Do you want to proceed?\",\"r\":\"r9\","
      "\"o\":[\"Yes\",\"Yes, and do not ask again\",\"No\"],"
      "\"w\":\"swn\"}]}";
    TEST_ASSERT_EQUAL(SHEPHERD_OK, shepherdParse(line, &f));
    TEST_ASSERT_EQUAL(3, f.agents[0].optCount);
    TEST_ASSERT_EQUAL_STRING("Yes", f.agents[0].options[0]);
    TEST_ASSERT_EQUAL_STRING("Yes, and do not ask again", f.agents[0].options[1]);
    TEST_ASSERT_EQUAL_STRING("No", f.agents[0].options[2]);
    TEST_ASSERT_EQUAL(SHEPHERD_OPT_SAFE, f.agents[0].optKind[0]);
    TEST_ASSERT_EQUAL(SHEPHERD_OPT_WIDEN, f.agents[0].optKind[1]);
    TEST_ASSERT_EQUAL(SHEPHERD_OPT_NO, f.agents[0].optKind[2]);
    TEST_ASSERT_TRUE(f.agents[0].answerable());
}

void test_a_prompt_with_no_option_block_still_works(void) {
    // Every frame before v3 looked like this, and a single-answer prompt
    // still does. The device falls back to letting the relay choose.
    const char* line =
      "{\"t\":\"snap\",\"v\":3,\"a\":["
      "{\"i\":\"w9:p1\",\"n\":\"a\",\"s\":\"blocked\","
      "\"q\":\"ok?\",\"r\":\"r9\"}]}";
    TEST_ASSERT_EQUAL(SHEPHERD_OK, shepherdParse(line, &f));
    TEST_ASSERT_EQUAL(0, f.agents[0].optCount);
    TEST_ASSERT_TRUE(f.agents[0].answerable());
}

void test_unclassified_options_read_as_other_not_as_safe(void) {
    // A short or missing `w` must never leave an option looking like the
    // plain Yes - that is the one classification it is dangerous to guess.
    const char* line =
      "{\"t\":\"snap\",\"v\":3,\"a\":["
      "{\"i\":\"w9:p1\",\"n\":\"a\",\"s\":\"blocked\","
      "\"q\":\"ok?\",\"r\":\"r9\","
      "\"o\":[\"One\",\"Two\",\"Three\"],\"w\":\"s\"}]}";
    TEST_ASSERT_EQUAL(SHEPHERD_OK, shepherdParse(line, &f));
    TEST_ASSERT_EQUAL(3, f.agents[0].optCount);
    TEST_ASSERT_EQUAL(SHEPHERD_OPT_SAFE, f.agents[0].optKind[0]);
    TEST_ASSERT_EQUAL(SHEPHERD_OPT_OTHER, f.agents[0].optKind[1]);
    TEST_ASSERT_EQUAL(SHEPHERD_OPT_OTHER, f.agents[0].optKind[2]);
}

void test_too_many_options_are_capped_not_overflowed(void) {
    char line[1024];
    int n = snprintf(line, sizeof(line),
                     "{\"t\":\"snap\",\"v\":3,\"a\":[{\"i\":\"w9:p1\",\"n\":\"a\","
                     "\"s\":\"blocked\",\"q\":\"ok?\",\"r\":\"r9\",\"o\":[");
    for (int i = 0; i < SHEPHERD_MAX_OPTIONS + 4; i++)
        n += snprintf(line + n, sizeof(line) - n, "%s\"opt%d\"", i ? "," : "", i);
    snprintf(line + n, sizeof(line) - n, "]}]}");
    TEST_ASSERT_EQUAL(SHEPHERD_OK, shepherdParse(line, &f));
    TEST_ASSERT_EQUAL(SHEPHERD_MAX_OPTIONS, f.agents[0].optCount);
}

void test_a_long_option_label_is_truncated_not_overflowed(void) {
    char line[512];
    char lbl[200];
    memset(lbl, 'y', sizeof(lbl) - 1);
    lbl[sizeof(lbl) - 1] = 0;
    snprintf(line, sizeof(line),
             "{\"t\":\"snap\",\"v\":3,\"a\":[{\"i\":\"w9:p1\",\"n\":\"a\","
             "\"s\":\"blocked\",\"q\":\"ok?\",\"r\":\"r9\",\"o\":[\"%s\"]}]}", lbl);
    TEST_ASSERT_EQUAL(SHEPHERD_OK, shepherdParse(line, &f));
    TEST_ASSERT_EQUAL(SHEPHERD_OPTION_LEN - 1,
                      (int)strlen(f.agents[0].options[0]));
}

void test_the_act_frame_carries_the_chosen_index(void) {
    char buf[192];
    size_t n = shepherdBuildAct(buf, sizeof(buf), "w9:p1", "approve", "r9",
                                "2026-09-05T12:30:00Z", "0011223344556677", 2);
    TEST_ASSERT_TRUE(n > 0);
    TEST_ASSERT_NOT_NULL(strstr(buf, "\"c\":2"));

    // Absent, not zero, when nothing was chosen: the relay reads a missing
    // `c` as "take your own safe path".
    n = shepherdBuildAct(buf, sizeof(buf), "w9:p1", "approve", "r9");
    TEST_ASSERT_TRUE(n > 0);
    TEST_ASSERT_NULL(strstr(buf, "\"c\""));
}

// -------------------------------------------------------------- rekey

void test_a_rekey_frame_is_recognised_and_parsed(void) {
    const char* line =
      "{\"t\":\"key\",\"v\":3,\"k\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\","
      "\"ts\":\"2026-09-05T18:00:00Z\",\"mac\":\"0011223344556677\"}";
    TEST_ASSERT_EQUAL(SHEPHERD_REKEY, shepherdParse(line, &f));

    ShepherdRekey r;
    TEST_ASSERT_TRUE(shepherdParseRekey(line, &r));
    TEST_ASSERT_EQUAL_STRING("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", r.hex);
    TEST_ASSERT_EQUAL_STRING("2026-09-05T18:00:00Z", r.ts);
    TEST_ASSERT_EQUAL_STRING("0011223344556677", r.mac);
}

void test_a_key_that_is_not_the_right_shape_is_refused_before_the_mac(void) {
    // Checked first on purpose. A malformed key that somehow got past the
    // signature would be stored, and every action after it refused with no
    // way to tell why from either end.
    TEST_ASSERT_FALSE(shepherdIsSecretHex(nullptr));
    TEST_ASSERT_FALSE(shepherdIsSecretHex(""));
    TEST_ASSERT_FALSE(shepherdIsSecretHex("abcd"));               // too short
    TEST_ASSERT_FALSE(shepherdIsSecretHex("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"));  // odd length
    TEST_ASSERT_FALSE(shepherdIsSecretHex("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"));  // too long
    TEST_ASSERT_FALSE(shepherdIsSecretHex("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"));  // uppercase
    TEST_ASSERT_FALSE(shepherdIsSecretHex("zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"));  // not hex
    TEST_ASSERT_TRUE(shepherdIsSecretHex("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"));
}

void test_a_rekey_missing_any_field_is_refused(void) {
    ShepherdRekey r;
    TEST_ASSERT_FALSE(shepherdParseRekey(
        "{\"t\":\"key\",\"v\":3,\"ts\":\"x\",\"mac\":\"0011223344556677\"}", &r));
    TEST_ASSERT_FALSE(shepherdParseRekey(
        "{\"t\":\"key\",\"v\":3,\"k\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\",\"mac\":\"0011223344556677\"}", &r));
    TEST_ASSERT_FALSE(shepherdParseRekey(
        "{\"t\":\"key\",\"v\":3,\"k\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\",\"ts\":\"x\"}", &r));
    // A short MAC would otherwise be compared against a full-length one and
    // simply never match, which looks like a wrong key rather than junk.
    TEST_ASSERT_FALSE(shepherdParseRekey(
        "{\"t\":\"key\",\"v\":3,\"k\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\",\"ts\":\"x\",\"mac\":\"00\"}", &r));
    TEST_ASSERT_FALSE(shepherdParseRekey(
        "{\"t\":\"key\",\"v\":99,\"k\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\",\"ts\":\"x\",\"mac\":\"0011223344556677\"}", &r));
    TEST_ASSERT_FALSE(shepherdParseRekey("{\"t\":\"snap\",\"v\":3,\"a\":[]}", &r));
    TEST_ASSERT_FALSE(shepherdParseRekey(nullptr, &r));
}

void test_the_rekey_signs_over_the_new_key(void) {
    // Mirrors rekey_message() in plugin/shepherd/auth.py. If these two ever
    // disagree, rotation refuses every time and says only "bad signature".
    char buf[160];
    size_t n = shepherdCanonicalMessage(buf, sizeof(buf), "2026-09-05T18:00:00Z",
                                        "device", "rekey", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa");
    TEST_ASSERT_TRUE(n > 0);
    TEST_ASSERT_EQUAL_STRING(
        "2026-09-05T18:00:00Z|device|rekey|aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa|", buf);
}

void test_the_ack_signs_a_different_action_than_the_request(void) {
    // Otherwise echoing the request back would pass as proof of storing it.
    char buf[96];
    size_t n = shepherdCanonicalMessage(buf, sizeof(buf), "2026-09-05T18:00:00Z",
                                        "device", "rekeyed", nullptr);
    TEST_ASSERT_TRUE(n > 0);
    TEST_ASSERT_EQUAL_STRING("2026-09-05T18:00:00Z|device|rekeyed||", buf);
}

void test_builds_the_key_ack(void) {
    char buf[96];
    size_t n = shepherdBuildKeyAck(buf, sizeof(buf), "2026-09-05T18:00:00Z",
                                   "0011223344556677");
    TEST_ASSERT_TRUE(n > 0);
    TEST_ASSERT_EQUAL_STRING(
        "{\"t\":\"kack\",\"v\":3,\"ts\":\"2026-09-05T18:00:00Z\","
        "\"f\":\"0011223344556677\"}\n", buf);

    char tiny[8];
    TEST_ASSERT_EQUAL(0, shepherdBuildKeyAck(tiny, sizeof(tiny), "x", "y"));
    TEST_ASSERT_EQUAL(0, shepherdBuildKeyAck(buf, sizeof(buf), "", "y"));
}

// ------------------------------------------------------------ long text

void test_a_long_question_is_truncated_not_overflowed(void) {
    char line[1024];
    char q[400];
    memset(q, 'y', sizeof(q) - 1);
    q[sizeof(q) - 1] = 0;
    snprintf(line, sizeof(line),
             "{\"t\":\"snap\",\"v\":3,\"a\":[{\"i\":\"w1:p1\",\"n\":\"a\","
             "\"s\":\"blocked\",\"q\":\"%s\",\"r\":\"r1\"}]}", q);
    TEST_ASSERT_EQUAL(SHEPHERD_OK, shepherdParse(line, &f));
    TEST_ASSERT_EQUAL(SHEPHERD_QUESTION_LEN - 1, (int)strlen(f.agents[0].question));
}

// ------------------------------------------------------------ act frames

void test_builds_an_act_frame(void) {
    char buf[128];
    size_t n = shepherdBuildAct(buf, sizeof(buf), "w9:p1", "approve", "a1b2c3");
    TEST_ASSERT_TRUE(n > 0);
    TEST_ASSERT_EQUAL_STRING(
        "{\"t\":\"act\",\"i\":\"w9:p1\",\"k\":\"approve\",\"r\":\"a1b2c3\"}\n", buf);
}

void test_act_frame_without_a_decision_id(void) {
    char buf[128];
    TEST_ASSERT_TRUE(shepherdBuildAct(buf, sizeof(buf), "w2:p1", "focus", nullptr) > 0);
    TEST_ASSERT_EQUAL_STRING("{\"t\":\"act\",\"i\":\"w2:p1\",\"k\":\"focus\"}\n", buf);
}

void test_act_frame_refuses_to_overflow_its_buffer(void) {
    char small[20];
    TEST_ASSERT_EQUAL(0, (int)shepherdBuildAct(small, sizeof(small),
                                               "w9:p1", "approve", "a1b2c3d4e5f6"));
}

void test_act_frame_refuses_empty_inputs(void) {
    char buf[128];
    TEST_ASSERT_EQUAL(0, (int)shepherdBuildAct(buf, sizeof(buf), "", "approve", "r"));
    TEST_ASSERT_EQUAL(0, (int)shepherdBuildAct(buf, sizeof(buf), "w9:p1", "", "r"));
    TEST_ASSERT_EQUAL(0, (int)shepherdBuildAct(buf, sizeof(buf), nullptr, "approve", "r"));
}


// ------------------------------------------------- canonical message + mac
// These mirror plugin/shepherd/auth.py exactly. The two sides diverging is
// the most likely way signing breaks, and it would break silently: every
// real action would simply be refused as a bad signature.

void test_canonical_message_shape(void) {
    char buf[128];
    size_t n = shepherdCanonicalMessage(buf, sizeof(buf),
        "2026-09-05T12:30:00Z", "w9:p1", "approve", "abc123");
    TEST_ASSERT_TRUE(n > 0);
    TEST_ASSERT_EQUAL_STRING("2026-09-05T12:30:00Z|w9:p1|approve|abc123|", buf);

    // Fifth field is the chosen option index. Pinned with the same
    // literal as tests/test_auth.py, because a disagreement about this
    // string refuses every action and says only "bad signature".
    n = shepherdCanonicalMessage(buf, sizeof(buf), "2026-09-05T12:30:00Z",
                                 "w9:p1", "approve", "abc123", 2);
    TEST_ASSERT_TRUE(n > 0);
    TEST_ASSERT_EQUAL_STRING(
        "2026-09-05T12:30:00Z|w9:p1|approve|abc123|2", buf);
}

void test_absent_decision_is_an_empty_field(void) {
    // focus carries no decision id. Dropping the separator instead of
    // emptying the field would let it collide with another action's message.
    char a[128], b[128];
    shepherdCanonicalMessage(a, sizeof(a), "T", "w9:p1", "focus", nullptr);
    shepherdCanonicalMessage(b, sizeof(b), "T", "w9:p1", "focus", "");
    TEST_ASSERT_EQUAL_STRING("T|w9:p1|focus||", a);
    TEST_ASSERT_EQUAL_STRING(a, b);

    // Same for the choice: "chose option 0" and "chose nothing" must not
    // produce the same message, or an approve that picked the first option
    // could be replayed as one that took the relay's own safe path.
    char c[128], d[128];
    shepherdCanonicalMessage(c, sizeof(c), "T", "w9:p1", "approve", "r", -1);
    shepherdCanonicalMessage(d, sizeof(d), "T", "w9:p1", "approve", "r", 0);
    TEST_ASSERT_EQUAL_STRING("T|w9:p1|approve|r|", c);
    TEST_ASSERT_EQUAL_STRING("T|w9:p1|approve|r|0", d);
}

void test_canonical_message_refuses_bad_input(void) {
    char buf[128];
    TEST_ASSERT_EQUAL(0, (int)shepherdCanonicalMessage(buf, sizeof(buf), "", "w9:p1", "approve", "r"));
    TEST_ASSERT_EQUAL(0, (int)shepherdCanonicalMessage(buf, sizeof(buf), "T", "", "approve", "r"));
    TEST_ASSERT_EQUAL(0, (int)shepherdCanonicalMessage(buf, sizeof(buf), "T", "w9:p1", "", "r"));
    char tiny[8];
    TEST_ASSERT_EQUAL(0, (int)shepherdCanonicalMessage(tiny, sizeof(tiny),
        "2026-09-05T12:30:00Z", "w9:p1", "approve", "abc123"));
}

void test_frame_carries_its_timestamp(void) {
    const char* line = "{\"t\":\"snap\",\"v\":3,\"ts\":\"2026-09-05T12:30:00Z\",\"a\":["
                       "{\"i\":\"w2:p1\",\"n\":\"x\",\"s\":\"idle\"}]}";
    TEST_ASSERT_EQUAL(SHEPHERD_OK, shepherdParse(line, &f));
    TEST_ASSERT_EQUAL_STRING("2026-09-05T12:30:00Z", f.ts);
}

void test_signed_act_frame_carries_ts_and_mac(void) {
    char buf[192];
    size_t n = shepherdBuildAct(buf, sizeof(buf), "w9:p1", "approve", "abc123",
                                "2026-09-05T12:30:00Z", "0011223344556677");
    TEST_ASSERT_TRUE(n > 0);
    TEST_ASSERT_EQUAL_STRING(
        "{\"t\":\"act\",\"i\":\"w9:p1\",\"k\":\"approve\",\"r\":\"abc123\","
        "\"ts\":\"2026-09-05T12:30:00Z\",\"mac\":\"0011223344556677\"}\n", buf);
}

void test_unsigned_act_frame_omits_both_fields(void) {
    // Half a signature is worse than none: the relay would refuse it as
    // malformed rather than as unsigned, which is a confusing log line.
    char buf[192];
    shepherdBuildAct(buf, sizeof(buf), "w2:p1", "focus", nullptr,
                     "2026-09-05T12:30:00Z", nullptr);
    TEST_ASSERT_EQUAL_STRING("{\"t\":\"act\",\"i\":\"w2:p1\",\"k\":\"focus\"}\n", buf);
    shepherdBuildAct(buf, sizeof(buf), "w2:p1", "focus", nullptr, nullptr, "aabb");
    TEST_ASSERT_EQUAL_STRING("{\"t\":\"act\",\"i\":\"w2:p1\",\"k\":\"focus\"}\n", buf);
}

int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_ignores_frames_that_are_not_shepherds);
    RUN_TEST(test_null_input_is_malformed_not_a_crash);
    RUN_TEST(test_version_mismatch_is_reported_not_rendered);
    RUN_TEST(test_parses_a_realistic_frame);
    RUN_TEST(test_missing_e_field_is_normal_not_an_error);
    RUN_TEST(test_rows_without_a_pane_or_status_are_dropped);
    RUN_TEST(test_overflow_is_counted_not_silently_dropped);
    RUN_TEST(test_host_reported_overflow_is_added);
    RUN_TEST(test_degraded_frame_carries_a_reason);
    RUN_TEST(test_only_a_blocked_agent_with_an_untruncated_question_is_answerable);
    RUN_TEST(test_first_answerable_walks_the_queue);
    RUN_TEST(test_showable_includes_the_ones_you_cannot_answer);
    RUN_TEST(test_done_is_found_separately_from_blocked);
    RUN_TEST(test_the_herd_can_be_counted_by_state);
    RUN_TEST(test_counting_an_empty_or_degraded_herd_is_all_zeroes);
    RUN_TEST(test_idle_is_not_done_and_must_never_ring);
    RUN_TEST(test_blocked_and_done_are_both_found_so_the_caller_can_rank_them);
    RUN_TEST(test_the_recap_and_its_timestamp_are_parsed);
    RUN_TEST(test_an_agent_with_no_recap_leaves_the_field_empty);
    RUN_TEST(test_a_long_recap_is_truncated_not_overflowed);
    RUN_TEST(test_iso_seconds_parses_the_hosts_exact_shape_and_nothing_else);
    RUN_TEST(test_a_clock_that_disagrees_reads_as_zero_not_as_negative);
    RUN_TEST(test_elapsed_is_formatted_at_the_coarsest_useful_unit);
    RUN_TEST(test_a_detail_reply_is_recognised_but_not_parsed_as_a_snapshot);
    RUN_TEST(test_an_empty_body_is_an_answer_not_a_failure);
    RUN_TEST(test_a_detail_reply_needs_a_pane_and_a_matching_version);
    RUN_TEST(test_a_long_body_is_truncated_not_overflowed);
    RUN_TEST(test_the_option_block_is_parsed_with_its_classification);
    RUN_TEST(test_a_prompt_with_no_option_block_still_works);
    RUN_TEST(test_unclassified_options_read_as_other_not_as_safe);
    RUN_TEST(test_too_many_options_are_capped_not_overflowed);
    RUN_TEST(test_a_long_option_label_is_truncated_not_overflowed);
    RUN_TEST(test_the_act_frame_carries_the_chosen_index);
    RUN_TEST(test_a_rekey_frame_is_recognised_and_parsed);
    RUN_TEST(test_a_key_that_is_not_the_right_shape_is_refused_before_the_mac);
    RUN_TEST(test_a_rekey_missing_any_field_is_refused);
    RUN_TEST(test_the_rekey_signs_over_the_new_key);
    RUN_TEST(test_the_ack_signs_a_different_action_than_the_request);
    RUN_TEST(test_builds_the_key_ack);
    RUN_TEST(test_a_long_question_is_truncated_not_overflowed);
    RUN_TEST(test_builds_an_act_frame);
    RUN_TEST(test_act_frame_without_a_decision_id);
    RUN_TEST(test_act_frame_refuses_to_overflow_its_buffer);
    RUN_TEST(test_act_frame_refuses_empty_inputs);
    RUN_TEST(test_canonical_message_shape);
    RUN_TEST(test_absent_decision_is_an_empty_field);
    RUN_TEST(test_canonical_message_refuses_bad_input);
    RUN_TEST(test_frame_carries_its_timestamp);
    RUN_TEST(test_signed_act_frame_carries_ts_and_mac);
    RUN_TEST(test_unsigned_act_frame_omits_both_fields);
    return UNITY_END();
}
