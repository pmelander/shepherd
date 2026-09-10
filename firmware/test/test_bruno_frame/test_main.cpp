// Bruno's announcement parser, off the board.
//
// Two tests here carry the design rather than the code. One asserts that the
// SHARED parser does not know `said` - which is what lets Shepherd ignore
// these frames without an edit or a reflash. The other asserts an
// announcement with no sentence is valid, because the relay publishes one the
// moment an agent finishes and fills the text in afterwards.
//
//   pio test -e native

#include <string.h>
#include <unity.h>

#include "bruno_frame.h"

static BrunoSaid s;

void setUp(void) { s.clear(); }
void tearDown(void) {}

// ------------------------------------------------------------ the happy path

void test_a_said_frame_is_parsed(void) {
    TEST_ASSERT_EQUAL(BRUNO_SAID, brunoParseSaid(
        "{\"t\":\"said\",\"v\":3,\"i\":\"w2:p1\","
        "\"b\":\"PR #75587 open, your review next\"}", &s));
    TEST_ASSERT_EQUAL_STRING("w2:p1", s.pane);
    TEST_ASSERT_EQUAL_STRING("PR #75587 open, your review next", s.body);
    TEST_ASSERT_FALSE(s.truncated);
    TEST_ASSERT_FALSE(s.empty());
}

void test_an_announcement_with_no_sentence_is_valid(void) {
    // NOT an error. The relay announces as soon as an agent finishes and only
    // fills in the text if reading the pane worked - a finished agent with
    // nothing to say still finished, and Bruno should still celebrate.
    TEST_ASSERT_EQUAL(BRUNO_SAID, brunoParseSaid(
        "{\"t\":\"said\",\"v\":3,\"i\":\"w2:p1\",\"b\":\"\"}", &s));
    TEST_ASSERT_EQUAL_STRING("w2:p1", s.pane);
    TEST_ASSERT_TRUE(s.empty());
    TEST_ASSERT_FALSE(s.truncated);
}

void test_a_missing_body_field_is_the_same_as_an_empty_one(void) {
    TEST_ASSERT_EQUAL(BRUNO_SAID, brunoParseSaid(
        "{\"t\":\"said\",\"v\":3,\"i\":\"w2:p1\"}", &s));
    TEST_ASSERT_EQUAL_STRING("w2:p1", s.pane);
    TEST_ASSERT_TRUE(s.empty());
}

void test_utf8_in_the_sentence_survives(void) {
    // Frames are encoded with ensure_ascii=False, so an agent's answer can
    // carry a typographic quote or a dash.
    TEST_ASSERT_EQUAL(BRUNO_SAID, brunoParseSaid(
        "{\"t\":\"said\",\"v\":3,\"i\":\"w2:p1\","
        "\"b\":\"Don\\u2019t stop the servers \\u2014 yet\"}", &s));
    TEST_ASSERT_TRUE(strstr(s.body, "stop the servers") != nullptr);
}

// ------------------------------------------------------ the shared parser

void test_the_shared_parser_does_not_know_said(void) {
    // THE test that makes Shepherd safe. If someone teaches shepherdParse
    // about `said`, it starts returning a value shepherdUiApply does not
    // handle, which falls through to the snapshot path - and the pocket
    // device would need an edit and a reflash to stay correct. Shepherd
    // ignores these frames because it genuinely does not know them, which is
    // stronger than remembering not to send them.
    static ShepherdFrame f;
    f.clear();
    TEST_ASSERT_EQUAL(SHEPHERD_NOT_MINE, shepherdParse(
        "{\"t\":\"said\",\"v\":3,\"i\":\"w2:p1\",\"b\":\"done\"}", &f));
    // And it left the frame alone.
    TEST_ASSERT_EQUAL(0, f.count);
}

