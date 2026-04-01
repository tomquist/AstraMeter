from .base import Powermeter
import requests
import urllib3
import logging

logger = logging.getLogger("b2500-meter")


def _find_measurement(entries, measurement_type):
    """Find a measurement block by type in the Envoy production/consumption array."""
    for entry in entries:
        if (
            entry.get("measurementType") == measurement_type
            or entry.get("type") == measurement_type
        ):
            return entry
    return None


ENLIGHTEN_LOGIN_URL = "https://enlighten.enphaseenergy.com/login/login.json"
ENLIGHTEN_TOKEN_URL = "https://entrez.enphaseenergy.com/tokens"


def obtain_token(username, password, serial):
    """Obtain a JWT token from the Enphase Enlighten cloud API.

    1. Authenticate with username/password to get a session_id
    2. Use session_id + serial to obtain a JWT token for local Envoy access
    """
    # Step 1: Login to Enlighten
    login_resp = requests.post(
        ENLIGHTEN_LOGIN_URL,
        data={"user[email]": username, "user[password]": password},
        timeout=30,
    )
    login_resp.raise_for_status()
    login_data = login_resp.json()
    session_id = login_data.get("session_id")
    if not session_id:
        raise ValueError(
            f"Enlighten login failed: no session_id in response "
            f"(message: {login_data.get('message', 'unknown')})"
        )

    # Step 2: Get token
    token_resp = requests.post(
        ENLIGHTEN_TOKEN_URL,
        json={
            "session_id": session_id,
            "serial_num": serial,
            "username": username,
        },
        timeout=30,
    )
    token_resp.raise_for_status()
    token = token_resp.text.strip()
    if not token or token.startswith("{"):
        raise ValueError(f"Enlighten token request failed: {token[:200]}")

    logger.info("Obtained new Envoy token from Enlighten cloud")
    return token


class Envoy(Powermeter):
    """Powermeter backend that reads grid power from an Enphase Envoy.

    Connects to the Envoy's local API at /production.json?details=1 and
    extracts net consumption (grid) power.  Supports both single-phase
    (total only) and three-phase (per-line) reporting.

    Authentication uses a Bearer token.  If username/password/serial are
    provided, the token is automatically refreshed via the Enphase Enlighten
    cloud API when a 401 is received.
    """

    def __init__(
        self,
        host,
        token="",
        phases=1,
        verify_ssl=False,
        username="",
        password="",
        serial="",
    ):
        if not isinstance(phases, int) or phases < 1:
            raise ValueError("PHASES must be a positive integer")
        self.host = host
        self.token = token
        self.phases = phases
        self.verify_ssl = verify_ssl
        self.username = username
        self.password = password
        self.serial = serial
        self.session = requests.Session()
        if not verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    @property
    def _has_credentials(self):
        return all([self.username, self.password, self.serial])

    def _refresh_token(self):
        """Refresh the token using Enlighten cloud credentials."""
        self.token = obtain_token(self.username, self.password, self.serial)

    def _fetch(self):
        """Fetch production.json from the Envoy, refreshing token on 401."""
        url = f"https://{self.host}/production.json?details=1"

        if not self.token and self._has_credentials:
            self._refresh_token()

        headers = {"Authorization": f"Bearer {self.token}"}
        response = self.session.get(
            url, headers=headers, verify=self.verify_ssl, timeout=10
        )

        if response.status_code == 401 and self._has_credentials:
            logger.warning("Envoy returned 401, refreshing token via Enlighten")
            self._refresh_token()
            headers = {"Authorization": f"Bearer {self.token}"}
            response = self.session.get(
                url, headers=headers, verify=self.verify_ssl, timeout=10
            )

        response.raise_for_status()
        return response.json()

    def get_powermeter_watts(self):
        """Return grid power in watts as a list (one entry per phase)."""
        data = self._fetch()
        consumption_list = data.get("consumption") or []
        if not isinstance(consumption_list, list):
            raise ValueError("Invalid Envoy response: consumption must be a list")

        net_meter = _find_measurement(consumption_list, "net-consumption")
        if net_meter is None:
            logger.error("Envoy response does not expose net-consumption")
            raise ValueError(
                "Envoy response does not expose net-consumption; "
                "grid CTs are required"
            )

        if self.phases == 1:
            return [int(net_meter.get("wNow", 0))]
        else:
            lines = net_meter.get("lines", [])
            values = [int(line.get("wNow", 0)) for line in lines[: self.phases]]
            while len(values) < self.phases:
                values.append(0)
            return values
