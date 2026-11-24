# Limitations

These are limits of the hardware or of what was actually run, not a to-do list hidden in the code.

## Not verified on hardware or against Apple

CI runs on a desktop Python. It does not have a Pi, a panel, a UPS, an iPhone, or an Apple ID.

- GSA login, 2FA, the negotiation proof's endianness, and the developer-services calls were checked against the AltSign / grandslam sources and a **local** SRP peer. They were not sent to `gsa.apple.com` or `developerservices2.apple.com`.
- `zsign -2` (SHA-256-only code directory) is on by default because of the public iOS 17+ AMFI failure with dual SHA-1/SHA-256 directories. It was not confirmed on a device here.
- USB install progress parsing matches `ideviceinstaller`'s source. It was not run against a phone. Wi-Fi install (`idevice_id -n`, `ideviceinstaller -n`, `idevicepair wifi on`) is best-effort and was not run either. Some builds of `idevicepair` do not have a `wifi` subcommand; the failure is ignored.
- The e-paper init sequences are ports of Waveshare's Python demos. None of V1, V2, V3, or V4 was powered in this project. `auto` is a best-effort BUSY probe and **cannot reject a wrong V1 guess**, because that demo's init never reads BUSY. Use `display-test` and the sticker.
- The CW2015 decode follows the UPS-Lite v1.3 vendor script. The gauge was not read on a board. GPIO 4 was not sampled.
- A Pi Zero's USB port was not measured with a USB-C phone on the end of an OTG hub. The brown-out warning is from the board schematic (VBUS tied to 5 V) and from how an iPhone negotiates charge, not from a scope capture in this repo.

## By design

- **Not self-contained for Apple login.** Anisette has to come from another machine. A public server is a bad idea and is not the default.
- **The phone can draw current from the UPS, and software cannot stop it.** See [SETUP.md](SETUP.md). Use the UPS charger, a powered hub, or a data-only cable.
- **V1 progress is a slow full refresh.** The upstream V1 driver has no partial display path. Flashing on every percent tick would be worse.
- **Free Apple ID quotas** are enforced before the call when we already know them: 3 apps, 10 App IDs in 7 days, and a 4th device only after a free profile has been seen. Apple can still say no for a reason we did not predict. Extensions count if you turn stripping off.
- **FairPlay IPAs** (anything with `SC_Info/`) are refused. This will not install App Store apps.
- **Certificates are not revoked** by this tool. A rejected CSR has to be cleaned up in Xcode.
- **The Apple ID password sits on the SD card** in a root-only file. That is what unattended refresh requires. Use a dedicated Apple ID. Turn on full-disk encryption yourself if you want it; Raspberry Pi OS Lite does not.
- **Low-battery shutdown is off** until the UPS pads are soldered and both config flags are set. A floating GPIO 4 must not be trusted.
- **The web UI is loopback-only** until you opt in. Tailscale SSH is the remote path this was written for.
- **One app per pass.** A library of three due apps takes three ticks.
- **The Pi will not reliably charge the iPhone.** Even when VBUS is connected, plan on the phone's own battery, not on the Zero as a charger.
