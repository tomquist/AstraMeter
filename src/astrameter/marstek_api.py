import hashlib
import json
import logging
import random
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


MANAGED_MAC_PREFIX = "02b250"


@dataclass
class MarstekConfig:
    base_url: str
    mailbox: str
    password: str
    timezone: str = "Europe/Berlin"


class MarstekApiError(Exception):
    pass


def _translate_marstek_message(code: Any, msg: Any) -> str:
    """Best-effort translation for common Marstek API messages."""
    msg_text = "" if msg is None else str(msg)
    code_text = "" if code is None else str(code)

    if code_text == "4" and ("密码错误" in msg_text or "password" in msg_text.lower()):
        return "password incorrect"

    return msg_text


def _http_get_json(
    url: str, params: dict[str, Any], headers: dict[str, str] | None = None
):
    query = urllib.parse.urlencode(params)
    full_url = f"{url}?{query}"
    req = urllib.request.Request(full_url, headers=headers or {}, method="GET")

    code = None
    body = ""
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            code = resp.status
    except urllib.error.HTTPError as exc:
        code = exc.code
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = str(exc)
    except urllib.error.URLError as exc:
        raise MarstekApiError(
            f"Network error calling {full_url}: {exc.reason}"
        ) from exc
    except Exception as exc:
        raise MarstekApiError(f"Unexpected error calling {full_url}: {exc}") from exc

    try:
        payload = json.loads(body)
    except Exception as exc:
        snippet = body[:200] if body else "<empty>"
        raise MarstekApiError(f"Non-JSON response from {full_url}: {snippet}") from exc

    if code is None or code < 200 or code >= 300:
        raise MarstekApiError(f"HTTP {code} from {full_url}: {payload}")
    return payload


def _random_hex(n: int) -> str:
    return "".join(random.choice("0123456789abcdef") for _ in range(n))


def _desired_type(device_type: str) -> str:
    return "HME-4" if device_type == "ct002" else "HME-3"


def _desired_name(device_type: str) -> str:
    return "AstraMeter CT002" if device_type == "ct002" else "AstraMeter CT003"


def _is_managed_prefix(value: str) -> bool:
    return isinstance(value, str) and value.lower().startswith(MANAGED_MAC_PREFIX)


def _fetch_token_and_devices(cfg: MarstekConfig) -> tuple[str, list[dict[str, Any]]]:
    pwd_md5 = hashlib.md5(cfg.password.encode("utf-8")).hexdigest()
    token_url = f"{cfg.base_url.rstrip('/')}/app/Solar/v2_get_device.php"
    token_resp = _http_get_json(token_url, {"mailbox": cfg.mailbox, "pwd": pwd_md5})

    if str(token_resp.get("code")) != "2" or not token_resp.get("token"):
        code = token_resp.get("code")
        raw_msg = token_resp.get("msg")
        translated_msg = _translate_marstek_message(code, raw_msg)
        if translated_msg and translated_msg != str(raw_msg):
            raise MarstekApiError(
                f"Token fetch failed (code={code}): {translated_msg} (raw: {raw_msg})"
            )
        raise MarstekApiError(f"Token fetch failed (code={code}): {raw_msg}")

    token = token_resp["token"]
    solar_devices = (
        token_resp.get("data") if isinstance(token_resp.get("data"), list) else []
    )

    list_url = f"{cfg.base_url.rstrip('/')}/ems/api/v1/getDeviceList"
    list_resp = _http_get_json(
        list_url,
        {"mailbox": cfg.mailbox, "token": token},
        headers={"User-Agent": "Dart/2.19 (dart:io)"},
    )
    ems_devices = (
        list_resp.get("data") if isinstance(list_resp.get("data"), list) else []
    )

    by_devid = {
        d.get("devid", ""): d
        for d in ems_devices
        if isinstance(d, dict) and d.get("devid")
    }

    merged = []
    for d in solar_devices:
        if not isinstance(d, dict):
            continue
        did = d.get("devid", "")
        e = by_devid.get(did, {})
        merged.append(
            {
                "devid": did,
                "name": d.get("name") or e.get("name"),
                "sn": d.get("sn"),
                "mac": d.get("mac") or e.get("mac"),
                "type": d.get("type") or e.get("type"),
                "access": d.get("access"),
                "bluetooth_name": d.get("bluetooth_name"),
                "version": e.get("version"),
                "salt": e.get("salt"),
            }
        )

    return token, merged


def _find_existing_managed_device(
    devices: list[dict[str, Any]], expected_type: str
) -> dict[str, Any] | None:
    for d in devices:
        devid = str(d.get("devid", "")).lower()
        mac = str(d.get("mac", "")).lower()
        dtype = str(d.get("type", ""))
        if dtype != expected_type:
            continue
        if _is_managed_prefix(devid) and _is_managed_prefix(mac):
            return d
    return None


def _generate_new_id(existing_devices: list[dict[str, Any]]) -> str:
    existing = {
        str(d.get("devid", "")).lower()
        for d in existing_devices
        if isinstance(d, dict) and d.get("devid")
    }
    existing |= {
        str(d.get("mac", "")).lower()
        for d in existing_devices
        if isinstance(d, dict) and d.get("mac")
    }

    prefix = MANAGED_MAC_PREFIX

    for _ in range(200):
        candidate = f"{prefix}{_random_hex(6)}"
        if candidate not in existing:
            return candidate
    raise MarstekApiError("Could not generate unique managed MAC/DEVID")


def _add_device(
    cfg: MarstekConfig,
    token: str,
    device_type: str,
    devid_mac: str,
):
    add_url = f"{cfg.base_url.rstrip('/')}/app/Solar/v2_add_device.php"
    type_value = _desired_type(device_type)
    suffix = devid_mac[-4:]
    payload = {
        "name": _desired_name(device_type),
        "mailbox": cfg.mailbox,
        "devid": devid_mac,
        "mac": devid_mac,
        "type": type_value,
        "token": token,
        "access": "1",
        "bluetooth_name": f"MST-SMR_{suffix}",
        "position": "{}",
        "timeZone": cfg.timezone,
    }

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "token": token,
        "User-Agent": "Dart/2.19 (dart:io)",
    }
    resp = _http_get_json(add_url, payload, headers=headers)
    code = str(resp.get("code", ""))
    if code not in ("1", "2"):
        raise MarstekApiError(
            f"Add device failed for {device_type} (code={code}): {resp.get('msg')}"
        )
    return resp


def ensure_managed_fake_device(
    cfg: MarstekConfig, device_type: str
) -> dict[str, Any] | None:
    if device_type not in ("ct002", "ct003"):
        return None

    token, devices = _fetch_token_and_devices(cfg)
    expected_type = _desired_type(device_type)

    existing = _find_existing_managed_device(devices, expected_type)
    if existing:
        logger.info(
            "Marstek managed %s already exists (devid=%s, mac=%s, type=%s)",
            device_type,
            existing.get("devid"),
            existing.get("mac"),
            existing.get("type"),
        )
        return existing

    new_id = _generate_new_id(devices)
    logger.info(
        "Creating managed fake %s device in Marstek cloud (devid=mac=%s, type=%s)",
        device_type,
        new_id,
        expected_type,
    )
    _add_device(cfg, token, device_type, new_id)

    # Re-fetch once for confirmation and return created record
    _, refreshed_devices = _fetch_token_and_devices(cfg)
    created = _find_existing_managed_device(refreshed_devices, expected_type)
    if not created:
        logger.warning(
            "Created %s device but could not confirm it in refreshed list", device_type
        )
        return None
    return created