void test_bruno_does_not_claim_a_snapshot_or_a_detail(void) {
    // The reverse direction: Bruno's parser must not eat frames belonging to
    // the shared path, or a Bruno build would swallow its own snapshots.
    TEST_ASSERT_EQUAL(BRUNO_NOT_MINE, brunoParseSaid(
        "{\"t\":\"snap\",\"v\":3,\"a\":[]}", &s));
    TEST_ASSERT_EQUAL(BRUNO_NOT_MINE, brunoParseSaid(
        "{\"t\":\"deet\",\"v\":3,\"i\":\"w2:p1\",\"b\":\"body\"}", &s));
    TEST_ASSERT_EQUAL(BRUNO_NOT_MINE, brunoParseSaid(
        "{\"t\":\"key\",\"v\":2,\"k\":\"beef\"}", &s));
}

void test_upstreams_own_protocol_passes_through(void) {
    TEST_ASSERT_EQUAL(BRUNO_NOT_MINE, brunoParseSaid(
        "{\"total\":3,\"running\":1,\"waiting\":0}", &s));
}

// ------------------------------------------------------------ refusals

void test_a_newer_relay_is_refused_rather_than_guessed_at(void) {
    TEST_ASSERT_EQUAL(BRUNO_BAD_VERSION, brunoParseSaid(
        "{\"t\":\"said\",\"v\":4,\"i\":\"w2:p1\",\"b\":\"done\"}", &s));
    // Nothing was written: a refused frame must not leave a sentence behind
    // that Bruno then shows as if it had been understood.
    TEST_ASSERT_TRUE(s.empty());
    TEST_ASSERT_EQUAL_STRING("", s.pane);
}

void test_a_missing_version_is_refused(void) {
    TEST_ASSERT_EQUAL(BRUNO_BAD_VERSION, brunoParseSaid(
        "{\"t\":\"said\",\"i\":\"w2:p1\",\"b\":\"done\"}", &s));
}

void test_an_announcement_with_no_pane_is_malformed(void) {
    // Without a pane id there is nobody to attribute the sentence to, and
    // Bruno draws one avatar per agent state - an unattributed bubble would
    // have to be shown against the wrong agent or not at all.
    TEST_ASSERT_EQUAL(BRUNO_MALFORMED, brunoParseSaid(
        "{\"t\":\"said\",\"v\":3,\"b\":\"done\"}", &s));
    TEST_ASSERT_EQUAL(BRUNO_MALFORMED, brunoParseSaid(
        "{\"t\":\"said\",\"v\":3,\"i\":\"\",\"b\":\"done\"}", &s));
}

void test_junk_is_not_mine_rather_than_a_crash(void) {
    TEST_ASSERT_EQUAL(BRUNO_NOT_MINE, brunoParseSaid("not json", &s));
    TEST_ASSERT_EQUAL(BRUNO_NOT_MINE, brunoParseSaid("", &s));
    TEST_ASSERT_EQUAL(BRUNO_NOT_MINE, brunoParseSaid("{\"t\":", &s));
}

void test_null_input_is_malformed_not_a_crash(void) {
    TEST_ASSERT_EQUAL(BRUNO_MALFORMED, brunoParseSaid(nullptr, &s));
    TEST_ASSERT_EQUAL(BRUNO_MALFORMED, brunoParseSaid("{}", nullptr));
}

// ------------------------------------------------------------ bounds

void test_an_oversized_sentence_is_truncated_not_overflowed(void) {
    // The host truncates to SAID_MAX, so this should not happen - which is
    // exactly why it is worth pinning. A host that changes its limit, or a
    // hand-written line on the stream, must not be able to write past the
    // buffer.
    char line[512];
    char body[400];
    memset(body, 'x', sizeof(body) - 1);
    body[sizeof(body) - 1] = 0;
    snprintf(line, sizeof(line),
             "{\"t\":\"said\",\"v\":3,\"i\":\"w2:p1\",\"b\":\"%s\"}", body);

    TEST_ASSERT_EQUAL(BRUNO_SAID, brunoParseSaid(line, &s));
    TEST_ASSERT_EQUAL(BRUNO_SAID_MAX, strlen(s.body));
    TEST_ASSERT_TRUE(s.truncated);
}

