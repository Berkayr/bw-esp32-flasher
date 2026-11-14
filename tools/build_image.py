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


def filesystem_image(size):
    from littlefs import LittleFS, UserContext
    config = dict(block_size=BLOCK_SIZE, block_count=size // BLOCK_SIZE,
                  read_size=32, prog_size=32, cache_size=128, lookahead_size=32)
    fs = LittleFS(**config)
    sources = sorted((ROOT / 'esp32').glob('*.py')) + [ROOT / 'esp32/webui.html']
    for path in sources:
        with fs.open(path.name, 'wb') as stream:
            stream.write(path.read_bytes())
    fs.unmount()
    data = bytes(fs.context.buffer)
    # Mount a fresh context without auto-formatting, then compare every byte.
    context = UserContext(len(data))
    context.buffer[:] = data
    check = LittleFS(context=context, mount=False, **config)
    check.mount()
    if sorted(check.listdir('/')) != sorted(p.name for p in sources):
        raise ValueError('LittleFS file listing mismatch')
    for path in sources:
        with check.open(path.name, 'rb') as stream:
            if stream.read() != path.read_bytes():
                raise ValueError('LittleFS read-back mismatch: ' + path.name)
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
    fs_data = filesystem_image(size)
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
    (ROOT / 'web/manifest.json').write_text(json.dumps(manifest(), indent=2) + '\n')
    print('Output:', OUTPUT)
    print('Output SHA-256:', hashlib.sha256(result).hexdigest())


if __name__ == '__main__':
    main()
