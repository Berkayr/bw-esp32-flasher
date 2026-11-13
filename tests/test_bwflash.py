"""Host-side tests for esp32/bwflash.py.

A fake ESC answers the DFU protocol, rebuilds the image from the XMODEM
frames and checks it. If the original bw-flasher source is given via
BW_FLASHER_SRC, both flashers run against the fake ESC and their TX byte
streams must be identical.

Run: [BW_FIRMWARE=<image.bin>] [BW_FLASHER_SRC=<bw-flasher checkout>] python3 -m pytest tests
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
@pytest.fixture
def fw_path(tmp_path):
    if FW_PATH:
        return FW_PATH
    return synthetic_firmware(tmp_path)


def synthetic_firmware(tmp_path, size=0x1801):
    """Small deterministic Brightway-like image, with no real firmware bytes."""
    raw = bytearray(b'\xff' * size)
    raw[0x22:0x2e] = b'001500010001'
    raw[0x800:0x808] = b'DEPRD5C\x00'
    raw[0x1100:0x1200] = bytes([0x63, 0x7c] + [n for n in range(255, -1, -1) if n not in (0x63, 0x7c)])
    raw[0x1300:0x130b] = bytes([0, 1, 2, 4, 8, 16, 32, 64, 128, 27, 54])
    path = tmp_path / 'synthetic.bin'
    path.write_bytes(raw)
    return str(path)


@pytest.fixture(autouse=True)
def fast_clock(monkeypatch):
    # Exercise the actual read loop without waiting 100 ms per command on the host.
    clock = iter(range(100000000))
    monkeypatch.setattr(time, 'ticks_ms', lambda: next(clock))


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
    rng = random.Random(1)
    data = bytes(rng.randrange(256) for _ in range(300))
    assert bwflash.crc16_xmodem(data) == binascii.crc_hqx(data, 0)


def test_sign_rand_matches_original(fw_path):
    if not BW_SRC:
        pytest.skip('set BW_FLASHER_SRC')
    sys.path.insert(0, BW_SRC)
    from bwflasher.keygen import sign_rand as orig
    fw = bwflash.Firmware(fw_path)
    raw = open(fw_path, 'rb').read()
    o0 = raw.find(b'\x63\x7C')
    o1 = raw.find(b'\x01\x02', o0) - 1
    rng = random.Random(7)
    for _ in range(50):
        uid = bytes(rng.randrange(256) for _ in range(16))
        rnd = bytes(rng.randrange(256) for _ in range(16))
        assert bwflash.sign_rand(uid, rnd, fw.lt0, fw.lt1) == bytes(orig(uid, rnd, raw, o0, o1))


def test_flash_writes_exact_image(fw_path):
    esc = FakeEsc(fw_path)
    logs = []
    f = bwflash.Flasher(bwflash.Link(esc, log=lambda *a: logs.append(a)), bwflash.Firmware(fw_path))
    f.flash()
    raw = open(fw_path, 'rb').read()
    img = esc.image()
    assert img[:len(raw)] == raw
    assert set(img[len(raw):]) <= {0xFF}
    assert esc.active and f.version == '9999'


def test_tx_stream_identical_to_bw_flasher(fw_path):
    if not BW_SRC:
        pytest.skip('set BW_FLASHER_SRC')
    sys.path.insert(0, BW_SRC)
    fake_serial = types.ModuleType('serial')
    fake_serial.Serial = lambda *a, **k: None
    sys.modules.setdefault('serial', fake_serial)
    from bwflasher.brightway_flasher import BrightwayFlasher

    ours = FakeEsc(fw_path)
    bwflash.Flasher(bwflash.Link(ours, log=lambda *a: None), bwflash.Firmware(fw_path)).flash()

    theirs = FakeEsc(fw_path)
    bf = BrightwayFlasher(simulation=True, log_callback=lambda *a: None)
    bf.simulation = False
    bf.serial_conn = theirs
    bf.load_file(fw_path)
    bf.run()

    assert ours.tx_log == theirs.tx_log


@pytest.mark.parametrize('size', [0x1800, 0x1801])
def test_packet_boundary_and_empty_final_packet(tmp_path, size):
    path = synthetic_firmware(tmp_path, size)
    esc = FakeEsc(path)
    f = bwflash.Flasher(bwflash.Link(esc, log=lambda *a: None), bwflash.Firmware(path))
    f.flash()
    raw = open(path, 'rb').read()
    assert esc.image() == raw + b'\xff' * (-size % bwflash.PACKET_SIZE)
    assert f.n_sent == f.fw.total_packets + 1
    assert f.packet == b''
    assert esc.crc_ok and esc.active


def test_preflight_accepts_unsigned_image_like_upstream(tmp_path):
    path = synthetic_firmware(tmp_path)
    raw = bytearray(open(path, 'rb').read())
    raw[0x800] = 0
    open(path, 'wb').write(raw)
    bwflash.Firmware(path)


@pytest.mark.parametrize('change, error', [
    ('signature', 'not a Brightway'), ('leqi', 'LEQI'), ('short', 'size'),
    ('duplicate', 'signing table'), ('table_end', 'signing table'),
    ('constants_end', 'round constants'), ('overlap', 'round constants'),
    ('missing_constants', 'round constants'), ('duplicate_constants', 'round constants'),
])
def test_preflight_rejects_bad_images(tmp_path, change, error):
    path = synthetic_firmware(tmp_path)
    raw = bytearray(open(path, 'rb').read())
    if change == 'signature':
        raw[0x800] = 0
        raw[0x1400:0x1402] = b'\x63\x7c'  # without signature, upstream needs a unique table
    elif change == 'leqi':
        raw[0x0a:0x0e] = b'EU1\x00'
    elif change == 'short':
        raw = raw[:0xfff]
    elif change == 'duplicate':
        raw[0x1400:0x1402] = b'\x63\x7c'
    elif change == 'table_end':
        raw = raw[:0x1102]
    elif change == 'constants_end':
        raw = raw[:0x1303]
    elif change == 'overlap':
        raw[0x1301:0x1303] = b'\xff\xff'
        raw[0x1150:0x1152] = b'\x01\x02'
    elif change == 'missing_constants':
        raw[0x1301:0x1303] = b'\xff\xff'
    else:
        raw[0x1400:0x1402] = b'\x01\x02'
    open(path, 'wb').write(raw)
    with pytest.raises(bwflash.FlashError, match='preflight:.*' + error):
        bwflash.Firmware(path)


def test_model_id_and_bounded_scan(tmp_path):
    path = synthetic_firmware(tmp_path)
    assert bwflash.Firmware(path).model_id == '001500010001'
    # A match crossing the scan boundary must appear once.
    open(path, 'wb').write(b'x' * 4095 + b'yz' + b'x' * 5000)
    assert bwflash._find_all(path, b'yz') == [4095]
    open(path, 'wb').write(b'yz' * 10000)
    assert bwflash._find_all(path, b'yz') == [0, 2]
    assert bwflash._model_id(path) is None
    open(path, 'wb').write(b'x' * 4090 + b'001500010001')
    assert bwflash._model_id(path) == '001500010001'


class Replies:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.sent = []
        self.log = lambda *args: None

    def send(self, data):
        self.sent.append(data)

    def read_until(self, *args):
        return next(self.replies)


def uid_reply(uid):
    body = b'\x64\x2a\x10' + uid
    return body + bytes([sum(body) & 255, 0x9b])


@pytest.mark.parametrize('reply', [b'', b'\x9b\x64', b'\x64\x9b', b'\x64\x2b\x10' + UID + b'\x00\x9b'])
def test_uid_rejects_malformed_frames(reply):
    assert not bwflash.Flasher(Replies([reply]))._get_uid()


def test_uid_is_binary_not_text():
    uid = b'\xff' + b'x' * 15
    f = bwflash.Flasher(Replies([uid_reply(uid), b'0015\r']))
    f.connect()
    assert f.uid == uid


def test_uid_parser_treats_metadata_as_opaque():
    uid = b'x' * 15 + bytes([(0x9b - sum(b'\x64\x2a\x10') - 15 * 120) & 255])
    f = bwflash.Flasher(Replies([uid_reply(uid)]))
    assert f._get_uid() and f.uid == uid


@pytest.mark.parametrize('reply', [b'ok', b'ok \x00\r', b'ok ' + b'x' * 16 + b'!'])
def test_handshake_retries_short_random(reply):
    f = bwflash.Flasher(Replies([reply]))
    assert not f._mcu_rand()
    assert f.mcu_rand is None


def test_invalid_version_retries():
    f = bwflash.Flasher(Replies([b'\xff\xff\xff\xff\r', b'\r']))
    assert not f._get_ver()
    assert not f._get_ver()


def test_state_retry_limit():
    link = Replies([b''] * bwflash.MAX_STATE_TRIES)
    f = bwflash.Flasher(link)
    with pytest.raises(bwflash.FlashError, match='connect/UID:.*200 attempts;.*wiring'):
        f.connect()
    assert len(link.sent) == bwflash.MAX_STATE_TRIES


@pytest.mark.parametrize('last_ack', [True, False])
def test_chunk_retry_boundary(last_ack):
    replies = [b''] * (bwflash.MAX_REPEATS - 1)
    replies += [b'\x06'] * 17 if last_ack else [b'']
    link = Replies(replies)
    f = bwflash.Flasher(link)
    f.packet = b'x' * bwflash.PACKET_SIZE
    if last_ack:
        f._send_packet()
        assert f.n_sent == 1
        assert len(link.sent) == bwflash.MAX_REPEATS + 16
    else:
        with pytest.raises(bwflash.FlashError, match='transfer packet 1 chunk 1:.*20 attempts'):
            f._send_packet()
        assert len(link.sent) == bwflash.MAX_REPEATS


def test_chunk_nak():
    f = bwflash.Flasher(Replies([b'\x15']))
    f.packet = b'x'
    with pytest.raises(bwflash.FlashError, match='transfer packet 1 chunk 1: CRC rejected;'):
        f._send_packet()


@pytest.mark.parametrize('stage, command', [('verify/dfu_verify', b'down dfu_verify\r'),
                                            ('activate/dfu_active', b'down dfu_active\r')])
def test_rejection_has_stage_and_cause(stage, command):
    f = bwflash.Flasher(Replies([b'er\r']))
    with pytest.raises(bwflash.FlashError, match=stage + ':.*incompatible'):
        f._retry(lambda: f._cmd_ok(command), stage)


def test_read_window_stays_100ms(monkeypatch):
    class Noise:
        def any(self):
            return True
        def read(self, n):
            return b'x'
    ticks = iter(range(1000))
    monkeypatch.setattr(time, 'ticks_ms', lambda: next(ticks))
    link = bwflash.Link(Noise(), log=lambda *a: None)
    assert link.read_until(5) == b'xxxxx'
    assert next(ticks) == 101
    assert bwflash.READ_TIMEOUT_MS == 100


def test_read_none_is_safe():
    class Empty:
        def any(self):
            return True
        def read(self, n):
            return None
    assert bwflash.Link(Empty()).read_until(5) == b''


@pytest.mark.parametrize('size', [0x1800, 0x1801])
def test_boundary_tx_stream_identical(tmp_path, size):
    test_tx_stream_identical_to_bw_flasher(synthetic_firmware(tmp_path, size))


@pytest.mark.parametrize('seq, payload', [(0, b'x'), (256, b'x'), (1, b'x' * 129)])
def test_invalid_frame(seq, payload):
    with pytest.raises(ValueError):
        bwflash.build_frame(seq, payload)


def test_missing_image_is_actionable(tmp_path):
    with pytest.raises(bwflash.FlashError, match='preflight:.*file path'):
        bwflash.Firmware(str(tmp_path / 'missing.bin'))


def test_image_truncated_after_preflight(tmp_path):
    path = synthetic_firmware(tmp_path)
    fw = bwflash.Firmware(path)
    open(path, 'wb').write(b'x')
    with pytest.raises(bwflash.FlashError, match='transfer packet 1: image size changed;'):
        fw.packet(0)


def test_ble_key_mismatch_has_stage(tmp_path):
    fw = bwflash.Firmware(synthetic_firmware(tmp_path))
    f = bwflash.Flasher(Replies([b'ok ' + b'x' * 16 + b'\r']), fw)
    f.uid = UID
    with pytest.raises(bwflash.FlashError, match='handshake/ble_rand:.*wrong firmware'):
        f._retry(f._ble_rand, 'handshake/ble_rand')


def test_boot_respects_keep_pin(monkeypatch):
    import runpy
    calls = []
    pin = lambda *args, **kwargs: calls.append((args, kwargs))
    pin.OUT = 1
    monkeypatch.setitem(sys.modules, 'machine', types.SimpleNamespace(Pin=pin))
    monkeypatch.setattr(bwflash, 'KEEP_PIN', 18)
    runpy.run_path(os.path.join(ROOT, 'esp32', 'main.py'))
    assert calls == [((18, 1), {'value': 1})]
    monkeypatch.setattr(bwflash, 'KEEP_PIN', None)
    runpy.run_path(os.path.join(ROOT, 'esp32', 'main.py'))
    assert len(calls) == 1


def test_probe_releases_pins_on_failure(monkeypatch):
    import runpy
    calls = []
    pin = lambda *args: calls.append(args)
    pin.IN = 0
    monkeypatch.setitem(sys.modules, 'machine', types.SimpleNamespace(Pin=pin, UART=None))
    module = runpy.run_path(os.path.join(ROOT, 'esp32', 'probe.py'))
    def fail(pin):
        raise OSError('probe failed')
    module['run'].__globals__['classify'] = fail
    with pytest.raises(OSError, match='probe failed'):
        module['run']((16,))
    assert calls == [(16, 0)]


def test_probe_releases_uart_on_failure(monkeypatch):
    import runpy
    calls = []
    pin = lambda *args: calls.append(args)
    pin.IN = 0
    class BrokenUART:
        def __init__(self, *args, **kwargs):
            pass
        def read(self):
            raise OSError('UART failed')
        def deinit(self):
            calls.append('deinit')
    monkeypatch.setitem(sys.modules, 'machine', types.SimpleNamespace(Pin=pin, UART=BrokenUART, mem32={0x3FF4904C: 0}))
    module = runpy.run_path(os.path.join(ROOT, 'esp32', 'probe.py'))
    with pytest.raises(OSError, match='UART failed'):
        module['_frames'](16)
    assert calls == ['deinit', (5, 0)]
