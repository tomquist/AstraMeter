//! CT002 UDP protocol — standalone copy of the frame builder/parser the
//! simulator uses to talk to an astrameter CT002 emulator. Mirrors
//! `src/astrameter/simulator/protocol.py` byte-for-byte (separator,
//! checksum, length encoding, firmware quirk tolerance).

pub const SOH: u8 = 0x01;
pub const STX: u8 = 0x02;
pub const ETX: u8 = 0x03;
pub const SEPARATOR: char = '|';

#[allow(dead_code)]
pub const RESPONSE_LABELS: &[&str] = &[
    "meter_dev_type",
    "meter_mac_code",
    "hhm_dev_type",
    "hhm_mac_code",
    "A_phase_power",
    "B_phase_power",
    "C_phase_power",
    "total_power",
    "A_chrg_nb",
    "B_chrg_nb",
    "C_chrg_nb",
    "ABC_chrg_nb",
    "wifi_rssi",
    "info_idx",
    "x_chrg_power",
    "A_chrg_power",
    "B_chrg_power",
    "C_chrg_power",
    "ABC_chrg_power",
    "x_dchrg_power",
    "A_dchrg_power",
    "B_dchrg_power",
    "C_dchrg_power",
    "ABC_dchrg_power",
];

/// Phase-letter → request-field index. Mirrors Python's `PHASE_FIELD_INDEX`.
pub fn phase_field_index(p: char) -> Option<usize> {
    match p {
        'A' => Some(4),
        'B' => Some(5),
        'C' => Some(6),
        _ => None,
    }
}

pub fn calculate_checksum(data: &[u8]) -> u8 {
    data.iter().fold(0u8, |acc, b| acc ^ *b)
}

pub fn compute_length(payload_without_length: &[u8]) -> usize {
    let base_size = 1 + 1 + payload_without_length.len() + 1 + 2;
    for length_digits in 1..5 {
        let total = base_size + length_digits;
        if total.to_string().len() == length_digits {
            return total;
        }
    }
    panic!("payload length too large");
}

pub fn build_payload(fields: &[String]) -> Vec<u8> {
    let mut message_str = String::new();
    message_str.push(SEPARATOR);
    for (i, f) in fields.iter().enumerate() {
        if i > 0 {
            message_str.push(SEPARATOR);
        }
        message_str.push_str(f);
    }
    let message_bytes = message_str.into_bytes();
    let total_length = compute_length(&message_bytes);

    let mut payload = Vec::with_capacity(total_length);
    payload.push(SOH);
    payload.push(STX);
    payload.extend(total_length.to_string().as_bytes());
    payload.extend_from_slice(&message_bytes);
    payload.push(ETX);
    let cs = calculate_checksum(&payload);
    payload.extend(format!("{cs:02x}").as_bytes());
    payload
}

/// Parse a CT002 response message. Returns `(fields, None)` on success,
/// `(None, Some(reason))` on failure.
pub fn parse_message(data: &[u8]) -> (Option<Vec<String>>, Option<String>) {
    if data.len() < 10 {
        return (None, Some("Too short".into()));
    }
    if data[0] != SOH || data[1] != STX {
        return (None, Some("Missing SOH/STX".into()));
    }
    let sep_index = match data[2..].iter().position(|b| *b == b'|') {
        Some(i) => i + 2,
        None => return (None, Some("No separator after length".into())),
    };
    let length: usize = match std::str::from_utf8(&data[2..sep_index])
        .ok()
        .and_then(|s| s.parse().ok())
    {
        Some(n) => n,
        None => return (None, Some("Invalid length field".into())),
    };
    if data.len() != length {
        return (
            None,
            Some(format!(
                "Length mismatch (expected {length}, got {})",
                data.len()
            )),
        );
    }
    if data[length - 3] != ETX {
        return (None, Some("Missing ETX".into()));
    }
    let xor = data[..length - 2].iter().fold(0u8, |acc, b| acc ^ *b);
    let expected = format!("{xor:02x}");
    let actual = &data[length - 2..];
    let ok = actual.eq_ignore_ascii_case(expected.as_bytes())
        || (actual[0] == b' ' && actual[1].eq_ignore_ascii_case(&expected.as_bytes()[1]));
    if !ok {
        return (
            None,
            Some(format!(
                "Checksum mismatch (expected {expected:?}, got {:?})",
                String::from_utf8_lossy(actual)
            )),
        );
    }
    let message = match std::str::from_utf8(&data[sep_index..length - 3]) {
        Ok(s) => s,
        Err(_) => return (None, Some("Invalid ASCII encoding".into())),
    };
    let fields: Vec<String> = message.split('|').skip(1).map(|s| s.to_string()).collect();
    (Some(fields), None)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip_request() {
        let fields = vec![
            "HMG-50".to_string(),
            "AABBCCDDEEFF".to_string(),
            "HME-4".to_string(),
            "112233445566".to_string(),
            "A".to_string(),
            "120".to_string(),
        ];
        let payload = build_payload(&fields);
        assert_eq!(payload[0], SOH);
        assert_eq!(payload[1], STX);
        let (parsed, err) = parse_message(&payload);
        assert!(err.is_none(), "{err:?}");
        assert_eq!(parsed.unwrap(), fields);
    }

    #[test]
    fn rejects_short() {
        let (p, e) = parse_message(&[0x01, 0x02]);
        assert!(p.is_none());
        assert!(e.unwrap().contains("Too short"));
    }
}
