"""Why a plugged-in iPhone can brown out a Pi Zero, and why code cannot stop it.

The Zero's micro-USB data port (the socket labeled USB, not PWR) wires VBUS
straight to the 5 V rail. There is no load switch. The dwc2 USB controller
has no host-mode sysfs control that opens that wire, and ``max_usb_current``
in config.txt only exists on later Pi boards that have a USB power switch.
A USB-C iPhone will try to charge. On battery that current comes out of the
UPS-Lite cell. The practical fixes are a powered hub, a data-only cable, and
leaving the UPS-Lite charger connected during a refresh. See docs/SETUP.md.
"""

from __future__ import annotations

NOTE = (
    "iPhone is on the USB data port. This Pi cannot turn off VBUS, so the "
    "phone may charge from the UPS-Lite battery. Use a powered hub or a "
    "data-only cable, and keep the UPS-Lite charger plugged in during a refresh."
)

_logged = False


def note_phone_on_usb(logger) -> None:
    """Log the power warning once per process. Safe to call on every tick."""
    global _logged
    if _logged:
        return
    _logged = True
    logger.info("%s", NOTE)


def reset_note_for_tests() -> None:
    global _logged
    _logged = False
