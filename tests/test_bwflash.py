"""Host-side tests for esp32/bwflash.py.

A fake ESC answers the DFU protocol, rebuilds the image from the XMODEM
frames and checks it. If the original bw-flasher source is given via
BW_FLASHER_SRC, both flashers run against the fake ESC and their TX byte
streams must be identical.

Run: BW_FIRMWARE=<image.bin> [BW_FLASHER_SRC=<bw-flasher checkout>] python3 -m pytest tests
"""
import binascii
import os
import random
import sys
import time
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'esp32'))

if not hasattr(time, 'ticks_ms'):
    time.ticks_ms = lambda: int(time.monotonic() * 1000)
    time.ticks_diff = lambda a, b: a - b

import bwflash  # noqa: E402

FW_PATH = os.environ.get('BW_FIRMWARE')
BW_SRC = os.environ.get('BW_FLASHER_SRC')
UID = b'TestUid123456789'
needs_fw = pytest.mark.skipif(not FW_PATH, reason='set BW_FIRMWARE to a Brightway image')


class FakeEsc:
    """Minimal Brightway DFU target. Implements write/any/read and pyserial's read_until."""

    def __init__(self, fw_path):
        self.fw = bwflash.Firmware(fw_path)
        self.rx = b''      # bytes the ESC sends
        self.tx_log = b''  # bytes the host sent
        self.mem = {}
        self.loc = None
        self.recv = b''
        self.crc_ok = False
        self.active = False
        self.mcu_rand = bytes(range(0x20, 0x30))

    # host -> ESC
    def write(self, data):
        self.tx_log += data
        d = bytes(data)
        if d == b'\x53\x2A\x7D\xAC':
            body = b'\x64\x2A\x10' + UID
            self.rx += body + bytes([sum(body) & 0xFF, 0x9B])
        elif d == b'down get_ver\r':
            self.rx += b'9999\r' if self.active else b'0015\r'
        elif d.startswith(b'down rd_info'):
            self.rx += b'ok ' + b'\x00' * 22 + b'\r'
        elif d.startswith(b'down ble_rand '):
            self.rx += b'ok ' + bwflash.sign_rand(UID, d[14:30], self.fw.lt0, self.fw.lt1) + b'\r'
        elif d == b'down mcu_rand\r':
            self.rx += b'ok ' + self.mcu_rand + b'\r'
        elif d.startswith(b'down mcu_key '):
            ok = d[13:29] == bwflash.sign_rand(UID, self.mcu_rand, self.fw.lt0, self.fw.lt1)
            self.rx += b'ok\r' if ok else b'er\r'
        elif d.startswith(b'down nvm_write '):
            self.loc = int(d[15:23], 16)
            self.recv = b''
            self.rx += b'ok\r'
        elif d[:1] == b'\x01' and len(d) == 133:
            body = d[3:131]
            assert d[2] == d[1] ^ 0xFF
            assert int.from_bytes(d[131:133], 'big') == binascii.crc_hqx(body, 0)
            self.recv += body
            self.rx += b'\x06'
        elif d == b'\x04\x04\x04':
            if self.recv:
                self.mem[self.loc] = self.recv
            self.rx += b'\x06'
        elif d.startswith(b'down wr_info '):
            n, crc, size = d[13:-1].split(b' ')
            data = b''.join(self.mem[k] for k in sorted(self.mem))
            self.crc_ok = int(crc, 16) == binascii.crc32(data) and int(size) == int(n) * 0x800
            self.rx += b'ok\r'
        elif d == b'down dfu_verify\r':
            self.rx += b'ok\r' if self.crc_ok else b'er\r'
        elif d == b'down dfu_active\r':
            self.active = True
            self.rx += b'ok\r'
        else:
            raise AssertionError('unexpected TX %r' % d[:20])

    # ESC -> host (MicroPython UART API)
    def any(self):
        return len(self.rx)

    def read(self, n=1):
        out, self.rx = self.rx[:n], self.rx[n:]
        return out

    # pyserial API used by bw-flasher
    def read_until(self, expected=b'\n', size=None):
        if isinstance(expected, str):  # pyserial never matches a str terminator
            return self.read(len(self.rx))
        i = self.rx.find(expected)
        n = len(self.rx) if i == -1 else i + len(expected)
        return self.read(n)

    def flush(self):
        pass

    def image(self):
        return b''.join(self.mem[k] for k in sorted(self.mem))


def test_crc16_matches_binascii():
    data = bytes(random.Random(1).randrange(256) for _ in range(300))
    assert bwflash.crc16_xmodem(data) == binascii.crc_hqx(data, 0)


@needs_fw
def test_sign_rand_matches_original():
    if not BW_SRC:
        pytest.skip('set BW_FLASHER_SRC')
    sys.path.insert(0, BW_SRC)
    from bwflasher.keygen import sign_rand as orig
    fw = bwflash.Firmware(FW_PATH)
    raw = open(FW_PATH, 'rb').read()
    o0 = raw.find(b'\x63\x7C')
    o1 = raw.find(b'\x01\x02', o0) - 1
    rng = random.Random(7)
    for _ in range(50):
        uid = bytes(rng.randrange(256) for _ in range(16))
        rnd = bytes(rng.randrange(256) for _ in range(16))
        assert bwflash.sign_rand(uid, rnd, fw.lt0, fw.lt1) == bytes(orig(uid, rnd, raw, o0, o1))


@needs_fw
def test_flash_writes_exact_image():
    esc = FakeEsc(FW_PATH)
    logs = []
    f = bwflash.Flasher(bwflash.Link(esc, log=lambda *a: logs.append(a)), bwflash.Firmware(FW_PATH))
    f.flash()
    raw = open(FW_PATH, 'rb').read()
    img = esc.image()
    assert img[:len(raw)] == raw
    assert set(img[len(raw):]) <= {0xFF}
    assert esc.active and f.version == '9999'


@needs_fw
def test_tx_stream_identical_to_bw_flasher():
    if not BW_SRC:
        pytest.skip('set BW_FLASHER_SRC')
    sys.path.insert(0, BW_SRC)
    fake_serial = types.ModuleType('serial')
    fake_serial.Serial = lambda *a, **k: None
    sys.modules.setdefault('serial', fake_serial)
    from bwflasher.brightway_flasher import BrightwayFlasher

    ours = FakeEsc(FW_PATH)
    bwflash.Flasher(bwflash.Link(ours, log=lambda *a: None), bwflash.Firmware(FW_PATH)).flash()

    theirs = FakeEsc(FW_PATH)
    bf = BrightwayFlasher(simulation=True, log_callback=lambda *a: None)
    bf.simulation = False
    bf.serial_conn = theirs
    bf.load_file(FW_PATH)
    bf.run()

    assert ours.tx_log == theirs.tx_log
