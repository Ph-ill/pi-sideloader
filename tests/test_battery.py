import pytest

from sideload_pi.battery import (
    BatteryReading,
    Cw2015,
    decode_smbus_word,
    should_shutdown,
    soc_percent,
    voltage_volts,
)


def test_vendor_word_swap_matches_the_high_byte_percent():
    # smbus returns the register little-endian. 0x0032 swaps to 0x3200 = 50%.
    assert decode_smbus_word(0x0032) == 0x3200
    assert soc_percent(0x0032) == pytest.approx(50.0)
    assert voltage_volts(0x0032) == pytest.approx(0x3200 * 0.305 / 1000.0)


class _Bus:
    def __init__(self, words):
        self.words = words
        self.writes = []

    def write_word_data(self, address, register, value):
        self.writes.append((address, register, value))

    def read_word_data(self, address, register):
        return self.words[register]


def test_gauge_reads_percent_and_does_not_invent_charging():
    bus = _Bus({0x02: 0x0032, 0x04: 0x0032})
    gauge = Cw2015(bus, power_high=None)
    reading = gauge.read()
    assert reading.percent == pytest.approx(50.0)
    assert reading.charging is None
    assert bus.writes[0][1:] == (0x0A, 0x30)


def test_i2c_failure_is_not_a_reading():
    class Dead:
        def write_word_data(self, *args):
            raise OSError("remote I/O error")

        def read_word_data(self, *args):
            raise OSError("remote I/O error")

    reading = Cw2015(Dead()).read()
    assert reading.percent is None
    assert reading.raw_error


def test_shutdown_needs_repeated_low_samples_while_unplugged():
    assert should_shutdown([5], charging=False, enabled=True, threshold=8, confirm=2) is False
    assert should_shutdown([5, 5], charging=None, enabled=True, threshold=8, confirm=2) is False
    assert should_shutdown([5, 5], charging=True, enabled=True, threshold=8, confirm=2) is False
    assert should_shutdown([5, 5], charging=False, enabled=False, threshold=8, confirm=2) is False
    assert should_shutdown([5, 9], charging=False, enabled=True, threshold=8, confirm=2) is False
    assert should_shutdown([4, 5], charging=False, enabled=True, threshold=8, confirm=2) is True
    assert isinstance(BatteryReading(1, 3.2, False), BatteryReading)
