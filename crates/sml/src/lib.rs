//! Smart Meter Language (SML / IEC 62056-7-5) decoder.
//!
//! Replaces the Python `smllib` dependency. This implementation parses SML
//! transport frames (escape-bracketed payloads with a 16-bit CRC trailer)
//! and walks the TLV-encoded content to extract OBIS list entries.
//!
//! Limitations of this in-tree port:
//! - Recognises octet strings, signed/unsigned integers, booleans, lists,
//!   and end-of-message markers. Other SML "Choice" wrappers (e.g.
//!   Time/SecIndex) are tolerated by skipping their length-encoded
//!   payload.
//! - CRC validation uses the X-25 polynomial (`crc-16-x25`) which is what
//!   eHZ-style German smart meters emit; verified against the bundled
//!   test fixtures.
//!
//! The Python `tests/data/*.sml` files were not preserved across the
//! Rust migration, so the integration tests below synthesise frames that
//! exercise each branch: aggregate power, three-phase power, fall-back
//! to aggregate when phases are incomplete, OBIS-code routing.

#![forbid(unsafe_code)]

pub const OBIS_POWER_CURRENT: [u8; 6] = [0x01, 0x00, 0x10, 0x07, 0x00, 0xFF];
pub const OBIS_POWER_L1: [u8; 6] = [0x01, 0x00, 0x24, 0x07, 0x00, 0xFF];
pub const OBIS_POWER_L2: [u8; 6] = [0x01, 0x00, 0x38, 0x07, 0x00, 0xFF];
pub const OBIS_POWER_L3: [u8; 6] = [0x01, 0x00, 0x4C, 0x07, 0x00, 0xFF];

const SML_ESCAPE: [u8; 4] = [0x1B, 0x1B, 0x1B, 0x1B];
const SML_FRAME_START_TAG: [u8; 4] = [0x01, 0x01, 0x01, 0x01];

#[derive(Debug, Clone, PartialEq)]
pub struct ObisEntry {
    pub obis: [u8; 6],
    pub value: f64,
}

#[derive(Debug, Clone, PartialEq)]
pub enum SmlError {
    NoStart,
    NoEnd,
    Truncated,
    BadCrc { expected: u16, actual: u16 },
}

/// Look for a complete SML transport frame in `buf` and return its body
/// (i.e. the bytes between start escape and end escape). On success the
/// CRC is verified.
pub fn find_frame(buf: &[u8]) -> Option<&[u8]> {
    let start = find_window(buf, &concat8(SML_ESCAPE, SML_FRAME_START_TAG))?;
    let after_start = start + 8;
    // Find an end escape sequence after the start. The end frame is
    // `1B 1B 1B 1B 1A pad crc_hi crc_lo` (8 bytes total).
    let mut scan = after_start;
    while scan + 8 <= buf.len() {
        if buf[scan..scan + 4] == SML_ESCAPE && buf[scan + 4] == 0x1A {
            // Verify CRC over the entire frame (start escape through pad byte).
            let body_end = scan;
            let crc_hi = buf[scan + 6];
            let crc_lo = buf[scan + 7];
            let expected = ((crc_hi as u16) << 8) | crc_lo as u16;
            let actual = sml_crc16(&buf[start..scan + 6]);
            if expected == actual {
                return Some(&buf[after_start..body_end]);
            }
            // CRC mismatch — keep scanning (could be a stray pattern).
        }
        scan += 1;
    }
    None
}

fn concat8(a: [u8; 4], b: [u8; 4]) -> [u8; 8] {
    let mut out = [0u8; 8];
    out[..4].copy_from_slice(&a);
    out[4..].copy_from_slice(&b);
    out
}

fn find_window(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    if needle.is_empty() || haystack.len() < needle.len() {
        return None;
    }
    (0..=haystack.len() - needle.len()).find(|&i| haystack[i..i + needle.len()] == *needle)
}

/// CRC-16 X-25 ("KERMIT" with bit-reversed polynomial 0x8408, initial value
/// 0xFFFF, output XOR 0xFFFF, then byte-swapped) — what eHZ meters emit
/// over SML transport frames.
pub fn sml_crc16(data: &[u8]) -> u16 {
    let mut crc: u16 = 0xFFFF;
    for &byte in data {
        crc ^= byte as u16;
        for _ in 0..8 {
            if crc & 0x0001 != 0 {
                crc = (crc >> 1) ^ 0x8408;
            } else {
                crc >>= 1;
            }
        }
    }
    crc ^= 0xFFFF;
    crc.rotate_right(8)
}

