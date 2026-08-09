// Host-gcc tests for the dashboard's write-path validation. Mirrors
// _CONTROL_RANGES / _CONTROL_SCALE / _coerce_control_value in
// src/astrameter/web_server.py — the bounds must be identical on both stacks,
// or a value one accepts and the other refuses would be settable from one
// dashboard and then silently reverted by the next retained MQTT replay.
// Compiles only controls.cpp (no ESPHome deps).

#include "esphome/components/ct002/controls.h"

#include <cmath>

#include <gtest/gtest.h>

namespace esphome {
namespace ct002 {
namespace controls {
namespace {

ControlValue number(float value) {
  ControlValue out;
  out.number = value;
  return out;
}

ControlValue boolean(bool value) {
  ControlValue out;
  out.is_bool = true;
  out.flag = value;
  return out;
}

TEST(Controls, KnowsTheConsumerFields) {
  for (const char *field : {"manual_target", "auto_target", "active", "distribution_weight",
                            "efficiency_window_weight", "min_dc_output"}) {
    EXPECT_TRUE(is_consumer_field(field)) << field;
  }
  EXPECT_FALSE(is_consumer_field("nonsense"));
  // Device-wide fields are not per-battery ones, and vice versa.
  EXPECT_FALSE(is_consumer_field("active_control"));
  EXPECT_TRUE(is_device_field("active_control"));
  EXPECT_TRUE(is_device_field("force_rotation"));
  EXPECT_FALSE(is_device_field("manual_target"));
}

TEST(Controls, AcceptsValuesInsideThePythonRanges) {
  for (const auto &pair : {std::make_pair("manual_target", -10000.0f),
                           std::make_pair("manual_target", 10000.0f),
                           std::make_pair("distribution_weight", 0.0f),
                           std::make_pair("distribution_weight", 10.0f),
                           std::make_pair("min_dc_output", 0.0f),
                           std::make_pair("min_dc_output", 1000.0f)}) {
    ControlValue value = number(pair.second);
    EXPECT_EQ(coerce_consumer_control(pair.first, value), "") << pair.first << " " << pair.second;
    EXPECT_FLOAT_EQ(value.number, pair.second);
  }
}

TEST(Controls, RefusesValuesOutsideThem) {
  ControlValue value = number(10000.5f);
  EXPECT_EQ(coerce_consumer_control("manual_target", value),
            "manual_target must be between -10000 and 10000");
  value = number(10.5f);
  EXPECT_EQ(coerce_consumer_control("distribution_weight", value),
            "distribution_weight must be between 0 and 10");
  value = number(101.0f);
  EXPECT_EQ(coerce_consumer_control("efficiency_window_weight", value),
            "efficiency_window_weight must be between 0 and 100");
  value = number(-1.0f);
  EXPECT_EQ(coerce_consumer_control("min_dc_output", value),
            "min_dc_output must be between 0 and 1000");
}

TEST(Controls, RefusesANonFiniteNumber) {
  ControlValue value = number(NAN);
  EXPECT_FALSE(coerce_consumer_control("manual_target", value).empty());
}

TEST(Controls, ScalesTheEfficiencyWindowFromPercent) {
  // The wire carries a percentage, matching the MQTT entity of the same name;
  // the setter wants a fraction.
  ControlValue value = number(50.0f);
  EXPECT_EQ(coerce_consumer_control("efficiency_window_weight", value), "");
  EXPECT_FLOAT_EQ(value.number, 0.5f);
}

TEST(Controls, KeepsBooleanFieldsBoolean) {
  ControlValue value = boolean(true);
  EXPECT_EQ(coerce_consumer_control("active", value), "");
  EXPECT_TRUE(value.flag);

  value = number(1.0f);
  EXPECT_EQ(coerce_consumer_control("active", value), "active must be true or false");
  EXPECT_EQ(coerce_consumer_control("auto_target", value), "auto_target must be true or false");
}

TEST(Controls, RefusesABooleanForANumericField) {
  ControlValue value = boolean(true);
  EXPECT_EQ(coerce_consumer_control("manual_target", value), "manual_target must be a number");
}

TEST(Controls, TellsButtonsApartFromSettings) {
  // Two consumers rely on this: a button may be written with no value, and a
  // button is never mirrored onto a retained MQTT topic.
  EXPECT_TRUE(is_device_button("force_rotation"));
  EXPECT_FALSE(is_device_button("active_control"));
  EXPECT_FALSE(is_device_button("manual_target"));
  EXPECT_FALSE(is_device_button("nonsense"));
  // Every button is still a device field.
  EXPECT_TRUE(is_device_field("force_rotation"));
}

TEST(Controls, ReportsAnUnknownFieldAsUnknown) {
  ControlValue value = number(1.0f);
  EXPECT_EQ(coerce_consumer_control("nonsense", value), "Unknown device or field");
}

}  // namespace
}  // namespace controls
}  // namespace ct002
}  // namespace esphome
