# bw-esp32-flasher

Flash Brightway scooter ESC firmware with an ESP32 alone: no USB-UART dongle needed.
The ESP32 talks to the ESC over the dashboard cable and runs the same DFU protocol as
[ScooterTeam bw-flasher](https://github.com/scooterteam/bw-flasher). It also includes
a wire probe that tells you which wire is which, so you don't have to trust colour tables.

Prepare the firmware image with [bw-patcher](https://github.com/scooterteam/bw-patcher)
as usual. This project only replaces the flashing hardware.

## Tested

| Scooter | ESC firmware | Board | Result |
|---|---|---|---|
| Xiaomi Electric Scooter 5 (standard) | 0015 → patched (reports 9999) | ESP32-WROOM-32D, MicroPython 1.29 | full flash OK, 251 s |

Anything else is untested. Other Brightway models that bw-flasher supports use the same protocol,
but they have not been tried with this project.

## What you need

- ESP32 dev board (ESP32-WROOM-32, 3.3 V logic) with [MicroPython](https://micropython.org/download/ESP32_GENERIC/)
- `mpremote` (`pip install mpremote`)
- A cable or adapter that reaches the ESC's dashboard connector
- Dupont jumper wires

## Wiring

Unplug the dashboard. The scooter must be switched on.

| Cable wire (our cable, see below) | Function | ESP32 pin |
|---|---|---|
| black | GND | GND |
| yellow | ESC RX | GPIO5 (TX) |
| green | ESC TX (open collector) | GPIO16 (RX, internal pull-up enabled) |
| blue | must stay high, or the ESC switches off | GPIO17 (driven high) |
| red | battery-side power | **not connected** |

Pins can be changed at the top of `esp32/bwflash.py` (`TX_PIN`, `RX_PIN`, `KEEP_PIN`).

### Colours are not reliable

We used a female M7 5-pin "3 Lite / 4 Lite" extension cable. Its colours did **not** match the
M7 table in the ScooterTeam guide (which lists white RX, yellow GND, green TX, red 5V, black BTN).
On our cable, black was ground and yellow was ESC RX. Also, the ESC switched off whenever the blue wire was left floating.
With the wrong mapping, the ESC never answers and the flasher shows empty responses to `53 2A 7D AC`
([bw-flasher #32](https://github.com/scooterteam/bw-flasher/issues/32) shows the same symptom).

So identify the wires first:

1. Connect ESP32 GND to the wire you think is ground. Connect each other unknown wire, except a
   wire that could carry battery voltage, to a free GPIO, for example 4, 16, 17 and 18.
2. Run the probe:
   ```
   mpremote cp esp32/bwflash.py :bwflash.py + cp esp32/probe.py :probe.py
   mpremote exec "import probe; probe.run((4, 16, 17, 18))"
   ```
3. Read the result for each wire:
   - **ESC TX**: status frames were seen. Connect it to the RX pin.
   - **held high even with pull-down**: ESC RX, which has its own pull-up. Connect it to the TX pin.
   - **held low even with pull-up**: ground.
   - **floating**: an ESC input. If the scooter switches off while it is being probed, it is the
     keep-alive line: hold it high (`KEEP_PIN`).

## Flashing

```
mpremote cp esp32/bwflash.py :bwflash.py + cp patched.bin :fw.bin
mpremote exec "import bwflash; print(bwflash.info())"    # reads UID + version, writes nothing
mpremote exec "import bwflash; bwflash.flash('fw.bin')"  # ~4 minutes
```

`info()` must print the UID and version before you flash. While flashing, don't touch the cable or
the power button. Recovery from an interrupted flash has not been tested with this project.

The image is streamed from the ESP32's filesystem, so it does not need to fit in RAM.

## How it works

`bwflash.py` is a line-by-line port of bw-flasher's `brightway_flasher.py`, `keygen.py` and
`xmodem.py` to MicroPython: UID → `get_ver` → `rd_info` → `ble_rand`/`mcu_rand`/`mcu_key`
challenge → 2 KiB `nvm_write` packets as 128-byte XMODEM-CRC frames → `wr_info` (CRC-32) →
`dfu_verify` → `dfu_active`.

`tests/test_bwflash.py` runs both this port and the original bw-flasher against a fake ESC. It
checks that the ESC receives the exact image and that both send byte-identical traffic:

```
BW_FIRMWARE=patched.bin BW_FLASHER_SRC=path/to/bw-flasher python -m pytest -q tests
```

## Risks

Flashing ESC firmware can brick the controller. Use only images made for your exact model and
ESC. Keep the stock image. The ESP32 is a 3.3 V part: never connect a supply wire to a GPIO.

## License and credits

Protocol, key generation and the flashing sequence come from
[ScooterTeam bw-flasher](https://github.com/scooterteam/bw-flasher) (commit `8baeea1`), licensed
CC BY-NC-SA 4.0. This port is released under the same license: see [LICENSE](LICENSE).
Non-commercial use only.
