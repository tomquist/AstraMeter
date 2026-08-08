// The dashboard's httpd-task → main-loop write handover (write_slot.h).
//
// dashboard.cpp itself cannot be built for the host platform — ESPHome has no
// web server there — so the handover lives in its own header precisely so this
// file can drive both sides of it, including with real threads.

#include <atomic>
#include <chrono>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include <gtest/gtest.h>

#include "esphome/components/ct002/write_slot.h"

namespace {

using esphome::ct002::dashboard::WriteSlot;

using Slot = WriteSlot<std::string>;

TEST(WriteSlot, ReserveTakeCompleteIsTheHappyPath) {
  Slot slot;
  const uint32_t ticket = slot.reserve("manual_target");
  ASSERT_NE(ticket, 0u);

  std::string taken;
  uint32_t taken_ticket = 0;
  ASSERT_TRUE(slot.take(&taken, &taken_ticket));
  EXPECT_EQ(taken, "manual_target");
  EXPECT_EQ(taken_ticket, ticket);

  bool applied = false;
  EXPECT_FALSE(slot.result(ticket, &applied));  // not finished yet
  slot.complete(ticket, true);
  EXPECT_EQ(slot.completed(), ticket);
  ASSERT_TRUE(slot.result(ticket, &applied));
  EXPECT_TRUE(applied);
}

TEST(WriteSlot, NotAppliedIsReportedAsSuchRatherThanAsATimeout) {
  Slot slot;
  const uint32_t ticket = slot.reserve("manual_target");
  std::string taken;
  uint32_t taken_ticket = 0;
  ASSERT_TRUE(slot.take(&taken, &taken_ticket));
  slot.complete(taken_ticket, false);

  bool applied = true;
  ASSERT_TRUE(slot.result(ticket, &applied));
  EXPECT_FALSE(applied);
}

TEST(WriteSlot, TakeOnAnEmptySlotDoesNothing) {
  Slot slot;
  std::string taken = "untouched";
  uint32_t taken_ticket = 7;
  EXPECT_FALSE(slot.take(&taken, &taken_ticket));
  EXPECT_EQ(taken, "untouched");
  EXPECT_EQ(taken_ticket, 7u);
}

TEST(WriteSlot, ASecondWriteIsRefusedWhileOneIsWaiting) {
  Slot slot;
  ASSERT_NE(slot.reserve("first"), 0u);
  EXPECT_EQ(slot.reserve("second"), 0u);
}

// The window CodeRabbit found: the slot is empty while the loop is applying,
// but the write is not finished, so it must still be closed to newcomers.
TEST(WriteSlot, ASecondWriteIsRefusedWhileTheFirstIsBeingApplied) {
  Slot slot;
  const uint32_t first = slot.reserve("first");
  std::string taken;
  uint32_t taken_ticket = 0;
  ASSERT_TRUE(slot.take(&taken, &taken_ticket));

  EXPECT_EQ(slot.reserve("second"), 0u) << "the slot is empty but the write is not done";

  slot.complete(first, true);
  EXPECT_NE(slot.reserve("second"), 0u) << "and open again once it is";
}

// A request that gave up waiting must not be handed the *next* write's answer.
TEST(WriteSlot, ATimedOutWriteNeverReadsTheNextWritesResult) {
  Slot slot;
  const uint32_t first = slot.reserve("first");
  std::string taken;
  uint32_t taken_ticket = 0;
  ASSERT_TRUE(slot.take(&taken, &taken_ticket));

  // First request times out here and withdraws — refused, it is in flight.
  EXPECT_FALSE(slot.withdraw(first));
  slot.complete(first, true);

  const uint32_t second = slot.reserve("second");
  ASSERT_NE(second, 0u);
  ASSERT_NE(second, first);

  bool applied = false;
  EXPECT_FALSE(slot.result(second, &applied))
      << "the second write has not been applied; the first one's result is not its own";
  EXPECT_EQ(slot.completed(), first);
}

// The mirror image: the timed-out request must not cancel the write that took
// its place in the slot.
TEST(WriteSlot, ATimedOutWriteCannotCancelALaterOne) {
  Slot slot;
  const uint32_t first = slot.reserve("first");
  std::string taken;
  uint32_t taken_ticket = 0;
  ASSERT_TRUE(slot.take(&taken, &taken_ticket));
  slot.complete(first, true);

  const uint32_t second = slot.reserve("second");
  ASSERT_NE(second, 0u);

  EXPECT_FALSE(slot.withdraw(first)) << "first is long gone; this must be a no-op";

  std::string still_there;
  uint32_t still_ticket = 0;
  ASSERT_TRUE(slot.take(&still_there, &still_ticket))
      << "the second write must still be waiting for the loop";
  EXPECT_EQ(still_there, "second");
  EXPECT_EQ(still_ticket, second);
}

TEST(WriteSlot, WithdrawingAWaitingWriteFreesTheSlot) {
  Slot slot;
  const uint32_t ticket = slot.reserve("first");
  EXPECT_TRUE(slot.withdraw(ticket));

  std::string taken;
  uint32_t taken_ticket = 0;
  EXPECT_FALSE(slot.take(&taken, &taken_ticket)) << "withdrawn, so there is nothing to take";
  EXPECT_NE(slot.reserve("second"), 0u) << "and the slot is usable again";
}

TEST(WriteSlot, TicketsAreNeverReused) {
  Slot slot;
  uint32_t previous = 0;
  for (int i = 0; i < 5; i++) {
    const uint32_t ticket = slot.reserve("field");
    EXPECT_NE(ticket, previous);
    EXPECT_NE(ticket, 0u);
    std::string taken;
    uint32_t taken_ticket = 0;
    ASSERT_TRUE(slot.take(&taken, &taken_ticket));
    slot.complete(taken_ticket, true);
    previous = ticket;
  }
}

// Both sides for real: a "main loop" thread against several "httpd" threads,
// each of which must be told about its own write and no one else's.
TEST(WriteSlot, ConcurrentWritersEachGetTheirOwnAnswer) {
  Slot slot;
  std::mutex lock;
  std::atomic<bool> stop{false};

  // The loop: take whatever is waiting, "apply" it, report it done. The
  // deliberate pause inside the apply is the window the races live in.
  std::thread loop([&] {
    while (!stop.load()) {
      std::string write;
      uint32_t ticket = 0;
      {
        std::lock_guard<std::mutex> guard(lock);
        if (!slot.take(&write, &ticket)) {
          // Nothing waiting. Yield rather than spin: on a single-core runner
          // this thread would otherwise starve the writers it is serving.
          std::this_thread::yield();
          continue;
        }
      }
      std::this_thread::sleep_for(std::chrono::microseconds(200));
      const bool applied = write != "unknown";
      std::lock_guard<std::mutex> guard(lock);
      slot.complete(ticket, applied);
    }
  });

  std::atomic<int> wrong_answers{0};
  std::vector<std::thread> writers;
  for (int i = 0; i < 4; i++) {
    writers.emplace_back([&, i] {
      for (int n = 0; n < 40; n++) {
        // Half the writes are ones the loop reports as not applied, so a
        // crossed answer shows up as a wrong bool and not just a wrong ticket.
        const std::string field = (i + n) % 2 == 0 ? "manual_target" : "unknown";
        uint32_t ticket = 0;
        {
          std::lock_guard<std::mutex> guard(lock);
          ticket = slot.reserve(field);
        }
        if (ticket == 0) {  // busy — exactly the 503 the handler answers
          std::this_thread::yield();
          continue;
        }
        bool applied = false;
        bool answered = false;
        for (int spin = 0; spin < 10000 && !answered; spin++) {
          if (slot.completed() != ticket) {
            std::this_thread::yield();
            continue;
          }
          std::lock_guard<std::mutex> guard(lock);
          answered = slot.result(ticket, &applied);
        }
        if (!answered) {
          std::lock_guard<std::mutex> guard(lock);
          slot.withdraw(ticket);
          continue;
        }
        if (applied != (field != "unknown")) wrong_answers++;
      }
    });
  }

  for (auto &writer : writers) writer.join();
  stop.store(true);
  loop.join();

  EXPECT_EQ(wrong_answers.load(), 0) << "a request was told about somebody else's write";
}

}  // namespace
