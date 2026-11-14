"""Probe cleanup must protect USB callers as well as UI wrappers."""
import importlib.util
from pathlib import Path
import sys
import types

import pytest
import bwflash


@pytest.mark.parametrize('failure', [None, 'classify', 'report'])
def test_shared_probe_restores_keep_pin(monkeypatch, failure):
    calls = []
    def pin(*args, **kwargs):
        calls.append((args, kwargs))
    pin.IN, pin.OUT = 0, 1
    monkeypatch.setitem(sys.modules, 'machine', types.SimpleNamespace(Pin=pin, UART=None))
    spec = importlib.util.spec_from_file_location('probe_under_test',
        Path(__file__).resolve().parents[1] / 'esp32/probe.py')
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    def classify(_):
        if failure == 'classify':
            raise OSError('classification failed')
        return 'hint'
    def report(*args):
        if failure == 'report':
            raise OSError('report failed')
    monkeypatch.setattr(probe, 'classify', classify)
    if failure:
        with pytest.raises(OSError):
            probe.run((bwflash.KEEP_PIN,), report=report)
    else:
        probe.run((bwflash.KEEP_PIN,), report=report)
    assert calls[-2:] == [((bwflash.KEEP_PIN, pin.IN), {}),
                          ((bwflash.KEEP_PIN, pin.OUT), {'value': 1})]


def test_probe_without_keep_pin(monkeypatch):
    calls = []
    def pin(*args, **kwargs):
        calls.append((args, kwargs))
    pin.IN, pin.OUT = 0, 1
    monkeypatch.setitem(sys.modules, 'machine', types.SimpleNamespace(Pin=pin, UART=None))
    monkeypatch.setattr(bwflash, 'KEEP_PIN', None)
    spec = importlib.util.spec_from_file_location('probe_without_keep',
        Path(__file__).resolve().parents[1] / 'esp32/probe.py')
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    monkeypatch.setattr(probe, 'classify', lambda pin: 'hint')
    probe.run((4,))
    assert calls == [((4, pin.IN), {})]
