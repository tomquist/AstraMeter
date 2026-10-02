#include "ble_server.h"

#ifdef USE_CT002_BLUETOOTH

#include <esp_gatt_common_api.h>

#include "esphome/components/esp32_ble/ble_uuid.h"
#include "esphome/components/esp32_ble_server/ble_2902.h"
#include "esphome/core/alloc_helpers.h"
#include "esphome/core/application.h"
#include "esphome/core/hal.h"
#include "esphome/core/log.h"

#ifdef USE_WIFI
#include "esphome/components/wifi/wifi_component.h"
#endif

namespace esphome {
namespace ct002 {
namespace bluetooth {

static const char *const TAG = "ct002.bluetooth";

using esp32_ble::ESPBTUUID;
using esp32_ble_server::BLE2902;
using esp32_ble_server::BLECharacteristic;

static constexpr uint16_t SERVICE_UUID = 0xFF00;
static constexpr uint16_t COMMAND_UUID = 0xFF01;
static constexpr uint16_t REPLY_UUID = 0xFF02;
static constexpr uint16_t AUX_UUID = 0xFF06;
// The largest MTU BLE allows. The app asks for 500 on Android; iOS
// negotiates its own. Bluedroid stays at 23 unless the local side allows more.
static constexpr uint16_t LOCAL_MTU = 517;
// ATT notification header.
static constexpr uint16_t ATT_OVERHEAD = 3;
// Upper bound on queued frames, so a misbehaving client cannot grow it.
static constexpr size_t MAX_PENDING = 8;

void BluetoothComponent::configure_identity() {
  this->model_ = ble::model_for_type(this->ct002_ != nullptr ? this->ct002_->ct_type() : "HME-4");
  std::string id = this->ct002_ != nullptr ? ble::normalize_id(this->ct002_->ct_mac()) : "";
  if (id.empty()) id = ble::normalize_id(get_mac_address());
  this->boot_id_ = id;
  this->name_ = ble::advertised_name(this->model_, id);
  if (this->ble_ != nullptr) this->ble_->set_name(this->name_.c_str());
}

void BluetoothComponent::setup() {
  if (this->ct002_ == nullptr) {
    ESP_LOGE(TAG, "No ct002 component bound");
    this->mark_failed();
    return;
  }
}

void BluetoothComponent::gatts_event_handler(esp_gatts_cb_event_t event, esp_gatt_if_t, esp_ble_gatts_cb_param_t *param) {
  if (event == ESP_GATTS_MTU_EVT && param != nullptr) {
    this->mtu_ = param->mtu.mtu;
    ESP_LOGD(TAG, "MTU %u", this->mtu_);
  } else if (event == ESP_GATTS_DISCONNECT_EVT) {
    // A half-received frame or a negotiated MTU belongs to that connection.
    this->assembler_.reset();
    this->mtu_ = 23;
  }
}

void BluetoothComponent::create_service_() {
  auto *server = esp32_ble_server::global_ble_server;
  this->service_ = server->create_service(ESPBTUUID::from_uint16(SERVICE_UUID), false);
  if (this->service_ == nullptr) {
    ESP_LOGE(TAG, "Could not create the BLE service");
    this->mark_failed();
    return;
  }
  // FF01: the app writes commands here, with or without response depending
  // on what the characteristic offers — so offer both, like the meter.
  this->command_ = this->service_->create_characteristic(ESPBTUUID::from_uint16(COMMAND_UUID),
                                                         BLECharacteristic::PROPERTY_WRITE |
                                                             BLECharacteristic::PROPERTY_WRITE_NR);
  this->command_->on_write([this](std::span<const uint8_t> data, uint16_t) { this->on_write_(data); });
  // FF02: every reply goes out as a notification.
  this->reply_ = this->service_->create_characteristic(
      ESPBTUUID::from_uint16(REPLY_UUID), BLECharacteristic::PROPERTY_READ | BLECharacteristic::PROPERTY_NOTIFY);
  this->reply_->add_descriptor(new BLE2902());  // NOLINT(cppcoreguidelines-owning-memory)
  // FF06: declared by the meter but not part of the command protocol.
  this->aux_ = this->service_->create_characteristic(
      ESPBTUUID::from_uint16(AUX_UUID), BLECharacteristic::PROPERTY_READ | BLECharacteristic::PROPERTY_WRITE |
                                            BLECharacteristic::PROPERTY_NOTIFY);
  this->aux_->add_descriptor(new BLE2902());  // NOLINT(cppcoreguidelines-owning-memory)
}

void BluetoothComponent::on_write_(std::span<const uint8_t> data) {
  for (auto &frame : this->assembler_.feed(data.data(), data.size(), millis())) {
    if (this->pending_.size() >= MAX_PENDING) {
      ESP_LOGW(TAG, "Dropping BLE command 0x%02X: too many pending", frame.cmd);
      continue;
    }
    this->pending_.push_back(std::move(frame));
  }
}

void BluetoothComponent::loop() {
  auto *server = esp32_ble_server::global_ble_server;
  if (this->ble_ == nullptr || !this->ble_->is_active() || server == nullptr || !server->is_running()) return;

  if (!this->local_mtu_set_) {
    const esp_err_t err = esp_ble_gatt_set_local_mtu(LOCAL_MTU);
    if (err != ESP_OK) ESP_LOGW(TAG, "esp_ble_gatt_set_local_mtu failed: %d", err);
    this->local_mtu_set_ = true;
  }
  if (this->service_ == nullptr) {
    this->create_service_();
    return;
  }
  if (this->service_->is_created()) {
    this->service_->start();
    return;
  }
  if (!this->service_->is_running()) return;
  if (!this->advertising_started_) {
    this->ble_->advertising_start();
    this->advertising_started_ = true;
    ESP_LOGI(TAG, "Advertising as %s", this->name_.c_str());
  }

  while (!this->pending_.empty()) {
    ble::Frame frame = std::move(this->pending_.front());
    this->pending_.pop_front();
    this->process_frame_(frame);
  }
}

void BluetoothComponent::process_frame_(const ble::Frame &frame) {
  ble::Result result = this->responder_.handle(frame, this->snapshot_());
  ESP_LOGD(TAG, "Command 0x%02X (%u payload bytes) -> %u reply bytes", frame.cmd,
           static_cast<unsigned>(frame.payload.size()), static_cast<unsigned>(result.reply.size()));

  if (result.wifi.has_value()) {
    // The device keeps the Wi-Fi from its YAML; the app only needs to see
    // its network echoed back to finish setup.
    ESP_LOGI(TAG, "App provisioned Wi-Fi '%s'; keeping the configured Wi-Fi", result.wifi->ssid.c_str());
  }

  if (!result.reply.empty()) {
    if (result.reply.size() + ATT_OVERHEAD > this->mtu_) {
      ESP_LOGW(TAG, "Reply to 0x%02X is %u bytes but the MTU is %u; the app may not receive it", frame.cmd,
               static_cast<unsigned>(result.reply.size()), this->mtu_);
    }
    this->reply_->set_value(std::move(result.reply));
    this->reply_->notify();
  }

  if (result.action == ble::Action::REBOOT) {
    ESP_LOGI(TAG, "Restart requested over Bluetooth");
    this->set_timeout("reboot", 500, []() { App.safe_reboot(); });
  }
}

std::string BluetoothComponent::device_id_() const {
  const std::string id = ble::normalize_id(this->ct002_->ct_mac());
  return id.empty() ? this->boot_id_ : id;
}

ble::Snapshot BluetoothComponent::snapshot_() const {
  ble::Snapshot s;
  s.model = this->model_;
  s.ct_type = this->ct002_->ct_type();
  s.device_id = this->device_id_();
  const std::vector<float> watts = this->ct002_->latest_grid_power();
  for (size_t i = 0; i < s.phase_w.size() && i < watts.size(); ++i) s.phase_w[i] = watts[i];
#ifdef USE_WIFI
  auto *wifi = wifi::global_wifi_component;
  if (wifi != nullptr && wifi->is_connected()) {
    s.wifi_connected = true;
    s.rssi_dbm = wifi->wifi_rssi();
    s.ssid = wifi->wifi_ssid();
  }
#endif
  for (const auto &row : this->ct002_->reporting_consumer_rows()) {
    s.batteries.push_back({row.device_type, row.consumer_id, row.last_ip, row.phase});
  }
  return s;
}

void BluetoothComponent::dump_config() {
  ESP_LOGCONFIG(TAG,
                "AstraMeter Bluetooth:\n"
                "  Name: %s\n"
                "  Device ID: %s",
                this->name_.c_str(), this->device_id_().c_str());
}

}  // namespace bluetooth
}  // namespace ct002
}  // namespace esphome

#endif  // USE_CT002_BLUETOOTH
