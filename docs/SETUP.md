# Set up the Pi

Do these steps in order. You need an IPA you already built on a Mac. This will not install App Store apps.

## What you need

- Raspberry Pi Zero W, a microSD card (8 GB or more), and Raspberry Pi OS Lite **32-bit** Bookworm or newer. The 64-bit image is the wrong kernel.
- UPS-Lite **v1.3** (CW2015 fuel gauge) and its own micro-USB charger cable.
- Waveshare **2.13 inch** black-and-white e-Paper HAT. V1, V2, V3, and V4 all work. Do not buy the red/black "B" HAT or the 212×104 "D" panel.
- A micro-USB OTG hub (micro-USB plug into the Pi, USB-A sockets on the hub) and a USB-A to USB-C cable for the phone.
- A free Apple ID you can dedicate to this Pi. Login uses `https://mac-ani.jkcoxson.com` unless you change it later.

## 1. Flash the card and turn on SSH

Use Raspberry Pi Imager. Choose **Raspberry Pi OS Lite (32-bit)**. Before you write, set a hostname (for example `sideload`), enable SSH with a password, set the Wi-Fi name and password, and set a user.

Write the card, put it in the Pi, and power the UPS-Lite. First boot can take a few minutes.

```sh
ssh sideload@sideload.local
```

If that name does not resolve, use the address from your router.

## 2. Wire the stack

Power is unplugged while you stack the boards.

1. UPS-Lite under the Pi. The pogo pins point **up** into the test pads. The SD card slot stays reachable.
2. e-Paper HAT on the 40-pin header. Pin 1 on the HAT goes to pin 1 on the Pi (the corner by the SD card).
3. Charge the stack from the **micro-USB socket on the UPS-Lite**, not from the Pi's **PWR** port.

The Pi Zero has two micro-USB sockets. **PWR** (at the end of the board) is power in and is not a USB host. **USB** (in the middle of the board edge) is the data port. The phone uses **USB**:

1. OTG hub into the Pi's **USB** port.
2. USB-A to USB-C cable into the hub.
3. USB-C into the iPhone.

The phone will try to charge from that port, and the Pi cannot turn that off. On battery the Zero can brown out and reboot in the middle of an install. Leave the UPS-Lite charger plugged in for every refresh. A powered hub or a data-only cable is safer if the Pi still reboots when the phone connects. Do not plan on the Pi charging the phone.

## 3. Install

On the Pi, as the user you created. This clones `main`. There is no branch to check out.

```sh
sudo apt-get update
sudo apt-get install -y git
git clone https://github.com/Ph-ill/pi-sideloader.git
cd pi-sideloader
sudo scripts/install.sh
sudo reboot
```

On a Pi Zero this downloads the published ARMv6 tools (`prebuilt-2026-11-24`). It does not compile them, and it does not start the refresh daemon. After the reboot, the web token is:

```sh
sudo cat /var/lib/sideload-pi/secrets/web.token
```

## 4. Check the panel

The shipped driver is V4. With the HAT stacked:

```sh
sudo sideload-pi display-test V4
```

