// Bluetooth interface of a real CT002/CT003, so the Marstek app can add the
// emulator through its regular "Add device" flow and read live data over BLE.
//
// A GATT service FF00 with FF01 (commands in), FF02 (replies out, notify) and
// FF06 (declared like on the meter, unused), advertised as MST-TPM_xxxx
// (CT002) or MST-SMR_xxxx (CT003). The protocol itself lives in
// ble_protocol.{h,cpp}; this component only moves bytes between the GATT
// server and that layer, and fills its snapshots from the CT002 component.
//
// ESPHome-only — the Python stack has no BLE peripheral. On by default on
// ESP32 (`bluetooth: false` leaves it out); gated by USE_CT002_BLUETOOTH, set
// from _to_code_bluetooth in ct002/__init__.py.
#pragma once

#include "esphome/core/defines.h"

#ifdef USE_CT002_BLUETOOTH

#include <cstdint>
#include <deque>
#include <span>
#include <string>
#include <vector>

#include "esphome/components/esp32_ble/ble.h"
#include "esphome/components/esp32_ble_server/ble_characteristic.h"
#include "esphome/components/esp32_ble_server/ble_server.h"
#include "esphome/components/esp32_ble_server/ble_service.h"
#include "esphome/core/component.h"

#include "ble_protocol.h"
#include "ct002.h"

namespace esphome {
namespace ct002 {
namespace bluetooth {

class BluetoothComponent : public Component {
 public:
  void setup() override;
  void loop() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::AFTER_BLUETOOTH; }

  void set_ct002(CT002Component *c) { this->ct002_ = c; }
  void set_ble(esp32_ble::ESP32BLE *ble) { this->ble_ = ble; }
  // false: answer the app's Wi-Fi step but stay on the configured network.
  void set_allow_wifi_change(bool allow) { this->wifi_changes_allowed_ = allow; }

  // Settle the device ID and hand the advertised name to esp32_ble. Called
  // from the generated setup code, after the CT002 configuration is applied
  // and before esp32_ble brings the stack up (it reads the name then). A CT
  // MAC applied later (marstek_registration) renames the device from loop().
  void configure_identity();

  // Registered with esp32_ble for the negotiated MTU and disconnects.
  void gatts_event_handler(esp_gatts_cb_event_t event, esp_gatt_if_t gatts_if, esp_ble_gatts_cb_param_t *param);

 protected:
  void create_service_();
  void set_name_(const std::string &id);
  // Re-advertise under a new name when the CT MAC changed after boot.
  void follow_identity_();
  void on_write_(std::span<const uint8_t> data);
  void process_frame_(const ble::Frame &frame);
  // Try the network the app sent; keep it only once connected (see .cpp).
  void apply_wifi_(const ble::WifiCredentials &wifi);
  void check_wifi_attempt_();
  ble::Snapshot snapshot_() const;
  // CT MAC when one is set (configured, or applied by marstek_registration),
  // else the identity settled at boot.
  std::string device_id_() const;

  CT002Component *ct002_{nullptr};
  esp32_ble::ESP32BLE *ble_{nullptr};
  esp32_ble_server::BLEService *service_{nullptr};
  esp32_ble_server::BLECharacteristic *command_{nullptr};
  esp32_ble_server::BLECharacteristic *reply_{nullptr};
  esp32_ble_server::BLECharacteristic *aux_{nullptr};

  ble::Model model_{ble::Model::CT002};
  std::string boot_id_;
  // The radio's Bluetooth address, as 12 lowercase hex characters. The app
  // reconnects to a known meter by its MAC and drops a connection whose
  // address differs, so this has to equal the device ID.
  std::string bt_address_;
  // ct_mac could not become the Bluetooth address (not a unicast MAC, or
  // refused); reported from setup(), where logging works.
  bool bt_address_rejected_{false};
  // The ID the current advertised name was made from.
  std::string named_id_;
  // esp32_ble keeps a pointer to this, so it is a fixed buffer that lives as
  // long as the component. BLE names are at most 20 characters.
  char name_[21]{};
  ble::FrameAssembler assembler_;
  ble::Responder responder_;
  // esp32_ble delivers GATT events (writes included) from its loop(); frames
  // are queued there and answered from this component's loop().
  std::deque<ble::Frame> pending_;
  uint16_t mtu_{23};
  bool wifi_changes_allowed_{true};
  bool advertising_started_{false};
  // A Wi-Fi network from the app being tried; saved only once connected.
  bool wifi_attempt_{false};
  std::string wifi_ssid_;
  std::string wifi_password_;
};

}  // namespace bluetooth
}  // namespace ct002
}  // namespace esphome

#endif  // USE_CT002_BLUETOOTH
