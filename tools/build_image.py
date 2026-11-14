"""Build a classic 4 MiB ESP32 installer; never opens a serial port."""
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
MICROPYTHON_URL = 'https://micropython.org/resources/firmware/ESP32_GENERIC-20260824-v1.29.0.bin'
MICROPYTHON_SHA256 = 'e67ad6015a0a504c1fec9aa9bbf589d0432ed28e62546f4f8dd8a147f8bd95f6'
MICROPYTHON_LICENSE_URL = 'https://raw.githubusercontent.com/micropython/micropython/v1.29.0/LICENSE'
MICROPYTHON_LICENSE_SHA256 = '80d3459978572b5bbb1dd274a98b01a625aeff467588ad69cefdf7eb607c1306'
IMAGE_BASE = 0x1000
PARTITION_TABLE = 0x8000
FLASH_SIZE = 4 * 1024 * 1024
BLOCK_SIZE = 4096
OUTPUT = ROOT / 'web/firmware/bw-esp32-flasher.bin'


def vfs_partition(image):
    """Validate the ESP-IDF partition table and return its unique vfs region."""
    table = image[PARTITION_TABLE - IMAGE_BASE:PARTITION_TABLE - IMAGE_BASE + 0x1000]
    entries, regions, matches = bytearray(), [], []
    application = False
    other_fs = False
    for pos in range(0, len(table), 32):
        entry = table[pos:pos + 32]
        if len(entry) != 32:
            raise ValueError('Truncated partition table')
        if entry == b'\xff' * 32:
            break
        if entry[:2] == b'\xeb\xeb':
            if entry[16:] != hashlib.md5(entries).digest():
                raise ValueError('Partition table MD5 mismatch')
            break
        magic, kind, subtype, offset, size, label, flags = struct.unpack('<HBBII16sI', entry)
        if magic != 0x50aa or not size or offset % BLOCK_SIZE or size % BLOCK_SIZE:
            raise ValueError('Invalid partition entry')
        if offset < 0x9000 or offset + size > FLASH_SIZE or flags:
            raise ValueError('Unsupported partition bounds or flags')
        if any(offset < end and offset + size > start for start, end in regions):
            raise ValueError('Overlapping partitions')
        regions.append((offset, offset + size))
        if label.rstrip(b'\0') == b'vfs':
            if kind != 1:
                raise ValueError('vfs must be a data partition')
            matches.append((offset, size))
        application |= kind == 0
        other_fs |= label.rstrip(b'\0') == b'ffat'
        entries.extend(entry)
    # v1.29.0 registers vfs dynamically at the end of the last partition.
    # Mirror ports/esp32/main.c rather than hard-coding the old vfs offset.
    if not matches and application and not other_fs:
        end = max(end for _, end in regions)
        if end < FLASH_SIZE:
            matches.append((end, FLASH_SIZE - end))
    if len(matches) != 1:
        raise ValueError('Expected exactly one vfs partition or a dynamic vfs region')
    offset, size = matches[0]
    if len(image) + IMAGE_BASE > offset:
        raise ValueError('MicroPython image overlaps vfs')
    return offset, size


def verify_download(data):
    if hashlib.sha256(data).hexdigest() != MICROPYTHON_SHA256:
        raise ValueError('MicroPython SHA-256 mismatch; refusing to build')
    return data


def manifest():
    return {'name': 'ESP32 scooter flasher', 'version': 'MicroPython 1.29.0',
            'new_install_prompt_erase': True,
            'builds': [{'chipFamily': 'ESP32', 'parts': [
                {'path': 'firmware/bw-esp32-flasher.bin', 'offset': 0}]}]}


