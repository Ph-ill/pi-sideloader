# Set up the Pi

This is the path from a blank microSD to a phone that refreshes its own apps. Do the steps in order. Nothing here needs Xcode after the first time you have built the IPA on a Mac.

## 1. Parts

- Raspberry Pi Zero W (the single-core, 512 MB board with 2.4 GHz Wi-Fi). Not a Zero 2 W, though a Zero 2 will also run this.
- A microSD card, 8 GB or more.
- UPS-Lite **v1.3**. The fuel gauge is a CW2015. Battery percentage comes from I2C, not from a GPIO pin.
- Waveshare **2.13 inch e-Paper HAT**, black and white. It may be V1, V2, V3, or V4. You do not have to know which one before you boot. Section 6 is how you find out.
- A short micro-USB cable for the UPS-Lite's own charger.
- A **micro-USB OTG to USB-A hub**. The plug that goes into the Pi is micro-USB. The hub has one or more full-size USB-A sockets.
- A **USB-A to USB-C cable** from that hub to the iPhone. The phone in this build is USB-C.
- Optional but recommended: a **powered** USB hub, or a data-only ("charge blocker") adapter. See the power section below. A normal unpowered OTG hub will let the iPhone try to charge from the Pi.

Do not buy the 2.13 inch "B" HAT (black/white/red) or the "D" panel (212×104). The drivers in this project do not talk to them.

## 2. Stack the boards

Power is unplugged while you do this.

1. UPS-Lite underneath the Pi. The pogo pins point **up** into the test pads on the underside of the Pi. The Pi sits right side up, SD card slot at the end you can still reach.
2. e-Paper HAT on the 40-pin header. Pin 1 on the HAT goes to pin 1 on the Pi (the corner by the SD card). The header is the only connection the panel needs. SPI and I2C do not fight: the panel uses SPI (BCM 10, 11, 8, 25, 17, 24) and the fuel gauge uses I2C (BCM 2 and 3).
3. Charge the stack from the **micro-USB socket on the UPS-Lite**, not from the Pi's **PWR** port. The PWR port bypasses the gauge and, on a stack like this, is the wrong place to feed 5 V.

GPIO 4 is the UPS "USB adapter is plugged in" pin, and only if you have soldered the two pads on the back of the UPS. Until those pads are bridged, the pin floats. The software ignores it unless you later set `battery.power_detect_enabled` to true. Leave shutdown off until then. A floating pin must not be allowed to power the Pi off.

## 3. The iPhone cable

The Pi Zero has two micro-USB sockets.

- **PWR**, at the end of the board, is power in. It is not a USB host. Plugging the phone in there does nothing useful and is not how this device works.
- **USB**, the socket in the middle of the board edge, is the USB OTG data port. This is the one the phone uses.

Hookup, in order, from the Pi out to the phone:

1. Micro-USB plug of the OTG hub into the Pi's **USB** port.
2. USB-A plug of the USB-A to USB-C cable into the hub.
3. USB-C plug into the iPhone.

Unlock the phone when you plug it in. The first time, tap **Trust** and enter the phone passcode. A later section covers Developer Mode.

### Power: the phone will try to charge, and the Pi cannot stop it

The Zero's USB data port ties the 5 V pin straight to the Pi's 5 V rail. There is no load switch. `max_usb_current` in `config.txt` is for later Pi boards that have a USB power chip. It does nothing here. The USB controller has no host-mode switch that opens that wire. The daemon logs this once when it sees a phone on USB, and then carries on. There is no software fix to ship.

A USB-C iPhone will ask for charge current. On battery, that current comes out of the UPS-Lite cell, shared with the Pi, the Wi-Fi radio, and the panel. The Zero's 5 V rail sags, the brown-out detector resets the board, and an install dies in the middle. That is the failure this section is trying to avoid.

Do one of these, and do it before you rely on an unattended refresh:

- Leave the **UPS-Lite's own charger plugged in** for the whole refresh. The charger socket is on the UPS board, not the Pi's PWR port. This is the minimum.
- Prefer a **powered hub**, so the phone's 5 V comes from the hub's supply instead of the cell. Some cheap "powered" hubs still let the phone draw from the host, or back-feed the Pi. If the Pi reboots the moment the phone enumerates, the hub is not isolating power.
- A **data-only cable or charge-blocker** (the red 5 V wire disconnected) between the hub and the phone stops the charge path entirely. The phone then runs on its own battery and only data crosses. This is the only arrangement that actually prevents charging. Use it if a powered hub still browns the Pi out.

