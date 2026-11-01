// The cloud reporter's main-loop → HTTP-worker handover (cloud_http_slot.h).
//
// cloud_reporting.cpp is only built when the YAML asks for it and needs
// http_request, so the handover lives in its own header precisely so this file
// can drive both sides of it, including with real threads.

#include <atomic>
#include <mutex>
#include <string>
#include <thread>

#include <gtest/gtest.h>

#include "esphome/components/ct002/cloud_http_slot.h"

namespace {

using esphome::ct002::cloud_reporting::HttpResult;
using esphome::ct002::cloud_reporting::HttpSlot;

TEST(CloudHttpSlot, SubmitTakeFinishCollectIsTheHappyPath) {
  HttpSlot slot;
  EXPECT_FALSE(slot.busy());
  ASSERT_TRUE(slot.submit("http://a/getDateInfoeu.php", true));
  EXPECT_TRUE(slot.busy());

  std::string url;
  bool want_body = false;
  ASSERT_TRUE(slot.take(&url, &want_body));
  EXPECT_EQ(url, "http://a/getDateInfoeu.php");
  EXPECT_TRUE(want_body);

  HttpResult r;
  EXPECT_FALSE(slot.collect(&r));  // still running
  slot.finish(HttpResult{200, "_2026_10_26_09_15_48_"});
  ASSERT_TRUE(slot.collect(&r));
  EXPECT_EQ(r.status, 200);
  EXPECT_EQ(r.body, "_2026_10_26_09_15_48_");
  EXPECT_FALSE(slot.busy());
}

TEST(CloudHttpSlot, OneRequestAtATime) {
  HttpSlot slot;
  ASSERT_TRUE(slot.submit("first", false));
  EXPECT_FALSE(slot.submit("second", false));  // queued
  std::string url;
  bool want_body = false;
  ASSERT_TRUE(slot.take(&url, &want_body));
  EXPECT_FALSE(slot.submit("second", false));  // running
  slot.finish(HttpResult{200, ""});
  EXPECT_FALSE(slot.submit("second", false));  // not collected yet
  HttpResult r;
  ASSERT_TRUE(slot.collect(&r));
  EXPECT_TRUE(slot.submit("second", false));
}

TEST(CloudHttpSlot, NothingToTakeOrCollectWhenIdle) {
  HttpSlot slot;
  std::string url = "untouched";
  bool want_body = true;
  EXPECT_FALSE(slot.take(&url, &want_body));
  EXPECT_EQ(url, "untouched");
  slot.finish(HttpResult{200, "stray"});  // nothing running: ignored
  HttpResult r;
  EXPECT_FALSE(slot.collect(&r));
}

TEST(CloudHttpSlot, AFailedRequestIsCollectedAsSuch) {
  HttpSlot slot;
  ASSERT_TRUE(slot.submit("u", true));
  std::string url;
  bool want_body = false;
  ASSERT_TRUE(slot.take(&url, &want_body));
  slot.finish(HttpResult{});
  HttpResult r{200, "stale"};
  ASSERT_TRUE(slot.collect(&r));
  EXPECT_EQ(r.status, -1);
  EXPECT_TRUE(r.body.empty());
}

TEST(CloudHttpSlot, AWorkerThreadAnswersEachRequestWithItsOwnResult) {
  HttpSlot slot;
  std::mutex mutex;
  std::atomic<bool> stop{false};
  std::thread worker([&] {
    while (!stop.load()) {
      std::string url;
      bool want_body = false;
      {
        std::lock_guard<std::mutex> lock(mutex);
        if (!slot.take(&url, &want_body)) continue;
      }
      std::this_thread::yield();  // the request runs outside the lock
      std::lock_guard<std::mutex> lock(mutex);
      slot.finish(HttpResult{200, url});
    }
  });
  for (int i = 0; i < 500; ++i) {
    const std::string url = "http://a/" + std::to_string(i);
    {
      std::lock_guard<std::mutex> lock(mutex);
      ASSERT_TRUE(slot.submit(url, true));
    }
    HttpResult r;
    for (;;) {
      std::lock_guard<std::mutex> lock(mutex);
      if (slot.collect(&r)) break;
    }
    EXPECT_EQ(r.body, url);
  }
  stop.store(true);
  worker.join();
}

}  // namespace
