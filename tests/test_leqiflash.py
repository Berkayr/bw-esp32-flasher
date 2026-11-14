"""Synthetic LEQI parity tests; never opens a physical serial port."""
import binascii
import builtins
import os
import struct
import sys
import time
import types

import pytest

from test_bwflash import BW_SRC, FakeEsc, synthetic_firmware
import bwflash
import flasher
import leqiflash


def reference_crc(data):
    crc = 0xffff
    for value in data:
        value = int('{:08b}'.format(value)[::-1], 2)
        crc ^= value << 8
        for _ in range(8):
            crc = ((crc << 1) ^ (0x8005 if crc & 0x8000 else 0)) & 0xffff
    return int('{:016b}'.format(crc)[::-1], 2)


def synthetic_leqi(tmp_path, size=0x9873, plaintext=False, valid_crc=None):
    body = bytearray((b'\x13\x24\x35\xaa' if not plaintext else b'\x13\x24\x35\x46') * ((size + 3) // 4))[:size]
    if valid_crc is None:
        valid_crc = not plaintext
    crc = reference_crc(body[0x40:-2])
    body[-2:] = struct.pack('>H', crc if valid_crc else crc ^ 1)
    header = bytearray(128)
    header[10:14] = b'EU1\x00'
    header[14:16] = struct.pack('<H', size)
    path = tmp_path / 'leqi.bin'
    # Extra bytes are intentionally outside the authoritative header window.
    path.write_bytes(header + body + b'ignored trailer')
    return str(path), bytes(body)


class Timestamp(float):
    def __sub__(self, other):
        # Avoid float cancellation adding an artificial poll at timeout boundaries.
        return round(float(self) - float(other), 6)


class Clock:
    def __init__(self):
        self.now = 0
        self.sleeps = []

    def sleep(self, delay):
        self.sleeps.append(delay)
        self.now = round(self.now + delay, 6)


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(time, 'time', lambda: Timestamp(clock.now))
    monkeypatch.setattr(time, 'sleep', clock.sleep)
    monkeypatch.setattr(time, 'ticks_ms', lambda: round(clock.now * 1000))
    return clock


class FakeLeqiEsc(FakeEsc):
    """Both pyserial and MicroPython interfaces, validating packets independently."""
    def __init__(self, clock, mode='ok'):
        self.clock = clock
        self.mode = mode
        self.rx = b''
        self.tx_log = b''
        self.sent_at = []
        self.mem = bytearray()
        self.is_open = True
        self.end_attempts = 0
        self.active = False

    def write(self, data):
        data = bytes(data)
        self.sent_at.append(self.clock.now)
        self.tx_log += data
        assert data[:2] == b'\x5a\x12'
        assert len(data) == data[3] + 6
        assert int.from_bytes(data[-2:], 'big') == binascii.crc_hqx(data[:-2], 0)
        command = data[2]
        if command == 2:
            self.rx += b'noise' + bytes([0x5a, 0x21, 2, 11, 0, 1, 2, 0, 0, 1, 0, 0, 69, 85, 49, 0, 0])
            return
        if command == 3:
            assert data[4:6] == b'\x31\x00' and data[8:10] == b'\x00\x00'
            self.size = struct.unpack('<H', data[6:8])[0]
            if self.mode == 'start_timeout':
                return
        elif command == 4:
            offset = struct.unpack('<I', data[4:8])[0]
            assert offset == len(self.mem)
            self.mem.extend(data[8:-2])
            if self.mode == 'chunk_timeout' and offset == 0:
                return
        elif command == 5:
            self.end_attempts += 1
            if self.mode == 'end_timeout' or (self.mode == 'retry' and self.end_attempts < 10):
                return
            self.active = True
        else:
            raise AssertionError(command)
        status = 0 if self.mode == 'chunk_reject' and command == 4 else 1
        # Bad response CRC is deliberate: upstream does not validate it.
        self.rx += bytes([0x5a, 0x21, command, 1, status, 0, 0])
        if self.mode == 'short':
            self.rx = self.rx[:5]
        if self.mode == 'wrong_command':
            self.rx = self.rx[:2] + b'\xff' + self.rx[3:]

    @property
    def in_waiting(self):
        return self.any()

    def reset_input_buffer(self):
        self.rx = b''

    def reset_output_buffer(self):
        pass

    def close(self):
        self.is_open = False

    deinit = close

    def image(self):
        return bytes(self.mem[:self.size])


def upstream(monkeypatch, esc):
    if not BW_SRC:
        pytest.skip('set BW_FLASHER_SRC')
    sys.path.insert(0, BW_SRC)
    serial = types.ModuleType('serial')
    serial.Serial = lambda **kwargs: esc
    serial.EIGHTBITS, serial.PARITY_NONE, serial.STOPBITS_ONE = 8, 'N', 1
    serialutil = types.ModuleType('serial.serialutil')
    serialutil.SerialException = OSError
    monkeypatch.setitem(sys.modules, 'serial', serial)
    monkeypatch.setitem(sys.modules, 'serial.serialutil', serialutil)
    from bwflasher import leqi_flasher
    monkeypatch.setattr(leqi_flasher, 'serial', serial)
    return leqi_flasher.LeqiFlasher()


@pytest.mark.parametrize('plaintext', [False, True])
@pytest.mark.parametrize('size', [0x42, 128, 129, 0x9873, 65535])
def test_streamed_image(tmp_path, clock, plaintext, size):
    path, body = synthetic_leqi(tmp_path, size, plaintext)
    fw = leqiflash.Firmware(path)
    assert fw.size == size
    assert fw.plaintext_body == plaintext
    esc = FakeLeqiEsc(clock)
    leqiflash.Flasher(leqiflash.Link(esc, log=lambda *a: None), fw).flash()
    assert esc.image() == body
    assert bytes(esc.mem[size:]) == b'\xff' * (-size % 128)
    assert esc.active


@pytest.mark.parametrize('plaintext', [False, True])
@pytest.mark.parametrize('mode', ['ok', 'short', 'retry', 'chunk_timeout', 'chunk_reject', 'start_timeout', 'end_timeout', 'wrong_command'])
def test_upstream_tx_and_timing_parity(tmp_path, monkeypatch, clock, plaintext, mode):
    path, body = synthetic_leqi(tmp_path, 0x801, plaintext)
    ours = FakeLeqiEsc(clock, mode)
    progress = []
    f = leqiflash.Flasher(leqiflash.Link(ours, log=lambda *a: None), leqiflash.Firmware(path), progress.append)
    ours_error = False
    try:
        f.flash()
    except bwflash.FlashError:
        ours_error = True
    ours_sleeps = clock.sleeps[:]
    clock.now = 0
    clock.sleeps.clear()
    theirs = FakeLeqiEsc(clock, mode)
    original = upstream(monkeypatch, theirs)
    original.load_file(path)
    theirs_progress = []
    original.progress_callback = theirs_progress.append
    theirs_error = False
    try:
        original.run()
    except Exception:
        theirs_error = True
    assert ours_error == theirs_error
    assert ours.tx_log == theirs.tx_log
    assert ours.mem == theirs.mem
    assert ours_sleeps == clock.sleeps
    assert ours.sent_at == theirs.sent_at
    assert progress == theirs_progress
    if not ours_error:
        assert ours.image() == theirs.image() == body


def test_info_matches_upstream(tmp_path, monkeypatch, clock):
    ours = FakeLeqiEsc(clock)
    assert leqiflash.Flasher(leqiflash.Link(ours, log=lambda *a: None)).info() == ('0120', 'EU1')
    theirs = FakeLeqiEsc(clock)
    upstream(monkeypatch, theirs).test_connection()
    assert ours.tx_log == theirs.tx_log
    assert not ours.mem and not theirs.mem


@pytest.mark.parametrize('kind', ['brightway', 'leqi', 'both', 'unknown', 'small_size', 'truncated', 'unique_table', 'duplicate_table'])
def test_detection_matches_upstream(tmp_path, monkeypatch, clock, kind):
    path, _ = synthetic_leqi(tmp_path, 0x1800)
    raw = bytearray(open(path, 'rb').read())
    if kind in ('brightway', 'both'):
        raw[0x800:0x808] = b'DEPRD5C\x00'
        if kind == 'brightway':
            raw[10:14] = b'xxxx'
    elif kind == 'unknown':
        raw[10:14] = b'xxxx'
    elif kind == 'small_size':
        raw[14:16] = b'\x41\x00'
    elif kind == 'truncated':
        raw = raw[:-200]
    elif kind in ('unique_table', 'duplicate_table'):
        raw[0x1100:0x1102] = b'\x63\x7c'
        if kind == 'duplicate_table':
            raw[0x1200:0x1202] = b'\x63\x7c'
    open(path, 'wb').write(raw)
    upstream(monkeypatch, FakeLeqiEsc(clock))
    from bwflasher.base_flasher import detect_firmware_type
    assert flasher.detect_firmware_type(path) == detect_firmware_type(bytes(raw)).value


@pytest.mark.parametrize('family', ['Brightway', 'LEQI', 'Unknown'])
def test_dispatch_without_opening_uart(tmp_path, monkeypatch, family):
    if family == 'Brightway':
        path = synthetic_firmware(tmp_path)
    else:
        path, _ = synthetic_leqi(tmp_path)
        if family == 'Unknown':
            open(path, 'wb').write(b'bad')
    calls = []
    target = bwflash if family == 'Brightway' else leqiflash
    monkeypatch.setattr(target, 'flash', lambda path, debug: calls.append((path, debug)))
    if family == 'Unknown':
        with pytest.raises(bwflash.FlashError, match='preflight: unknown'):
            flasher.flash(path)
        assert not calls
    else:
        flasher.flash(path, debug=True)
        assert calls == [(path, True)]


def test_crc_and_preflight_failures(tmp_path):
    data = bytes(range(256)) * 20
    assert leqiflash.crc16_firmware(data) == reference_crc(data)
    assert leqiflash.crc16_firmware(data[100:], leqiflash.crc16_firmware(data[:100])) == reference_crc(data)
    path, _ = synthetic_leqi(tmp_path, valid_crc=False)
    with pytest.raises(bwflash.FlashError, match='preflight:.*CRC mismatch'):
        leqiflash.Firmware(path)
    with pytest.raises(bwflash.FlashError, match='preflight:.*file path'):
        leqiflash.Firmware(path + '.missing')
    with pytest.raises(bwflash.FlashError, match='preflight:.*file path'):
        flasher.detect_firmware_type(path + '.missing')
    open(path, 'wb').write(b'EU1')
    with pytest.raises(bwflash.FlashError, match='preflight:.*header'):
        leqiflash.Firmware(path)


def test_valid_crc_overrides_plaintext_density(tmp_path):
    path, _ = synthetic_leqi(tmp_path, plaintext=True, valid_crc=True)
    assert not leqiflash.Firmware(path).plaintext_body


def test_truncation_after_preflight(tmp_path):
    path, _ = synthetic_leqi(tmp_path)
    fw = leqiflash.Firmware(path)
    open(path, 'wb').write(b'bad')
    with pytest.raises(bwflash.FlashError, match='transfer chunk 1: image size changed'):
        fw.chunk(0)


def test_bounded_file_reads(tmp_path, monkeypatch):
    path, _ = synthetic_leqi(tmp_path, 65535)
    real_open = builtins.open
    sizes = []
    class BoundedFile:
        def __init__(self, *args):
            self.f = real_open(*args)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.f.close()
        def seek(self, *args):
            return self.f.seek(*args)
        def tell(self):
            return self.f.tell()
        def read(self, size=-1):
            assert 0 <= size <= 4096
            sizes.append(size)
            return self.f.read(size)
    monkeypatch.setattr(builtins, 'open', BoundedFile)
    assert flasher.detect_firmware_type(path) == 'LEQI'
    fw = leqiflash.Firmware(path)
    for offset in range(0, fw.size, 128):
        assert len(fw.chunk(offset)) == 128
    assert max(sizes) == 4096


def test_public_api_releases_uart(tmp_path, monkeypatch, clock):
    path, _ = synthetic_leqi(tmp_path, 128)
    esc = FakeLeqiEsc(clock)
    monkeypatch.setattr(leqiflash, 'open_uart', lambda: esc)
    leqiflash.flash(path)
    assert not esc.is_open and esc.active
    esc = FakeLeqiEsc(clock, 'start_timeout')
    monkeypatch.setattr(leqiflash, 'open_uart', lambda: esc)
    with pytest.raises(bwflash.FlashError, match='start/update:.*wiring'):
        leqiflash.flash(path)
    assert not esc.is_open
    esc = FakeLeqiEsc(clock)
    monkeypatch.setattr(leqiflash, 'open_uart', lambda: esc)
    assert leqiflash.info() == ('0120', 'EU1')
    assert not esc.is_open
