from .http_client import HttpPowermeter


class Fronius(HttpPowermeter):
    """Reads a Fronius Smart Meter via the inverter's local Solar API.

    Polls ``GetMeterRealtimeData.cgi`` and, by default, returns the signed total
    real power (``PowerReal_P_Sum``): positive = grid import (consumption),
    negative = feed-in (export). Flip the sign with ``POWER_MULTIPLIER = -1`` if
    your readings are reversed.

    Set ``per_phase=True`` to return the three per-phase real powers
    (``PowerReal_P_Phase_1..3``) instead of the aggregate. Note that several
    meter firmwares report these phase fields *unsigned*, which would break
    export readings — verify your meter reports signed per-phase power before
    enabling it; otherwise stick with the always-signed sum (the default).
    """

    def __init__(self, ip: str, device_id: str = "0", per_phase: bool = False):
        self.ip = ip
        self.device_id = device_id
        self.per_phase = per_phase

    async def get_powermeter_watts(self) -> list[float]:
        response = await self.get_json(
            f"http://{self.ip}/solar_api/v1/GetMeterRealtimeData.cgi"
            f"?Scope=Device&DeviceId={self.device_id}"
        )
        status = response.get("Head", {}).get("Status", {})
        if status.get("Code", 0) != 0:
            reason = (
                status.get("Reason") or status.get("UserMessage") or "unknown error"
            )
            raise ValueError(
                f"Fronius API returned status {status.get('Code')}: {reason}"
            )
        data = response["Body"]["Data"]
        if self.per_phase:
            return [
                float(data.get("PowerReal_P_Phase_1", 0.0)),
                float(data.get("PowerReal_P_Phase_2", 0.0)),
                float(data.get("PowerReal_P_Phase_3", 0.0)),
            ]
        return [float(data["PowerReal_P_Sum"])]
