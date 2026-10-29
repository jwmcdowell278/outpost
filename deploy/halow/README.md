# HaLow mesh (Heltec HT-HC01P) + batman-adv

Turns a Raspberry Pi 3/4 (64-bit Raspberry Pi OS Bookworm) with a Heltec
HT-HC01P Wi-Fi HaLow HAT into a batman-adv mesh node. Outpost then finds
`bat0` by itself (`enable_multicast: true`); no Outpost config change needed.

What runs on the Pi:

- Morse Micro's patched kernel (`mm/rpi-6.6.31/1.15.x`), booted from
  `/boot/firmware/morse/` via `os_prefix=morse/` in `config.txt`. The stock
  kernel is left as it is, and apt kernel updates don't touch this one.
- Morse driver, firmware and `wpa_supplicant_s1g` 1.15.3, plus Heltec's board
  config file (`bcf_HC0P.bin`, made for driver 1.15.3).
- Overlay `hc01p-spi` (Heltec pins: RESET 5, WAKE 3, BUSY 7, IRQ 25, CS 8).
  GPIO 3 is I2C SCL, so I2C can't be used alongside the HAT; GPIO 17 (Outpost's
  PTT button) is free.
- `halow-mesh.service`: resets the chip, loads the driver (retrying if the
  firmware upload fails), brings up an 802.11s mesh point (SAE, 802.11s
  forwarding off) on `halow0`, and puts batman-adv's `bat0` on top. The module
  has no factory MAC, so the radio's MAC (and therefore the `10.42.x.y/16`
  address on `bat0`) is derived from `/etc/machine-id`: fixed per Pi,
  different on every node.

Board quirks (Pi 3 + HT-HC01P), all handled by the kit:

- The SPI clock is set to 5 MHz in `/etc/modprobe.d/morse.conf`. 25 MHz and up
  always failed during the firmware upload, and 10 MHz failed about 1 load in 4.
- The driver doesn't reset the chip before talking to it, so `halow-mesh`
  pulses RESET (GPIO 5) first. The overlay leaves RESET and WAKE as pulled-up
  inputs so the chip is never held in reset at boot.
- Morse's mesh code won't join unless `dtim_period=1` is set explicitly.
- Turning Wi-Fi off in NetworkManager also blocks the HaLow radio (both are
  Wi-Fi to the kernel). `halow-mesh` unblocks just `halow0` when it starts.
  Turning the onboard Wi-Fi off *after* that blocks HaLow again until
  `sudo systemctl restart halow-mesh`.

## Build (on a Mac with Apple Silicon, or any arm64 Linux machine with Docker)

```sh
cd deploy/halow
docker build --platform linux/arm64 -t outpost-halow-build .
docker run --rm --platform linux/arm64 -v outpost-halow-work:/work \
    -v "$PWD:/src:ro" -v "$PWD/out:/out" outpost-halow-build
```

The first build takes about 30-60 minutes. Later runs reuse the
`outpost-halow-work` volume.

## Install on a running Pi (over SSH)

```sh
./install.sh john@192.168.88.10 --reboot                  # first node: new mesh password
./install.sh pi@<new-pi> --env halow.env --reboot         # more nodes: same settings
```

Without `--env`, the first install writes a random mesh password to
`/etc/halow-mesh/halow.env`. Every node needs that same file (country, channel,
mesh ID, password). Get it with `ssh <first-pi> sudo cat /etc/halow-mesh/halow.env > halow.env`
and pass it to the other nodes with `--env`.

## Install on a fresh SD card (from a Mac)

Flash Raspberry Pi OS (Bookworm or Trixie, 64-bit) with Raspberry Pi Imager,
leave the card plugged in, then run:

```sh
./sd-card.sh --env halow.env            # boot partition defaults to /Volumes/bootfs
```

This copies the kit (with that release's `batctl`/`iw` packages, so the
first boot needs no network) to the boot partition. It also adds a
first-boot step to Imager's cloud-init `user-data`. On first boot the Pi
installs the kit, deletes it from the boot partition (it holds the mesh
password), and reboots into the HaLow kernel by itself. The log is in
`/var/log/halow-install.log`.

## Check it

```sh
sudo halow-mesh status        # bat0 address, mesh peers, batman neighbours
dmesg | grep -i morse         # driver/firmware loading
cat /run/halow-mesh/wpa_supplicant.log
```

## Change settings

Edit `/etc/halow-mesh/halow.env` (channel, country, password, routing
algorithm, address), then run `sudo systemctl restart halow-mesh`. If you change
the country, also change it in `/etc/modprobe.d/morse.conf` and reboot.

## Known issue

On the first Pi this was installed on, a `reboot` sometimes hangs at the very
end of shutdown (this happened on the stock kernel too). The hardware watchdog
restarts the Pi after about 10 minutes. Pulling the power also works.

## Go back to the stock kernel

Delete the `# --- HaLow` block at the end of `/boot/firmware/config.txt` and
reboot. If the Pi won't boot, put its SD card in any computer and edit
`config.txt` on the boot partition.