def license_notice(data):
    if hashlib.sha256(data).hexdigest() != MICROPYTHON_LICENSE_SHA256:
        raise ValueError('MicroPython licence SHA-256 mismatch; refusing to build')
    return ("bw-esp32-flasher\n"
            "Copyright (C) 2024-2025 ScooterTeam\n"
            "Modified: ported to MicroPython by Berkayr, 2026\n"
            "Based on https://github.com/scooterteam/bw-flasher\n"
            "Port: CC BY-NC-SA 4.0 (non-commercial, attribution, share alike).\n"
            "https://creativecommons.org/licenses/by-nc-sa/4.0/\n"
            "Changes: MicroPython UART/streaming ports, probe, telemetry, Wi-Fi and USB UIs.\n"
            "Unofficial, use at your own risk. No warranty.\n\n"
            "The installer includes unmodified MicroPython 1.29.0 (MIT); other components\n"
            "retain their own licences. This is not a blanket licence for the image.\n"
            "MicroPython licence source: " + MICROPYTHON_LICENSE_URL + "\n"
            "ESP-IDF licence inventory and component notices: \n"
            "https://docs.espressif.com/projects/esp-idf/en/stable/esp32/COPYRIGHT.html\n"
            "https://github.com/espressif/esp-idf (see LICENSE and components/).\n"
            "Consult the MicroPython v1.29.0 ESP32 build configuration for SDK selection.\n"
            "ESP Web Tools 10.5.2: Apache-2.0, loaded from CDN, not in this image.\n"
            "esptool and littlefs-python are host build tools only.\n\n"
            + data.decode('utf-8'))


def filesystem_image(size, notice):
    from littlefs import LittleFS, UserContext
    config = dict(block_size=BLOCK_SIZE, block_count=size // BLOCK_SIZE,
                  read_size=32, prog_size=32, cache_size=128, lookahead_size=32)
    fs = LittleFS(**config)
    sources = sorted((ROOT / 'esp32').glob('*.py')) + [ROOT / 'esp32/webui.html']
    files = {path.name: path.read_bytes() for path in sources}
    files['NOTICE.txt'] = notice.encode('utf-8')
    for name, content in files.items():
        with fs.open(name, 'wb') as stream:
            stream.write(content)
    fs.unmount()
    data = bytes(fs.context.buffer)
    # Mount a fresh context without auto-formatting, then compare every byte.
    context = UserContext(len(data))
    context.buffer[:] = data
    check = LittleFS(context=context, mount=False, **config)
    check.mount()
    if sorted(check.listdir('/')) != sorted(files):
        raise ValueError('LittleFS file listing mismatch')
    for name, content in files.items():
        with check.open(name, 'rb') as stream:
            if stream.read() != content:
                raise ValueError('LittleFS read-back mismatch: ' + name)
    print('LittleFS read-back:', ', '.join(check.listdir('/')))
    check.unmount()
    return data


def main():
    from importlib.metadata import version
    for package, expected in [('littlefs-python', '0.15.0'), ('esptool', '5.1.0')]:
        if version(package) != expected:
            raise RuntimeError('Install tools/requirements-build.txt: ' + package + '==' + expected)
    data = verify_download(urllib.request.urlopen(MICROPYTHON_URL, timeout=60).read())
    offset, size = vfs_partition(data)
    print('MicroPython SHA-256:', MICROPYTHON_SHA256)
    print('vfs: offset=0x%x size=0x%x blocks=%d' % (offset, size, size // BLOCK_SIZE))
    notice = license_notice(urllib.request.urlopen(MICROPYTHON_LICENSE_URL, timeout=60).read())
    # The on-board notice keeps the complete MIT grant; the sidecar also has the inventory.
    short_notice = notice.split('--------------------------------------------------------------------------------', 1)[0]
    fs_data = filesystem_image(size, short_notice)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory:
        temp = Path(directory)
        mp, vfs, merged = temp / 'micropython.bin', temp / 'vfs.bin', temp / 'merged.bin'
        mp.write_bytes(data)
        vfs.write_bytes(fs_data)
        subprocess.run([sys.executable, '-m', 'esptool', '--chip', 'esp32', 'merge-bin',
                        '-o', str(merged), '--flash-size', '4MB',
                        hex(IMAGE_BASE), str(mp), hex(offset), str(vfs)], check=True)
        result = merged.read_bytes()
        if len(result) != offset + size or result[offset:offset + size] != fs_data:
            raise ValueError('Merged image offset/length mismatch')
        OUTPUT.write_bytes(result)
    OUTPUT.with_name('NOTICE.txt').write_text(notice, encoding='utf-8')
    (ROOT / 'web/manifest.json').write_text(json.dumps(manifest(), indent=2) + '\n')
    print('Output:', OUTPUT)
    print('Output SHA-256:', hashlib.sha256(result).hexdigest())


if __name__ == '__main__':
    main()
