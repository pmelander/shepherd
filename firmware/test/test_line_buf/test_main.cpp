// Line reassembly, off the board.
//
// This exists because the buffer was too small for months and the symptom was
// a screen that stopped updating: no error, no log line, nothing on screen.
// The test that matters here is the one for a line one byte too long. Every
// other test in this file is there so that one cannot be satisfied by
// accident.
//
//   pio test -e native

#include <string.h>
#include <unity.h>

#include "line_buf.h"

// Small enough to overflow deliberately in a test, and the same shape as the
// real thing. The real sizes are asserted separately at the bottom.
static LineBuf<16> b;

void setUp(void) {
    b.reset();
    b.dropped = 0;
}
void tearDown(void) {}

// Feed a whole string. Returns how many complete lines came out, and leaves
// the last one in b.buf.
static int feed(const char* s) {
    int lines = 0;
    for (const char* p = s; *p; p++) {
        if (b.push(*p)) lines++;
    }
    return lines;
}

void test_nothing_is_ready_before_a_terminator(void) {
    TEST_ASSERT_EQUAL(0, feed("{\"t\":\"snap\"}"));
    TEST_ASSERT_EQUAL(0, b.dropped);
}

void test_a_short_line_is_delivered(void) {
    TEST_ASSERT_EQUAL(1, feed("{\"a\":1}\n"));
    TEST_ASSERT_EQUAL_STRING("{\"a\":1}", b.buf);
    TEST_ASSERT_EQUAL(0, b.dropped);
}

void test_a_line_exactly_at_the_limit_is_delivered(void) {
    // N-1 payload bytes plus the NUL is exactly full, and must still arrive.
    // An off-by-one here would drop the largest frame that legitimately fits,
    // which is the one most worth receiving.
    TEST_ASSERT_EQUAL(1, feed("123456789012345\n"));   // 15 chars, N is 16
    TEST_ASSERT_EQUAL_STRING("123456789012345", b.buf);
    TEST_ASSERT_EQUAL(15, strlen(b.buf));
    TEST_ASSERT_EQUAL(0, b.dropped);
}

void test_a_line_one_byte_over_the_limit_is_dropped_not_truncated(void) {
    // THE regression test. The old code kept the first N-1 bytes, NUL
    // terminated them, and handed that to the JSON parser, which rejected it
    // as somebody else's frame. Silent, and indistinguishable from the relay
    // having gone quiet.
    TEST_ASSERT_EQUAL(0, feed("1234567890123456\n"));  // 16 chars, N is 16
    TEST_ASSERT_EQUAL(1, b.dropped);
}

void test_a_much_longer_line_is_still_one_drop(void) {
    // The overflow is swallowed to the terminator rather than being chopped
    // into fragments that each look like a line.
    char big[200];
    memset(big, 'x', sizeof(big) - 2);
    big[sizeof(big) - 2] = '\n';
    big[sizeof(big) - 1] = 0;
    TEST_ASSERT_EQUAL(0, feed(big));
    TEST_ASSERT_EQUAL(1, b.dropped);
}

void test_the_line_after_a_dropped_one_is_delivered(void) {
    // The recovery case, and the reason `poisoned` is separate from `len`. If
    // the overflow state leaked past the terminator, one over-long frame
    // would poison every frame after it and the screen would never update
    // again.
    TEST_ASSERT_EQUAL(0, feed("1234567890123456\n"));
    TEST_ASSERT_EQUAL(1, b.dropped);
    TEST_ASSERT_EQUAL(1, feed("{\"ok\":1}\n"));
    TEST_ASSERT_EQUAL_STRING("{\"ok\":1}", b.buf);
    TEST_ASSERT_EQUAL(1, b.dropped);   // still one, not two
}

void test_drops_accumulate(void) {
    for (int i = 0; i < 4; i++) feed("1234567890123456\n");
    TEST_ASSERT_EQUAL(4, b.dropped);
}

