# Building the browser installer

This is a maintainer tool. Users only need desktop Chrome/Edge and a USB cable.
Use Python 3.10+ (verified with 3.12), `littlefs-python==0.15.0` and `esptool==5.1.0`:

```sh
python -m pip install -r tools/requirements-build.txt
python tools/build_image.py
```

Output: `web/firmware/bw-esp32-flasher.bin`, a single 4 MiB image written at offset **0**.
It includes the stock bootloader, partition table and application at their original addresses,
then a LittleFS2 volume with every `esp32/*.py` file and `esp32/webui.html`, plus an on-board `NOTICE.txt`.
The script never opens a serial port. Binaries are ignored; include the generated file in the
Pages artifact; `.github/workflows/pages.yml` builds and publishes it on every release.
The manifest and local wiring image make `web/` self-contained apart from ESP Web Tools' CDN.
The source wiring drawing is `docs/wiring-esp32.svg`; keep its `web/docs/` copy in sync.

Pinned inputs:

| Input | Exact version/source | SHA-256 |
|---|---|---|
| MicroPython | [ESP32_GENERIC-20260824-v1.29.0.bin](https://micropython.org/resources/firmware/ESP32_GENERIC-20260824-v1.29.0.bin) | `e67ad6015a0a504c1fec9aa9bbf589d0432ed28e62546f4f8dd8a147f8bd95f6` |
| littlefs-python | 0.15.0 (sdist) | `0d1151d9b868c589da7c8ab6202ff52eaa99c116b9f9216b054a252f75eeb260` |
| esptool | 5.1.0 (sdist) | `2ea9bcd7eb263d380a4fe0170856a10e4c65e3f38c757ebdc73584c8dd8322da` |
| ESP Web Tools | [10.5.2 install-button.js](https://unpkg.com/esp-web-tools@10.5.2/dist/web/install-button.js?module) | Exact version URL in `web/index.html`; dependencies loaded by the CDN |

The builder also downloads the [MicroPython v1.29.0 licence](https://github.com/micropython/micropython/blob/v1.29.0/LICENSE),
SHA-256 `80d3459978572b5bbb1dd274a98b01a625aeff467588ad69cefdf7eb607c1306`.
It writes `web/firmware/NOTICE.txt` with port attribution, CC BY-NC-SA terms/link,
the complete MicroPython licence and ESP-IDF licence pointers. The on-board notice
keeps attribution, component pointers and the complete MIT grant; the sidecar also includes
the upstream licence inventory. Publish the notice alongside the image. Only `.bin` files
are ignored in that directory. No per-board password is baked into the image: `wifi.txt`
is created on first boot.

The builder enforces the MicroPython hash and Python tool versions. The sdist hashes above
identify the releases; pip may install platform wheels. Transitive Python build dependencies
are not locked. `littlefs-python` is needed to author/read back the filesystem; esptool is
needed to merge at ESP32 flash addresses. Neither dependency runs on the board or in our UI.

The partition parser validates entry bounds, overlap and the table MD5 when present.
MicroPython 1.29.0's stock table has **no explicit vfs entry**. As in
[`ports/esp32/main.c` at v1.29.0](https://github.com/micropython/micropython/blob/v1.29.0/ports/esp32/main.c),
the filesystem starts at the end of the last partition and extends to physical flash size.
For the supported **exactly 4 MiB** board this is offset `0x200000`, size `0x200000`.
An explicit vfs entry is also supported. Other flash sizes need a separately built image and
hardware validation; a larger physical flash would change the dynamically registered volume size.
Do not use this image on C3, S2 or S3 boards.

LittleFS uses block size 4096, block count 512, read/prog size 32, cache size 128 and
lookahead size 32. These follow
[`esp32_partition.c`](https://github.com/micropython/micropython/blob/v1.29.0/ports/esp32/esp32_partition.c),
[`vfs_lfs.c`](https://github.com/micropython/micropython/blob/v1.29.0/extmod/vfs_lfs.c) and
[`vfs_lfsx.c`](https://github.com/micropython/micropython/blob/v1.29.0/extmod/vfs_lfsx.c).
After creating the image, the tool unmounts it, opens a fresh context without auto-format,
lists the expected files and compares every file byte. It also checks the filesystem bytes
at the final merged offset. Actual boot/mount still needs an ESP32 test.

The JS transport follows [mpremote's standard raw REPL](https://github.com/micropython/micropython/blob/v1.29.0/tools/mpremote/mpremote/transport_serial.py):
interrupt twice, enter raw REPL, send code in 256-byte pieces at 10 ms intervals, Ctrl-D,
`OK`, stdout EOF, stderr EOF, prompt. Ctrl-B leaves on a clean disconnect. Timeouts poison
the session and never send an automatic interrupt because a flash may still be running.
File writes use a fixed temporary path, 256-byte base64 chunks and device-side size/CRC-32
read-back before family preflight and promotion to `fw.bin`. Flash rechecks size/CRC/family
and fresh battery before calling the existing dispatcher. Binary chunks never become Python code.
