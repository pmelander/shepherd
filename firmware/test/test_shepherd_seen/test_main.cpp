// What the device believes has been acknowledged.
//
// Extracted into its own header specifically so these can run off the board,
// because this logic has been got wrong twice and both times the symptom was
// a notification behaving wrongly on hardware with nothing to point at.
//
//   pio test -e native

#include <unity.h>

#include "shepherd_seen.h"

static ShepherdSeen s;
static ShepherdFrame f;

void setUp(void) { s.clear(); f.clear(); }
void tearDown(void) {}

static void load(const char* line) {
    TEST_ASSERT_EQUAL(SHEPHERD_OK, shepherdParse(line, &f));
}

void test_nothing_is_seen_to_begin_with(void) {
    TEST_ASSERT_FALSE(s.has("w1:p1", SHEPHERD_SEEN_DONE));
    TEST_ASSERT_EQUAL(0, s.count);
}

void test_marking_is_per_pane_and_per_kind(void) {
    s.mark("w1:p1", SHEPHERD_SEEN_DONE);
    TEST_ASSERT_TRUE(s.has("w1:p1", SHEPHERD_SEEN_DONE));
    // A different agent has not been acknowledged...
    TEST_ASSERT_FALSE(s.has("w2:p1", SHEPHERD_SEEN_DONE));
    // ...and neither has the same agent doing a different thing. Blocking
    // after finishing is news again.
    TEST_ASSERT_FALSE(s.has("w1:p1", SHEPHERD_SEEN_BLOCKED));
}

void test_marking_twice_does_not_grow_the_set(void) {
    for (int i = 0; i < 5; i++) s.mark("w1:p1", SHEPHERD_SEEN_DONE);
    TEST_ASSERT_EQUAL(1, s.count);
}

void test_an_acknowledged_agent_does_not_mask_the_ones_behind_it(void) {
    // The second bug. Seen was ONE entry, compared against "the first done
    // agent" - and frames sort done to the front, so one acknowledged agent
    // at the top silenced every agent that finished after it.
    load("{\"t\":\"snap\",\"v\":3,\"a\":["
         "{\"i\":\"w1:p1\",\"n\":\"a\",\"s\":\"done\"},"
         "{\"i\":\"w2:p1\",\"n\":\"b\",\"s\":\"done\"},"
         "{\"i\":\"w3:p1\",\"n\":\"c\",\"s\":\"idle\"}]}");

    uint8_t kind = SHEPHERD_SEEN_NONE;
    TEST_ASSERT_EQUAL(0, s.firstUnseen(f, &kind));
    TEST_ASSERT_EQUAL(SHEPHERD_SEEN_DONE, kind);

    s.mark("w1:p1", SHEPHERD_SEEN_DONE);
    TEST_ASSERT_EQUAL(1, s.firstUnseen(f, &kind));
    TEST_ASSERT_EQUAL(SHEPHERD_SEEN_DONE, kind);

    s.mark("w2:p1", SHEPHERD_SEEN_DONE);
    TEST_ASSERT_EQUAL(-1, s.firstUnseen(f, &kind));
    TEST_ASSERT_EQUAL(SHEPHERD_SEEN_NONE, kind);
}

void test_blocked_outranks_done_even_when_done_comes_first(void) {
    load("{\"t\":\"snap\",\"v\":3,\"a\":["
         "{\"i\":\"w1:p1\",\"n\":\"a\",\"s\":\"done\"},"
         "{\"i\":\"w2:p1\",\"n\":\"b\",\"s\":\"blocked\",\"q\":\"ok?\",\"r\":\"r2\"}]}");
    uint8_t kind = SHEPHERD_SEEN_NONE;
    TEST_ASSERT_EQUAL(1, s.firstUnseen(f, &kind));
    TEST_ASSERT_EQUAL(SHEPHERD_SEEN_BLOCKED, kind);

    // Acknowledging the blocked one falls through to the done one, rather
    // than going quiet altogether.
    s.mark("w2:p1", SHEPHERD_SEEN_BLOCKED);
    TEST_ASSERT_EQUAL(0, s.firstUnseen(f, &kind));
    TEST_ASSERT_EQUAL(SHEPHERD_SEEN_DONE, kind);
}

