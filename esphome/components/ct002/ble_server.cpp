#include "ble_server.h"

#ifdef USE_CT002_BLUETOOTH

#include <cstring>

#include <esp_gap_ble_api.h>
#include <esp_heap_caps.h>
#include <esp_mac.h>

#include "esphome/components/esp32_ble/ble_uuid.h"
#include "esphome/components/esp32_ble_server/ble_2902.h"
#include "esphome/core/alloc_helpers.h"
#include "esphome/core/application.h"
#include "esphome/core/hal.h"
#include "esphome/core/helpers.h"
#include "esphome/core/log.h"

#ifdef USE_NETWORK
#include "esphome/components/network/util.h"
#endif
#ifdef USE_CT002_MARSTEK_REGISTRATION
#include "marstek_registration.h"
#endif
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
// ATT notification header. Bluedroid already accepts MTUs up to 517, so the
// app's request (500 on Android; iOS picks its own) goes through as is.
static constexpr uint16_t DEFAULT_MTU = 23;
static constexpr uint16_t ATT_OVERHEAD = 3;
// Preferences slot for the phase reversal set from the app.
static constexpr uint32_t DIRECTION_PREF_KEY = 0xC7002D17;
// Upper bound on queued frames, so a misbehaving client cannot grow it.
static constexpr size_t MAX_PENDING = 8;
// How long a network from the app gets to connect before the board goes back
// to its previous Wi-Fi (the same 30 s ESPHome's Improv allows).
static constexpr uint32_t WIFI_ATTEMPT_TIMEOUT_MS = 30000;

static std::string mac_hex(const uint8_t *mac) {
  static const char *const DIGITS = "0123456789abcdef";
  std::string out;
  for (size_t i = 0; i < 6; ++i) {
    out.push_back(DIGITS[mac[i] >> 4]);
    out.push_back(DIGITS[mac[i] & 0x0F]);
  }
  return out;
}

void BluetoothComponent::configure_identity() {
  this->model_ = ble::model_for_type(this->ct002_ != nullptr ? this->ct002_->ct_type() : "HME-4");
  // A real meter's ID, its MAC and its Bluetooth address are one value: the
  // app stores the reported MAC and later reconnects only to a device at that
  // address. So the radio takes the ID: ct_mac, else the registered MAC, else
  // one derived from the chip with the managed prefix.
  std::string configured = this->ct002_ != nullptr ? ble::normalize_id(this->ct002_->ct_mac()) : "";
#ifdef USE_CT002_MARSTEK_REGISTRATION
  // marstek_registration applies the MAC it saved over ct_mac once it starts,
  // which is after Bluetooth. Take that MAC now, while the radio can still
  // follow it, so the app finds the registered device at its address.
  if (this->registration_ != nullptr) {
    const std::string saved = ble::normalize_id(this->registration_->saved_mac());
    if (!saved.empty()) {
      configured = saved;
      this->address_from_registration_ = true;
    }
  }
#endif
  if (configured.empty()) {
    // The same prefix registration assigns, so hame-relay recognises the CT
    // the app adds, and stable, so the app finds it again after a restart.
    std::array<uint8_t, 6> chip{};
    get_mac_address_raw(chip.data());
    configured = ble::managed_id(chip);
  }
  uint8_t mac[6]{};
  if (!configured.empty()) {
    // normalize_id() guarantees 12 lowercase hex characters.
    auto nibble = [](char c) -> uint8_t { return static_cast<uint8_t>(c <= '9' ? c - '0' : c - 'a' + 10); };
    for (size_t i = 0; i < 6; ++i) mac[i] = static_cast<uint8_t>((nibble(configured[2 * i]) << 4) | nibble(configured[2 * i + 1]));
    // Must happen before the Bluetooth controller starts, which reads it then.
    if ((mac[0] & 0x01) != 0 || esp_iface_mac_addr_set(mac, ESP_MAC_BT) != ESP_OK) this->bt_address_rejected_ = true;
  }
  if (esp_read_mac(mac, ESP_MAC_BT) == ESP_OK) this->bt_address_ = mac_hex(mac);
  std::string id = configured.empty() ? this->bt_address_ : configured;
  if (id.empty()) id = ble::normalize_id(get_mac_address());
  this->boot_id_ = id;
  if (this->ct002_ != nullptr) this->ct002_->set_app_mac_fallback(id);
  this->set_name_(id);
  if (this->ble_ != nullptr) this->ble_->set_name(this->name_);
}

