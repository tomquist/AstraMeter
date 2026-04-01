from b2500_meter.config.logger import logger

from .base import Powermeter


class TransformedPowermeter(Powermeter):
    """
    A wrapper around a powermeter that applies a linear transformation
    (multiplier and offset) to each returned power value.

    Formula per value: value * multiplier + offset

    Supports per-phase configuration: if a single multiplier/offset is given,
    it applies to all phases. If multiple values are given, each applies to
    the corresponding phase.
    """

    def __init__(
        self,
        wrapped_powermeter: Powermeter,
        offsets: list[float],
        multipliers: list[float],
    ) -> None:
        if not offsets:
            raise ValueError("offsets must be a non-empty list")
        if not multipliers:
            raise ValueError("multipliers must be a non-empty list")
        self.wrapped_powermeter = wrapped_powermeter
        self.offsets = offsets
        self.multipliers = multipliers
        self._offsets_mismatch_warned = False
        self._multipliers_mismatch_warned = False

    async def wait_for_message(self, timeout=5):
        return await self.wrapped_powermeter.wait_for_message(timeout)

    async def start(self):
        await self.wrapped_powermeter.start()

    async def stop(self):
        await self.wrapped_powermeter.stop()

    def _apply_transform(self, values: list[float]) -> list[float]:
        result = []
        for i, value in enumerate(values):
            multiplier = self.multipliers[i % len(self.multipliers)]
            offset = self.offsets[i % len(self.offsets)]
            result.append(value * multiplier + offset)

        if len(self.offsets) > 1 and len(self.offsets) != len(values):
            if not self._offsets_mismatch_warned:
                logger.warning(
                    "POWER_OFFSET has %d values but powermeter returned %d phases",
                    len(self.offsets),
                    len(values),
                )
                self._offsets_mismatch_warned = True
        else:
            self._offsets_mismatch_warned = False

        if len(self.multipliers) > 1 and len(self.multipliers) != len(values):
            if not self._multipliers_mismatch_warned:
                logger.warning(
                    "POWER_MULTIPLIER has %d values but powermeter returned %d phases",
                    len(self.multipliers),
                    len(values),
                )
                self._multipliers_mismatch_warned = True
        else:
            self._multipliers_mismatch_warned = False

        return result

    async def get_powermeter_watts(self) -> list[float]:
        values = await self.wrapped_powermeter.get_powermeter_watts()
        return self._apply_transform(values)
