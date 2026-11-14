# MicroPython port of ScooterTeam bw-flasher leqi_flasher.py (8baeea1).
# Copyright (C) 2024-2025 ScooterTeam. CC BY-NC-SA 4.0; see LICENSE.
import struct
import time

from bwflash import FlashError, crc16_xmodem, open_uart


def header_size(header, file_size):
    if len(header) < 16 or header[10:14] != b'EU1\x00':
        return None
    size = struct.unpack('<H', header[14:16])[0]
    return size if size >= 0x42 and file_size >= 0x80 + size else None


def crc16_firmware(data, crc=0xFFFF):
    # Reflected form of upstream's bit-reversed polynomial 0x8005.
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return crc


class Firmware:
    def __init__(self, path):
        self.path = path
        try:
            with open(path, 'rb') as f:
                f.seek(0, 2)
                self.file_size = f.tell()
                f.seek(0)
                self.size = header_size(f.read(16), self.file_size)
                if self.size is None:
                    raise FlashError('preflight: invalid LEQI EU1 header or body size; select the complete raw .bin')
                # Detection requires a valid header, making upstream's legacy fallback unreachable.
                f.seek(0x80 + 0x40)
                left = self.size - 0x42
                crc = 0xFFFF
                while left:
                    data = f.read(min(1024, left))
                    if not data:
                        raise FlashError('preflight: truncated LEQI body; recopy firmware')
                    crc = crc16_firmware(data, crc)
                    left -= len(data)
                stored = f.read(2)
                if len(stored) != 2:
                    raise FlashError('preflight: missing LEQI CRC; recopy firmware')
                embedded = struct.unpack('>H', stored)[0]
                start = min(0x80, self.size)
                end = min(0x400, self.size)
                if end <= start:
                    start, end = 0, self.size
                f.seek(0x80 + start)
                sample = f.read(end - start)
                self.plaintext_body = crc != embedded and sample.count(b'\xaa') / len(sample) < 0.02
                if crc != embedded and not self.plaintext_body:
                    raise FlashError('preflight: embedded LEQI CRC mismatch; select an intact image for this ESC')
        except OSError as exc:
            raise FlashError('preflight: cannot read LEQI image; check file path and filesystem: ' + str(exc))

    def chunk(self, offset):
        expected = min(128, self.size - offset)
        try:
            with open(self.path, 'rb') as f:
                f.seek(0, 2)
                if f.tell() != self.file_size:
                    raise FlashError('transfer chunk %d: image size changed; recopy firmware' % (offset // 128 + 1))
                f.seek(0x80 + offset)
                data = f.read(expected)
        except OSError as exc:
            raise FlashError('transfer chunk %d: cannot read image; check filesystem: %s' % (offset // 128 + 1, exc))
        if len(data) != expected:
            raise FlashError('transfer chunk %d: truncated image; recopy firmware' % (offset // 128 + 1))
        # XOR bodies are already encoded: upstream sends both variants unchanged.
        return data + b'\xff' * (128 - len(data))


def packet(command, payload=b''):
    data = b'\x5a\x12' + bytes([command, len(payload)]) + payload
    return data + struct.pack('>H', crc16_xmodem(data))


class Link:
    def __init__(self, uart, log=print, debug=False):
        self.u = uart
        self.log = log
        self.debug = debug

    def exchange(self, data, expected_len=7, timeout_ms=2000):
        # Match reset_input_buffer before each upstream transaction.
        pending = self.u.any()
        while pending:
            discarded = self.u.read(min(pending, 128))
            if not discarded:
                break
            pending -= len(discarded)
        self.u.write(data)
        self.u.flush()
        if self.debug:
            self.log('TX', data)
        time.sleep(0.05)
        response = bytearray()
        start = time.ticks_ms()
        while time.ticks_diff(time.ticks_ms(), start) < timeout_ms:
            if self.u.any():
                value = self.u.read(1)
                if value == b'\x5a':
                    response.extend(value)
                    break
            time.sleep(0.01)
        if not response:
            return None
        while time.ticks_diff(time.ticks_ms(), start) < timeout_ms:
            if self.u.any():
                value = self.u.read(1)
                if value:
                    response.extend(value)
                if len(response) >= expected_len:
                    break
            elif len(response) >= 5:
                time.sleep(0.02)
                if not self.u.any():
                    break
            time.sleep(0.01)
        if self.debug:
            self.log('RX', bytes(response))
        return bytes(response)


def valid_reply(response, command):
    # shortcut: upstream ignores response CRC/length/status for start/end/info;
    # strengthen only after hardware/protocol review, to preserve compatibility.
    return bool(response and len(response) >= 5 and response[1] == 0x21 and response[2] == command)


class Flasher:
    def __init__(self, link, fw=None, progress=None, packet_progress=None):
        self.k = link
        self.fw = fw
        self.progress = progress
        self.packet_progress = packet_progress
        self.stage = 'connect'

    def info(self):
        self.stage = 'info/DeviceInfo'
        response = self.k.exchange(packet(2), expected_len=17)
        if not response or len(response) < 15 or not valid_reply(response, 2):
            raise FlashError(self.stage + ': no valid reply; check wiring and ESC power')
        version = ''.join(str(value) for value in response[4:8])
        region = ''.join(chr(value) for value in response[12:15] if value < 128)
        self.k.log('MCU version:', version, 'Region:', region)
        return version, region

    def flash(self):
        self.stage = 'start/update'
        payload = b'\x31\x00' + struct.pack('<H', self.fw.size) + b'\x00\x00'
        if not valid_reply(self.k.exchange(packet(3, payload)), 3):
            raise FlashError(self.stage + ': no valid reply; check wiring, power and image compatibility')
        failed = 0
        total = (self.fw.size + 127) // 128
        for offset in range(0, self.fw.size, 128):
            number = offset // 128 + 1
            self.stage = 'transfer chunk %d' % number
            data = struct.pack('<I', offset) + self.fw.chunk(offset)
            response = self.k.exchange(packet(4, data))
            if not valid_reply(response, 4) or response[4] != 1:
                failed += 1
                self.k.log(self.stage + ': missing/invalid reply or rejected; check signal quality and image compatibility')
            if self.progress:
                self.progress(int(number / total * 90))
            if self.packet_progress:
                self.packet_progress(number, total)
            time.sleep(0.044)
        if failed:
            raise FlashError('transfer: %d chunks had invalid/missing responses; check wiring, power and image compatibility before retrying' % failed)
        time.sleep(0.69)
        self.stage = 'finalize/end'
        for attempt in range(10):
            if attempt:
                time.sleep(0.06)
            if valid_reply(self.k.exchange(packet(5), timeout_ms=400), 5):
                break
        else:
            raise FlashError(self.stage + ': no valid reply after 10 attempts; check ESC power and wiring')
        self.k.log('LEQI firmware update completed')
        if self.progress:
            self.progress(100)


def _run(fw, debug, progress=None, log=print):
    stage = 'connect/UART'
    uart = None
    flasher = None
    try:
        uart = open_uart()
        flasher = Flasher(Link(uart, log=log, debug=debug), fw,
                          lambda value: log('progress %d%%' % value), progress)
        if fw is None:
            return flasher.info()
        return flasher.flash()
    except OSError as exc:
        if flasher is not None:
            stage = flasher.stage
        raise FlashError(stage + ': UART/filesystem error; check wiring, power and filesystem: ' + str(exc))
    finally:
        if uart is not None:
            uart.deinit()


def info(debug=False, log=print):
    """Read LEQI DeviceInfo without starting an update; returns (version, region)."""
    return _run(None, debug, log=log)


def flash(path, debug=False, progress=None, log=print):
    return _run(Firmware(path), debug, progress, log)