void BluetoothComponent::set_name_(const std::string &id) {
  const std::string name = ble::advertised_name(this->model_, id);
  std::strncpy(this->name_, name.c_str(), sizeof(this->name_) - 1);
  this->name_[sizeof(this->name_) - 1] = '\0';
  this->named_id_ = id;
}

void BluetoothComponent::follow_identity_() {
  const std::string id = this->device_id_();
  if (id == this->named_id_) return;
  this->set_name_(id);
  const esp_err_t err = esp_ble_gap_set_device_name(this->name_);
  if (err != ESP_OK) {
    ESP_LOGW(TAG, "Could not rename to %s: %d", this->name_, err);
    return;
  }
  // Restarting advertising rebuilds the packet with the new name.
  this->ble_->advertising_set_service_data_and_name(std::span<const uint8_t>{}, true);
  ESP_LOGI(TAG, "CT MAC changed; now advertising as %s", this->name_);
  if (id != this->bt_address_) {
    // The radio's address is fixed once Bluetooth runs.
#ifdef USE_CT002_MARSTEK_REGISTRATION
    // First registration on this board: the MAC is saved now, and the next
    // boot gives the radio that address. Restart once, rather than leave the
    // app unable to reach the registered device until someone does. Only
    // from a boot whose address did not already come from the registration,
    // so an address the radio refuses can't turn this into a restart loop.
    if (!this->address_from_registration_ && !this->restart_pending_ && this->registration_ != nullptr &&
        ble::normalize_id(this->registration_->saved_mac()) == id) {
      this->restart_pending_ = true;
      ESP_LOGI(TAG, "Registered as %s; restarting so the Bluetooth address follows", id.c_str());
      this->set_timeout("identity-restart", 5000, []() { App.safe_reboot(); });
      return;
    }
#endif
    ESP_LOGW(TAG,
             "The Bluetooth address stays %s until ct_mac is set to %s in the YAML; until then the "
             "app cannot reconnect to this meter over Bluetooth",
             this->bt_address_.c_str(), id.c_str());
  }
}

void BluetoothComponent::setup() {
  if (this->ct002_ == nullptr) {
    ESP_LOGE(TAG, "No ct002 component bound");
    this->mark_failed();
    return;
  }
  if (this->address_from_registration_) {
    ESP_LOGI(TAG, "Bluetooth address taken from the Marstek registration: %s", this->boot_id_.c_str());
  }
  // Restore the reversal the app set before the last restart. Only while the
  // app may change it: turning the option off returns every phase to the
  // direction its sensor reports.
  if (this->direction_changes_allowed_) {
    this->direction_pref_ = global_preferences->make_preference<uint8_t>(DIRECTION_PREF_KEY, true);
    uint8_t bits = 0;
    if (this->direction_pref_.load(&bits) && bits <= ble::DIRECTION_BITS_MASK && bits != 0) {
      this->ct002_->set_phase_reversal(bits);
      ESP_LOGI(TAG, "Phase reversal from the app restored: 0x%02X", bits);
    }
  }
  if (this->bt_address_rejected_) {
    ESP_LOGW(TAG,
             "%s cannot be the Bluetooth address (it must be a unicast MAC); the app will "
             "not reconnect to this meter after adding it",
             this->boot_id_.c_str());
  }
}