You should see a card titled **TEST V4**. Answer `y` only if that label is sharp. A wrong driver can return without an error, so a blank or snowy card is a no: power-cycle, then try `V1`, then `V2`, then `V3`. Do not chain the four tests without a power-cycle. The sticker on the **back of the screen** (not the Rev2.x print on the HAT) is the version. How that choice is stored is in [Details](#which-panel) below.

## 5. Sign in

That public anisette server is shared. Other people present the same device identity to Apple, so Apple can lock the Apple ID, and the server can be down or go away. Use an Apple ID you can lose, not the one you buy apps with. The password is stored at `/var/lib/sideload-pi/secrets/apple-id.json`, mode 0600, in a directory mode 0700. The SD card is not encrypted unless you set that up.

```sh
sudo sideload-pi login
```

Type the email and the password. The password is not echoed. If Apple sends a code, type the 6 digits. The code is not saved. From another shell:

```sh
sudo sideload-pi 2fa 123456
```

You have 10 minutes. A free Personal Team can have **3 apps** installed, **10 App IDs** in 7 days, and **3 devices**. Profiles expire **7 days** after they are issued. This tool refreshes on day 6. Re-signing an app that already has an App ID does not spend a new slot. Creating that App ID again does. Extensions and a watch app are stripped by default so they do not use extra slots.

## 6. Start the daemon

```sh
sudo sideload-pi enable
```

That refuses to start until an Apple ID is saved. The shipped panel setting `V4` is enough. You do not set an anisette URL.

## 7. Pair the phone

Plug the phone into the **USB** port, not **PWR**, and unlock it.

```sh
sudo sideload-pi pair
```

Tap Trust. On iOS 16 and later, turn on **Settings → Privacy & Security → Developer Mode**, reboot the phone, and confirm. Without that, install fails even when the profile is valid.

## 8. Add an app

Copy your IPA to the Pi, then:

```sh
sudo sideload-pi add ~/Notes.ipa
sudo sideload-pi refresh
```

After that, plug the phone into the USB port when the idle screen says an app is due. The daemon also notices a USB insert. One app is refreshed per pass. `sudo sideload-pi status` shows what is installed. `sudo sideload-pi refresh --app com.example.notes` refreshes that one bundle even if it is not due yet.

To upload from a laptop instead, tunnel the page and open `http://127.0.0.1:8741/`:

```sh
ssh -L 8741:127.0.0.1:8741 sideload@sideload.local
```

Paste the web token. It is not your Apple ID.

## Details and troubleshooting

### Which panel

All four supported panels are 250×122. What changes is the init sequence, not the resolution. `sudo sideload-pi display-test V2 --preview /tmp/v2.png` writes the card to a PNG and does not touch the glass. `sudo sideload-pi display-test V4 --yes` records the driver without the question. A yes writes `/var/lib/sideload-pi/display-version`, and that file wins over `config.json`. With no file, `V4` is what `enable` accepts. `auto` is refused until a test has been confirmed. If `sideload-pi.service` is already running, stop it first (`sudo systemctl stop sideload-pi`); the test will not share the panel.

V1 has no partial refresh, so the progress bar is a full flash, and only after the percent has moved by 20 or 15 seconds have passed. V2, V3, and V4 move the bar without a full flash.

### Anisette

You do not run a server, and you do not edit `anisette_url` for a normal install. The config already contains `https://mac-ani.jkcoxson.com`. The client checks the HTTPS certificate in the usual way.

A different URL is optional. A self-hosted Linux anisette server is currently rejected by Apple (`ec=-29003` / `-80045`): its Android data is presented as a Mac ([Dadoum issue 61](https://github.com/Dadoum/anisette-v3-server/issues/61)). Set `anisette_url` to an empty string only if you want login to stop. How the client uses that server is in [ARCHITECTURE.md](ARCHITECTURE.md).

### Codes, limits, and the password file

`sudo sideload-pi 2fa 123456` is a trusted-device code. If Apple offered several SMS numbers, send `id:123456` (the id it printed, then the 6 digits). The same form is on the web page.

App extensions and a watch app are removed at sign time unless you set `strip_extensions` or `strip_watch` to false. The status line says so when that happens.

If iOS asks you to trust the developer after an install: **Settings → General → VPN & Device Management**.

### Web page and Tailscale

The page listens on `127.0.0.1:8741`. Leave it there. Install Tailscale the way their Pi OS instructions describe, then `tailscale up`, and SSH to the address on the panel. Uploads are capped at 64 MB.

### Optional low-battery shutdown

Leave this off unless you have soldered the two pads on the back of the UPS-Lite so GPIO 4 is really connected. Then, in `/etc/sideload-pi/config.json`:

```json
"battery": {
  "power_detect_enabled": true,
  "shutdown_on_low_battery": true,
  "low_percent": 8,
  "confirm_readings": 2
}
```

Shutdown runs only when power-detect is on, the pin says the adapter is unplugged, and two readings in a row are under 8%. A failed reading does not shut the Pi down.

### Remove it

`sudo scripts/uninstall.sh` stops the service and leaves the Apple ID in place. Add `--purge` only when you also want `/var/lib/sideload-pi` and `/etc/sideload-pi` deleted.

### When something fails

- **Blank or snowy panel:** wrong driver. Power-cycle and run `display-test` one version at a time.
- **Panel stuck in a refresh:** power-cycle. Check the ribbon and that pin 1 matches.
- **`waiting_for_clock`:** the Pi does not refresh until the clock is synced. Wait. It is not a step you configure.
- **Apple rejected the password or the code:** the message names the code (`-20101` / `-22406` password, `-21669` code, `-22421` anisette). Fix that, then `sudo sideload-pi login` again.
- **App ID limit:** 10 new App IDs in 7 days. Re-signing an existing bundle id is free.
- **Device limit:** a free team can register 3 devices.
- **Install failed:** Developer Mode, Trust, or the profile does not list this phone. `sudo sideload-pi pair` again on USB.
- **Pi reboots when the phone is plugged in:** charger on the UPS, a powered hub, or a data-only cable. The phone plugs into **USB**, not **PWR**.
- **Logs:** `journalctl -u sideload-pi -f` and `/var/lib/sideload-pi/logs/sideload-pi.log`. Passwords and tokens are stripped. Do not add the verification code to a log.

What this has not been tried on, and the limits that are not settings, are in [LIMITATIONS.md](LIMITATIONS.md). Why the phone tools are built the way they are is in [ARCHITECTURE.md](ARCHITECTURE.md).
