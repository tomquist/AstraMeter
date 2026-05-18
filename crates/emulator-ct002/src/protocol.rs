//! Port of `src/astrameter/ct002/protocol.py`. ASCII-framed UDP packets with
//! SOH/STX/length/payload/ETX/XOR-checksum.

pub const SOH: u8 = 0x01;
pub const STX: u8 = 0x02;
pub const ETX: u8 = 0x03;
pub const SEPARATOR: u8 = b'|';

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

pub fn checksum(data: &[u8]) -> u8 {
    data.iter().fold(0u8, |acc, b| acc ^ b)
}

fn compute_length(payload_without_length: usize) -> Option<usize> {
    let base = 1 + 1 + payload_without_length + 1 + 2;
    for length_digits in 1..=4 {
        let total = base + length_digits;
        if total.to_string().len() == length_digits {
            return Some(total);
        }
    }
    None
}

/// Build a complete UDP packet from field strings (each field is ASCII).
pub fn build_payload(fields: &[&str]) -> Result<Vec<u8>, &'static str> {
    let mut message = String::with_capacity(64);
    for f in fields {
        message.push('|');
        message.push_str(f);
    }
    let msg_bytes = message.as_bytes();
    let total_length = compute_length(msg_bytes.len()).ok_or("payload too large")?;
    let len_str = total_length.to_string();
    let mut out = Vec::with_capacity(total_length);
    out.push(SOH);
    out.push(STX);
    out.extend_from_slice(len_str.as_bytes());
    out.extend_from_slice(msg_bytes);
    out.push(ETX);
    let xor = checksum(&out);
    out.extend_from_slice(format!("{xor:02x}").as_bytes());
    Ok(out)
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParseError(pub &'static str);

/// Parse a CT002 request packet. Returns the payload fields (without the
/// leading empty field).
pub fn parse_request(data: &[u8]) -> Result<Vec<String>, ParseError> {
    if data.len() < 10 {
        return Err(ParseError("too short"));
    }
    if data[0] != SOH || data[1] != STX {
        return Err(ParseError("missing SOH/STX"));
    }
    let sep_index = data[2..]
        .iter()
        .position(|&b| b == SEPARATOR)
        .map(|i| i + 2)
        .ok_or(ParseError("no separator after length"))?;
    let length: usize = std::str::from_utf8(&data[2..sep_index])
        .map_err(|_| ParseError("non-ASCII length"))?
        .parse()
        .map_err(|_| ParseError("invalid length"))?;
    if data.len() != length {
        return Err(ParseError("length mismatch"));
    }
    if data[length - 3] != ETX {
        return Err(ParseError("missing ETX"));
    }
    let xor = checksum(&data[..length - 2]);
    let expected = format!("{xor:02x}");
    let actual = &data[length - 2..];
    let actual_lower: Vec<u8> = actual.iter().map(|b| b.to_ascii_lowercase()).collect();
    if actual_lower.as_slice() != expected.as_bytes() {
        // Tolerate leading space (firmware quirk).
        let tolerable = actual.len() == 2
            && actual[0] == b' '
            && actual[1].to_ascii_lowercase() == expected.as_bytes()[1];
        if !tolerable {
            return Err(ParseError("checksum mismatch"));
        }
    }
    let message = std::str::from_utf8(&data[sep_index..length - 3])
        .map_err(|_| ParseError("non-ASCII payload"))?;
    Ok(message.split('|').skip(1).map(|s| s.to_string()).collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trip() {
        let fields = ["AB", "1234"];
        let bytes = build_payload(&fields).unwrap();
        let parsed = parse_request(&bytes).unwrap();
        assert_eq!(parsed, vec!["AB".to_string(), "1234".to_string()]);
    }

    #[test]
    fn rejects_bad_checksum() {
        let mut bytes = build_payload(&["X"]).unwrap();
        let last = bytes.len() - 1;
        bytes[last] = b'0';
        assert!(parse_request(&bytes).is_err());
    }
}