void BluetoothComponent::gatts_event_handler(esp_gatts_cb_event_t event, esp_gatt_if_t, esp_ble_gatts_cb_param_t *param) {
  if (event == ESP_GATTS_MTU_EVT && param != nullptr) {
    this->mtu_ = param->mtu.mtu;
    ESP_LOGD(TAG, "MTU %u", this->mtu_);
  } else if (event == ESP_GATTS_DISCONNECT_EVT) {
    // A half-received frame, unanswered requests, the negotiated MTU and a
    // provisioned SSID all belong to that connection.
    this->assembler_.reset();
    this->pending_.clear();
    this->responder_.reset();
    this->mtu_ = DEFAULT_MTU;
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
    // Name in the advertisement itself, not only the scan response, so even
    // a passive scan finds it. Set the payload first, then request
    // advertising: since ESPHome 2026.9 advertising is reference-counted and
    // an auto-loaded esp32_ble_server with no YAML services never asks for
    // it, so without our own request the device stays silent. (Before 2026.9
    // the payload call alone started it; requesting as well is harmless.)
    this->ble_->advertising_set_service_data_and_name(std::span<const uint8_t>{}, true);
    this->ble_->advertising_start();
    this->advertising_started_ = true;
    ESP_LOGI(TAG, "Advertising as %s; free internal heap %u bytes (largest block %u)", this->name_,
             static_cast<unsigned>(heap_caps_get_free_size(MALLOC_CAP_INTERNAL)),
             static_cast<unsigned>(heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL)));
  }
  this->follow_identity_();
  this->check_wifi_attempt_();

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
    if (this->wifi_changes_allowed_) {
      this->apply_wifi_(*result.wifi);
    } else {
      // The app only needs its network echoed back to finish setup.
      ESP_LOGI(TAG, "App sent Wi-Fi '%s'; keeping the configured Wi-Fi (set allow_wifi_change: true to apply it)",
               result.wifi->ssid.c_str());
    }
  }

  if (result.direction.has_value()) {
    // Only produced when allowed (see ble::Responder).
    this->ct002_->set_phase_reversal(*result.direction);
    if (!this->direction_pref_.save(&*result.direction)) ESP_LOGW(TAG, "Could not save the phase reversal");
    ESP_LOGI(TAG, "App set the phase reversal to 0x%02X (L1 %s, L2 %s, L3 %s)", *result.direction,
             (*result.direction & 1) ? "reversed" : "normal", (*result.direction & 2) ? "reversed" : "normal",
             (*result.direction & 4) ? "reversed" : "normal");
  } else if (frame.cmd == ble::CMD_SET_DIRECTION && this->model_ == ble::Model::CT002 &&
             !this->direction_changes_allowed_) {
    ESP_LOGI(TAG, "App asked to reverse phase directions; refused (set allow_direction_change: true to allow it)");
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

// Wi-Fi from the app, done the way ESPHome's Improv provisioning does it: try
// the network first and save it only once the board is connected, so a typo
// costs a reconnect rather than a board stuck offline. A network the board is
// already connected to is left alone, for the same reason; on a board that is
// offline (a changed router password, say) the credentials are applied.
void BluetoothComponent::apply_wifi_(const ble::WifiCredentials &wifi) {
#ifdef USE_WIFI
  auto *wifi_component = wifi::global_wifi_component;
  if (wifi_component == nullptr || wifi_component->is_disabled()) {
    ESP_LOGW(TAG, "App sent Wi-Fi '%s', but Wi-Fi is not in use here", wifi.ssid.c_str());
    return;
  }
  if (wifi_component->is_connected()) {
    char current[wifi::SSID_BUFFER_SIZE];
    if (wifi.ssid == wifi_component->wifi_ssid_to(current)) {
      ESP_LOGI(TAG, "App sent Wi-Fi '%s', which this board is already on; keeping its credentials", wifi.ssid.c_str());
      return;
    }
  }
  ESP_LOGI(TAG, "App sent Wi-Fi '%s'; trying it", wifi.ssid.c_str());
  wifi::WiFiAP sta{};
  sta.set_ssid(wifi.ssid);
  sta.set_password(wifi.password);
  this->wifi_ssid_ = wifi.ssid;
  this->wifi_password_ = wifi.password;
  this->wifi_attempt_ = true;
  wifi_component->set_sta(sta);
  wifi_component->start_connecting(sta);
  // On failure, restart rather than clear_sta() as Improv does: that empties
  // the network list, YAML networks included, until the next boot. Nothing
  // was saved yet, so the board comes back on the Wi-Fi it had before.
  this->set_timeout("wifi-attempt", WIFI_ATTEMPT_TIMEOUT_MS, [this]() {
    if (!this->wifi_attempt_) return;
    this->wifi_attempt_ = false;
    this->wifi_password_.clear();
    ESP_LOGW(TAG, "Could not connect to '%s'; restarting on the previous Wi-Fi", this->wifi_ssid_.c_str());
    App.safe_reboot();
  });
#else
  ESP_LOGW(TAG, "App sent Wi-Fi '%s', but this board has no Wi-Fi", wifi.ssid.c_str());
#endif
}

void BluetoothComponent::check_wifi_attempt_() {
#ifdef USE_WIFI
  if (!this->wifi_attempt_) return;
  auto *wifi_component = wifi::global_wifi_component;
  if (wifi_component == nullptr || !wifi_component->is_connected()) return;
  char current[wifi::SSID_BUFFER_SIZE];
  if (this->wifi_ssid_ != wifi_component->wifi_ssid_to(current)) return;
  // Connected: keep it across restarts. It takes precedence over the YAML
  // networks from now on, like Improv-provisioned credentials.
  wifi_component->save_wifi_sta(this->wifi_ssid_, this->wifi_password_);
  this->cancel_timeout("wifi-attempt");
  this->wifi_attempt_ = false;
  this->wifi_password_.clear();
  ESP_LOGI(TAG, "Connected to '%s'; saved it", this->wifi_ssid_.c_str());
#endif
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
  s.max_frame_len = this->mtu_ > ATT_OVERHEAD ? this->mtu_ - ATT_OVERHEAD : 0;
  s.direction_bits = this->ct002_->phase_reversal();
  s.direction_change_allowed = this->direction_changes_allowed_;
  const std::vector<float> watts = this->ct002_->latest_grid_power();
  for (size_t i = 0; i < s.phase_w.size() && i < watts.size(); ++i) s.phase_w[i] = watts[i];
#ifdef USE_NETWORK
  // Wi-Fi or Ethernet: a board on a cable is just as online, and reporting
  // it as down would have the app push Wi-Fi credentials at it.
  s.wifi_connected = network::is_connected();
#endif
#ifdef USE_WIFI
  auto *wifi = wifi::global_wifi_component;
  if (wifi != nullptr && wifi->is_connected()) {
    s.wifi_connected = true;
    s.rssi_dbm = wifi->wifi_rssi();
    // wifi_ssid_to(), not wifi_ssid(): the latter is deprecated since
    // ESPHome 2026.3 and gone from 2026.9.
    char ssid[wifi::SSID_BUFFER_SIZE];
    s.ssid = wifi->wifi_ssid_to(ssid);
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
                "  Device ID: %s\n"
                "  Bluetooth address: %s\n"
                "  Advertising: %s\n"
                "  Allow Wi-Fi change from the app: %s\n"
                "  Allow direction change from the app: %s (reversed phases 0x%02X)\n"
                "  Free internal heap: %u bytes",
                this->name_, this->device_id_().c_str(), this->bt_address_.c_str(),
                YESNO(this->advertising_started_), YESNO(this->wifi_changes_allowed_),
                YESNO(this->direction_changes_allowed_), this->ct002_->phase_reversal(),
                static_cast<unsigned>(heap_caps_get_free_size(MALLOC_CAP_INTERNAL)));
}

}  // namespace bluetooth
}  // namespace ct002
}  // namespace esphome

#endif  // USE_CT002_BLUETOOTH
