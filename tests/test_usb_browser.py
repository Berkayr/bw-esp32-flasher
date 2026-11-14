"""Execute the browser's actual generated Python against isolated fake device APIs."""
import binascii
import json
from pathlib import Path
import shutil
import subprocess
import sys
import types

import pytest
import telemetry

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def programs():
    if not shutil.which('node'):
        pytest.skip('Node is needed to generate browser device programs')
    source = """
      import * as d from './web/device.js';
      const image={family:'Brightway',size:4096,crc:3340501009};
      console.log(JSON.stringify({normal:d.flashCode(image,false),override:d.flashCode(image,true),
        leqi:d.flashCode({...image,family:'LEQI'},false),inspect:d.inspectCode(),
        wifi:d.wifiCode(),probe:d.probeCode([17]),info:d.infoCode('Brightway'),data:d.dataCode(true),light:d.lightCode(),battery:d.batteryCode()}));
    """
    result = json.loads(subprocess.check_output(['node', '--input-type=module', '-e', source], cwd=ROOT))
    for code in result.values():
        compile(code, '<usb-browser>', 'exec')
    return result


@pytest.fixture
def device(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    data = b'x' * 4096
    Path('fw.bin').write_bytes(data)
    events = []
    def wlan(mode):
        return types.SimpleNamespace(active=lambda enabled: events.append(('radio', enabled)))
    wlan.IF_AP = 1
    monkeypatch.setitem(sys.modules, 'network', types.SimpleNamespace(WLAN=wlan))
    monkeypatch.setitem(sys.modules, 'time', types.SimpleNamespace(sleep_ms=lambda ms: None))
    bw = types.SimpleNamespace(Firmware=lambda path: types.SimpleNamespace(model_id='FAKE'), KEEP_PIN=17)
    monkeypatch.setitem(sys.modules, 'bwflash', bw)
    monkeypatch.setitem(sys.modules, 'leqiflash', bw)
    state = dict(family='Brightway', battery={'battery_percent': 80}, fail_read=False)
    def read():
        events.append('battery')
        if state['fail_read']:
            raise OSError('UART failed')
        return state['battery']
    monkeypatch.setitem(sys.modules, 'telemetry', types.SimpleNamespace(
        read_status=read, battery_warning=telemetry.battery_warning))
    def flash(path, progress):
        events.append('flash')
        progress(1, 1)
    monkeypatch.setitem(sys.modules, 'flasher', types.SimpleNamespace(
        detect_firmware_type=lambda path: state['family'], flash=flash))
    return state, events, binascii.crc32(data) & 0xffffffff


@pytest.mark.parametrize('battery,override,allowed', [
    ({'battery_percent': 19}, False, False), ({'battery_percent': 20}, False, True),
    ({'battery_percent': 80}, False, True), ({}, False, False), ({}, True, True),
    ({'battery_percent': 1}, True, True), ({'battery_percent': True}, False, False),
    ({'battery_percent': 101}, False, False),
])
def test_fresh_battery_guard_on_device(programs, device, battery, override, allowed):
    state, events, crc = device
    state['battery'] = battery
    code = programs['override' if override else 'normal'].replace('3340501009', str(crc))
    if allowed:
        exec(code, {})
    else:
        with pytest.raises(ValueError, match='brick the controller'):
            exec(code, {})
    assert ('flash' in events) is allowed
    assert events[0] == ('radio', False)
    assert events.index('battery') > 0


@pytest.mark.parametrize('override', [True, False])
def test_unreadable_battery_requires_override(programs, device, override):
    state, events, crc = device
    state['fail_read'] = True
    code = programs['override' if override else 'normal'].replace('3340501009', str(crc))
    if override:
        exec(code, {})
    else:
        with pytest.raises(ValueError, match='could not be read'):
            exec(code, {})
    assert ('flash' in events) is override


def test_changed_image_crc_or_family_prevents_flash(programs, device):
    state, events, crc = device
    with pytest.raises(ValueError, match='Image changed'):
        exec(programs['normal'].replace('3340501009', str(crc ^ 1)), {})
    state['family'] = 'LEQI'
    with pytest.raises(ValueError, match='family changed'):
        exec(programs['normal'].replace('3340501009', str(crc)), {})
    assert 'flash' not in events


def test_leqi_does_not_use_brightway_battery(programs, device):
    state, events, crc = device
    state['family'] = 'LEQI'
    exec(programs['leqi'].replace('3340501009', str(crc)), {})
    assert 'battery' not in events
    assert 'flash' in events


def test_bad_preflight_keeps_previous_firmware(programs, device):
    state, events, _ = device
    Path('usb-upload.tmp').write_bytes(b'invalid')
    state['family'] = 'Unknown'
    with pytest.raises(ValueError, match='complete raw'):
        exec(programs['inspect'], {})
    assert Path('fw.bin').read_bytes() == b'x' * 4096


def test_probe_restores_keepalive_on_error(programs, device, monkeypatch):
    _, events, _ = device
    def fail(*args, **kwargs):
        raise OSError('probe failed')
    def pin(*args, **kwargs):
        events.append(('keep', args, kwargs))
    pin.OUT = 1
    monkeypatch.setitem(sys.modules, 'probe', types.SimpleNamespace(run=fail))
    monkeypatch.setitem(sys.modules, 'machine', types.SimpleNamespace(Pin=pin))
    with pytest.raises(OSError, match='probe failed'):
        exec(programs['probe'], {})
    assert events[-1] == ('keep', (17, 1), {'value': 1})


def test_usb_reads_wifi_credentials_without_starting_ap(programs, device, monkeypatch, capsys):
    import webui
    credentials = {'ssid': 'bw-flasher-TEST', 'password': 'ABCDEFGHJKLMNPQR'}
    monkeypatch.setattr(webui, 'wifi_credentials', lambda: credentials)
    exec(programs['wifi'], {})
    assert json.loads(capsys.readouterr().out.strip()[3:]) == credentials
    assert device[1] == [('radio', False)]