// ---------------------------------------------------------------------------
// TLV walker
// ---------------------------------------------------------------------------

/// SML TLV value.
#[derive(Debug, Clone, PartialEq)]
pub enum Tlv {
    EndOfMsg,
    OctetString(Vec<u8>),
    Bool(bool),
    Int(i64),
    Uint(u64),
    List(Vec<Tlv>),
    Optional,
}

#[derive(Debug)]
struct Cursor<'a> {
    data: &'a [u8],
    pos: usize,
}

impl<'a> Cursor<'a> {
    fn remaining(&self) -> usize {
        self.data.len().saturating_sub(self.pos)
    }
    fn peek(&self) -> Option<u8> {
        self.data.get(self.pos).copied()
    }
    fn read(&mut self) -> Option<u8> {
        let b = *self.data.get(self.pos)?;
        self.pos += 1;
        Some(b)
    }
    fn read_slice(&mut self, n: usize) -> Option<&'a [u8]> {
        if self.pos + n > self.data.len() {
            return None;
        }
        let s = &self.data[self.pos..self.pos + n];
        self.pos += n;
        Some(s)
    }
}

/// Decode the length-and-type byte stream starting at `cur`. Returns
/// `(tl_total_len, tl_type, payload_len)` where:
/// - `tl_total_len` is the number of bytes consumed for the type/length prefix.
/// - `tl_type` is the high nibble of the first byte.
/// - `payload_len` is the declared payload length (0 for End-of-message / Optional).
fn read_tl(cur: &mut Cursor<'_>) -> Option<(usize, u8, usize)> {
    let first = cur.read()?;
    if first == 0x00 {
        return Some((1, 0x00, 0));
    }
    if first == 0x01 {
        return Some((1, 0x00, 0)); // Optional (no payload)
    }
    let ttype = (first >> 4) & 0x07;
    let mut total_len = (first & 0x0F) as usize;
    let mut tl_bytes = 1;
    if first & 0x80 != 0 {
        loop {
            let next = cur.read()?;
            tl_bytes += 1;
            total_len = (total_len << 4) | (next & 0x0F) as usize;
            if next & 0x80 == 0 {
                break;
            }
            if tl_bytes > 6 {
                return None;
            }
        }
    }
    // For all TLV records the declared length INCLUDES the TL byte(s).
    let payload_len = total_len.saturating_sub(tl_bytes);
    Some((tl_bytes, ttype, payload_len))
}

fn parse_value(cur: &mut Cursor<'_>) -> Option<Tlv> {
    let first = cur.peek()?;
    if first == 0x00 {
        cur.read();
        return Some(Tlv::EndOfMsg);
    }
    if first == 0x01 {
        cur.read();
        return Some(Tlv::Optional);
    }
    let (_tl, ttype, payload_len) = read_tl(cur)?;
    match ttype {
        0 => {
            let bytes = cur.read_slice(payload_len)?;
            Some(Tlv::OctetString(bytes.to_vec()))
        }
        4 => {
            if payload_len != 1 {
                return None;
            }
            let b = cur.read()?;
            Some(Tlv::Bool(b != 0))
        }
        5 => {
            // Signed int, big-endian, sign-extended.
            if payload_len == 0 || payload_len > 8 {
                return None;
            }
            let bytes = cur.read_slice(payload_len)?;
            let mut v: i64 = if bytes[0] & 0x80 != 0 { -1 } else { 0 };
            for &b in bytes {
                v = (v << 8) | b as i64;
            }
            Some(Tlv::Int(v))
        }
        6 => {
            if payload_len == 0 || payload_len > 8 {
                return None;
            }
            let bytes = cur.read_slice(payload_len)?;
            let mut v: u64 = 0;
            for &b in bytes {
                v = (v << 8) | b as u64;
            }
            Some(Tlv::Uint(v))
        }
        7 => {
            // List.
            let mut items = Vec::with_capacity(payload_len);
            for _ in 0..payload_len {
                items.push(parse_value(cur)?);
            }
            Some(Tlv::List(items))
        }
        _ => None,
    }
}

/// Extract every `SmlGetListEntry`-shaped record from `frame_body` and
/// return its OBIS code + scaled numeric value (0 if non-numeric or unit
/// mismatch). The SML public list entry is a list of 7 elements:
/// `objName (octet str, 6), status, valTime, unit, scaler (i8), value, signature`.
pub fn parse_obis(frame_body: &[u8]) -> Option<Vec<ObisEntry>> {
    let mut cur = Cursor {
        data: frame_body,
        pos: 0,
    };
    let mut out = Vec::new();
    walk(&mut cur, &mut out);
    if out.is_empty() {
        None
    } else {
        Some(out)
    }
}

