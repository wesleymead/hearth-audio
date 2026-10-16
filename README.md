# Hearth Audio

Hearth Audio is an experimental, self-hosted whole-home audio system for Raspberry Pi. A Hub receives AirPlay 2, publishes a Snapcast stream, and provides a touchscreen-friendly web interface. Raspberry Pi nodes play the synchronized stream through an available analog or HDMI output. The web interface also offers pairing, room volume, IR/CEC actions, a soundboard, and scheduling.

This is a prototype, not a security-hardened appliance. Keep it on a trusted home network. Its web API currently has no login or transport encryption; **do not forward ports 8080, 1704, 1705, or 1780 to the internet**. Back up your Pi before installation.

## Hardware and limits

- Hub: Raspberry Pi with Raspberry Pi OS Desktop, network access, and an optional touchscreen. Ethernet is recommended.
- Nodes: Raspberry Pi OS Lite (32-bit), an HDMI display if wanted, network access, and an amplifier/receiver. Pi 2 B can use Ethernet; Pi Zero 2 W can use Wi-Fi or Ethernet via adapter. Other Pi models should work where their audio/display drivers support the selected output, but are not all hardware-verified.
- The installer detects the Pi model and the runtime probes HDMI hot-plug/EDID and available audio outputs. A 3.5 mm jack's *cable insertion* cannot be sensed on standard Pi hardware. Pi Zero/Zero 2/Pi 5 have no built-in analog 3.5 mm audio jack.
- The SpotPear 1.54-inch ST7789V Game HAT requires an explicit install option. A bare Pi cannot reliably identify this HAT before its overlay is installed. Other SPI HATs need their own driver and configuration.
- Synchronized surround is multichannel PCM only when the source, Hub playback mode, HDMI sink, and node all support it. Stereo AirPlay music does not become discrete 5.1/7.1. Atmos/DTS bitstream passthrough and HDMI ARC are not implemented in the synchronized path.
- Existing IR blasters, CEC wiring, GPIO buttons, and DACs are hardware-dependent. The generic node installer leaves the button off until you supply a safe GPIO pin.

## Install a Hub

1. Install Raspberry Pi OS Desktop, set a unique hostname such as `hearth-hub`, enable SSH if you want remote administration, and connect it to your LAN.
2. On the Pi, clone this repository into `/opt/hearth-audio` and run the installer:

   ```sh
   sudo apt update && sudo apt install -y git
   sudo git clone https://github.com/wesleymead/hearth-audio.git /opt/hearth-audio
   sudo sh /opt/hearth-audio/deploy/install-hub.sh
   ```

   The installer installs the AirPlay 2 and Snapcast services; compiling Shairport Sync can take a while on older Pis. Reboot after installing.

3. Open `http://hearth-hub.local:8080` from a phone/computer on the same LAN, substituting your Hub hostname or IP. The Hub also runs a local Snapclient, so it can be an audio location itself.
4. For the optional Hub touchscreen kiosk on Raspberry Pi OS Desktop, set the desktop to auto-login and configure Chromium to open `http://localhost:8080` fullscreen at login. Browser startup instructions vary by Raspberry Pi OS desktop release; verify the web page works in a normal browser first. The web UI works without a touchscreen.

Check services: `systemctl status nqptp shairport-sync snapserver snapclient hearth-hub`.

## Install a node

1. Install Raspberry Pi OS Lite, set a **unique hostname** (for example `hearth-kitchen`), enable SSH, and connect the Pi to the same LAN. For an HDMI display, plug the screen in before the first boot.
2. Clone and install. Change `hearth-hub.local` if your Hub uses a different hostname or IP:

   ```sh
   sudo apt update && sudo apt install -y git
   sudo git clone https://github.com/wesleymead/hearth-audio.git /opt/hearth-audio
   sudo HEARTH_HUB_URL=http://hearth-hub.local:8080 sh /opt/hearth-audio/deploy/install-node.sh
   ```

   The installer creates `/etc/hearth-audio/node.json` only if it does not already exist. That file holds the node's private pairing token after pairing. Reinstalling does not replace it. The installer chooses the actual Pi OS login user for the display service rather than assuming `pi`.

3. In the Hub, select **Add node**. While its pairing window is open, run `sudo systemctl restart hearth-node` on the node. It should appear in Rooms; choose its output in node settings. A configured physical pairing button can open pairing locally instead.

Optional install settings:

```sh
sudo HEARTH_HUB_URL=http://hearth-hub.local:8080 HEARTH_DISPLAY=none sh /opt/hearth-audio/deploy/install-node.sh
sudo HEARTH_HUB_URL=http://hearth-hub.local:8080 HEARTH_DISPLAY=spotpear sh /opt/hearth-audio/deploy/install-node.sh
sudo HEARTH_HUB_URL=http://hearth-hub.local:8080 HEARTH_BUTTON_GPIO=26 sh /opt/hearth-audio/deploy/install-node.sh
```

`HEARTH_DISPLAY=none` is for a headless audio node. `spotpear` is only for the specific 240×240 ST7789V/XPT2046 Game HAT; reboot after the overlay is installed. Choose a button GPIO that does not conflict with your HAT or other wiring. Never connect GPIO directly to an amplifier's voltage.

Check services: `systemctl status snapclient hearth-node hearth-node-display` (omit the display service for a headless node). If audio is silent, verify the receiver input and the node's audio profile before changing gain.

## Updating

Back up `/var/lib/hearth-audio` on the Hub and `/etc/hearth-audio/node.json` on each node. Pull a reviewed release on each Pi with `cd /opt/hearth-audio && sudo git pull --ff-only`, then rerun its installer (`install-hub.sh` or `install-node.sh`). The Hub installer rebuilds Shairport Sync, so allow time for it. Do not copy another node's pairing file to a new device.

## Development and privacy

Run `python3 -m unittest discover -q` to exercise the test suite. The private runtime state and uploaded sounds live under `/var/lib/hearth-audio` on the Hub; paired node settings live under `/etc/hearth-audio`. Neither belongs in Git. The generic source tree contains no device pairing tokens. Before publishing a fork, review every staged file and Git history, and use GitHub's secret scanning/push protection.

Hearth Audio's own source is licensed under the MIT License; see `LICENSE`. Shairport Sync, Snapcast, and other separately installed dependencies retain their own licenses.
