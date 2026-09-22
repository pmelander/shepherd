// Bruno's one-bubble policy, off the board.
//
// The rule under test: blocked holds the bubble, completions queue behind it,
// and every finish still gets its turn. Decided rather than invented - it
// agrees with shepherd_seen.h, which already put blocked ahead of done on the
// pocket device.
//
// This is the part of the UI with decisions in it, which is why it lives in a
// header with no M5 types and gets tested here. The pixels are not testable
// off the board; the policy is, and the policy is what will be wrong.
//
//   pio test -e native

#include <string.h>
#include <unity.h>

#include "bruno_view.h"

static ShepherdFrame f;
static BrunoQueue q;
static BrunoView v;

void setUp(void) {
    f.clear();
    q.clear();
    v.clear();
}
void tearDown(void) {}

static void load(const char* line) {
    TEST_ASSERT_EQUAL(SHEPHERD_OK, shepherdParse(line, &f));
}

static void decide(uint32_t now = 1000) {
    brunoDecide(f, q, now, /*haveFrame=*/true, /*badVersion=*/false, &v);
}

// ------------------------------------------------------------ the plain moods

void test_an_empty_quiet_herd_potters(void) {
    load("{\"t\":\"snap\",\"v\":3,\"a\":[]}");
    decide();
    TEST_ASSERT_EQUAL(BRUNO_MOOD_POTTER, v.mood);
}

void test_an_idle_herd_potters(void) {
    load("{\"t\":\"snap\",\"v\":3,\"a\":["
         "{\"i\":\"w1:p1\",\"n\":\"a\",\"s\":\"idle\"},"
         "{\"i\":\"w2:p1\",\"n\":\"b\",\"s\":\"idle\"}]}");
    decide();
    TEST_ASSERT_EQUAL(BRUNO_MOOD_POTTER, v.mood);
    TEST_ASSERT_EQUAL(0, v.working);
}

void test_a_working_agent_makes_him_busy(void) {
    load("{\"t\":\"snap\",\"v\":3,\"a\":["
         "{\"i\":\"w1:p1\",\"n\":\"a\",\"s\":\"working\"},"
         "{\"i\":\"w2:p1\",\"n\":\"b\",\"s\":\"idle\"}]}");
    decide();
    TEST_ASSERT_EQUAL(BRUNO_MOOD_BUSY, v.mood);
    TEST_ASSERT_EQUAL(1, v.working);
}

void test_a_blocked_agent_puts_its_question_in_the_bubble(void) {
    load("{\"t\":\"snap\",\"v\":3,\"a\":["
         "{\"i\":\"w1:p1\",\"n\":\"a\",\"s\":\"working\"},"
         "{\"i\":\"w2:p1\",\"n\":\"b\",\"s\":\"blocked\",\"q\":\"Allow force push?\",\"r\":\"d1\"}]}");
    decide();
    TEST_ASSERT_EQUAL(BRUNO_MOOD_ATTENTION, v.mood);
    TEST_ASSERT_EQUAL_STRING("w2:p1", v.pane);
    TEST_ASSERT_EQUAL_STRING("Allow force push?", v.text);
    TEST_ASSERT_EQUAL(1, v.blocked);
}

// ------------------------------------------------------------- THE rule

void test_blocked_outranks_a_waiting_completion(void) {
    // The whole decision. Someone waiting ON you beats something waiting FOR
    // you, which is the same call shepherd_seen.h makes for the pocket device.
    q.push("w9:p1", "shipped the migration");
    load("{\"t\":\"snap\",\"v\":3,\"a\":["
         "{\"i\":\"w2:p1\",\"n\":\"b\",\"s\":\"blocked\",\"q\":\"Allow rm -rf?\",\"r\":\"d1\"}]}");
    decide();
    TEST_ASSERT_EQUAL(BRUNO_MOOD_ATTENTION, v.mood);
    TEST_ASSERT_EQUAL_STRING("w2:p1", v.pane);
    // And the finish is not lost, only delayed.
    TEST_ASSERT_EQUAL(1, q.count);
}

