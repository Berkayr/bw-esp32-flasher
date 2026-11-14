"""Host-only HTTP, filesystem and ESC-worker tests; never opens a UART."""
import asyncio
import json
from pathlib import Path
import runpy
import sys
import threading
import time
import types

import pytest

from test_bwflash import FakeEsc, synthetic_firmware
from test_leqiflash import Clock, FakeLeqiEsc, synthetic_leqi
import bwflash
import leqiflash
import webui


class Reader:
    def __init__(self, data, chunk=97):
        self.data = data
        self.chunk = chunk
        self.requests = []

    async def read(self, count):
        self.requests.append(count)
        size = min(count, self.chunk)
        result, self.data = self.data[:size], self.data[size:]
        await asyncio.sleep(0)
        return result


class Writer:
    def __init__(self, broken=False):
        self.data = bytearray()
        self.broken = broken
        self.closed = False

    def write(self, data):
        if self.broken:
            raise OSError('phone disconnected')
        self.data.extend(data)

    async def drain(self):
        await asyncio.sleep(0)

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


def request(path='/api/status', body=b'', token=None, method=None, extra=b''):
    method = method or ('POST' if path in ('/api/upload', '/api/info', '/api/flash', '/api/probe', '/api/data', '/api/battery', '/api/light', '/api/light_off') else 'GET')
    kind = 'application/octet-stream' if path == '/api/upload' else 'application/json'
    return (('%s %s HTTP/1.1\r\nHost: %s\r\nContent-Length: %d\r\nContent-Type: %s\r\n' %
             (method, path, webui.IP, len(body), kind)).encode() +
            (('X-Flasher-Token: %s\r\n' % token).encode() if token else b'') + extra + b'\r\n' + body)


def call(ui, path='/api/status', data=None, raw=None, writer=None, **kwargs):
    body = raw if raw is not None else (json.dumps(data).encode() if data is not None else b'')
    reader = Reader(request(path, body, token=ui.token, **kwargs))
    writer = writer or Writer()
    asyncio.run(ui.handle(reader, writer))
    if writer.broken:
        return writer
    head, body = bytes(writer.data).split(b'\r\n\r\n', 1)
    return int(head.split()[1]), json.loads(body)


def settled(jobs):
    deadline = time.monotonic() + 3
    while jobs.snapshot()['busy'] and time.monotonic() < deadline:
        time.sleep(.002)
    assert not jobs.snapshot()['busy'], 'worker did not finish'
    return jobs.snapshot()


