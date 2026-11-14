# Brightway runtime telemetry; verified on Xiaomi 5 / ESC 0015 only.
# CC BY-NC-SA 4.0; see LICENSE.
import time
import bwflash

LOW_BATTERY = 20
MAX_PAYLOAD = 64
READ_CHUNK = 64
INFO_SUBS = (0x21, 0x23, 0x24, 0x50, 0x27, 0x28, 0x29)
LIGHT_FRAME = b'\x51\x10\x06\x32\x01\x88\x00\x00\x1e\x40\xae'


def valid_frame(frame):
    return (len(frame) >= 5 and frame[0] in (0x61, 0x64) and
            frame[2] <= MAX_PAYLOAD and len(frame) == frame[2] + 5 and
            frame[-1] == 0xff - frame[0] and
            frame[-2] == sum(frame[:-2]) & 0xff)


class FrameSplitter:
    def __init__(self):
        self.buffer = b''

    def feed(self, data):
        # Linear scan: the ESP32 also serves the web UI, so per-byte rescans starved this loop.
        buf = self.buffer + bytes(data)
        i = 0
        while len(buf) - i >= 5:
            if buf[i] not in (0x61, 0x64) or buf[i + 2] > MAX_PAYLOAD:
                i += 1
                continue
            end = i + buf[i + 2] + 5
            if end > len(buf):
                break
            frame = buf[i:end]
            if valid_frame(frame):
                i = end
                yield frame
            else:
                i += 1  # a dropped byte can corrupt LEN; resync one byte later
        self.buffer = buf[i:][-(MAX_PAYLOAD + 5):]


def decode_status(frame):
    if not valid_frame(frame) or frame[0] != 0x61:
        return {}
    if frame[1] == 0x30 and frame[2] in (8, 10):
        return dict(esc_temperature=frame[6], event=frame[8])
    if frame[1] == 0x31 and frame[2] == 9 and frame[4] <= 100:
        return dict(battery_percent=frame[4],
                    voltage=(frame[5] * 256 + frame[6]) / 1000,
                    battery_temperature=frame[9])
    return {}


def _date(data):
    year, month, day = 2000 + data[0], data[1], data[2]
    if not 1 <= month <= 12:
        return None
    days = (31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
            31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    if not 1 <= day <= days[month - 1]:
        return None
    return '%04d-%02d-%02d' % (year, month, day)


def decode_info(frame):
    if not valid_frame(frame) or frame[0] != 0x64:
        return {}
    sub, payload = frame[1], frame[3:-2]
    if sub in (0x21, 0x23, 0x24) and len(payload) >= 7:
        if not all(32 <= b < 127 for b in payload[:4]):
            return {}
        version = payload[:4].decode('ascii')
        date = _date(payload[4:7])
        if sub == 0x24:
            return dict(battery_date=date)
        prefix = 'esc' if sub == 0x21 else 'bms'
        return {prefix + '_version': version, prefix + '_date': date}
    if sub == 0x50 and len(payload) == 10:
        # Bytes 0-4 matched the app's energy/Ah once, then read 0 with the dashboard unplugged: not shown.
        return dict(cycles=payload[7])
    if sub in (0x27, 0x28, 0x29):
        payload = payload.rstrip(b'\x00')
        if not payload or not all(32 <= b < 127 for b in payload):
            return {}
        key = {0x27: 'battery_serial', 0x28: 'esc_serial', 0x29: 'scooter_serial'}[sub]
        return {key: payload.decode('ascii')}
    return {}


def _frames(uart, splitter, ms):
    start = time.ticks_ms()
    while time.ticks_diff(time.ticks_ms(), start) < ms:
        if uart.any():
            for frame in splitter.feed(uart.read(READ_CHUNK) or b''):
                yield frame
        else:
            time.sleep(.005)


def read_status(ms=300):
    if type(ms) is not int or not 1 <= ms <= 2000:
        raise ValueError('Status window must be 1 to 2000 ms')
    uart = bwflash.open_uart()
    try:
        result = {}
        for frame in _frames(uart, FrameSplitter(), ms):
            result.update(decode_status(frame))
        return result
    finally:
        uart.deinit()


def _read_reply(uart, sub):
    seen = []
    for _ in range(3):
        uart.write(bytes((0x53, sub, (sub + 0x53) & 0xff, 0xac)))
        # A new splitter prevents partial replies crossing request boundaries.
        for frame in _frames(uart, FrameSplitter(), 250):
            if frame[0] == 0x64 and frame[1] == sub and decode_info(frame):
                if frame in seen:
                    return decode_info(frame)
                seen.append(frame)
                break
    return {}


def read_info():
    uart = bwflash.open_uart()
    try:
        result = {}
        unavailable = []
        for sub in INFO_SUBS:
            values = _read_reply(uart, sub)
            if values:
                result.update(values)
            else:
                unavailable.append('0x%02X' % sub)
        result['unavailable'] = unavailable
        return result
    finally:
        uart.deinit()


def battery_warning(values):
    percent = values.get('battery_percent')
    if type(percent) is not int or not 0 <= percent <= 100:
        return 'Battery could not be read: flashing requires an explicit override (a power loss during flashing can brick the controller)'
    if percent < LOW_BATTERY:
        return ('Battery below %d%%: flashing is not recommended '
                '(a power loss during flashing can brick the controller)' % LOW_BATTERY)
    return None


LIGHT_MAX_SECONDS = 60


def light_test(seconds=LIGHT_MAX_SECONDS, stop=None):
    # The ESC lights the tail light only while control frames keep arriving; stopping them turns it off.
    if type(seconds) not in (int, float) or not 0 < seconds <= LIGHT_MAX_SECONDS:
        raise ValueError('Light test must last more than 0 and at most %d seconds' % LIGHT_MAX_SECONDS)
    uart = bwflash.open_uart()
    try:
        start = time.ticks_ms()
        while time.ticks_diff(time.ticks_ms(), start) < seconds * 1000 and not (stop and stop()):
            if uart.write(LIGHT_FRAME) != len(LIGHT_FRAME):
                raise bwflash.FlashError('Light test: short UART write')
            time.sleep(.020)
        return 'Light off. It stays on only while the ESP32 sends control frames.'
    finally:
        uart.deinit()
