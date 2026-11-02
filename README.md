# ESP32-S3 USB Ethernet over Wi-Fi

Turn an **ESP32-S3** into a USB Network Control Model (NCM) adapter. The board joins a 2.4 GHz Wi-Fi access point as a station and forwards Ethernet frames between that network and a computer connected to its **native USB OTG** port. The computer should then obtain its address from the Wi-Fi network's DHCP server.

This project is based on Espressif's [`tusb_ncm` example](https://github.com/espressif/esp-idf/tree/release/v5.5/examples/peripherals/usb/device/tusb_ncm) and builds with ESP-IDF 5.5. The code is deliberately restricted to ESP32-S3.

```text
Computer (USB NCM) <-- USB OTG --> ESP32-S3 <-- 2.4 GHz Wi-Fi --> AP/router
       DHCP client                                             DHCP server
```

[Watch the ESP32-S3 USB Ethernet demonstration](assets/usb-ncm-speed-test.mp4). The clip opens and closes with the USB cable plugged into the board, with a laptop speed test between those views. The repeatable throughput measurements are described below.

## Before you start

- An ESP32-S3 board with its native USB OTG D+/D- connected to a USB connector, plus a data-capable USB cable. The board's USB-to-UART connector, if present, is for flashing and logs; it does not carry NCM traffic.
- A **2.4 GHz** Wi-Fi network with DHCP and permission for devices on the network to communicate. ESP32-S3 cannot join a 5 GHz-only network. An SSID ending in `5G` is a clue, not proof of its actual band; check the router settings.
- A host with a USB NCM driver, such as a recent Linux system or Windows 11, and ESP-IDF 5.5 installed.
- A separate UART logging path is useful when testing NCM. On some boards, using the native USB port for the serial monitor stops working once TinyUSB takes over that port.

## Configure, build, and flash

Run these commands in this directory. `sdkconfig.defaults` fixes the target to ESP32-S3; the existing `sdkconfig` also uses ESP32-S3.

```bash
source ~/esp/esp-idf/export.sh  # adjust if ESP-IDF is installed elsewhere
idf.py menuconfig
```

In **Component config → ESP32-S3 USB Ethernet**, set the **Wi-Fi SSID** and **Wi-Fi password** for a 2.4 GHz network. They are stored as `CONFIG_ESP_WIFI_SSID` and `CONFIG_ESP_WIFI_PASSWORD` in the local, ignored `sdkconfig` file. Keep that file private; `main/Kconfig` contains only placeholder defaults.

```bash
idf.py -B build build
idf.py -B build -p PORT flash
idf.py -B build -p PORT monitor
```

Replace `PORT` with the board's flashing/logging port, for example `/dev/ttyUSB0` for a USB-to-UART bridge. Exit the monitor with `Ctrl+]`. Connect the computer to the **USB OTG** connector for Ethernet. If the board has only one native USB connector, flash first, **close the serial monitor, then unplug and reconnect USB without holding BOOT**. Opening its native USB serial/JTAG port can reset the chip into ROM download mode instead of leaving the NCM app running. Use an external UART adapter for continuous logs while NCM uses native USB.

## Packet format and forwarding

Over the air, the ESP32-S3 receives 802.11 Wi-Fi frames. Its Wi-Fi driver processes the radio framing and passes `pkt_wifi2usb()` an Ethernet-style frame:

```text
Destination MAC (6 bytes) | Source MAC (6 bytes) | EtherType (2 bytes) | Payload (for example, ARP or IP)
```

`wifi_event_handler()` does not move packet data: on Wi-Fi connection it registers `pkt_wifi2usb()` and brings the USB NCM link up; on disconnection it removes the callback, brings the link down, and retries. For Wi-Fi → laptop traffic, `pkt_wifi2usb()` gives the Ethernet frame to TinyUSB, which sends it inside a USB NCM transfer and then releases the Wi-Fi receive buffer. For laptop → Wi-Fi traffic, `usb_recv_callback()` passes the Ethernet frame from USB to `esp_wifi_internal_tx()` once Wi-Fi is connected. The router supplies the laptop's IP address through DHCP; the ESP does not run NAT or a DHCP server.

The included `pytest_usb_device_ncm.py` checks that the host enumerates an NCM interface; it does **not** verify Wi-Fi association, DHCP, or packet forwarding. A successful build likewise does not prove those hardware stages.

## Measure throughput

With the NCM interface connected, use the included benchmark script to run two 20 MB downloads and two 20 MB uploads through Cloudflare's speed-test endpoint. It binds all test traffic to the named interface, pings the router during each transfer, and saves timings and network-interface counters to JSON. This uses about 80 MB of Internet data by default.

```bash
python3 benchmarks/run_capacity.py \
  --interface IFACE \
  --gateway GATEWAY_IP \
  --output benchmarks/result.json
```

For this board and laptop on 3 November 2026, the measured 20 MB transfers delivered **5.30–6.59 Mb/s**. Some loaded ping loss and NCM interface drops/errors were observed. The ESP32-S3 USB OTG port runs at 12 Mb/s full speed, so the Wi-Fi radio's own capacity cannot be inferred from this USB adapter test alone. Raw measurements and the local PDF report are excluded from the repository.

### Measure ESP32-S3 Wi-Fi transmit capacity directly

The separate ESP32-S3-only project in [`benchmarks/wifi_iperf`](benchmarks/wifi_iperf) is based on Espressif's Wi-Fi iperf example. Flashing it replaces the bridge firmware on the board. It creates test traffic on the ESP itself; the laptop receives it over the ESP's 2.4 GHz SoftAP. There is no router, Internet, or USB network data path in this test. Keep the USB connection only for the ESP console and power.

Build and flash from `benchmarks/wifi_iperf` with `idf.py -B build build` and `idf.py -B build -p PORT flash`. Its `sdkconfig.defaults` also fixes the target to ESP32-S3. Boot the app with BOOT/GPIO0 released. On the ESP console, enter `wifi_mode ap`, then `ap_set S3-TX-BENCH S3bench2026 -n 6`. Connect the laptop's Wi-Fi interface to `S3-TX-BENCH` (password `S3bench2026`); it should receive a `192.168.4.x` address from the ESP.

Install **iperf 2** on the laptop (iperf 3 is incompatible with the ESP example). The [validator](benchmarks/validate_wifi_tx.py) refuses to run unless the laptop is on the benchmark SSID and its route to `192.168.4.1` uses the specified Wi-Fi interface. Start it, then type the printed `iperf` command at the ESP console:

```bash
python3 benchmarks/validate_wifi_tx.py --interface wlp8s0 --mode tcp --output benchmarks/wifi-tx-tcp.json
python3 benchmarks/validate_wifi_tx.py --interface wlp8s0 --mode udp --bandwidth 20 --output benchmarks/wifi-tx-udp20.json
```

The TCP receiver result is reliable application goodput. For UDP, compare received throughput and packet loss while increasing the offered rate; the highest offered rate is not necessarily the highest usable throughput. Record the Wi-Fi channel, signal strength, board distance, and test duration with the result. A measured peak under these conditions is not a universal chip maximum.

## References

- [ESP32-S3 radio capabilities](https://www.espressif.com/products/socs/esp32-s3)
- [ESP-IDF USB device stack for ESP32-S3](https://docs.espressif.com/projects/esp-idf/en/v5.5/esp32s3/api-reference/peripherals/usb_device.html)
- [Espressif NCM link-state issue and resolution](https://github.com/espressif/esp-idf/issues/18079)