@pytest.fixture
def ui(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(webui, 'radio', lambda on: None)
    monkeypatch.setattr(webui.telemetry, 'read_status', lambda: {'battery_percent': 80})
    return webui.WebUI()


def test_parser_leaves_body_unread():
    reader = Reader(request('/api/upload', b'abcd'))
    path, headers, length = asyncio.run(webui.parse_request(reader))
    assert (path, length) == ('/api/upload', 4)
    assert reader.data == b'abcd'


@pytest.mark.parametrize('payload,status', [
    (b'GET / HTTP/2\r\nHost: 192.168.4.1\r\n\r\n', 400),
    (b'GET / HTTP/1.1\r\nHost: foreign\r\n\r\n', 403),
    (b'GET / HTTP/1.1\r\nHost: 192.168.4.1\r\nHost: 192.168.4.1\r\n\r\n', 400),
    (request('/../../secret'), 404),
    (request('/', method='PUT'), 405),
    (request('/', body=b'x'), 400),
    (request(extra=b'Transfer-Encoding: chunked\r\n'), 400),
    (request(extra=b'Expect: 100-continue\r\n'), 400),
    (b'GET / HTTP/1.1\r\nX:' + b'x' * 513, 431),
    (b'GET / HTTP/1.1\r\n' + b'X%d: a\r\n' * 300, 431),
    (b'POST /api/info HTTP/1.1\r\nHost: 192.168.4.1\r\n\r\n', 411),
    (b'GET / HTTP/1.1\r\nContent-Length: -1\r\n\r\n', 400),
    (b'GET / HTTP/1.1\r\nX: \x01\r\n\r\n', 400),
    (request('/api/info', b'x' * 513), 413),
    (request('/api/upload', b'x' * (webui.MAX_UPLOAD + 1)), 413),
    (b'GET / HTTP/1.1\r\n', 400),
])
def test_bad_http(payload, status):
    with pytest.raises(webui.HTTPError) as error:
        asyncio.run(webui.parse_request(Reader(payload)))
    assert error.value.status == status


@pytest.mark.parametrize('chunk', [1, 127, 1024, 4097])
def test_stream_upload_boundaries(ui, chunk):
    data = bytes(range(256)) * 561
    Path(webui.IMAGE_PATH).write_bytes(b'old image')
    reader = Reader(data, chunk)
    asyncio.run(webui.stream_upload(reader, len(data)))
    assert Path(webui.TEMP_PATH).read_bytes() == data
    assert Path(webui.IMAGE_PATH).read_bytes() == b'old image'
    assert max(reader.requests) <= 1024


@pytest.mark.parametrize('length', [0, 4095, webui.MAX_UPLOAD + 1])
def test_upload_size_before_write(ui, length):
    with pytest.raises(webui.HTTPError):
        asyncio.run(webui.stream_upload(Reader(b''), length))
    assert not Path(webui.TEMP_PATH).exists()


def test_upload_no_space_before_write(ui, monkeypatch):
    monkeypatch.setattr(webui.os, 'statvfs', lambda path: (4096, 4096, 0, 0, 1))
    with pytest.raises(webui.HTTPError, match='free filesystem'):
        asyncio.run(webui.stream_upload(Reader(b'x' * 5000), 5000))
    assert not Path(webui.TEMP_PATH).exists()


def test_upload_abort_releases_lock_and_preserves_previous_file(ui):
    Path(webui.IMAGE_PATH).write_bytes(b'previous')
    with pytest.raises(webui.HTTPError, match='interrupted'):
        asyncio.run(ui.upload(Reader(b'x' * 2000), 5000))
    assert not Path(webui.TEMP_PATH).exists()
    assert Path(webui.IMAGE_PATH).read_bytes() == b'previous'
    assert not ui.jobs.snapshot()['busy']
    ui.jobs.claim('next')
    ui.jobs.release()


def test_cancelled_upload_cleans_temp_and_lock(ui):
    class Stalled(Reader):
        async def read(self, count):
            if self.data:
                return await super().read(count)
            await asyncio.Event().wait()
    async def cancel():
        task = asyncio.create_task(ui.upload(Stalled(b'x' * 1024), 5000))
        while not Path(webui.TEMP_PATH).exists():
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(cancel())
    assert not Path(webui.TEMP_PATH).exists()
    assert not ui.jobs.snapshot()['busy']


@pytest.mark.parametrize('family', ['Brightway', 'LEQI'])
def test_upload_preflight_atomic_rename(ui, tmp_path, family):
    path = synthetic_firmware(tmp_path) if family == 'Brightway' else synthetic_leqi(tmp_path, 0x1800)[0]
    data = Path(path).read_bytes()
    status, _ = call(ui, '/api/upload', raw=data)
    assert status == 202
    result = settled(ui.jobs)
    assert result['image']['family'] == family
    assert result['image']['size'] == len(data)
    assert Path(webui.IMAGE_PATH).read_bytes() == data
    assert not Path(webui.TEMP_PATH).exists()
    assert result['error'] is None


def test_bad_image_not_promoted(ui):
    Path(webui.IMAGE_PATH).write_bytes(b'old')
    ui.jobs.update(image={'id': 'old'})
    assert call(ui, '/api/upload', raw=b'x' * 5000)[0] == 202
    state = settled(ui.jobs)
    assert state['image'] is None and 'raw Brightway' in state['error']
    assert Path(webui.IMAGE_PATH).read_bytes() == b'old'
    assert not Path(webui.TEMP_PATH).exists()


def test_family_preflight_failure_not_promoted(ui, tmp_path):
    path = synthetic_firmware(tmp_path)
    data = bytearray(Path(path).read_bytes())
    data[0x1100:0x1200] = b'\x00' * 256
    assert call(ui, '/api/upload', raw=data)[0] == 202
    assert 'signing table' in settled(ui.jobs)['error']
    assert not Path(webui.IMAGE_PATH).exists()


def test_lock_rejects_all_mutations_but_keeps_status(ui):
    ui.jobs.claim('flash')
    try:
        for path, data, raw in [('/api/info', {'family': 'Brightway'}, None),
                                ('/api/probe', {'pins': [4]}, None),
                                ('/api/flash', {'confirm': 'id'}, None),
                                ('/api/upload', None, b'x' * 5000)]:
            assert call(ui, path, data, raw)[0] == 409
        status, data = call(ui)
        assert status == 200 and data['busy'] and data['job'] == 'flash'
    finally:
        ui.jobs.release()


@pytest.mark.parametrize('family', ['Brightway', 'LEQI'])
def test_info_shows_only_protocol_fields(ui, monkeypatch, family):
    module = ui.jobs.module(family)
    monkeypatch.setattr(module, 'info', lambda: (b'uid', '0015') if family == 'Brightway' else ('0120', 'EU1'))
    assert call(ui, '/api/info', {'family': family})[0] == 202
    state = settled(ui.jobs)
    assert state['info']['family'] == family
    assert state['info']['uid'] == ('756964' if family == 'Brightway' else None)


def test_info_failure_clears_stale_read(ui, monkeypatch):
    ui.jobs.update(info={'family': 'Brightway'})
    def fail():
        raise bwflash.FlashError('connect/UID: no valid reply')
    monkeypatch.setattr(bwflash, 'info', fail)
    call(ui, '/api/info', {'family': 'Brightway'})
    state = settled(ui.jobs)
    assert state['info'] is None and 'no valid reply' in state['error']


def test_flash_needs_current_confirmation_and_matching_info(ui):
    ui.jobs.update(image={'id': 'current', 'family': 'LEQI'})
    for data in ({}, {'confirm': 'previous'}, {'confirm': 'current'}):
        assert call(ui, '/api/flash', data)[0] == 409
    ui.jobs.update(info={'family': 'Brightway'})
    assert call(ui, '/api/flash', {'confirm': 'current'})[0] == 409
    assert not ui.jobs.snapshot()['busy']


def test_disconnect_during_flash_continues_and_reconnect_reads_result(ui, monkeypatch):
    running, finish = threading.Event(), threading.Event()
    ui.jobs.update(image={'id': 'current', 'family': 'Brightway'}, info={'family': 'Brightway'})
    def flash(path, progress):
        assert path == webui.IMAGE_PATH
        progress(1, 2)
        running.set()
        assert finish.wait(3)
        progress(2, 2)
    monkeypatch.setattr(bwflash, 'flash', flash)
    try:
        writer = call(ui, '/api/flash', {'confirm': 'current'}, writer=Writer(broken=True))
        assert writer.closed and running.wait(2)
        status, state = call(ui)
        assert status == 200 and state['busy'] and (state['done'], state['total']) == (1, 2)
    finally:
        finish.set()
    settled(ui.jobs)
    _, state = call(ui)
    assert not state['busy'] and state['done'] == 2
    assert 'verify OK' in state['result'] and state['error'] is None


def test_worker_exception_and_start_failure_release_lock(ui, monkeypatch):
    def fail():
        raise OSError('UART failure')
    ui.jobs.claim('info')
    ui.jobs.start_owned(fail)
    assert settled(ui.jobs)['error'] == 'UART failure'
    monkeypatch.setattr(webui._thread, 'start_new_thread', lambda *a: fail())
    assert call(ui, '/api/info', {'family': 'Brightway'})[0] == 500
    assert not ui.jobs.snapshot()['busy']


@pytest.mark.parametrize('pins', [[6], [0], [1], [3], [34], [4, 4], [True], [], list(webui.SAFE_PINS), '4,16'])
def test_probe_rejects_unsafe_gpio(ui, pins):
    assert call(ui, '/api/probe', {'pins': pins})[0] == 400
    assert not ui.jobs.snapshot()['busy']


def test_probe_verdicts_and_keep_restore(ui, monkeypatch):
    pin_calls = []
    pin = lambda *a, **kw: pin_calls.append((a, kw))
    pin.OUT = 1
    monkeypatch.setitem(sys.modules, 'machine', types.SimpleNamespace(Pin=pin))
    monkeypatch.setitem(sys.modules, 'probe', types.SimpleNamespace(
        run=lambda pins, report: [report(p, 'floating -> RX or keep-alive') for p in pins]))
    ui.jobs.update(info={'family': 'Brightway'})
    assert call(ui, '/api/probe', {'pins': [4, 16]})[0] == 202
    state = settled(ui.jobs)
    assert state['result'] == ['GPIO4: floating -> RX or keep-alive', 'GPIO16: floating -> RX or keep-alive']
    assert state['info'] is None
    assert pin_calls[-1] == ((bwflash.KEEP_PIN, 1), {'value': 1})


def test_probe_keep_restore_on_error(ui, monkeypatch):
    calls = []
    pin = lambda *a, **kw: calls.append(kw)
    pin.OUT = 1
    def fail(*a, **kw):
        raise OSError('probe failed')
    monkeypatch.setitem(sys.modules, 'machine', types.SimpleNamespace(Pin=pin))
    monkeypatch.setitem(sys.modules, 'probe', types.SimpleNamespace(run=fail))
    ui.jobs.claim('probe')
    ui.jobs.start_owned(ui.jobs.probe, ((17,),))
    assert 'probe failed' in settled(ui.jobs)['error']
    assert calls[-1] == {'value': 1}


def test_csrf_rejects_missing_token_and_external_origin(ui):
    writer = Writer()
    asyncio.run(ui.handle(Reader(request('/api/info', b'{}')), writer))
    assert b'403' in writer.data.split(b'\r\n')[0]
    assert call(ui, '/api/info', {'family': 'Brightway'}, extra=b'Origin: https://other.example\r\n')[0] == 403
    assert call(ui, '/api/info', raw=b'[]')[0] == 400
    assert call(ui, '/api/info', raw=b'{')[0] == 400


def test_page_is_streamed(ui, monkeypatch):
    page = Path(webui.__file__).with_name('webui.html')
    monkeypatch.setattr(webui, 'PAGE_PATH', str(page))
    writer = Writer()
    asyncio.run(ui.handle(Reader(request('/')), writer))
    header, data = bytes(writer.data).split(b'\r\n\r\n', 1)
    assert b'200' in header and data == page.read_bytes()
    assert b'Content-Security-Policy:' in header
    assert b'<script src=' not in data and b'https://' not in data


def test_connection_limit_closes(ui):
    ui.clients = 3
    writer = Writer()
    asyncio.run(ui.handle(Reader(b''), writer))
    assert writer.closed and not writer.data


def test_boot_keeps_pin_high_before_ui(monkeypatch):
    calls = []
    pin = lambda *a, **kw: calls.append(('pin', a, kw))
    pin.OUT = 1
    monkeypatch.setitem(sys.modules, 'machine', types.SimpleNamespace(Pin=pin))
    monkeypatch.setitem(sys.modules, 'webui', types.SimpleNamespace(run=lambda: calls.append('webui')))
    runpy.run_path(str(Path(webui.__file__).with_name('main.py')), run_name='__main__')
    assert calls == [('pin', (bwflash.KEEP_PIN, 1), {'value': 1}), 'webui']


def test_disabled_flag_and_gpio(ui, monkeypatch):
    class Pin:
        IN, PULL_UP = 0, 1
        def __init__(self, *a):
            pass
        def value(self):
            return level[0]
    monkeypatch.setitem(sys.modules, 'machine', types.SimpleNamespace(Pin=Pin))
    level = [1]
    assert not webui.disabled()
    level[0] = 0
    assert webui.disabled()
    level[0] = 1
    Path('webui.disabled').touch()
    assert webui.disabled()
    monkeypatch.setattr(webui, 'start_ap', lambda: pytest.fail('AP should not start'))
    webui.run()


def test_ap_name_security_and_ip(ui, monkeypatch):
    class WLAN:
        IF_STA, IF_AP = 0, 1
        def __init__(self, mode):
            self.mode = mode
            calls.append(('new', mode))
        def active(self, value):
            calls.append(('active', self.mode, value))
        def config(self, key=None, **values):
            if key == 'mac':
                return b'\x01\x02\x03\x04\xab\xcd'
            calls.append(('config', values))
        def ifconfig(self, value):
            calls.append(('ip', value))
    calls = []
    monkeypatch.setitem(sys.modules, 'network', types.SimpleNamespace(WLAN=WLAN))
    webui.start_ap()
    assert ('active', 0, False) in calls
    assert ('config', dict(essid='bw-flasher-ABCD', authmode=3,
                           password=webui.AP_PASSWORD, max_clients=2)) in calls
    assert ('ip', (webui.IP, '255.255.255.0', webui.IP, webui.IP)) in calls


def test_brightway_public_hooks_preserve_wire_and_close_uart(tmp_path, monkeypatch):
    path = synthetic_firmware(tmp_path)
    baseline, hooked = FakeEsc(path), FakeEsc(path)
    for esc in (baseline, hooked):
        esc.is_open = True
        esc.deinit = lambda esc=esc: setattr(esc, 'is_open', False)
    monkeypatch.setattr(bwflash, 'open_uart', lambda: baseline)
    bwflash.flash(path)
    updates = []
    monkeypatch.setattr(bwflash, 'open_uart', lambda: hooked)
    bwflash.flash(path, progress=lambda done, total: updates.append((done, total)), log=lambda *a: None)
    assert baseline.tx_log == hooked.tx_log
    assert updates == [(n, 4) for n in range(1, 5)]
    assert not baseline.is_open and not hooked.is_open


@pytest.mark.parametrize('mode', ['ok', 'chunk_reject', 'retry'])
def test_leqi_packet_hooks_preserve_bytes_and_delays(tmp_path, monkeypatch, mode):
    path, _ = synthetic_leqi(tmp_path, 0x801)
    clock = Clock()
    monkeypatch.setattr(time, 'time', lambda: clock.now)
    monkeypatch.setattr(time, 'ticks_ms', lambda: round(clock.now * 1000))
    monkeypatch.setattr(time, 'sleep', clock.sleep)
    baseline, hooked = FakeLeqiEsc(clock, mode), FakeLeqiEsc(clock, mode)
    def run(esc, **kwargs):
        monkeypatch.setattr(leqiflash, 'open_uart', lambda: esc)
        try:
            leqiflash.flash(path, log=lambda *a: None, **kwargs)
        except bwflash.FlashError:
            assert mode == 'chunk_reject'
    run(baseline)
    delays = clock.sleeps[:]
    clock.sleeps.clear()
    clock.now = 0
    updates = []
    run(hooked, progress=lambda done, total: updates.append((done, total)))
    assert hooked.tx_log == baseline.tx_log and hooked.sent_at == baseline.sent_at
    assert clock.sleeps == delays
    assert updates == [(n, 17) for n in range(1, 18)]
    assert not hooked.is_open


def test_upload_timeout_cleans_temp_and_unlocks(ui, monkeypatch):
    class Stalled(Reader):
        async def read(self, count):
            if self.data:
                return await super().read(count)
            await asyncio.Event().wait()
    original = asyncio.wait_for
    async def short_timeout(awaitable, timeout):
        return await original(awaitable, .01)
    monkeypatch.setattr(webui.asyncio, 'wait_for', short_timeout)
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(ui.upload(Stalled(b'x' * 1024), 5000))
    assert not Path(webui.TEMP_PATH).exists()
    assert not ui.jobs.snapshot()['busy']
    assert 'timed out' in ui.jobs.snapshot()['error']


def test_brightway_uart_cleanup_on_connection_error(tmp_path, monkeypatch):
    class Silent:
        def __init__(self):
            self.closed = False
        def write(self, data):
            pass
        def any(self):
            return 0
        def read(self, count):
            return b''
        def deinit(self):
            self.closed = True
    monkeypatch.setattr(bwflash, 'MAX_STATE_TRIES', 1)
    clock = iter(range(100000))
    monkeypatch.setattr(time, 'ticks_ms', lambda: next(clock))
    for operation in (lambda: bwflash.info(), lambda: bwflash.flash(synthetic_firmware(tmp_path))):
        uart = Silent()
        monkeypatch.setattr(bwflash, 'open_uart', lambda: uart)
        with pytest.raises(bwflash.FlashError, match='connect/UID'):
            operation()
        assert uart.closed


def test_info_reads_until_two_replies_agree(ui, monkeypatch):
    replies = iter([(b'uid', '999'), (b'uid', '9999'), (b'uid', '9999')])
    monkeypatch.setattr(bwflash, 'info', lambda: next(replies))
    call(ui, '/api/info', {'family': 'Brightway'})
    assert settled(ui.jobs)['info']['version'] == '9999'


def test_info_rejects_unstable_replies(ui, monkeypatch):
    replies = iter(['1', '2', '3', '4', '5'])
    monkeypatch.setattr(bwflash, 'info', lambda: (b'uid', next(replies)))
    call(ui, '/api/info', {'family': 'Brightway'})
    state = settled(ui.jobs)
    assert state['info'] is None and 'replies differ' in state['error']


@pytest.mark.parametrize('fails', [False, True])
def test_flash_turns_radio_off_and_back_on(ui, monkeypatch, fails):
    events = []
    monkeypatch.setattr(webui, 'RADIO_OFF_DELAY', 0)
    monkeypatch.setattr(webui, 'radio', lambda on: events.append(on))
    def flash(path, progress):
        events.append('flash')
        if fails:
            raise bwflash.FlashError('transfer packet 1: CRC rejected')
    monkeypatch.setattr(bwflash, 'flash', flash)
    ui.jobs.update(image={'id': 'current', 'family': 'Brightway'}, info={'family': 'Brightway'})
    call(ui, '/api/flash', {'confirm': 'current'})
    settled(ui.jobs)
    assert events == [False, 'flash', True]


@pytest.mark.parametrize('values,override,allowed', [
    ({'battery_percent': 19}, None, False), ({'battery_percent': 20}, None, True),
    ({'battery_percent': 80}, None, True), ({}, None, False),
    ({'battery_percent': 19}, True, True), ({}, True, True),
    ({'battery_percent': 1}, 'true', False), ({}, 1, False),
    ({'battery_percent': 101}, None, False), ({'battery_percent': True}, None, False),
])
def test_battery_guard_rechecks_on_server(ui, monkeypatch, values, override, allowed):
    flashed, radio = [], []
    monkeypatch.setattr(webui, 'RADIO_OFF_DELAY', 0)
    monkeypatch.setattr(webui, 'radio', radio.append)
    monkeypatch.setattr(webui.telemetry, 'read_status', lambda: values)
    monkeypatch.setattr(bwflash, 'flash', lambda *a, **kw: flashed.append(True))
    ui.jobs.update(image={'id': 'current', 'family': 'Brightway'}, info={'family': 'Brightway'},
                   data={'battery_percent': 99}, battery_warning=None)
    assert call(ui, '/api/flash', {'confirm': 'current', 'battery_override': override})[0] == 202
    state = settled(ui.jobs)
    assert bool(flashed) is allowed
    assert (state['error'] is None) is allowed
    assert radio == [False, True]
    if not allowed:
        assert 'brick the controller' in state['error']
        assert state['battery_warning']


@pytest.mark.parametrize('override', [True, False])
def test_battery_uart_failure_requires_override(ui, monkeypatch, override):
    flashed = []
    def fail():
        raise OSError('read failed')
    monkeypatch.setattr(webui.telemetry, 'read_status', fail)
    monkeypatch.setattr(webui, 'RADIO_OFF_DELAY', 0)
    monkeypatch.setattr(bwflash, 'flash', lambda *a, **kw: flashed.append(True))
    ui.jobs.update(image={'id': 'id', 'family': 'Brightway'}, info={'family': 'Brightway'})
    call(ui, '/api/flash', {'confirm': 'id', 'battery_override': override})
    state = settled(ui.jobs)
    assert bool(flashed) is override
    assert 'could not be read' in state['battery_warning']


def test_leqi_flash_does_not_read_brightway_battery(ui, monkeypatch):
    def fail():
        pytest.fail('LEQI must not use Brightway telemetry')
    monkeypatch.setattr(webui.telemetry, 'read_status', fail)
    monkeypatch.setattr(webui, 'RADIO_OFF_DELAY', 0)
    monkeypatch.setattr(leqiflash, 'flash', lambda *a, **kw: None)
    ui.jobs.update(image={'id': 'id', 'family': 'LEQI'}, info={'family': 'LEQI'})
    call(ui, '/api/flash', {'confirm': 'id'})
    assert settled(ui.jobs)['error'] is None


@pytest.mark.parametrize('path', ['/api/data', '/api/battery', '/api/light'])
def test_new_uart_jobs_share_lock_and_reject_leqi(ui, path):
    ui.jobs.claim('flash')
    assert call(ui, path, {'family': 'Brightway'})[0] == 409
    ui.jobs.release()
    assert call(ui, path, {'family': 'LEQI'})[0] == 400
    assert not ui.jobs.snapshot()['busy']


def test_data_full_then_live_drops_stale_live_fields(ui, monkeypatch):
    monkeypatch.setattr(webui.telemetry, 'read_info', lambda: {'esc_version': '0015', 'battery_serial': 'FAKE'})
    call(ui, '/api/data', {'family': 'Brightway', 'full': True})
    assert settled(ui.jobs)['data']['battery_serial'] == 'FAKE'
    monkeypatch.setattr(webui.telemetry, 'read_status', lambda: {})
    call(ui, '/api/data', {'family': 'Brightway'})
    assert settled(ui.jobs)['data'] == {'esc_version': '0015', 'battery_serial': 'FAKE'}


def test_light_api_uses_fixed_duration_and_no_control_arguments(ui, monkeypatch):
    calls = []
    monkeypatch.setattr(webui.telemetry, 'light_test', lambda seconds, stop: calls.append((seconds, stop())) or 'Done')
    call(ui, '/api/light', {'family': 'Brightway', 'seconds': 100, 'throttle': 100, 'brake': 100})
    assert settled(ui.jobs)['result'] == 'Done'
    assert calls == [(webui.telemetry.LIGHT_MAX_SECONDS, False)]


def test_light_off_stops_running_light(ui, monkeypatch):
    def light(seconds, stop):
        assert call(ui, '/api/light_off', {})[0] == 200
        return 'stopped' if stop() else 'still on'
    monkeypatch.setattr(webui.telemetry, 'light_test', light)
    call(ui, '/api/light', {'family': 'Brightway'})
    assert settled(ui.jobs)['result'] == 'stopped'


def test_review_reads_current_battery(ui, monkeypatch):
    monkeypatch.setattr(webui.telemetry, 'read_status', lambda: {'battery_percent': 19})
    call(ui, '/api/battery', {'family': 'Brightway'})
    assert settled(ui.jobs)['battery_warning'].startswith('Battery below 20%:')
