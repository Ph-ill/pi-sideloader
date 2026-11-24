# Architecture

Python 3.11+ orchestrates. The heavy work is native: `zsign` signs the IPA, and Debian's `ideviceinstaller` / `usbmuxd` / `libimobiledevice` talk to the phone. A Zero W has 512 MB and an ARMv6 core. That rules out the tools people usually reach for, which is why they are not vendored here.

## What was not used, and why

- **NyaMisty AltServer-Linux 0.0.5.** The published binaries are armv7, aarch64, i586, and x86_64. An armv7 binary does not run on a Zero (it dies with SIGILL). Its bundled `ldid` signer writes a code directory with both SHA-1 and SHA-256. AMFI on iOS 17 and later rejects that (the report is code 85). This project signs with `zsign -2` so only SHA-256 is requested. That flag is the published fix; it was not re-checked on a device in this repo.
- **dragoshont/AltServer-Linux.** It does switch the signer to zsign and has been reported working on new iOS, but the build wants Boost, cpprestsdk, and corecrypto, and the containers are amd64/arm64. That is not a fit for this board.
- **Dadoum Sideloader and splice.** Written in D. There is no reliable ARMv6 toolchain story, and the anisette side loads Apple's ADI shared library (armeabi-v7a / arm64 / x86 only).
- **sideloop, and AltServer in Docker.** amd64/arm64 images. A Zero cannot run them.
- **netmuxd.** A Rust proxy for AltStore's heartbeat protocol. There is no ARMv6 build, and this project installs with `ideviceinstaller` directly, so it does not need that heartbeat.
- **A local anisette provider.** Same ADI library problem. The Pi calls a remote anisette-v3-server (`GET /` returns the header JSON AltServer already consumes). The server's identity has to persist on disk or Apple asks for a code on every login. There is no default public server.

`zsign` is C++ and MIT-licensed. The install script builds it with `g++ -j1`. The key and certificate are PEM files mode 0600. The password is not passed on the command line, because `ps` would show it. `-E` and `-W` strip extensions and a watch app by default so they do not consume App IDs. `-f` replaces a profile that changes every week. FairPlay payloads (`SC_Info/`) are rejected before signing.

## Apple, at the level the code follows

The login and provisioning calls follow AltSign (`ALTAppleAPI+Authentication.m`) and the pure-Python client in nythepegasus/grandslam. They were not executed against Apple's servers in CI. The unit test runs a local SRP peer and checks the SPD and app-token crypto round-trip. That proves the glue, not that Apple still speaks this dialect.

- GrandSlam `POST https://gsa.apple.com/grandslam/GsService2`, plist, header version 1.0.1.
- SRP through the `srp` package's pure-Python module with `rfc5054_enable()` and `no_username_in_x()`. That is the combination those clients use to avoid error `-22406`. The password material is PBKDF2-HMAC-SHA256: `s2k` uses the raw SHA-256 of the password, `s2k_fo` uses the hex of that digest.
- SPD is AES-256-CBC. The key is HMAC-SHA256 of the session under `"extra data key:"`, and the IV is the first 16 bytes of the same under `"extra data iv:"`.
- The negotiation proof is checked. A missing or mismatched proof is a hard failure. The length prefix in that digest is little-endian. This was not confirmed live against Apple.
- `pysrp`'s `User.verify_session` returns `None` even when the proof matches, so the code checks `authenticated()` afterwards.
- A trusted-device code is `GET /auth/verify/trusteddevice` and then `GET /grandslam/GsService2/validate` with a `security-code` header. SMS uses the phone verify endpoints. After a code is accepted the whole login runs once more. A second code request fails closed.
- The Xcode token is not `GsIdmsToken`. An `apptokens` request for `com.apple.gs.xcode.auth` returns `et`, unwrapped as AES-GCM with the `XYZ` envelope those clients document. `X-Apple-GS-Token` is base64 of `adsid + ":" + that token`.
- Developer services are `https://developerservices2.apple.com/services/QH65B2/` with client id `XABBG36SBA`. The calls used are list teams, list/add devices, list/add App IDs, submit a development CSR, and download the team profile. Result `9120` is the App ID limit. HTTP 401 logs in once and retries once.
- The profile expiry is read back out of the CMS profile (the embedded XML plist), not assumed to be 7 days. A profile whose lifetime is 8 days or less, or whose `IsFreeProvisioningProfile` is true, marks the account as free.
- A fourth device is blocked locally only after the account is already known to be free. Otherwise the add is attempted and Apple's error is shown. There is no reliable "free team" flag on `listTeams` (a free personal team and a paid individual team can both come back as Individual).

The development key is RSA 2048, stored unencrypted at mode 0600 so zsign does not need a passphrase. The certificate is refreshed when it expires inside the same margin as the profile. Certificates are not revoked automatically. Sources disagree on whether a free development certificate itself lasts 7 days or longer; refreshing inside the margin covers the shorter claim without a revoke.

## The daemon

`tick(now)` is one pass and is what the tests call. The process handles `SIGUSR1` (udev, or `sideload-pi refresh`) and `SIGTERM`. One due app per pass. A force file ignores the due date and the backoff. State lives in `/var/lib/sideload-pi/state.json` and does not contain the password. Logs go through a filter that strips password, token, and anisette header values.

USB is preferred. `idevice_id -l -n` and `ideviceinstaller -n` are the Wi-Fi path, after a USB pair. If `ideviceinstaller list` already shows the bundle, the install is an `upgrade` so the app's data stays. Progress lines are the ones `ideviceinstaller`'s status callback prints: `Install: Stage (N%)`, with a carriage return between updates. When the tool omits a percent, a fixed stage table moves the bar. A percent from the device always wins.

## The panel

UI bitmaps are 250×122, mode `1`, drawn with Pillow's default font. The driver rotates them into the controller's 122×250 buffer. V2 and V3 use the lookup tables from Waveshare's demos. V4 uses that demo's fast-refresh commands (it does not take a software LUT the way V3 does). V1 follows `epd2in13.py`: different init (booster, VCOM, a 30-byte LUT), a different bitmap pack (no V2 mirror), and a full-frame update command `0xC4` written a row at a time. See [SETUP.md](SETUP.md) for the sticker and the `display-test` command.

Busy is high while the controller works. That matches all four demos.

## The battery

CW2015 at I2C `0x62` on bus 1, the way the UPS-Lite v1.3 vendor script reads it. `smbus` word reads are byte-swapped. Voltage is `raw * 0.305 mV`. State of charge is `raw / 256` (the high byte is the integer percent). A quick-start writes word `0x30` to register `0x0A`. GPIO 4 is high when the adapter is in, and only if the pads are soldered. Shutdown requires that signal to be explicitly enabled and to read "not charging", twice, under the threshold. An I2C error never shuts the Pi down.

## USB power

Documented in [SETUP.md](SETUP.md). The data-port VBUS pin is wired to 5 V. The service does not try to clear a sysfs bit that this board does not have. The practical mitigations are a powered hub, a data-only cable, and leaving the UPS charger connected during a refresh.

## The web page

A threaded stdlib HTTP server inside the daemon, bound to loopback unless `web.allow_remote` is set. Token in an HttpOnly cookie or a Bearer header, compared in constant time. Upload size defaults to 200 MB. No token in the query string.