The Pi will not fast-charge the phone in any of these arrangements. Expect either no charging (data-only cable) or a small, risky trickle (plain OTG hub on battery). Do not plan on the Pi topping the phone up.

## 4. Flash the SD card

Use Raspberry Pi Imager.

- OS: **Raspberry Pi OS Lite (32-bit)**, Bookworm or newer. The 64-bit image is the wrong kernel for a Zero W (ARMv6).
- In the imager's settings, before you write: set the hostname (for example `sideload`), enable SSH with a password, set the Wi-Fi name and password, and set a user. Remember the password.
- Write the card, put it in the Pi, and power the UPS-Lite.

First boot can take a few minutes. From another machine:

```sh
ssh sideload@sideload.local
```

If mDNS is awkward, look the address up in the router.

## 5. Install

On the Pi, as the user you created:

```sh
sudo apt-get update
sudo apt-get install -y git
git clone https://github.com/Ph-ill/pi-sideloader.git
cd pi-sideloader
sudo scripts/install.sh
```

The script is safe to run again. It enables I2C and SPI in `/boot/firmware/config.txt` (or `/boot/config.txt` on older images), builds `libplist`, `libtatsu`, `libimobiledevice-glue`, `libusbmuxd`, `libimobiledevice`, `usbmuxd`, and `ideviceinstaller` from the commits pinned in `scripts/install.sh`, and builds [zsign](https://github.com/zhlynn/zsign) at the commit of pull request 391 (`make -C build/linux -j1`). `libimobiledevice` at that pin needs `libtatsu` >= 1.0.3, which apt does not ship, so the script builds it after `libplist`. Bookworm's phone packages are not used: `libimobiledevice` 1.3.0 is too old for iOS 17, and `ideviceinstaller` 1.1.1 has no `list --xml`. Apt does supply `libcurl` and `libzip` (`libcurl4-openssl-dev`, `libzip-dev`), which `libtatsu` and `ideviceinstaller` require. It also installs `avahi-daemon` so a later Wi-Fi lookup can resolve the phone. It does not enable `systemd-time-wait-sync.service`: that oneshot does not exit until NTP, and `sudo sideload-pi enable` runs `systemctl enable --now`, which would hang on it. The daemon starts anyway. Each tick paints `waiting_for_clock` and does not refresh until the clock is at least 2026-01-01 and `timedatectl show -p NTPSynchronized --value` is `yes`. If the `timedatectl` binary is missing, only the date check applies. A non-zero exit, a timeout, or any other output counts as not synced. A temporary swap file is added only while those compiles run, and only if the Pi has no swap yet.

It does **not** start `sideload-pi.service`. The panel test, `anisette_url`, and `sideload-pi login` come first. `sudo sideload-pi enable` is the step that starts the daemon.

Python's cryptography and Pillow come from apt (`python3-cryptography`, `python3-pil`). Do not pip-install those on a Zero: cryptography wants a Rust compiler and the board will run out of memory. The only pip package is the pure-Python `srp` module, and only if apt did not already provide it.

The daemon runs as the system user `sideload` (groups `gpio`, `spi`, `i2c`, and `plugdev` when those groups exist). `sudo sideload-pi` drops to that user for every command except `enable`, so the Apple ID file stays readable by the service. Installing packages, udev rules, and `systemctl enable` still need root. Low-battery shutdown is the only thing the service does as root, via a sudoers rule that allows `systemctl poweroff` and nothing else. GPIO, SPI, and the usbmuxd socket are what the groups are for; if a group is missing on your image, say so in the install log and the drop-in omits it.

It writes `/etc/sideload-pi/config.json` **only if that file does not exist**, creates `/var/lib/sideload-pi` mode 0700 owned by `sideload`, and generates `secrets/web.token` mode 0600 if it is missing. Re-running the installer does not rotate that token and does not wipe an Apple ID you have already saved.

Reboot once so SPI and I2C come up:

```sh
sudo reboot
```

Then:

```sh
sudo cat /var/lib/sideload-pi/secrets/web.token
```

That token is the password for the web page. It is not your Apple ID.

To remove the service later without deleting the Apple ID: `sudo scripts/uninstall.sh`. Add `--purge` only when you also want `/var/lib/sideload-pi` and `/etc/sideload-pi` gone.

## 6. Which e-paper do you have?

Waveshare's own manual (the 2.13 inch e-Paper HAT page) is the source for this. A few facts that are easy to get wrong:

- The **screen** version and the **driver board** version are not the same thing. Silkscreen that says Rev2.1 or Rev2.2 is the HAT's PCB. An older HAT may have no board revision printed. The driver you select follows the **panel**, not that silkscreen.
- All four black/white 2.13 inch panels this project supports are **250×122** (about 48.55 × 23.71 mm). V1 is not a different resolution from V2. What changes is the init sequence and whether a partial (non-flashing) refresh exists.
- **V2:** Waveshare's Raspberry Pi readme says the V2 logo is stuck to the back of the screen. Use the V2 driver.
- **V3:** a V3 label on the back of the screen. The hardware matches V2, but the demo does not. Use the V3 driver. Partial refresh exists; Waveshare notes it can be slow.
- **V4:** "fully compatible with V3" and it has its own fast-refresh demo. If the sticker says V4, use V4. The default in `config.json` is V4 because that is the current product, not because auto-detect is trustworthy.
- **V1:** the original panel. Waveshare publishes a separate "Datasheet V1" and the driver file is `epd2in13.py` (no `_V2` suffix). There is no V1 sticker described the way V2 and V3 are. If the back of the **screen** has no V2, V3, or V4 mark, treat it as V1 and confirm with the test pattern.

There is no version register to read. `display.version` may be `V1`, `V2`, `V3`, `V4`, or `auto`.

`auto` tries V4, then V3, then V2, and only then V1. The newer inits wait for the BUSY pin, so a stuck panel fails that guess. The V1 init sequence **never reads BUSY**, so a V1 guess cannot fail. If your HAT might be V1, do not leave the setting on `auto`. Set it after the test below.

On the Pi, with the HAT stacked and SPI enabled. If the daemon is already running it holds the panel, and `display-test` refuses until you stop it:

```sh
sudo systemctl stop sideload-pi
sudo sideload-pi display-test V1
```

You should see a card titled **TEST V1**, four solid bars, and a line that says the label should be crisp. Compare it with [docs/previews/test-v1.png](previews/test-v1.png). The command then asks whether that card looked sharp (`y` or `N`, default no). A finished init is not proof: the V1 sequence never reads BUSY, so a wrong driver can return without an error. Nothing is written until you answer `y` or pass `--yes`.

If the card was snow, blank, or the panel stayed busy, answer `n`:

1. Power-cycle the Pi. A wrong waveform can leave the controller busy. Do not chain the four tests without a power cycle.
2. `sudo sideload-pi display-test V2`, then V3, then V4, same question.

A yes records the driver in `/var/lib/sideload-pi/display-version`. The daemon and `enable` use that file when it exists, including when `display.version` is still the shipped default `V4` or `auto`. With no file, the installed default `V4` is an explicit choice and `enable` accepts it. `auto` with no confirmed file is refused. You can also set the version yourself in `/etc/sideload-pi/config.json`:

```json
"display": { "version": "V1", "allow_partial": true }
```

Use the version that worked. `allow_partial` does not give V1 a partial refresh. V1's published `display()` always rewrites the whole frame and activates it with command `0xC4`. The partial lookup table in that file is never used by `display()`, and this project does not use it either. On V1 the progress bar is a full refresh, and only after the percent has moved by `full_only_percent` (default 20) or `full_only_seconds` (default 15) have passed. That is deliberate: a full flash on every installer tick wears the panel and is slow.

V2, V3, and V4 partial-refresh the progress bar, and take a full refresh when the screen changes, after every `full_refresh_every` partials (default 8; Waveshare's note is about five), or every `full_refresh_minutes` (default 30). Set `allow_partial` to false if you want the slower full-only behavior on those panels too.

The daemon is not running yet. Do not start it here. Next is the anisette URL, then login, then `sudo sideload-pi enable`.

`sudo sideload-pi display-test V2 --preview /tmp/v2.png` writes the card to a PNG and does not touch the panel. Useful over SSH if you want to look at the artwork, not useful for identifying the glass.

## 7. Anisette, somewhere that is not this Pi

Apple's login needs a short-lived device identity called anisette. The library that produces it ships for ARMv7, ARM64, and x86. A Zero is ARMv6. It cannot run that library, and this project will not pretend otherwise.

Run [Dadoum's anisette-v3-server](https://github.com/Dadoum/anisette-v3-server) on a machine that can (a Linux box, a newer Pi, a small VPS). Give it a **persistent disk**. A fresh identity on every boot makes Apple ask for a verification code every time. Do not point this Pi at a public anisette server: you would share an identity with strangers, and Apple locks those.

The client reads `GET /v3/client_info` for the `user_agent` and `client_info` pair, and `GET /` for the one-time password. Since 12 October 2026 Apple rejects a login (`-80045`) when those two headers do not match the program that produced the anisette data. Do not point this at a server that only speaks the old header set.

If Apple answers `hsc` 434, the client asks the server to resynchronize and retries the login once with the same SRP public key, but only when that request succeeds. Dadoum's current `anisette-v3-server` has no resync route, and restarting it does not apply a resync. Login stops. Re-provision by wiping that server's provisioning data, or wait and try the login again. A server that answers `POST /v3/resync` or `GET /reprovision` with success still gets the one retry.

Put the URL in the config. A Tailscale address is a good choice, so the Pi can reach it from a cafe without opening a port:

```json
"anisette_url": "http://100.x.y.z:6969"
```

No trailing path is required. The client requests `{url}/v3/client_info` first (User-Agent and client info), then `GET {url}/` for the one-time password.

## 8. Apple ID

Use an Apple ID you can afford to put on a single-purpose device. The password is stored so a refresh on day 6 can sign in again without you. The file is `/var/lib/sideload-pi/secrets/apple-id.json`, mode 0600, in a directory mode 0700. The SD card is not encrypted unless you set that up yourself. Anyone with the card can read the password.

```sh
sudo sideload-pi login
```

Enter the email and the password. The password is not echoed. If Apple sends a code to your trusted devices, type the 6 digits. That code is not saved. If you are not at the keyboard, the e-paper screen says a code is needed and you can submit it from another shell or from the web page:

```sh
sudo sideload-pi 2fa 123456
```

You have 10 minutes (`two_fa_timeout_seconds`) before that attempt gives up. Trigger the refresh again afterwards.

A free Personal Team, per Apple's account help, can have up to **10 App IDs** and **3 devices**, each expiring after 7 days, and **3 apps installed** on a device. Profiles expire 7 days after they are issued. Re-signing an app that already has an App ID does not spend a new slot. Creating that App ID again, including the same bundle after Apple dropped it, does. This tool refreshes on day 6 by default (`refresh_margin_days` is 1). The certificate is reused until it expires inside that margin. Replacing it revokes only the certificate this tool recorded (serial and id in `secrets/development.json`), using the v1 certificates API. If Apple then says the slot is full (result 35 or 7460), that same certificate is revoked and the CSR is submitted once more. Other certificates on the account are left alone. If the slot is still full, revoke a different certificate in Xcode and try again.

App extensions and a watch app each need their own App ID, and they count toward both limits. By default they are stripped at sign time (`strip_extensions` and `strip_watch`). The status line says so when that happens. Turn the flags off only if you have the slots and you need the extension.

The daemon starts only after the panel test, `anisette_url`, and this login:

```sh
sudo sideload-pi enable
```

That refuses to start while `anisette_url` is empty or no Apple ID is saved. The installed `display.version` is `V4`, and that explicit value is enough. `auto` is refused until `display-test` has recorded a driver you confirmed (or `--yes`). `enable` does not wait for NTP. The daemon starts, and a tick paints `waiting_for_clock` until the clock is synced.

## 9. Trust and Developer Mode

Plug the phone in with the cable from section 3. Unlock it.

```sh
sudo sideload-pi pair
```

Tap Trust. On iOS 16 and later, after the first developer install (or after connecting to a computer), enable Developer Mode: **Settings → Privacy & Security → Developer Mode**, then reboot the phone and confirm. Without it, install fails even when the profile is valid. The error screen will show the text `ideviceinstaller` returned. It will not show your password.

The daemon also runs `idevicepair wifi on` after a USB pair, when that subcommand exists. Wi-Fi install is best-effort. It only works after a USB pair, and only if the phone and this build of libimobiledevice agree. USB remains the path to count on. `prefer_wifi` defaults to false.

## 10. Add an IPA

Copy an IPA you built (an Xcode archive exported for development, or the `.ipa` your project already produces) to the Pi and:

```sh
sudo sideload-pi add ~/Notes.ipa
sudo sideload-pi refresh
```

Or open the web page over an SSH tunnel:

```sh
ssh -L 8741:127.0.0.1:8741 sideload@sideload.local
```

Browse to `http://127.0.0.1:8741/`, paste the web token, and upload the IPA. The page is a single file form. The token is a cookie (`HttpOnly`, `SameSite=Strict`) or `Authorization: Bearer`. It is not put in the URL.

From then on, plug the phone into the USB port when the idle screen says an app is due, or leave the daemon to notice the USB insert (a udev rule sends `SIGUSR1`; the daemon also polls). One app is refreshed per pass, soonest expiry first. A failure waits 5 minutes, then 15, then 45, up to 6 hours, and the screen shows the error.

`sudo sideload-pi refresh --app com.example.notes` asks for that one bundle even if it is not due yet. If the service is already running, the command writes a flag and signals the daemon instead of starting a second copy.

## 11. Tailscale

Install Tailscale the way their Pi OS instructions describe, and `tailscale up`. The idle screen prefers a 100.64.0.0/10 address when it sees one, so you can read the address off the panel. SSH to that address from your phone or laptop. Leave `web.bind` at `127.0.0.1`. If you truly want the page on the tailnet interface, set `web.bind` to that address and `web.allow_remote` to true, and do not do that on a network you do not trust. The token is the only check.

## 12. What the panel is telling you

- **Idle:** up to four apps, days left, battery percent, whether the gauge thinks the UPS charger is in (only after the pads are soldered and power detect is on), Wi-Fi, and an IP.
- **Waiting:** a refresh is due and no phone is visible. The cable is the USB port, not PWR.
- **Apple / Signing / Installing:** the pass in progress. On V2/V3/V4 the bar moves without a full flash. On V1 it flashes, less often.
- **Installed:** the new profile's whole days remaining. If iOS asks you to trust the developer, that is on the phone: Settings → General → VPN & Device Management.
- **Needs attention:** the last error, with anything that looked like a password or token removed.
- **Apple ID code:** type it with `sideload-pi 2fa` or the web form.
- **Battery low:** only if you turned shutdown on. Otherwise the footer just says to plug the UPS in.

## 13. Optional low-battery shutdown

Solder the two pads on the back of the UPS-Lite so GPIO 4 is actually connected. Then, in the config:

```json
"battery": {
  "power_detect_enabled": true,
  "shutdown_on_low_battery": true,
  "low_percent": 8,
  "confirm_readings": 2
}
```

Shutdown runs only when power-detect is on, the pin says the adapter is **unplugged**, and two readings in a row are under 8%. A failed I2C read does not shut the Pi down. The default is off, on purpose.

## 14. When something fails

- **Blank or snowy panel:** wrong `display.version`. Power-cycle and run `display-test` one driver at a time. Section 6.
- **Panel never comes out of a refresh:** BUSY stayed high. Power-cycle. Check the ribbon and that pin 1 matches.
- **`No anisette server is configured`:** section 7.
- **Apple rejected the password or the code:** the message names the Apple error code (`-20101` / `-22406` password, `-21669` code, `-22421` anisette). Fix that, then `sideload-pi login` again.
- **App ID limit:** 10 new App IDs in 7 days, or Apple returned result `9120`. Wait, or stop refreshing an app you no longer use. Re-signing an existing bundle id is free.
- **Device limit:** a free team can register 3 devices. The tool only blocks the fourth up front once it has seen a free profile. Otherwise it tries, and shows Apple's error.
- **Install verification failed:** Developer Mode, Trust, or the profile does not list this phone. Pair again on USB.
- **Pi reboots when the phone is plugged in:** section 3. Charger on the UPS, or a powered hub, or a data-only cable.
- **Logs:** `journalctl -u sideload-pi -f` and `/var/lib/sideload-pi/logs/sideload-pi.log`. Passwords and tokens are stripped from log lines. The verification code is not written to the log on purpose; don't add it.

Known limits that are not bugs you can configure away are in [LIMITATIONS.md](LIMITATIONS.md).