void test_a_blocked_agent_takes_the_bubble_off_a_celebration(void) {
    // Something that got stuck while you were reading a finish is still the
    // thing that cannot proceed without you.
    q.push("w9:p1", "all done");
    load("{\"t\":\"snap\",\"v\":3,\"a\":[]}");
    decide(1000);
    TEST_ASSERT_EQUAL(BRUNO_MOOD_CELEBRATE, v.mood);
    TEST_ASSERT_TRUE(q.holding);

    f.clear();
    load("{\"t\":\"snap\",\"v\":3,\"a\":["
         "{\"i\":\"w2:p1\",\"n\":\"b\",\"s\":\"blocked\",\"q\":\"ok?\",\"r\":\"d1\"}]}");
    decide(1500);                       // well inside the celebrate window
    TEST_ASSERT_EQUAL(BRUNO_MOOD_ATTENTION, v.mood);
    TEST_ASSERT_FALSE(q.holding);
    // And it was interrupted, not spent: the finish goes back in the queue.
    TEST_ASSERT_EQUAL(1, q.count);
}

void test_a_displaced_celebration_gets_its_turn_back(void) {
    // Found by driving the real device: a completion that happened to be
    // mid-show when an agent blocked was released and never re-queued, so it
    // lost its turn entirely. That quietly contradicted "delayed, never
    // dropped", which is the whole promise the blocked-first rule rests on.
    q.push("w1:p1", "the one that was interrupted");
    q.push("w2:p1", "the one behind it");
    load("{\"t\":\"snap\",\"v\":3,\"a\":[]}");

    decide(1000);
    TEST_ASSERT_EQUAL(BRUNO_MOOD_CELEBRATE, v.mood);
    TEST_ASSERT_EQUAL_STRING("w1:p1", v.pane);

    // Something blocks a second in.
    f.clear();
    load("{\"t\":\"snap\",\"v\":3,\"a\":["
         "{\"i\":\"w9:p1\",\"n\":\"z\",\"s\":\"blocked\",\"q\":\"ok?\",\"r\":\"d1\"}]}");
    decide(2000);
    TEST_ASSERT_EQUAL(BRUNO_MOOD_ATTENTION, v.mood);
    TEST_ASSERT_EQUAL(2, q.count);

    // Prompt dealt with. The interrupted one goes FIRST, ahead of the one it
    // was already ahead of - being interrupted must not cost it its place.
    f.clear();
    load("{\"t\":\"snap\",\"v\":3,\"a\":[]}");
    decide(3000);
    TEST_ASSERT_EQUAL(BRUNO_MOOD_CELEBRATE, v.mood);
    TEST_ASSERT_EQUAL_STRING("the one that was interrupted", v.text);
    decide(3000 + BRUNO_CELEBRATE_MS);
    TEST_ASSERT_EQUAL_STRING("the one behind it", v.text);
}

void test_the_queue_drains_once_nothing_is_blocked(void) {
    q.push("w1:p1", "first");
    q.push("w2:p1", "second");
    load("{\"t\":\"snap\",\"v\":3,\"a\":[]}");

    decide(1000);
    TEST_ASSERT_EQUAL(BRUNO_MOOD_CELEBRATE, v.mood);
    TEST_ASSERT_EQUAL_STRING("w1:p1", v.pane);
    TEST_ASSERT_EQUAL_STRING("first", v.text);

    // Still inside the window: the same one holds.
    decide(1000 + BRUNO_CELEBRATE_MS - 1);
    TEST_ASSERT_EQUAL_STRING("w1:p1", v.pane);

    // Past it: the next takes its turn.
    decide(1000 + BRUNO_CELEBRATE_MS);
    TEST_ASSERT_EQUAL(BRUNO_MOOD_CELEBRATE, v.mood);
    TEST_ASSERT_EQUAL_STRING("w2:p1", v.pane);
    TEST_ASSERT_EQUAL_STRING("second", v.text);
}

