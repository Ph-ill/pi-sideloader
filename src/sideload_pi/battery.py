"""UPS-Lite v1.3 / CW2015 fuel gauge.

Vendor script (linshuqin329/UPS-Lite, UPS_Lite_V1.3_CW2015.py) and the
board's instruction PDF:

- CW2015 I2C address 0x62 on bus 1
- VCELL register 0x02, voltage = raw * 0.305 mV (raw is big-endian)
- SOC register 0x04, percent = raw / 256 (high byte is the integer percent)
- MODE register 0x0A, write 0x30 to quick-start a stuck gauge
- BCM GPIO 4 is high when the USB power adapter is plugged in, low when it
  is not. The two pads on the back of the board must be soldered together
  or GPIO 4 is not connected and must not be trusted.

smbus.read_word_data returns the register little-endian, so the vendor swaps
the bytes. `decode_smbus_word` does that swap. Nothing here talks to hardware;
`Cw2015` is a thin reader over an injected bus.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

CW2015_ADDRESS = 0x62
REG_VCELL = 0x02
REG_SOC = 0x04
REG_MODE = 0x0A
QUICKSTART = 0x30

# GPIO is high when external power is inserted (vendor script).
POWER_GPIO_BCM = 4


def decode_smbus_word(raw: int) -> int:
    """Swap an smbus word into the CW2015's big-endian register value."""
    swapped = struct.unpack("<H", struct.pack(">H", raw & 0xFFFF))[0]
    return swapped


def voltage_volts(raw_word: int) -> float:
    return decode_smbus_word(raw_word) * 0.305 / 1000.0


def soc_percent(raw_word: int) -> float:
    return decode_smbus_word(raw_word) / 256.0


def soc_plausible(percent: float) -> bool:
    return 0.0 <= percent <= 100.0


@dataclass(frozen=True)
class BatteryReading:
    percent: float | None
    volts: float | None
    charging: bool | None
    raw_error: str | None = None

    @property
    def ok(self) -> bool:
        return self.percent is not None and self.raw_error is None


def should_shutdown(
    recent_percents: list[float],
    *,
    charging: bool | None,
    enabled: bool,
    threshold: float,
    confirm: int,
) -> bool:
    """Shut down only on repeated low readings while we know we are unplugged.

    A missing charging signal (pads not soldered, or GPIO unread) never shuts
    the Pi down. A single glitch reading does not either.
    """
    if not enabled:
        return False
    if charging is not False:
        return False
    if len(recent_percents) < confirm:
        return False
    window = recent_percents[-confirm:]
    return all(sample < threshold for sample in window)


class Cw2015:
    """Read the gauge. `bus` is an smbus-like object, `power_high` a callable."""

    def __init__(self, bus, address: int = CW2015_ADDRESS, power_high=None):
        self.bus = bus
        self.address = address
        self.power_high = power_high
        self._started = False

    def quickstart(self) -> None:
        # Vendor writes a word of 0x30 to MODE. Match that, not a byte write.
        self.bus.write_word_data(self.address, REG_MODE, QUICKSTART)
        self._started = True

    def read(self) -> BatteryReading:
        try:
            if not self._started:
                self.quickstart()
            volts = voltage_volts(self.bus.read_word_data(self.address, REG_VCELL))
            percent = soc_percent(self.bus.read_word_data(self.address, REG_SOC))
        except OSError as exc:
            return BatteryReading(None, None, self._charging(), str(exc))
        if not soc_plausible(percent):
            return BatteryReading(None, volts, self._charging(), "implausible state of charge")
        return BatteryReading(percent, volts, self._charging(), None)

    def _charging(self) -> bool | None:
        if self.power_high is None:
            return None
        try:
            return bool(self.power_high())
        except OSError:
            return None