void test_a_sentence_exactly_at_the_limit_is_not_flagged_truncated(void) {
    // The off-by-one. A sentence that exactly fits arrived whole, and
    // claiming otherwise would put a cut marker on a complete answer.
    char line[512];
    char body[BRUNO_SAID_MAX + 1];
    memset(body, 'y', BRUNO_SAID_MAX);
    body[BRUNO_SAID_MAX] = 0;
    snprintf(line, sizeof(line),
             "{\"t\":\"said\",\"v\":3,\"i\":\"w2:p1\",\"b\":\"%s\"}", body);

    TEST_ASSERT_EQUAL(BRUNO_SAID, brunoParseSaid(line, &s));
    TEST_ASSERT_EQUAL(BRUNO_SAID_MAX, strlen(s.body));
    TEST_ASSERT_FALSE(s.truncated);
}

void test_an_over_long_pane_id_is_truncated_not_overflowed(void) {
    char line[256];
    snprintf(line, sizeof(line),
             "{\"t\":\"said\",\"v\":3,\"i\":\"%s\",\"b\":\"x\"}",
             "w123456789012345678901234567890:p1");
    TEST_ASSERT_EQUAL(BRUNO_SAID, brunoParseSaid(line, &s));
    TEST_ASSERT_EQUAL(SHEPHERD_PANE_LEN - 1, strlen(s.pane));
}

void test_a_second_frame_replaces_the_first_entirely(void) {
    // clear() runs before anything is written, so a short sentence following
    // a long one cannot leave the tail of the long one behind.
    brunoParseSaid("{\"t\":\"said\",\"v\":3,\"i\":\"w1:p1\","
                   "\"b\":\"a considerably longer first sentence\"}", &s);
    TEST_ASSERT_EQUAL(BRUNO_SAID, brunoParseSaid(
        "{\"t\":\"said\",\"v\":3,\"i\":\"w2:p1\",\"b\":\"short\"}", &s));
    TEST_ASSERT_EQUAL_STRING("short", s.body);
    TEST_ASSERT_EQUAL_STRING("w2:p1", s.pane);
}

void test_the_struct_is_small_enough_to_be_a_local(void) {
    // ShepherdFrame had to become static after a 6604-byte local blew the
    // 8192-byte loopTask stack. This one is ~140 bytes and can stay a local,
    // which is worth stating so nobody copies the static-by-default habit
    // without knowing why it exists.
    TEST_ASSERT_TRUE(sizeof(BrunoSaid) < 256);
}

int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_a_said_frame_is_parsed);
    RUN_TEST(test_an_announcement_with_no_sentence_is_valid);
    RUN_TEST(test_a_missing_body_field_is_the_same_as_an_empty_one);
    RUN_TEST(test_utf8_in_the_sentence_survives);
    RUN_TEST(test_the_shared_parser_does_not_know_said);
    RUN_TEST(test_bruno_does_not_claim_a_snapshot_or_a_detail);
    RUN_TEST(test_upstreams_own_protocol_passes_through);
    RUN_TEST(test_a_newer_relay_is_refused_rather_than_guessed_at);
    RUN_TEST(test_a_missing_version_is_refused);
    RUN_TEST(test_an_announcement_with_no_pane_is_malformed);
    RUN_TEST(test_junk_is_not_mine_rather_than_a_crash);
    RUN_TEST(test_null_input_is_malformed_not_a_crash);
    RUN_TEST(test_an_oversized_sentence_is_truncated_not_overflowed);
    RUN_TEST(test_a_sentence_exactly_at_the_limit_is_not_flagged_truncated);
    RUN_TEST(test_an_over_long_pane_id_is_truncated_not_overflowed);
    RUN_TEST(test_a_second_frame_replaces_the_first_entirely);
    RUN_TEST(test_the_struct_is_small_enough_to_be_a_local);
    UNITY_END();
    return 0;
}