void test_a_completion_is_delayed_by_a_prompt_but_never_dropped(void) {
    // The cost of the rule, stated as a test so it is a choice rather than a
    // surprise: a finish can wait minutes behind a prompt. It must still
    // arrive.
    q.push("w9:p1", "the thing you were waiting for");
    load("{\"t\":\"snap\",\"v\":3,\"a\":["
         "{\"i\":\"w2:p1\",\"n\":\"b\",\"s\":\"blocked\",\"q\":\"ok?\",\"r\":\"d1\"}]}");
    for (uint32_t t = 1000; t < 600000; t += 30000) decide(t);
    TEST_ASSERT_EQUAL(BRUNO_MOOD_ATTENTION, v.mood);
    TEST_ASSERT_EQUAL(1, q.count);

    f.clear();
    load("{\"t\":\"snap\",\"v\":3,\"a\":[]}");      // the prompt is answered
    decide(600000);
    TEST_ASSERT_EQUAL(BRUNO_MOOD_CELEBRATE, v.mood);
    TEST_ASSERT_EQUAL_STRING("the thing you were waiting for", v.text);
}

// ------------------------------------------------------------- the queue

void test_a_second_announcement_for_one_agent_replaces_it(void) {
    // Same news, said better - not two turns for one finish.
    q.push("w1:p1", "first attempt");
    q.push("w1:p1", "actually, this");
    TEST_ASSERT_EQUAL(1, q.count);
    load("{\"t\":\"snap\",\"v\":3,\"a\":[]}");
    decide();
    TEST_ASSERT_EQUAL_STRING("actually, this", v.text);
}

void test_the_queue_is_bounded_and_keeps_the_newest(void) {
    for (int i = 0; i < BRUNO_QUEUE_MAX + 3; i++) {
        char pane[SHEPHERD_PANE_LEN];
        snprintf(pane, sizeof(pane), "w%d:p1", i);
        q.push(pane, "done");
    }
    TEST_ASSERT_EQUAL(BRUNO_QUEUE_MAX, q.count);
    // The oldest three fell off the front; the newest survived.
    load("{\"t\":\"snap\",\"v\":3,\"a\":[]}");
    decide();
    TEST_ASSERT_EQUAL_STRING("w3:p1", v.pane);
}

void test_an_announcement_with_nothing_to_say_still_gets_its_moment(void) {
    // A finished agent that said nothing still finished.
    q.push("w1:p1", "");
    load("{\"t\":\"snap\",\"v\":3,\"a\":[]}");
    decide();
    TEST_ASSERT_EQUAL(BRUNO_MOOD_CELEBRATE, v.mood);
    TEST_ASSERT_EQUAL_STRING("w1:p1", v.pane);
    TEST_ASSERT_EQUAL_STRING("", v.text);
}

void test_junk_is_not_queued(void) {
    q.push(nullptr, "x");
    q.push("", "x");
    TEST_ASSERT_EQUAL(0, q.count);
}

void test_a_done_agent_with_no_announcement_is_still_celebrated(void) {
    // The relay publishes the snapshot and the sentence separately, and a
    // restart loses the queue but not the herd. Bruno should not sit looking
    // bored in front of a finished agent just because it has no words.
    load("{\"t\":\"snap\",\"v\":3,\"a\":["
         "{\"i\":\"w7:p1\",\"n\":\"g\",\"s\":\"done\"}]}");
    decide();
    TEST_ASSERT_EQUAL(BRUNO_MOOD_CELEBRATE, v.mood);
    TEST_ASSERT_EQUAL_STRING("w7:p1", v.pane);
    TEST_ASSERT_EQUAL_STRING("", v.text);
}

// --------------------------------------------------------- honesty first