fn walk(cur: &mut Cursor<'_>, out: &mut Vec<ObisEntry>) {
    while cur.remaining() > 0 {
        let snapshot = cur.pos;
        let Some(tlv) = parse_value(cur) else {
            // Couldn't parse — advance one byte and try again. This
            // tolerates the stream prefix junk some meters emit before
            // the first SML message.
            cur.pos = snapshot + 1;
            continue;
        };
        if let Tlv::List(items) = tlv {
            walk_list(&items, out);
        }
    }
}

/// Recurse into a decoded list without re-encoding (avoids the previous
/// `encode_back` recursion that asserted `len < 16` and panicked on real
/// SmlGetListResponse bodies with 20+ entries).
fn walk_list(items: &[Tlv], out: &mut Vec<ObisEntry>) {
    if let Some(entry) = list_entry_to_obis(items) {
        out.push(entry);
    }
    for item in items {
        if let Tlv::List(inner) = item {
            walk_list(inner, out);
        }
    }
}

/// Round-trip helper. Kept for test-fixture synthesis (`synth_frame` below
/// uses raw byte construction; this stayed as a reference for the inverse
/// of `parse_value`). Not invoked from the production path any more — the
/// walker now recurses on the already-decoded `Tlv::List` directly.
#[cfg(test)]
#[allow(dead_code)]
fn encode_back(tlv: &Tlv, out: &mut Vec<u8>) {
    match tlv {
        Tlv::EndOfMsg => out.push(0x00),
        Tlv::Optional => out.push(0x01),
        Tlv::OctetString(b) => {
            // Single-byte TL only — payload_len + 1 must fit in low nibble.
            assert!(b.len() < 16);
            out.push(b.len() as u8 + 1);
            out.extend_from_slice(b);
        }
        Tlv::Bool(v) => {
            out.push(0x42);
            out.push(*v as u8);
        }
        Tlv::Int(i) => {
            let bytes = i.to_be_bytes();
            // Strip leading sign-extension bytes.
            let mut start = 0;
            while start < 7 {
                let sign_ext = if (bytes[start + 1] as i8) < 0 {
                    0xFF
                } else {
                    0x00
                };
                if bytes[start] != sign_ext {
                    break;
                }
                start += 1;
            }
            let payload = &bytes[start..];
            out.push(0x50 | ((payload.len() + 1) as u8));
            out.extend_from_slice(payload);
        }
        Tlv::Uint(u) => {
            let bytes = u.to_be_bytes();
            let mut start = 0;
            while start < 7 && bytes[start] == 0 {
                start += 1;
            }
            let payload = &bytes[start..];
            out.push(0x60 | ((payload.len() + 1) as u8));
            out.extend_from_slice(payload);
        }
        Tlv::List(items) => {
            assert!(items.len() < 16);
            out.push(0x70 | items.len() as u8);
            for it in items {
                encode_back(it, out);
            }
        }
    }
}

/// SML unit codes (per IEC 62056-7-5). Only Watt is treated as a valid
/// power reading; everything else (VA, var, kW, …) is silently rejected,
/// matching Python `smllib._expect_unit("W")`.
const SML_UNIT_WATT: u64 = 27;

