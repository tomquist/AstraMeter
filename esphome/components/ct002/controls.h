// Validation for the dashboard's write path.
//
// This is the C++ side of `_CONTROL_RANGES` / `_CONTROL_SCALE` /
// `_coerce_control_value` in `src/astrameter/web_server.py`, and the bounds
// MUST stay identical on both. The CT002 setters do not bound their inputs —
// the ranges live in the MQTT command handlers — so a value one stack accepts
// and the other rejects would be settable here and then silently reverted the
// next time the broker replays its retained command.
//
// Dependency-free (only <string>) so the host gtest can hold the two tables
// against each other.
#pragma once

#include <string>
#include <vector>

namespace esphome {
namespace ct002 {
namespace controls {

/// Whether a Content-Type header declares JSON, comparing the parsed media
/// type rather than searching the raw value.
///
/// This is what keeps a cross-origin write off the dashboard, so it has to
/// match how a *browser* reads the header: only the essence — the part before
/// the first ';', trimmed and case-insensitive — decides whether the request
/// is sent without a preflight. `text/plain; x=application/json` is plain text
/// to the browser and travels with no preflight, so a `find()` on the raw
/// header would wave through exactly the request this exists to stop.
///
/// Mirrors `_requires_json_content_type` in `src/astrameter/web_server.py`,
/// which compares `request.content_type` for the same reason.
bool is_json_content_type(const std::string &header);

/// Whether *host* — a raw `Host` header value — is an address this device may
/// answer under, given the operator's extra *allowed* names.
///
/// The defence against DNS rebinding, which the content-type check above
/// cannot cover: an attacker who answers a lookup for their own name with this
/// device's address makes their page *same-origin* with it, at which point any
/// content type is theirs to send and the reply is theirs to read. The one
/// thing they cannot forge is the name in the header, because the browser
/// copies it from the URL and the URL has to carry a name their nameserver is
/// asked about. So an IP literal (no lookup to answer), the names that resolve
/// without a nameserver, and whatever the operator listed are the whole
/// allowlist.
///
/// Mirrors `is_allowed_host` in `src/astrameter/web_server.py`; the two must
/// accept the same addresses (see AGENTS.md — the write path has parity).
bool is_allowed_host(const std::string &host, const std::vector<std::string> &allowed);

/// One control value as it arrived on the wire, after JSON typing.
struct ControlValue {
  bool is_bool{false};
  bool flag{false};
  float number{0.0f};
};

/// True when *field* is a per-battery control this firmware knows.
bool is_consumer_field(const std::string &field);

/// True when *field* is a device-wide control this firmware knows.
bool is_device_field(const std::string &field);

/// Whether *field* is a button rather than a setting. A button write carries
/// no value, and nothing about it is worth mirroring onto a retained topic.
bool is_device_button(const std::string &field);

/// Validate and scale *value* for *field*, mirroring _coerce_control_value.
///
/// Returns an empty string on success (with *value* scaled to the unit the
/// setter expects), or the message to report back — worded like the Python
/// one, since the page shows whichever backend answered it verbatim.
std::string coerce_consumer_control(const std::string &field, ControlValue &value);

}  // namespace controls
}  // namespace ct002
}  // namespace esphome
