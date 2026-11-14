"""Bounded Brightway telemetry and fixed zero-throttle light control; no hardware."""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'esp32'))
import bwflash
import telemetry


def frame(cmd, sub, payload):
    prefix = bytes((cmd, sub, len(payload))) + payload
    return prefix + bytes((sum(prefix) & 255, 255 - cmd))


BATTERY = frame(0x61, 0x31, bytes.fromhex('93 56 c9 7b 80 03 11 00 00'))
STATUS = frame(0x61, 0x30, bytes.fromhex('31 00 88 1b 18 10 00 00 00 00'))
INFO = {
    0x21: b'0015\x1a\x01\x02',
    0x23: b'0108\x18\x0a\x1f',
    0x24: b'0001\x18\x0a\x1f',
    0x50: bytes.fromhex('00 05 65 00 1b 00 00 03 03 e8'),
    0x27: b'FAKE-BATTERY', 0x28: b'FAKE-ESC', 0x29: b'FAKE-SCOOTER',
}


class Clock:
    def __init__(self):
        self.now = 0
    def ticks(self):
        return round(self.now * 1000)
    def sleep(self, seconds):
        self.now += seconds


class UART:
    def __init__(self, rx=b'', replies=None, clock=None):
        self.rx, self.replies, self.clock = rx, replies, clock
        self.closed = False
        self.tx = []
        self.reads = []
    def write(self, data):
        self.tx.append((self.clock.ticks() if self.clock else 0, data))
        if self.replies:
            self.rx += self.replies(data, len(self.tx))
        return len(data)
    def any(self):
        return len(self.rx)
    def read(self, count):
        self.reads.append(count)
        result, self.rx = self.rx[:min(7, count)], self.rx[min(7, count):]
        return result
    def deinit(self):
        self.closed = True


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(time, 'ticks_ms', clock.ticks, raising=False)
    monkeypatch.setattr(time, 'ticks_diff', lambda a, b: a - b, raising=False)
    monkeypatch.setattr(time, 'sleep', clock.sleep)
    return clock


@pytest.mark.parametrize('chunk', [1, 2, 7, 64, 4096])
def test_split_partial_interleaved_and_corrupt(chunk):
    info = frame(0x64, 0x21, INFO[0x21])
    bad_sum = BATTERY[:-2] + bytes((BATTERY[-2] ^ 1, BATTERY[-1]))
    bad_tail = BATTERY[:-1] + b'\x9b'
    # Corrupted length must not hold a later complete frame hostage.
    stream = b'noise\x61\x31\x40' + bad_sum + bad_tail + BATTERY + info + STATUS
    splitter = telemetry.FrameSplitter()
    got = []
    for start in range(0, len(stream), chunk):
        got.extend(splitter.feed(stream[start:start + chunk]))
        assert len(splitter.buffer) <= telemetry.MAX_PAYLOAD + 5
    assert got == [BATTERY, info, STATUS]
    assert list(splitter.feed(b'\x00' * 10000)) == []
    assert len(splitter.buffer) <= 69


def test_validator_and_verified_status():
    assert BATTERY.hex() == '6131099356c97b80031100005c9e'
    assert telemetry.valid_frame(BATTERY)
    assert telemetry.decode_status(BATTERY) == dict(battery_percent=86, voltage=51.579, battery_temperature=17)
    assert telemetry.decode_status(STATUS) == dict(esc_temperature=27, event=16)
    older = frame(0x61, 0x30, bytes.fromhex('31 00 88 1a 18 01 00 00'))
    assert telemetry.decode_status(older) == dict(esc_temperature=26, event=1)
    for invalid in (b'', BATTERY[:-1], BATTERY + b'x', BATTERY[:-2] + b'\x00\x9e',
                    frame(0x61, 0x31, b'\x00' * 8), frame(0x61, 0x31, b'\x00\xff' + b'\x00' * 7)):
        assert telemetry.decode_status(invalid) == {}


def test_info_fields_and_no_unknown_labels():
    got = {}
    for sub, payload in INFO.items():
        got.update(telemetry.decode_info(frame(0x64, sub, payload)))
    assert got == dict(esc_version='0015', esc_date='2026-01-02', bms_version='0108',
                      bms_date='2024-10-31', battery_date='2024-10-31',
                      cycles=3,
                      battery_serial='FAKE-BATTERY', esc_serial='FAKE-ESC', scooter_serial='FAKE-SCOOTER')
    assert telemetry.decode_info(frame(0x64, 0x27, b'FAKE\x00\x00')) == {'battery_serial': 'FAKE'}
    for sub, data in ((0x21, b'ab'), (0x50, b'\x00'), (0x27, b'\x80'), (0x29, b''), (0x22, b'unknown')):
        assert telemetry.decode_info(frame(0x64, sub, data)) == {}
    assert telemetry.decode_info(frame(0x64, 0x24, b'0001\x18\x02\x1f')) == {'battery_date': None}


