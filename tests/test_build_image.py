"""Offline builder contract tests; no serial devices or network access."""
import hashlib
import importlib.util
import json
from pathlib import Path
import struct

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('build_image', ROOT / 'tools/build_image.py')
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


def entry(label='vfs', offset=0x200000, size=0x200000, kind=1):
    return struct.pack('<HBBII16sI', 0x50aa, kind, 0, offset, size, label.encode(), 0)


def image(entries, md5=True):
    table = b''.join(entries)
    if md5:
        table += b'\xeb\xeb' + b'\xff' * 14 + hashlib.md5(table).digest()
    table = table.ljust(0x1000, b'\xff')
    return b'\xff' * (build.PARTITION_TABLE - build.IMAGE_BASE) + table


def test_explicit_vfs_and_dynamic_v129_vfs():
    assert build.vfs_partition(image([entry()])) == (0x200000, 0x200000)
    assert build.vfs_partition(image([entry('factory', 0x10000, 0x1f0000, 0)])) == (0x200000, 0x200000)
    assert build.vfs_partition(image([entry()], md5=False)) == (0x200000, 0x200000)


@pytest.mark.parametrize('entries', [[], [entry('ffat')], [entry(size=1)],
    [entry(offset=0x400000)], [entry(offset=0x1000)], [entry(kind=0)],
    [entry(), entry()], [entry('factory', 0x10000, 0x200000, 0), entry()]])
def test_invalid_partition_tables_fail_closed(entries):
    with pytest.raises(ValueError):
        build.vfs_partition(image(entries))


def test_partition_md5_and_download_sha256():
    raw = bytearray(image([entry()]))
    raw[0x7000 + 32 + 16] ^= 1
    with pytest.raises(ValueError, match='MD5'):
        build.vfs_partition(raw)
    with pytest.raises(ValueError, match='SHA-256'):
        build.verify_download(b'not the pinned release')


def test_manifest_has_single_image_at_merged_base_zero():
    saved = json.loads((ROOT / 'web/manifest.json').read_text())
    assert saved == build.manifest()
    assert saved['builds'] == [{'chipFamily': 'ESP32', 'parts': [
        {'path': 'firmware/bw-esp32-flasher.bin', 'offset': 0}]}]
    assert saved['new_install_prompt_erase'] is True


def test_notice_rejects_unpinned_text_and_preserves_attribution(monkeypatch):
    with pytest.raises(ValueError, match='licence SHA-256'):
        build.license_notice(b'wrong licence')
    text = b'Permission is hereby granted: test fixture'
    monkeypatch.setattr(build, 'MICROPYTHON_LICENSE_SHA256', hashlib.sha256(text).hexdigest())
    notice = build.license_notice(text)
    for value in ('Copyright (C) 2024-2025 ScooterTeam', 'Berkayr, 2026',
                  'CC BY-NC-SA 4.0', 'ESP-IDF', 'MicroPython 1.29.0 (MIT)', text.decode()):
        assert value in notice


def test_filesystem_notice_readback():
    pytest.importorskip('littlefs')
    data = build.filesystem_image(2 * 1024 * 1024, 'test notice')
    from littlefs import LittleFS, UserContext
    context = UserContext(len(data))
    context.buffer[:] = data
    fs = LittleFS(context=context, mount=False, block_size=4096, block_count=512,
                  read_size=32, prog_size=32, cache_size=128, lookahead_size=32)
    fs.mount()
    with fs.open('NOTICE.txt', 'r') as source:
        assert source.read() == 'test notice'
    assert 'webui.py' in fs.listdir('/') and 'main.py' in fs.listdir('/')
    assert 'wifi.txt' not in fs.listdir('/')
    fs.unmount()