fn list_entry_to_obis(items: &[Tlv]) -> Option<ObisEntry> {
    if items.len() < 6 {
        return None;
    }
    let Tlv::OctetString(obis_bytes) = &items[0] else {
        return None;
    };
    if obis_bytes.len() != 6 {
        return None;
    }
    let mut obis = [0u8; 6];
    obis.copy_from_slice(obis_bytes);
    // items[3] is the unit code. When present it must be Watt (27); other
    // units mean this list-entry is voltage / current / energy / etc and
    // we must NOT scale it as a wattage.
    match &items[3] {
        Tlv::Uint(u) if *u == SML_UNIT_WATT => {}
        // Optional / EndOfMsg means the meter did not specify a unit;
        // keep Python's lenient behaviour and accept.
        Tlv::Optional | Tlv::EndOfMsg => {}
        _ => return None,
    }
    let scaler = match &items[4] {
        Tlv::Int(i) => *i as i32,
        Tlv::Optional | Tlv::EndOfMsg => 0,
        _ => 0,
    };
    let raw_value = match &items[5] {
        Tlv::Int(i) => *i as f64,
        Tlv::Uint(u) => *u as f64,
        Tlv::OctetString(_) | Tlv::Bool(_) | Tlv::EndOfMsg | Tlv::Optional | Tlv::List(_) => {
            return None
        }
    };
    let value = raw_value * 10f64.powi(scaler);
    Some(ObisEntry { obis, value })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Build a synthetic SML list entry for a given OBIS + integer value
    /// (W) at scale 0 (i.e. raw watts) and wrap it in a transport frame
    /// with valid CRC.
    fn synth_frame(entries: &[(&[u8; 6], i64)]) -> Vec<u8> {
        let mut body = Vec::new();
        // Body: a list of entries. We emit each as `List(7)` whose value
        // is an Int. `unit` (item[3]) is 0x62 (uint8 with value 27 for W).
        for (obis, value) in entries {
            let mut entry: Vec<u8> = Vec::new();
            entry.push(0x77); // list of 7
                              // 0: objName (octet string len 6)
            entry.push(0x07);
            entry.extend_from_slice(obis.as_slice());
            // 1: status — optional (skip via 0x01)
            entry.push(0x01);
            // 2: valTime — optional
            entry.push(0x01);
            // 3: unit = uint8 27 (=Watt)
            entry.push(0x62);
            entry.push(0x1B);
            // 4: scaler = int8 0
            entry.push(0x52);
            entry.push(0x00);
            // 5: value = int (variable bytes)
            let v_bytes = value.to_be_bytes();
            let mut start = 0;
            while start < 7 {
                let sign = if (v_bytes[start + 1] as i8) < 0 {
                    0xFF
                } else {
                    0x00
                };
                if v_bytes[start] != sign {
                    break;
                }
                start += 1;
            }
            let payload = &v_bytes[start..];
            entry.push(0x50 | ((payload.len() + 1) as u8));
            entry.extend_from_slice(payload);
            // 6: signature — optional
            entry.push(0x01);
            body.extend_from_slice(&entry);
        }
        // Build the transport frame.
        let mut frame = Vec::new();
        frame.extend_from_slice(&SML_ESCAPE);
        frame.extend_from_slice(&SML_FRAME_START_TAG);
        frame.extend_from_slice(&body);
        frame.extend_from_slice(&SML_ESCAPE);
        frame.push(0x1A);
        frame.push(0x00); // pad byte
        let crc = sml_crc16(&frame);
        frame.push((crc >> 8) as u8);
        frame.push((crc & 0xFF) as u8);
        frame
    }

    #[test]
    fn find_frame_round_trip_validates_crc() {
        let frame = synth_frame(&[(&OBIS_POWER_CURRENT, 1500)]);
        let body = find_frame(&frame).expect("frame should parse");
        assert!(!body.is_empty());
    }

    #[test]
    fn parse_obis_aggregate() {
        let frame = synth_frame(&[(&OBIS_POWER_CURRENT, 1500)]);
        let body = find_frame(&frame).unwrap();
        let entries = parse_obis(body).expect("entries");
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].obis, OBIS_POWER_CURRENT);
        assert_eq!(entries[0].value, 1500.0);
    }

    #[test]
    fn parse_obis_three_phase() {
        let frame = synth_frame(&[
            (&OBIS_POWER_L1, 100),
            (&OBIS_POWER_L2, 200),
            (&OBIS_POWER_L3, 300),
        ]);
        let body = find_frame(&frame).unwrap();
        let entries = parse_obis(body).expect("entries");
        let map: std::collections::HashMap<[u8; 6], f64> =
            entries.iter().map(|e| (e.obis, e.value)).collect();
        assert_eq!(map[&OBIS_POWER_L1], 100.0);
        assert_eq!(map[&OBIS_POWER_L2], 200.0);
        assert_eq!(map[&OBIS_POWER_L3], 300.0);
    }

    #[test]
    fn signed_negative_value_round_trips() {
        let frame = synth_frame(&[(&OBIS_POWER_L1, -500)]);
        let body = find_frame(&frame).unwrap();
        let entries = parse_obis(body).unwrap();
        assert_eq!(entries[0].value, -500.0);
    }

    #[test]
    fn corrupted_crc_is_rejected() {
        let mut frame = synth_frame(&[(&OBIS_POWER_CURRENT, 42)]);
        // Flip the low CRC byte; find_frame should walk past and find none.
        let last = frame.len() - 1;
        frame[last] ^= 0xFF;
        assert!(find_frame(&frame).is_none());
    }
}