def test_read_status_latest_no_writes_and_cleanup(clock, monkeypatch):
    newer = frame(0x61, 0x31, bytes.fromhex('93 57 c9 7b 80 03 12 00 00'))
    uart = UART(BATTERY + STATUS + newer)
    monkeypatch.setattr(bwflash, 'open_uart', lambda: uart)
    got = telemetry.read_status(100)
    assert got['battery_percent'] == 87 and got['battery_temperature'] == 18
    assert got['esc_temperature'] == 27
    assert not uart.tx and uart.closed and max(uart.reads) <= 64
    uart.rx = b'noise'
    assert telemetry.read_status(100) == {}


def test_read_info_two_identical_replies(clock, monkeypatch, capsys):
    def reply(request, n):
        assert request == bytes((0x53, request[1], (request[1] + 0x53) & 255, 0xac))
        return BATTERY + frame(0x64, request[1], INFO[request[1]]) + STATUS
    uart = UART(replies=reply)
    monkeypatch.setattr(bwflash, 'open_uart', lambda: uart)
    got = telemetry.read_info()
    assert got['unavailable'] == [] and got['cycles'] == 3 and 'energy_wh' not in got
    assert len(uart.tx) == 14 and uart.closed
    assert capsys.readouterr().out == ''


@pytest.mark.parametrize('mode,success', [('bad_first', True), ('different_first', True),
                                         ('aba', True), ('silent', False), ('unstable', False)])
def test_info_retry_until_agree_max_three(clock, monkeypatch, mode, success):
    def reply(request, n):
        good = frame(0x64, 0x21, INFO[0x21])
        if mode == 'silent':
            return BATTERY
        if mode == 'bad_first' and n == 1:
            return good[:-2] + b'\x00\x9b'
        if mode == 'different_first' and n == 1 or mode == 'aba' and n == 2:
            return frame(0x64, 0x21, b'9999\x1a\x01\x02')
        if mode == 'unstable':
            return frame(0x64, 0x21, ('%04d' % n).encode() + b'\x1a\x01\x02')
        return frame(0x64, 0x23, INFO[0x23]) + good
    uart = UART(replies=reply)
    assert bool(telemetry._read_reply(uart, 0x21)) is success
    assert len(uart.tx) == 3


def test_info_missing_fields_are_reported(clock, monkeypatch):
    uart = UART()
    monkeypatch.setattr(bwflash, 'open_uart', lambda: uart)
    assert telemetry.read_info() == {'unavailable': ['0x%02X' % sub for sub in telemetry.INFO_SUBS]}
    assert len(uart.tx) == 21 and uart.closed


@pytest.mark.parametrize('operation', [telemetry.read_status, telemetry.read_info, telemetry.light_test])
def test_uart_error_always_closes(clock, monkeypatch, operation):
    uart = UART()
    def fail(*args):
        raise OSError('UART failure')
    uart.any = fail
    uart.write = fail
    monkeypatch.setattr(bwflash, 'open_uart', lambda: uart)
    with pytest.raises(OSError):
        operation()
    assert uart.closed


@pytest.mark.parametrize('seconds', [5, 10])
def test_light_bytes_timing_and_zero_controls(clock, monkeypatch, seconds):
    uart = UART(clock=clock)
    monkeypatch.setattr(bwflash, 'open_uart', lambda: uart)
    telemetry.light_test(seconds)
    assert len(uart.tx) == seconds * 50
    for n, (at, data) in enumerate(uart.tx):
        assert at == n * 20
        assert data == bytes.fromhex('51 10 06 32 01 88 00 00 1e 40 ae')
        assert data[6:8] == b'\x00\x00'
        assert data[-2] == sum(data[:-2]) & 255
    assert uart.closed


@pytest.mark.parametrize('duration', [0, -1, 10.01, True, '5', float('nan'), float('inf')])
def test_light_rejects_invalid_duration_before_uart(monkeypatch, duration):
    monkeypatch.setattr(bwflash, 'open_uart', lambda: pytest.fail('must not open UART'))
    with pytest.raises(ValueError):
        telemetry.light_test(duration)


def test_light_short_write_closes(clock, monkeypatch):
    uart = UART()
    uart.write = lambda data: 1
    monkeypatch.setattr(bwflash, 'open_uart', lambda: uart)
    with pytest.raises(bwflash.FlashError, match='short UART write'):
        telemetry.light_test()
    assert uart.closed
