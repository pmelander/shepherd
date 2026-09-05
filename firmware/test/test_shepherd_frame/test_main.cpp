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
        shepherdParse("{\"t\":\"other\",\"v\":2,\"a\":[]}", &f));
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
      "{\"t\":\"snap\",\"v\":2,\"ts\":\"2026-09-05T10:21:30Z\",\"a\":["
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
    const char* line = "{\"t\":\"snap\",\"v\":2,\"a\":["
                       "{\"i\":\"w2:p1\",\"n\":\"x\",\"s\":\"idle\"}]}";
    TEST_ASSERT_EQUAL(SHEPHERD_OK, shepherdParse(line, &f));
    TEST_ASSERT_EQUAL(1, f.count);
}

void test_rows_without_a_pane_or_status_are_dropped(void) {
    const char* line = "{\"t\":\"snap\",\"v\":2,\"a\":["
                       "{\"n\":\"nopane\",\"s\":\"idle\"},"
                       "{\"i\":\"w2:p1\"},"
                       "{\"i\":\"w5:p1\",\"n\":\"ok\",\"s\":\"idle\"}]}";
    TEST_ASSERT_EQUAL(SHEPHERD_OK, shepherdParse(line, &f));
    TEST_ASSERT_EQUAL(1, f.count);
    TEST_ASSERT_EQUAL_STRING("w5:p1", f.agents[0].pane);
}

void test_overflow_is_counted_not_silently_dropped(void) {
    char line[3072];
    int n = snprintf(line, sizeof(line), "{\"t\":\"snap\",\"v\":2,\"a\":[");
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
    const char* line = "{\"t\":\"snap\",\"v\":2,\"more\":4,\"a\":["
                       "{\"i\":\"w2:p1\",\"n\":\"x\",\"s\":\"idle\"}]}";
    TEST_ASSERT_EQUAL(SHEPHERD_OK, shepherdParse(line, &f));
    TEST_ASSERT_EQUAL(4, f.more);
}

void test_degraded_frame_carries_a_reason(void) {
    const char* line = "{\"t\":\"snap\",\"v\":2,\"why\":\"cannot run herdr\",\"a\":["
                       "{\"i\":\"w2:p1\",\"n\":\"x\",\"s\":\"unknown\"}]}";
    TEST_ASSERT_EQUAL(SHEPHERD_OK, shepherdParse(line, &f));
    TEST_ASSERT_TRUE(f.degraded);
    TEST_ASSERT_EQUAL_STRING("cannot run herdr", f.why);
    TEST_ASSERT_TRUE(f.agents[0].isUnknown());
}

// ---------------------------------------------------------- answerable

void test_only_a_blocked_agent_with_an_untruncated_question_is_answerable(void) {
    const char* line =
      "{\"t\":\"snap\",\"v\":2,\"a\":["
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
      "{\"t\":\"snap\",\"v\":2,\"a\":["
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
      "{\"t\":\"snap\",\"v\":2,\"a\":["
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

// -------------------------------------------------------- recap + elapsed

void test_the_recap_and_its_timestamp_are_parsed(void) {
    const char* line =
      "{\"t\":\"snap\",\"v\":2,\"ts\":\"2026-09-05T14:00:00Z\",\"a\":["
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
      "{\"t\":\"snap\",\"v\":2,\"a\":["
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
             "{\"t\":\"snap\",\"v\":2,\"a\":["
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
      "{\"t\":\"snap\",\"v\":2,\"ts\":\"2026-09-05T14:00:00Z\",\"a\":["
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
    const char* line = "{\"t\":\"deet\",\"v\":2,\"i\":\"w9:p1\",\"b\":\"Done.\"}";
    TEST_ASSERT_EQUAL(SHEPHERD_DETAIL, shepherdParse(line, &f));

    ShepherdDetail d;
    TEST_ASSERT_TRUE(shepherdParseDetail(line, &d));
    TEST_ASSERT_EQUAL_STRING("w9:p1", d.pane);
    TEST_ASSERT_EQUAL_STRING("Done.", d.body);
}

void test_an_empty_body_is_an_answer_not_a_failure(void) {
    // "this agent has not said anything readable" is a real reply. Rejecting
    // it would leave the screen saying "asking..." forever.
    const char* line = "{\"t\":\"deet\",\"v\":2,\"i\":\"w9:p1\",\"b\":\"\"}";
    ShepherdDetail d;
    TEST_ASSERT_TRUE(shepherdParseDetail(line, &d));
    TEST_ASSERT_EQUAL_STRING("w9:p1", d.pane);
    TEST_ASSERT_EQUAL_STRING("", d.body);
}

void test_a_detail_reply_needs_a_pane_and_a_matching_version(void) {
    ShepherdDetail d;
    TEST_ASSERT_FALSE(shepherdParseDetail(
        "{\"t\":\"deet\",\"v\":2,\"b\":\"orphan\"}", &d));
    TEST_ASSERT_FALSE(shepherdParseDetail(
        "{\"t\":\"deet\",\"v\":99,\"i\":\"w9:p1\",\"b\":\"x\"}", &d));
    TEST_ASSERT_FALSE(shepherdParseDetail(
        "{\"t\":\"snap\",\"v\":2,\"a\":[]}", &d));
    TEST_ASSERT_FALSE(shepherdParseDetail("not json", &d));
    TEST_ASSERT_FALSE(shepherdParseDetail(nullptr, &d));
}

void test_a_long_body_is_truncated_not_overflowed(void) {
    char line[2048];
    char b[1400];
    memset(b, 'w', sizeof(b) - 1);
    b[sizeof(b) - 1] = 0;
    snprintf(line, sizeof(line),
             "{\"t\":\"deet\",\"v\":2,\"i\":\"w9:p1\",\"b\":\"%s\"}", b);
    ShepherdDetail d;
    TEST_ASSERT_TRUE(shepherdParseDetail(line, &d));
    TEST_ASSERT_EQUAL(SHEPHERD_BODY_LEN - 1, (int)strlen(d.body));
}

// ------------------------------------------------------------ long text

void test_a_long_question_is_truncated_not_overflowed(void) {
    char line[1024];
    char q[400];
    memset(q, 'y', sizeof(q) - 1);
    q[sizeof(q) - 1] = 0;
    snprintf(line, sizeof(line),
             "{\"t\":\"snap\",\"v\":2,\"a\":[{\"i\":\"w1:p1\",\"n\":\"a\","
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
    TEST_ASSERT_EQUAL_STRING("2026-09-05T12:30:00Z|w9:p1|approve|abc123", buf);
}

void test_absent_decision_is_an_empty_field(void) {
    // focus carries no decision id. Dropping the separator instead of
    // emptying the field would let it collide with another action's message.
    char a[128], b[128];
    shepherdCanonicalMessage(a, sizeof(a), "T", "w9:p1", "focus", nullptr);
    shepherdCanonicalMessage(b, sizeof(b), "T", "w9:p1", "focus", "");
    TEST_ASSERT_EQUAL_STRING("T|w9:p1|focus|", a);
    TEST_ASSERT_EQUAL_STRING(a, b);
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
    const char* line = "{\"t\":\"snap\",\"v\":2,\"ts\":\"2026-09-05T12:30:00Z\",\"a\":["
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
