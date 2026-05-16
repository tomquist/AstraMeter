"""CT002 UDP protocol — standalone copy for simulator decoupling.

This duplicates ~60 lines of stable protocol code from
``astrameter.ct002.ct002`` so the simulator has zero imports from
the main package and can be moved to a separate repo.
"""

SOH = 0x01
STX = 0x02
ETX = 0x03
SEPARATOR = "|"

RESPONSE_LABELS = [
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
]

PHASE_FIELD_INDEX: dict[str, int] = {"A": 4, "B": 5, "C": 6}


def calculate_checksum(data_bytes: bytes | bytearray) -> int:
    xor = 0
    for b in data_bytes:
        xor ^= b
    return xor


def compute_length(payload_without_length: bytes | bytearray) -> int:
    base_size = 1 + 1 + len(payload_without_length) + 1 + 2  # SOH+STX+msg+ETX+chk
    for length_digits in range(1, 5):
        total_length = base_size + length_digits
        if len(str(total_length)) == length_digits:
            return total_length
    raise ValueError("Payload length too large")


def build_payload(fields: list[str]) -> bytes:
    message_str = SEPARATOR + SEPARATOR.join(fields)
    message_bytes = message_str.encode("ascii")
    total_length = compute_length(message_bytes)
    payload = bytearray([SOH, STX])
    payload.extend(str(total_length).encode("ascii"))
    payload.extend(message_bytes)
    payload.append(ETX)
    checksum_val = calculate_checksum(payload)
    payload.extend(f"{checksum_val:02x}".encode("ascii"))
    return bytes(payload)


def parse_message(
    data: bytes,
) -> tuple[list[str] | None, str | None]:
    if len(data) < 10:
        return None, "Too short"
    if data[0] != SOH or data[1] != STX:
        return None, "Missing SOH/STX"
    sep_index = data.find(b"|", 2)
    if sep_index == -1:
        return None, "No separator after length"
    try:
        length = int(data[2:sep_index].decode("ascii"))
    except ValueError:
        return None, "Invalid length field"
    if len(data) != length:
        return None, f"Length mismatch (expected {length}, got {len(data)})"
    if data[-3] != ETX:
        return None, "Missing ETX"
    xor = 0
    for b in data[: length - 2]:
        xor ^= b
    expected_checksum = f"{xor:02x}".encode("ascii")
    actual_checksum = data[-2:]
    if actual_checksum.lower() != expected_checksum:
        if (
            actual_checksum[0:1] == b" "
            and actual_checksum[1:2].lower() == expected_checksum[1:2]
        ):
            pass
        else:
            return (
                None,
                f"Checksum mismatch (expected {expected_checksum!r}, "
                f"got {actual_checksum!r})",
            )
    try:
        message = data[sep_index:-3].decode("ascii")
    except UnicodeDecodeError:
        return None, "Invalid ASCII encoding"
    fields = message.split("|")[1:]
    return fields, None