void test_the_same_agent_blocking_after_finishing_is_news_again(void) {
    load("{\"t\":\"snap\",\"v\":3,\"a\":["
         "{\"i\":\"w1:p1\",\"n\":\"a\",\"s\":\"done\"}]}");
    uint8_t kind = SHEPHERD_SEEN_NONE;
    s.mark("w1:p1", SHEPHERD_SEEN_DONE);
    TEST_ASSERT_EQUAL(-1, s.firstUnseen(f, &kind));

    f.clear();
    load("{\"t\":\"snap\",\"v\":3,\"a\":["
         "{\"i\":\"w1:p1\",\"n\":\"a\",\"s\":\"blocked\",\"q\":\"ok?\",\"r\":\"r1\"}]}");
    TEST_ASSERT_EQUAL(0, s.firstUnseen(f, &kind));
    TEST_ASSERT_EQUAL(SHEPHERD_SEEN_BLOCKED, kind);
}

void test_an_idle_or_working_herd_wants_nothing(void) {
    load("{\"t\":\"snap\",\"v\":3,\"a\":["
         "{\"i\":\"w1:p1\",\"n\":\"a\",\"s\":\"idle\"},"
         "{\"i\":\"w2:p1\",\"n\":\"b\",\"s\":\"working\"}]}");
    uint8_t kind = SHEPHERD_SEEN_BLOCKED;   // seed with the wrong answer
    TEST_ASSERT_EQUAL(-1, s.firstUnseen(f, &kind));
    TEST_ASSERT_EQUAL(SHEPHERD_SEEN_NONE, kind);
}

void test_a_blocked_agent_with_no_question_is_not_attention(void) {
    // Consistent with firstShowable: there is nothing to put on screen and
    // nothing to press, so lighting up would say "look at me" about a screen
    // that would say nothing back.
    load("{\"t\":\"snap\",\"v\":3,\"a\":["
         "{\"i\":\"w1:p1\",\"n\":\"a\",\"s\":\"blocked\"}]}");
    uint8_t kind = SHEPHERD_SEEN_NONE;
    TEST_ASSERT_EQUAL(-1, s.firstUnseen(f, &kind));
}

void test_the_set_is_bounded_and_drops_the_oldest(void) {
    for (int i = 0; i < SHEPHERD_MAX_AGENTS + 4; i++) {
        char pane[SHEPHERD_PANE_LEN];
        snprintf(pane, sizeof(pane), "w%d:p1", i);
        s.mark(pane, SHEPHERD_SEEN_DONE);
    }
    TEST_ASSERT_EQUAL(SHEPHERD_MAX_AGENTS, s.count);
    // The oldest four were dropped; the newest survive.
    TEST_ASSERT_FALSE(s.has("w0:p1", SHEPHERD_SEEN_DONE));
    TEST_ASSERT_TRUE(s.has("w15:p1", SHEPHERD_SEEN_DONE));
}

void test_marking_junk_is_ignored_rather_than_stored(void) {
    s.mark(nullptr, SHEPHERD_SEEN_DONE);
    s.mark("", SHEPHERD_SEEN_DONE);
    TEST_ASSERT_EQUAL(0, s.count);
    TEST_ASSERT_FALSE(s.has(nullptr, SHEPHERD_SEEN_DONE));
    TEST_ASSERT_FALSE(s.has("", SHEPHERD_SEEN_DONE));
}

int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_nothing_is_seen_to_begin_with);
    RUN_TEST(test_marking_is_per_pane_and_per_kind);
    RUN_TEST(test_marking_twice_does_not_grow_the_set);
    RUN_TEST(test_an_acknowledged_agent_does_not_mask_the_ones_behind_it);
    RUN_TEST(test_blocked_outranks_done_even_when_done_comes_first);
    RUN_TEST(test_the_same_agent_blocking_after_finishing_is_news_again);
    RUN_TEST(test_an_idle_or_working_herd_wants_nothing);
    RUN_TEST(test_a_blocked_agent_with_no_question_is_not_attention);
    RUN_TEST(test_the_set_is_bounded_and_drops_the_oldest);
    RUN_TEST(test_marking_junk_is_ignored_rather_than_stored);
    return UNITY_END();
}
