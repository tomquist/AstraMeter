// The dashboard's view of the live component, and the writes back into it.
//
// Both halves run on the main loop only — the HTTP handler hands work here
// rather than touching the consumer map from the httpd task (see dashboard.h).
#include "ct002.h"

#ifdef USE_CT002_DASHBOARD_STATE
// Compiled for a `dashboard:` build, and for the test-hooks build so the
// host-platform e2e suite can exercise the same document and the same write
// path without an HTTP server (there is no ESPHome web server for `host`).
// A plain ct002 build carries none of it.

#include <algorithm>
#include <cmath>

#include "esphome/core/application.h"
#include "esphome/core/hal.h"

namespace esphome {
namespace ct002 {

namespace {

/// A monotonic mark as a wall-clock epoch, or absent when the clock is
/// unsynced (*wall_now* == 0) or the mark was never set. Never guesses: an
/// unsynced device reports ages, not dates.
std::optional<double> wall_time_for(double wall_now, double age_seconds) {
  if (wall_now <= 0.0) return {};
  return wall_now - std::max(0.0, age_seconds);
}

}  // namespace

// Mirrors CT002.status_snapshot() / CT002.snapshot_consumer() in
// src/astrameter/ct002/ct002.py, rendered into the shared wire structs.
status::DeviceStatus CT002Component::status_snapshot(double wall_now) const {
  const double now = this->now_seconds_();
  status::DeviceStatus out;
  out.device_id = App.get_name();
  out.ct_type = this->ct_type_;
  out.ct_mac = this->ct_mac_;
  out.udp_port = this->udp_port_;
  out.wifi_rssi_dbm = this->wifi_rssi_;
  out.running = this->socket_ != nullptr;
  out.started_at = wall_time_for(wall_now, static_cast<double>(::esphome::millis()) / 1000.0);
  out.active_control = this->active_control_;
  out.consumer_ttl_s = this->consumer_ttl_seconds_;
  out.dedupe_window_s = static_cast<float>(this->dedupe_window_ms_) / 1000.0f;

  // The post-filter, pre-balancer reading the control loop last acted on —
  // the same value the UDP replies were computed from.
  out.grid = this->last_grid_power_;
  // Same rule as Python's CT002.status_snapshot: under active control the
  // device-level filtered total is the number the loop steers on, but it is
  // only written by that path — in relay mode sum the per-phase values, which
  // are recorded either way.
  if (this->active_control_ && this->last_smooth_target_.has_value()) {
    out.grid_total_w = *this->last_smooth_target_;
  } else {
    float total = 0.0f;
    for (uint8_t i = 0; i < this->num_phases_ && i < 3; i++) total += this->last_grid_power_[i];
    out.grid_total_w = total;
  }
  const optional<uint32_t> freshest = this->freshest_sensor_age_ms();
  if (freshest.has_value())
    out.grid_sample_at = wall_time_for(wall_now, static_cast<double>(*freshest) / 1000.0);
  // "Failed" here is the same condition SensorBackedPowermeter zeroes on: no
  // phase has delivered a reading inside max_sensor_age.
  out.meter_failed = !freshest.has_value() || *freshest > this->max_sensor_age_ms_;

  const PhaseReports reports = this->collect_reports_by_phase_();
  static_assert(BUCKET_COUNT == 5, "the status document carries one bucket per PhaseBucket");
  for (size_t i = 0; i < BUCKET_COUNT; i++) {
    out.buckets[i].chrg_w = reports.chrg_power[i];
    out.buckets[i].dchrg_w = reports.dchrg_power[i];
    out.buckets[i].count = reports.count[i];
    out.buckets[i].active = reports.active[i];
  }

  if (this->balancer_) out.balancer = this->balancer_->status_snapshot();

  // Sorted by id, like the Python snapshot: the page draws one card per
  // consumer in document order, and hash-map order would shuffle them on
  // every poll.
  std::vector<const Consumer *> ordered;
  ordered.reserve(this->consumers_.size());
  for (const auto &entry : this->consumers_) ordered.push_back(&entry.second);
  std::sort(ordered.begin(), ordered.end(),
            [](const Consumer *a, const Consumer *b) { return a->consumer_id < b->consumer_id; });

  out.consumers.reserve(ordered.size());
  for (const Consumer *consumer : ordered) {
    const DeviceCapabilities caps = device_capabilities(consumer->device_type);
    status::ConsumerStatus row;
    row.consumer_id = consumer->consumer_id;
    row.device_type = consumer->device_type;
    row.last_ip = consumer->last_ip;
    row.phase = consumer->phase;
    row.bucket = status::BUCKET_NAMES[bucket_index_for_phase(consumer->phase)];
    row.builtin_inverter = caps.has_builtin_inverter;
    row.ac_input = caps.has_ac_input;
    row.dc_input = caps.has_dc_input;
    row.participates = consumer->participates;
    row.reported_power_w = consumer->power;
    row.last_instructed_power_w = consumer->last_instructed_power;
    if (consumer->timestamp > 0.0) {
      const double age = std::max(0.0, now - consumer->timestamp);
      row.last_seen_age_s = static_cast<float>(age);
      row.last_seen_at = wall_time_for(wall_now, age);
    } else {
      // Stated, not inferred from the missing timestamp: a reduced document
      // has no timestamps at all, and the page must still tell a battery that
      // has never polled from one whose backend cannot date its last poll.
      row.never_reported = true;
    }
    row.poll_interval_s = consumer->poll_interval;
    row.answer_interval_s = consumer->answer_interval;
    row.ttl_s = static_cast<float>(this->consumer_ttl_for_(*consumer));
    row.expired = this->consumer_expired_(*consumer, now);
    row.active = consumer->active;
    row.manual_enabled = consumer->manual_enabled;
    row.manual_target_w = consumer->manual_target;
    row.mode = (!consumer->active || !consumer->participates) ? "inactive"
               : consumer->manual_enabled                     ? "manual"
                                                              : "auto";
    row.distribution_weight = consumer->distribution_weight;
    row.efficiency_window_weight_pct = consumer->efficiency_window_weight * 100.0f;
    row.min_dc_output_w = consumer->min_dc_output;
    row.min_dc_output_applicable = needs_dc_output_floor(consumer->device_type);
    if (this->balancer_) row.balancer = this->balancer_->snapshot_consumer(consumer->consumer_id);
    out.consumers.push_back(std::move(row));
  }
  return out;
}

// The ESPHome counterpart of PowermeterHealth: identity of the grid-power feed
// plus the filter chain applied to it. Unlike the device's `grid` block (the
// post-filter value the balancer last acted on, refreshed only when a battery
// polls) this reports the sensor feed itself, which is what "power source"
// means — and on a device nobody is polling, the only one that is live.
//
// It must never pull through the pipeline: get_powermeter_watts() advances the
// Hampel/smoothing/PID state and would inject a phantom sample into the
// control loop, the same rule health.py states on the Python side.
status::PowermeterStatus CT002Component::powermeter_status() const {
  status::PowermeterStatus out;
  if (this->power_sensor_l1_ != nullptr) out.name = this->power_sensor_l1_->get_name();
  out.kind = "SensorBackedPowermeter";
  // Innermost first, so the list reads in the order a reading travels
  // outward from the sensor — and with the same class names Python reports.
  if (this->hampel_cfg_.has_value()) out.pipeline.emplace_back("HampelPowermeter");
  if (this->smoothing_cfg_.has_value()) out.pipeline.emplace_back("SmoothedPowermeter");
  if (this->deadband_threshold_.has_value()) out.pipeline.emplace_back("DeadbandPowermeter");
  if (this->pid_cfg_.has_value()) out.pipeline.emplace_back("PidPowermeter");

  const optional<uint32_t> freshest = this->freshest_sensor_age_ms();
  std::vector<float> values;
  values.reserve(std::min<uint8_t>(this->num_phases_, 3));
  for (uint8_t i = 0; i < this->num_phases_ && i < 3; i++) {
    // Already in watts: the sensor callback applies the unit scale before
    // caching (see ct002.cpp), so scaling again here would multiply a kW
    // source by 1000 a second time.
    values.push_back(this->raw_values_[i]);
  }
  if (!freshest.has_value()) {
    // Nothing has ever arrived: no age, no values, and explicitly down —
    // this is a push source, so silence is knowledge, not ignorance.
    out.online = false;
    return out;
  }
  const float age_s = static_cast<float>(*freshest) / 1000.0f;
  const bool fresh = *freshest <= this->max_sensor_age_ms_;
  out.last_read_age_s = age_s;
  out.online = fresh;
  out.last_read_ok = fresh;
  float total = 0.0f;
  for (const float value : values) total += value;
  out.last_total_w = total;
  out.last_values_w = std::move(values);
  return out;
}

// ── write path ──────────────────────────────────────────────────────────
//
// Mirrors the _CONSUMER_SETTERS table and the device-wide branch of
// src/astrameter/web_server.py: the same field names reach the same setters,
// so a control behaves identically whichever stack is answering. The value has
// already been validated and scaled by controls::coerce_consumer_control.

bool apply_consumer_control(CT002Component *ct002, const std::string &consumer_id,
                            const std::string &field, const controls::ControlValue &value) {
  // The page only offers controls for batteries in the document it was drawn
  // from, so an id from anywhere else is either a stale tab or someone
  // poking the endpoint — and the setters below would silently create it.
  if (!ct002->knows_consumer(consumer_id)) return false;
  if (field == "manual_target") {
    ct002->set_consumer_manual_target(consumer_id, value.number);
  } else if (field == "auto_target") {
    ct002->set_consumer_auto_target(consumer_id, value.flag);
  } else if (field == "active") {
    ct002->set_consumer_active(consumer_id, value.flag);
  } else if (field == "distribution_weight") {
    ct002->set_consumer_distribution_weight(consumer_id, value.number);
  } else if (field == "efficiency_window_weight") {
    ct002->set_consumer_efficiency_window_weight(consumer_id, value.number);
  } else if (field == "min_dc_output") {
    ct002->set_consumer_min_dc_output(consumer_id, value.number);
  } else {
    return false;
  }
  return true;
}

bool apply_device_control(CT002Component *ct002, const std::string &field,
                          const controls::ControlValue &value) {
  if (field == "active_control") {
    ct002->set_active_control(value.is_bool ? value.flag : value.number != 0.0f);
  } else if (field == "force_rotation") {
    ct002->force_balancer_rotation();
  } else {
    return false;
  }
  return true;
}

}  // namespace ct002
}  // namespace esphome

#endif  // USE_CT002_DASHBOARD_STATE
