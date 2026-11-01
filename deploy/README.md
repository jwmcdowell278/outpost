# Setting up a node

`install-node.sh` sets up a Linux machine as an Outpost mesh node, over SSH
from your computer. It works on any Linux that uses systemd and apt, dnf,
pacman or zypper: Raspberry Pi OS, Debian, Ubuntu, Fedora, Arch, openSUSE.

It installs:

- **Outpost**, with Bluetooth headset support (Inrico B02PTT PTT and audio)
  and a service that starts it at boot, with or without anyone logged in.
- **On a Raspberry Pi 3/4 with 64-bit Raspberry Pi OS:** the HaLow HAT
  drivers and the batman-adv mesh (`halow/`, see `halow/README.md`). Done
  automatically on a Pi; `--no-halow` skips it.
- **The network tools, on every machine:** `outpost-net` (the main menu:
  status, gateways, forwarding, HaLow settings), `outpost-mesh` (which cards
  the batman-adv mesh uses, Wi-Fi mesh settings) and `outpost-wifi` (client
  or access point; needs NetworkManager). With no cards chosen the mesh
  tool changes nothing. A machine whose kernel has no batman-adv skips it
  with a note, unless you asked for mesh cards.
- **Optionally, on any machine:** a batman-adv mesh over ordinary Wi-Fi
  (802.11s or ad-hoc) or Ethernet (`--mesh-select`, `--mesh-wifi`,
  `--mesh-eth`).

## Before you start

1. The machine needs internet access during the install, SSH, and `sudo`
   (it asks for a password if needed).
2. For a Raspberry Pi: flash **Raspberry Pi OS (64-bit)** with Raspberry Pi
   Imager (username, Wi-Fi, SSH on), and build the HaLow kit once
   (`halow/README.md`, "Build").
3. Get the shared files from a node that already works. Keep them private:
   they hold the mesh passwords and channel keys.

   ```sh
   scp john@192.168.88.10:outpost/config/outpost.yaml config/node.yaml
   ssh john@192.168.88.10 sudo cat /etc/halow-mesh/halow.env > deploy/halow/halow.env
   # only once a Wi-Fi/Ethernet mesh exists:
   ssh <node> sudo cat /etc/outpost-mesh/mesh.env > deploy/mesh/mesh-site.env
   ```

## Install

A Raspberry Pi with the HaLow HAT:

```sh
./deploy/install-node.sh john@<pi> --config config/node.yaml \
    --env deploy/halow/halow.env --name "Pi 2" --usb-bluetooth --reboot
```

A laptop or mini PC, picking its mesh interfaces from a menu:

```sh
./deploy/install-node.sh john@<pc> --config config/node.yaml \
    --mesh-select --mesh-env deploy/mesh/mesh-site.env --reboot
```

Options:

- `--name`: the name other nodes see (default: the machine's hostname).
- `--usb-bluetooth`: Raspberry Pi only. Turns off the built-in Bluetooth so
  a USB adapter is used (see "Raspberry Pi with a Bluetooth headset" in the
  main README).
- `--mesh-select`: choose the mesh interfaces from a menu during the
  install (see "Choosing the mesh interfaces" below).
- `--mesh-wifi IFACE` / `--mesh-eth IFACE`: name the interfaces instead
  (repeat for more). The interface is taken over by the mesh, so use a
  spare one. The script refuses the one the machine is reached through
  unless you add `--force-mesh`.
- `--wifi-setup`: open the Wi-Fi menu at the end (see "Wi-Fi: client or
  access point" below).
- `--net-setup`: open the forwarding/gateway menu at the end (see
  "Forwarding and gateways" below).
- `--mesh-env FILE`: the shared Wi-Fi/Ethernet mesh settings. The first
  node without it gets a new password. Copy its `/etc/outpost-mesh/mesh.env`
  to the other nodes.

After the reboot, pair the headset from the web UI's **Bluetooth** page and
switch it to hands-free mode (main README, "Bluetooth headset buttons").

## HaLow settings

`sudo outpost-net` → **HaLow settings** (or `sudo outpost-net halow`) changes
the HaLow mesh on this node: channel and width (picked together, so the
operating class always matches), country, mesh name and password. It
restarts HaLow afterwards. A country change also updates the driver
(`/etc/modprobe.d/morse.conf`) and needs a reboot. The channel list is for
the US (902-928 MHz); for other countries you enter the channel and
operating class yourself. Every HaLow node needs the same settings: copy
`/etc/halow-mesh/halow.env` to the others (`install-node.sh --env`).

The **Wi-Fi mesh settings** entry is separate: it's for ordinary Wi-Fi cards
in the mesh (802.11s or ad-hoc), not HaLow.

## Choosing the mesh interfaces

On the machine (over SSH or at its keyboard):

```sh
sudo outpost-mesh select
```

This shows every network card with its type, driver and address, and
whether it can mesh. Tick the ones the mesh should use and save. The change
applies straight away and is kept across reboots. Interfaces taken out of
the mesh go back to NetworkManager. The menu:

- leaves out Wi-Fi cards that can't do 802.11s mesh (the Pi's onboard Wi-Fi
  for one), and the HaLow radio, which is always in the mesh;
- warns about the interface you're connected through and asks you to type
  `yes`, because using it cuts that connection off.

Other commands:

```sh
outpost-mesh list                      # network cards and what they can do
sudo outpost-mesh use wlan1 eth1       # set the interfaces directly
sudo outpost-mesh use                  # no interfaces: Wi-Fi/Ethernet mesh off
```

Use a spare card for the mesh (e.g. a USB Wi-Fi adapter), so the machine
keeps its normal network connection.

## Wi-Fi: client or access point

On the machine:

```sh
sudo outpost-wifi
```

Pick a Wi-Fi card, then what it should do:

- **Join a Wi-Fi network (client):** it scans and lists the networks it can
  see, strongest first, with their security. Pick one and enter its
  password. There's an "other" entry for hidden networks. The machine joins
  it again by itself after a reboot.
- **Be an access point:** phones and laptops join it to reach the machine
  (and its web UI) and share its internet connection. You choose the name,
  password (8-63 characters; left empty, one is made up and shown), band
  (2.4 GHz for range and compatibility, 5 GHz for speed) and channel
  (empty = automatic). The machine is `192.168.4.1` on it by default, so
  the web UI is at `http://192.168.4.1:<web_ui_port>`.
- **Off:** the card stays off across reboots.

Each card is set on its own, so a machine with two can join a network on
one and be an access point on the other. Cards in the mesh and the HaLow
radio aren't offered. Take a card out of the mesh first
(`sudo outpost-mesh select`) to use it here.

**Changing the Wi-Fi you're connected through** asks first. If the change
cuts you off, it's undone after 2 minutes. If you can still reach the
machine, press Enter when asked (or run `sudo outpost-wifi keep wlan0`) to
keep it. A change that fails is put back straight away.

Without the menu:

```sh
outpost-wifi list                                   # cards and what they're doing
sudo outpost-wifi client wlan0 "Home WiFi"          # asks for the password
sudo outpost-wifi ap wlan0 --ssid outpost-pi2 --password 'at least 8' --band 2.4
sudo outpost-wifi off wlan0
```

Notes:

- It uses NetworkManager (standard on Raspberry Pi OS, Ubuntu, Fedora and
  most desktops). Machines without it don't get `outpost-wifi`.
- The access point uses `192.168.4.x`, not NetworkManager's default
  `10.42.0.x`, which would clash with the mesh's `10.42.x.x` addresses.
- If another DNS server already runs on the machine (e.g. Pi-hole), the
  access point hands out addresses only and points clients at that server.
  Otherwise it would fail with "IP configuration could not be reserved".
- Access points use plain WPA2 (WPA2-PSK). NetworkManager 1.42 also offers
  the WPA2-PSK-SHA256 variant, which the Raspberry Pi's built-in Wi-Fi
  doesn't advertise. Phones notice the mismatch during the handshake and
  report a wrong password. A small NetworkManager hook,
  `/etc/NetworkManager/dispatcher.d/90-outpost-ap`, switches each Outpost
  access point to plain WPA2 as it starts.
- 5 GHz access points need the country set (`iw reg get`). On a Pi, set it
  with `sudo raspi-config` (Localisation, WLAN Country).

## Forwarding and gateways

On the machine:

```sh
sudo outpost-net
```

- **Gateway (NAT):** pick the interface with internet (the uplink, e.g.
  `eth0`, a Wi-Fi client card, or an LTE modem) and tick the interfaces
  that should use it (the mesh `bat0` is ticked already; also e.g. an access
  point or a WireGuard tunnel). Their traffic goes out with the uplink's
  address. Nothing on the uplink side can open connections back in.
- **Forward between two interfaces (no NAT):** routes traffic between them
  both ways with addresses unchanged. Use it to join two networks that
  should reach each other, e.g. the mesh and a WireGuard link to another
  site. Devices on each side need a route to the other side through this
  machine (a routing protocol such as Babel does that, see "Bridging sites"
  in the main README).
- **Use the mesh's gateway for internet:** for nodes without their own
  internet. A gateway that shares internet with `bat0` announces itself in
  the mesh (batman-adv's gateway feature). These nodes route through the
  best one and switch if it goes away (checked every 20 seconds). A
  machine's own internet connection, if it has one, is still used first.
- **Remove** a gateway or forwarding.

Without the menu:

```sh
outpost-net status
sudo outpost-net gateway eth0 bat0 wlan1   # eth0's internet for the mesh and wlan1
sudo outpost-net gateway-off eth0
sudo outpost-net forward bat0 wg0          # both ways, no NAT
sudo outpost-net unforward bat0 wg0
sudo outpost-net mesh-internet auto        # on the other mesh nodes
```

Notes:

- Rules go in their own nftables table (`outpost_net`), loaded at boot by
  `outpost-net.service`. Between the interfaces set up here, only what's
  allowed passes; other traffic (Docker, VPNs set up elsewhere) is left to
  its own rules. Changes don't affect connections to the machine itself,
  so they can't cut off SSH.
- **Docker** blocks forwarding by default. Allow rules are also added to
  its `DOCKER-USER` chain. **ufw** gets matching `route allow` rules.
  With **firewalld**, allow forwarding and masquerading there as well.
- Mesh nodes using a gateway need a DNS server too, e.g. set one with
  `nmcli` or in `/etc/resolv.conf` (the gateway's upstream DNS or a public
  one).
- Finding the mesh gateway assumes the nodes' standard mesh addresses. If
  `BAT_ADDRESS` is set by hand, set `MESH_GATEWAY=<its address>` in
  `/etc/outpost-net/net.env` on the other nodes.

## The Wi-Fi/Ethernet mesh

`outpost-mesh.service` runs batman-adv (`bat0`) over the chosen interfaces.
Wi-Fi uses an encrypted 802.11s mesh (WPA3/SAE) on the frequency in
`mesh.env` (default 2437 MHz, channel 6). Settings live in
`/etc/outpost-mesh/mesh.env` (the same on every node) and `node.env` (this
machine's interfaces, set by `outpost-mesh select`). After editing
`mesh.env`: `sudo systemctl restart outpost-mesh`.

- **Wi-Fi card, two ways:**
  - **802.11s (encrypted, recommended):** cards that support "mesh point"
    (`iw list`). Most Atheros (ath9k, ath10k), MediaTek (mt76) and Intel
    AX2xx cards do, e.g. USB adapters with an MT7612U or AR9271 chip. Meshes
    with other 802.11s nodes using `MESH_ID`/`MESH_PASSWORD`.
  - **Ad-hoc (IBSS), no encryption:** for cards that can't do 802.11s but
    support ad-hoc mode, like the Raspberry Pi's built-in Wi-Fi. They show
    as "ad-hoc only" in the menu, which asks before using one. Anyone nearby
    can join an ad-hoc mesh and reach the network; Outpost's voice stays
    encrypted end to end. Ad-hoc nodes only mesh with other ad-hoc nodes
    (`ADHOC_ID`, `ADHOC_BSSID` and `FREQ` in `mesh.env`, the same on every
    node). A node with both kinds of card (or with HaLow) joins them into
    one network.
  - Either way the card is used only for the mesh: an access point or Wi-Fi
    connection on it stops while it's in the mesh. On a Pi, keep the
    built-in Wi-Fi as an access point and add a USB adapter for an
    encrypted mesh, or use the built-in Wi-Fi for an ad-hoc mesh and give up
    its access point.
- **One network with HaLow:** a HaLow mesh and a Wi-Fi mesh can't hear each
  other directly. A Pi with both (`--mesh-wifi` or `--mesh-eth` on a HaLow
  Pi) joins them: both go into the same `bat0`, so every node can reach
  every other one. The script copies the HaLow settings' routing algorithm
  and address range so the two match.
- **Ethernet:** `--mesh-eth` on a spare port links nodes by cable, e.g. a
  HaLow Pi to a PC, through a switch if you like.

## Update a node

Run it again with only the host. The code is updated; the config, mesh
settings and interfaces stay as they are.

```sh
./deploy/install-node.sh john@<node>
```

## Check it

`sudo outpost-net` → **Status** (or `sudo outpost-net overview`) shows
everything on one page: each network card and what it's doing (mesh, access
point with how many devices, Wi-Fi client with signal, internet uplink),
the mesh's neighbours with link quality and how many nodes are reachable,
HaLow, whether the internet and DNS work, gateways and forwarding, and
whether Outpost is running. The details:

```sh
sudo halow-mesh status                 # HaLow mesh peers and bat0 address
sudo outpost-mesh status               # Wi-Fi/Ethernet mesh
outpost-wifi list                      # Wi-Fi cards: client / access point / off
outpost-net status                     # gateways, forwarding, mesh gateway in use
systemctl --user status outpost        # Outpost service
journalctl --user -u outpost -f        # Outpost log
```

## Not supported

- Linux without systemd (e.g. Alpine, Void, OpenWrt).
- HaLow on anything other than a Raspberry Pi 3/4 with 64-bit Raspberry Pi OS.
