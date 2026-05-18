//! Smart Meter Language (SML / IEC 62056) decoder.
//!
//! Replaces Python `smllib`. The implementation here is a pragmatic subset:
//! it finds SML transport frames by their escape-sequence boundaries, then
//! scans the body for OBIS codes prefixed by the SML "octet string length 6"
//! marker (`0x07 0x06`) and extracts the immediately-following numeric value
//! with optional `scaler` and `unit` adjustments.
//!
//! This is **mechanically translated** from the Python port and the SML
//! protocol notes published by VOLKSZAEHLER. It has not been validated
//! against real-meter captures during the migration. Treat the SML
//! powermeter as Phase 3 work-in-progress until snapshot tests against
//! `tests/data/*.sml` (in the Python project) are ported.

#![forbid(unsafe_code)]

pub const OBIS_POWER_CURRENT: [u8; 6] = [0x01, 0x00, 0x10, 0x07, 0x00, 0xFF];
pub const OBIS_POWER_L1: [u8; 6] = [0x01, 0x00, 0x24, 0x07, 0x00, 0xFF];
pub const OBIS_POWER_L2: [u8; 6] = [0x01, 0x00, 0x38, 0x07, 0x00, 0xFF];
pub const OBIS_POWER_L3: [u8; 6] = [0x01, 0x00, 0x4C, 0x07, 0x00, 0xFF];

const SML_ESCAPE: [u8; 4] = [0x1B, 0x1B, 0x1B, 0x1B];
const SML_FRAME_START: [u8; 4] = [0x01, 0x01, 0x01, 0x01];
const SML_FRAME_END_TAG: u8 = 0x1A;

/// Look for a complete SML transport frame in `buf`. Returns a slice
/// covering the message body (after the start escape and before the end
/// escape) if a complete frame is present.
pub fn find_frame(buf: &[u8]) -> Option<&[u8]> {
    let start = find_window(buf, &concat8(SML_ESCAPE, SML_FRAME_START))?;
    let after_start = start + 8;
    // Search for the end escape after the start.
    let rest = &buf[after_start..];
    let end_rel = find_window(rest, &SML_ESCAPE)?;
    if rest.len() < end_rel + 5 {
        return None;
    }
    if rest[end_rel + 4] != SML_FRAME_END_TAG {
        return None;
    }
    // End frame is 8 bytes: 1B 1B 1B 1B 1A pad crc_hi crc_lo
    if rest.len() < end_rel + 8 {
        return None;
    }
    Some(&rest[..end_rel])
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
    let last = haystack.len() - needle.len();
    for i in 0..=last {
        if &haystack[i..i + needle.len()] == needle {
            return Some(i);
        }
    }
    None
}

#[derive(Debug, Clone)]
pub struct ObisEntry {
    pub obis: [u8; 6],
    pub value: f64,
}

/// Scan `frame_body` for OBIS readings. Each reading appears as
///
///     0x07 0x06 <6 OBIS bytes>     // listEntry::objName (octet string len 6)
///     ... (status field)
///     ... (valTime field)
///     <unit byte or 0x01>          // unit
///     <scaler i8>                  // scaler
///     <type+len byte> <value bytes>// the value
///     <signature> ...              // ignored
///
/// We accept any signed integer (`0x5X`) or unsigned (`0x6X`) tag for the
/// value, where X is the byte count (X bytes follow). Values are scaled by
/// 10^scaler.
pub fn parse_obis(frame_body: &[u8]) -> Option<Vec<ObisEntry>> {
    let mut out = Vec::new();
    let mut i = 0usize;
    while i + 8 < frame_body.len() {
        if frame_body[i] == 0x07 && frame_body[i + 1] == 0x06 {
            let mut obis = [0u8; 6];
            obis.copy_from_slice(&frame_body[i + 2..i + 8]);
            // Walk forward, skipping the status / valTime / unit / scaler
            // fields. They are TLV-encoded; an SML list begins with 0x7X
            // (length X). For robustness we scan up to 32 bytes for a
            // value tag (0x5n or 0x6n).
            let mut j = i + 8;
            let end = (i + 8 + 32).min(frame_body.len());
            // Track scaler when we see a 0x52 (signed int, length 2).
            let mut scaler: i32 = 0;
            let mut scaler_seen = false;
            while j < end {
                let tag = frame_body[j];
                if !scaler_seen
                    && (tag & 0xF0) == 0x50
                    && (tag & 0x0F) as usize <= 8
                    && j + (tag & 0x0F) as usize <= frame_body.len()
                {
                    let len = (tag & 0x0F) as usize;
                    // Heuristic: the scaler is the last 1-byte signed int
                    // before the value. We adopt the most recent 1-byte
                    // signed int as the scaler.
                    if len == 2 {
                        scaler = frame_body[j + 1] as i8 as i32;
                        scaler_seen = true;
                        j += len;
                        continue;
                    }
                }
                // Value tag: 0x5n (signed int, n bytes incl tag, 2..=9) or 0x6n.
                let high = tag & 0xF0;
                let n = (tag & 0x0F) as usize;
                if (high == 0x50 || high == 0x60)
                    && (2..=9).contains(&n)
                    && j + n <= frame_body.len()
                {
                    // After scaler is consumed, treat the next signed/unsigned
                    // int as the value.
                    if scaler_seen {
                        let payload_len = n - 1;
                        let bytes = &frame_body[j + 1..j + 1 + payload_len];
                        let raw = if high == 0x50 {
                            // Sign-extend
                            let mut v: i64 = if (bytes[0] & 0x80) != 0 { -1 } else { 0 };
                            for &b in bytes {
                                v = (v << 8) | b as i64;
                            }
                            v as f64
                        } else {
                            let mut v: u64 = 0;
                            for &b in bytes {
                                v = (v << 8) | b as u64;
                            }
                            v as f64
                        };
                        let value = raw * 10f64.powi(scaler);
                        out.push(ObisEntry { obis, value });
                        i = j + n;
                        break;
                    }
                }
                j += 1;
            }
            if i < j {
                i = j;
                continue;
            }
        }
        i += 1;
    }
    if out.is_empty() {
        None
    } else {
        Some(out)
    }
}