void test_no_frame_means_stale_not_a_calm_herd(void) {
    load("{\"t\":\"snap\",\"v\":3,\"a\":["
         "{\"i\":\"w1:p1\",\"n\":\"a\",\"s\":\"working\"}]}");
    brunoDecide(f, q, 1000, /*haveFrame=*/false, /*badVersion=*/false, &v);
    TEST_ASSERT_EQUAL(BRUNO_MOOD_STALE, v.mood);
}

void test_a_bad_version_outranks_everything(void) {
    load("{\"t\":\"snap\",\"v\":3,\"a\":["
         "{\"i\":\"w2:p1\",\"n\":\"b\",\"s\":\"blocked\",\"q\":\"ok?\",\"r\":\"d1\"}]}");
    brunoDecide(f, q, 1000, /*haveFrame=*/true, /*badVersion=*/true, &v);
    TEST_ASSERT_EQUAL(BRUNO_MOOD_BAD_VERSION, v.mood);
}

void test_a_blocked_agent_with_no_question_is_not_attention(void) {
    // Consistent with firstShowable and with Shepherd: there is nothing to put
    // in the bubble, so lighting up would say "look at me" about a screen that
    // would say nothing back.
    load("{\"t\":\"snap\",\"v\":3,\"a\":["
         "{\"i\":\"w1:p1\",\"n\":\"a\",\"s\":\"blocked\"},"
         "{\"i\":\"w2:p1\",\"n\":\"b\",\"s\":\"working\"}]}");
    decide();
    TEST_ASSERT_EQUAL(BRUNO_MOOD_BUSY, v.mood);
    TEST_ASSERT_EQUAL(1, v.blocked);      // still counted in the strip
}

void test_the_celebrate_window_survives_the_millis_wrap(void) {
    // A device left on a desk crosses it every ~49.7 days. A sloppy
    // comparison here would either end a celebration instantly or hold it
    // forever.
    const uint32_t nearWrap = 0xFFFFFF00u;
    q.push("w1:p1", "done");
    load("{\"t\":\"snap\",\"v\":3,\"a\":[]}");
    decide(nearWrap);
    TEST_ASSERT_EQUAL(BRUNO_MOOD_CELEBRATE, v.mood);

    // Just before the window closes, having wrapped past zero.
    decide((uint32_t)(nearWrap + BRUNO_CELEBRATE_MS - 1));
    TEST_ASSERT_TRUE(q.holding);
    // And just after.
    decide((uint32_t)(nearWrap + BRUNO_CELEBRATE_MS));
    TEST_ASSERT_EQUAL(BRUNO_MOOD_POTTER, v.mood);
}

int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_an_empty_quiet_herd_potters);
    RUN_TEST(test_an_idle_herd_potters);
    RUN_TEST(test_a_working_agent_makes_him_busy);
    RUN_TEST(test_a_blocked_agent_puts_its_question_in_the_bubble);
    RUN_TEST(test_blocked_outranks_a_waiting_completion);
    RUN_TEST(test_a_blocked_agent_takes_the_bubble_off_a_celebration);
    RUN_TEST(test_a_displaced_celebration_gets_its_turn_back);
    RUN_TEST(test_the_queue_drains_once_nothing_is_blocked);
    RUN_TEST(test_a_completion_is_delayed_by_a_prompt_but_never_dropped);
    RUN_TEST(test_a_second_announcement_for_one_agent_replaces_it);
    RUN_TEST(test_the_queue_is_bounded_and_keeps_the_newest);
    RUN_TEST(test_an_announcement_with_nothing_to_say_still_gets_its_moment);
    RUN_TEST(test_junk_is_not_queued);
    RUN_TEST(test_a_done_agent_with_no_announcement_is_still_celebrated);
    RUN_TEST(test_no_frame_means_stale_not_a_calm_herd);
    RUN_TEST(test_a_bad_version_outranks_everything);
    RUN_TEST(test_a_blocked_agent_with_no_question_is_not_attention);
    RUN_TEST(test_the_celebrate_window_survives_the_millis_wrap);
    UNITY_END();
    return 0;
}