void test_both_cr_and_lf_terminate(void) {
    TEST_ASSERT_EQUAL(1, feed("{\"a\":1}\r"));
    TEST_ASSERT_EQUAL_STRING("{\"a\":1}", b.buf);
    b.reset();
    TEST_ASSERT_EQUAL(1, feed("{\"b\":2}\n"));
    TEST_ASSERT_EQUAL_STRING("{\"b\":2}", b.buf);
}

void test_crlf_yields_one_line_not_two(void) {
    // A host writing \r\n must not produce a phantom empty line, which would
    // otherwise be handed on as a zero-length frame.
    TEST_ASSERT_EQUAL(1, feed("{\"a\":1}\r\n"));
    TEST_ASSERT_EQUAL_STRING("{\"a\":1}", b.buf);
}

void test_blank_lines_are_ignored(void) {
    TEST_ASSERT_EQUAL(0, feed("\n\n\r\r\n"));
    TEST_ASSERT_EQUAL(0, b.dropped);
}

void test_several_lines_in_one_chunk(void) {
    // A BLE notification or a serial read can deliver several frames at once.
    TEST_ASSERT_EQUAL(3, feed("{\"a\":1}\n{\"b\":2}\n{\"c\":3}\n"));
    TEST_ASSERT_EQUAL_STRING("{\"c\":3}", b.buf);
}

void test_a_partial_line_survives_between_pushes(void) {
    // Frames arrive in pieces; that is the entire job.
    TEST_ASSERT_EQUAL(0, feed("{\"t\":"));
    TEST_ASSERT_EQUAL(0, feed("\"snap\"}"));
    TEST_ASSERT_EQUAL(1, feed("\n"));
    TEST_ASSERT_EQUAL_STRING("{\"t\":\"snap\"}", b.buf);
}

void test_reset_discards_a_partial_line(void) {
    feed("{\"half\":");
    b.reset();
    TEST_ASSERT_EQUAL(1, feed("{\"whole\":1}\n"));
    TEST_ASSERT_EQUAL_STRING("{\"whole\":1}", b.buf);
}

void test_reset_clears_the_overflow_state(void) {
    feed("1234567890123456");     // poisoned, no terminator yet
    TEST_ASSERT_TRUE(b.poisoned);
    b.reset();
    TEST_ASSERT_FALSE(b.poisoned);
    TEST_ASSERT_EQUAL(1, feed("{\"ok\":1}\n"));
}

void test_the_real_buffer_holds_the_largest_frame_the_host_can_send(void) {
    // The whole point of the size. 6317 bytes was measured by building the
    // worst case through the real FrameBuilder; if the frame grows a field,
    // re-measure rather than nudging this.
    LineBuf<SHEPHERD_LINE_MAX>* real = new LineBuf<SHEPHERD_LINE_MAX>();
    for (int i = 0; i < SHEPHERD_WORST_FRAME; i++) {
        TEST_ASSERT_FALSE(real->push('x'));
    }
    TEST_ASSERT_TRUE(real->push('\n'));
    TEST_ASSERT_EQUAL(SHEPHERD_WORST_FRAME, strlen(real->buf));
    TEST_ASSERT_EQUAL(0, real->dropped);
    delete real;
}

int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_nothing_is_ready_before_a_terminator);
    RUN_TEST(test_a_short_line_is_delivered);
    RUN_TEST(test_a_line_exactly_at_the_limit_is_delivered);
    RUN_TEST(test_a_line_one_byte_over_the_limit_is_dropped_not_truncated);
    RUN_TEST(test_a_much_longer_line_is_still_one_drop);
    RUN_TEST(test_the_line_after_a_dropped_one_is_delivered);
    RUN_TEST(test_drops_accumulate);
    RUN_TEST(test_both_cr_and_lf_terminate);
    RUN_TEST(test_crlf_yields_one_line_not_two);
    RUN_TEST(test_blank_lines_are_ignored);
    RUN_TEST(test_several_lines_in_one_chunk);
    RUN_TEST(test_a_partial_line_survives_between_pushes);
    RUN_TEST(test_reset_discards_a_partial_line);
    RUN_TEST(test_reset_clears_the_overflow_state);
    RUN_TEST(test_the_real_buffer_holds_the_largest_frame_the_host_can_send);
    return UNITY_END();
}
