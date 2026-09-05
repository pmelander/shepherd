// The key lock, tested off the board.
//
// The thing being pinned is a safety property — "a stray press cannot
// approve" — and the only way to test that on hardware is to put the device
// in a pocket and go for a walk. So the state machine is dependency-free and
// the clock is a parameter.
//
//   pio test -e native

#include <unity.h>

#include "shepherd_lock.h"

static ShepherdLock lk;

void setUp(void) {
    lk = ShepherdLock();
    lk.begin(1000);
}
void tearDown(void) {}

void test_a_fresh_device_is_locked(void) {
    // It comes back from a flat battery in your pocket, not on your desk.
    TEST_ASSERT_TRUE(lk.locked(1000));
    TEST_ASSERT_FALSE(lk.accept(1000));
}

void test_the_chord_unlocks_and_the_same_chord_puts_it_away(void) {
    TEST_ASSERT_FALSE(lk.toggle(2000));      // returns the NEW locked state
    TEST_ASSERT_TRUE(lk.accept(2000));
    TEST_ASSERT_TRUE(lk.toggle(3000));
    TEST_ASSERT_FALSE(lk.accept(3000));
}

void test_it_relocks_itself_after_the_idle_window(void) {
    lk.toggle(2000);
    TEST_ASSERT_TRUE(lk.accept(2000 + SHEPHERD_LOCK_IDLE_MS - 1));
    // That key reset the timer, so the window runs from it and not from the
    // unlock.
    TEST_ASSERT_FALSE(lk.locked(2000 + SHEPHERD_LOCK_IDLE_MS + 1));
    TEST_ASSERT_TRUE(lk.locked(2000 + 2 * SHEPHERD_LOCK_IDLE_MS));
}

void test_an_untouched_device_locks_without_anyone_pressing_anything(void) {
    // The pocket case exactly: nothing is pressed, so nothing hangs a
    // re-lock off a key event. Asking has to be what advances the timer.
    lk.toggle(2000);
    TEST_ASSERT_TRUE(lk.locked(2000 + SHEPHERD_LOCK_IDLE_MS));
    TEST_ASSERT_FALSE(lk.accept(2000 + SHEPHERD_LOCK_IDLE_MS));
}

void test_using_it_keeps_it_awake(void) {
    lk.toggle(0);
    uint32_t t = 0;
    for (int i = 0; i < 20; i++) {
        t += SHEPHERD_LOCK_IDLE_MS - 1;
        TEST_ASSERT_TRUE_MESSAGE(lk.accept(t), "a key inside the window must act");
    }
}

void test_the_chord_still_works_on_an_idle_locked_device(void) {
    // Otherwise the only way back in from an auto-lock would be a reset.
    lk.toggle(2000);
    TEST_ASSERT_TRUE(lk.locked(2000 + SHEPHERD_LOCK_IDLE_MS));
    TEST_ASSERT_FALSE(lk.toggle(2000 + SHEPHERD_LOCK_IDLE_MS));
    TEST_ASSERT_TRUE(lk.accept(2000 + SHEPHERD_LOCK_IDLE_MS));
}

void test_the_millis_wrap_does_not_unlock_the_device(void) {
    // millis() wraps every ~49.7 days. A device left on a shelf crosses it,
    // and a naive (now - last > idle) on signed or promoted types can go
    // negative there — which would hold the lock OPEN across the wrap.
    const uint32_t nearMax = 0xFFFFFFFFu - 5000;
    lk = ShepherdLock();
    lk.begin(nearMax);
    lk.toggle(nearMax);                       // unlocked just before the wrap
    TEST_ASSERT_TRUE(lk.accept(nearMax + 1000));
    // 5000ms later the counter has wrapped through zero; the idle window has
    // still not elapsed, so it must still be usable.
    TEST_ASSERT_TRUE(lk.accept((uint32_t)(nearMax + 5000)));
    // And once it genuinely has elapsed, across the wrap, it must lock.
    TEST_ASSERT_TRUE(lk.locked((uint32_t)(nearMax + 5000 + SHEPHERD_LOCK_IDLE_MS)));
}

void test_the_idle_window_is_short_enough_to_matter(void) {
    // The gap being defended is "put it away, a prompt arrives, cloth presses
    // y". A generous timeout would leave that gap wide open.
    TEST_ASSERT_TRUE(SHEPHERD_LOCK_IDLE_MS <= 60000);
}

int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_a_fresh_device_is_locked);
    RUN_TEST(test_the_chord_unlocks_and_the_same_chord_puts_it_away);
    RUN_TEST(test_it_relocks_itself_after_the_idle_window);
    RUN_TEST(test_an_untouched_device_locks_without_anyone_pressing_anything);
    RUN_TEST(test_using_it_keeps_it_awake);
    RUN_TEST(test_the_chord_still_works_on_an_idle_locked_device);
    RUN_TEST(test_the_millis_wrap_does_not_unlock_the_device);
    RUN_TEST(test_the_idle_window_is_short_enough_to_matter);
    return UNITY_END();
}
