# bw-esp32-flasher: flash Brightway ESC firmware from an ESP32 (MicroPython).
#
# Port of the DFU logic and key generation of ScooterTeam bw-flasher
# (https://github.com/scooterteam/bw-flasher, commit 8baeea1).
# Licensed under CC BY-NC-SA 4.0, same as the original.
#
# Usage on the ESP32 REPL:
#   import bwflash
#   bwflash.info()                 # UID + version, writes nothing
#   bwflash.flash('fw.bin')        # full DFU flash

import binascii
import time

try:
    import machine
    from machine import UART, Pin
except ImportError:  # running under CPython for tests
    machine = None

TX_PIN = 5      # to ESC RX
RX_PIN = 16     # from ESC TX (open collector, needs pull-up)
KEEP_PIN = 17   # line that must stay high or the ESC powers off; None to skip

PACKET_SIZE = 0x800
CHUNK_SIZE = 0x80
CHUNKS_PER_PACKET = PACKET_SIZE // CHUNK_SIZE
MAX_REPEATS = 20
MAX_STATE_TRIES = 200
READ_TIMEOUT_MS = 100   # pyserial timeout=0.1 in bw-flasher

# IO_MUX register per GPIO (ESP32 TRM); bit 8 = FUN_PU.
# Pull-up via Pin() after UART init would detach the UART input, so use the register.
IO_MUX = {0: 0x44, 2: 0x40, 4: 0x48, 5: 0x6C, 12: 0x34, 13: 0x38, 14: 0x30, 15: 0x3C,
          16: 0x4C, 17: 0x50, 18: 0x70, 19: 0x74, 21: 0x7C, 22: 0x80, 23: 0x8C,
          25: 0x24, 26: 0x28, 27: 0x2C, 32: 0x1C, 33: 0x20}


class FlashError(Exception):
    pass


# ---- CRC -------------------------------------------------------------------

def _crc16_table():
    t = []
    for i in range(256):
        c = i << 8
        for _ in range(8):
            c = ((c << 1) ^ 0x1021) if c & 0x8000 else (c << 1)
        t.append(c & 0xFFFF)
    return t


_CRC16 = _crc16_table()


def crc16_xmodem(data):
    crc = 0
    for b in data:
        crc = ((crc << 8) & 0xFFFF) ^ _CRC16[(crc >> 8) ^ b]
    return crc


def build_frame(seq, payload):
    body = bytes(payload) + b'\xFF' * (CHUNK_SIZE - len(payload))
    crc = crc16_xmodem(body)
    return bytes([0x01, seq, seq ^ 0xFF]) + body + bytes([crc >> 8, crc & 0xFF])


# ---- key generation (bw-flasher keygen.py) ---------------------------------

def _gen_key(dst, src, lt0, lt1):
    for i in range(16):
        dst[i] = src[i]
    local = bytearray(4)
    for j in range(16, 176, 4):
        for i in range(4):
            dst[j + i] = dst[j - 16 + i]
        if j % 16:
            for i in range(4):
                local[i] = dst[j - 4 + i]
        else:
            local[0] = lt0[dst[j - 3]] ^ lt1[j // 16]
            local[1] = lt0[dst[j - 2]]
            local[2] = lt0[dst[j - 1]]
            local[3] = lt0[dst[j - 4]]
        for i in range(4):
            dst[j + i] ^= local[i]


def _xor_block(dst, src, block):
    for j in range(block * 16, (block + 1) * 16):
        dst[j % 16] ^= src[j]


def _manipulate(d):
    for o in range(0, 16, 4):
        l0 = d[o] ^ d[o + 1]
        l1 = d[o + 1] ^ d[o + 2]
        l2 = d[o + 2] ^ d[o + 3]
        l3 = d[o + 3] ^ d[o]
        l4 = l0 ^ l2
        for i, li in enumerate((l0, l1, l2, l3)):
            v = d[o + i] ^ ((li << 1) & 0xFF)
            if li & 0x80:
                v ^= 0x1B
            d[o + i] = v ^ l4


def _roll(b, idx):
    first = b[idx[0]]
    for k in range(len(idx) - 1):
        b[idx[k]] = b[idx[k + 1]]
    b[idx[-1]] = first


def sign_rand(uid, rand, lt0, lt1):
    key = bytearray(176)
    _gen_key(key, uid, lt0, lt1)
    d = bytearray(rand)
    for blk in range(10):
        if blk:
            _manipulate(d)
        _xor_block(d, key, blk)
        for i in range(16):
            d[i] = lt0[d[i]]
        _roll(d, (1, 5, 9, 13))
        _roll(d, (2, 10))
        _roll(d, (3, 15, 11, 7))
        _roll(d, (6, 14))
    _xor_block(d, key, 10)
    return bytes(d)


# ---- firmware file (streamed: the ESP32 has less RAM than the image) --------

def _find_all(path, pat, start=0):
    hits = []
    with open(path, 'rb') as f:
        f.seek(start)
        pos = start
        tail = b''
        while True:
            blk = f.read(4096)
            if not blk:
                break
            buf = tail + blk
            base = pos - len(tail)
            i = buf.find(pat)
            while i != -1:
                if base + i not in hits:
                    hits.append(base + i)
                i = buf.find(pat, i + 1)
            pos += len(blk)
            tail = buf[-(len(pat) - 1):]
    return hits


class Firmware:
    def __init__(self, path):
        self.path = path
        with open(path, 'rb') as f:
            f.seek(0, 2)
            self.size = f.tell()
            f.seek(0x800)
            sig = f.read(8)
        o0 = _find_all(path, b'\x63\x7C')
        if self.size < 0x1000 or (sig != b'DEPRD5C\x00' and not (len(o0) == 1 and o0[0] > 0x1000)):
            raise FlashError("not a Brightway firmware file")
        if len(o0) != 1:
            raise FlashError("invalid / unsupported firmware file")
        o1 = _find_all(path, b'\x01\x02', o0[0])
        if len(o1) != 1:
            raise FlashError("invalid / unsupported firmware file")
        with open(path, 'rb') as f:
            f.seek(o0[0])
            self.lt0 = f.read(256)
            f.seek(o1[0] - 1)
            self.lt1 = f.read(11)
        self.total_packets = (self.size + PACKET_SIZE - 1) // PACKET_SIZE

    def packet(self, n):
        with open(self.path, 'rb') as f:
            f.seek(n * PACKET_SIZE)
            return f.read(PACKET_SIZE)


# ---- serial ----------------------------------------------------------------

def open_uart(tx=TX_PIN, rx=RX_PIN, keep=KEEP_PIN):
    if keep is not None:
        Pin(keep, Pin.OUT, value=1)
    u = UART(2, baudrate=19200, bits=8, parity=None, stop=1, tx=tx, rx=rx, rxbuf=4096)
    if rx in IO_MUX:
        a = 0x3FF49000 + IO_MUX[rx]
        machine.mem32[a] = machine.mem32[a] | 0x100
    return u


class Link:
    def __init__(self, uart, log=print, debug=False):
        self.u = uart
        self.log = log
        self.debug = debug

    def send(self, data):
        if self.debug:
            self.log('TX', binascii.hexlify(data, ' '))
        self.u.write(data)

    def read_until(self, n, end=None, timeout_ms=READ_TIMEOUT_MS):
        """Read until `end` or timeout and keep the last n bytes.

        end=None reads the whole window: bw-flasher passes '\\r' as str to pyserial,
        which never matches bytes, so its text replies always wait the full timeout.
        """
        buf = b''
        t0 = time.ticks_ms()
        while time.ticks_diff(time.ticks_ms(), t0) < timeout_ms:
            if self.u.any():
                buf += self.u.read(1)
                if end is not None and buf.endswith(end):
                    break
        buf = buf[-n:]
        if self.debug:
            self.log('RX', binascii.hexlify(buf, ' '))
        return buf


# ---- DFU state machine (bw-flasher brightway_flasher.py) ---------------------

class Flasher:
    BLE_RAND = bytes(range(1, 17))

    def __init__(self, link, fw=None, progress=None):
        self.k = link
        self.fw = fw
        self.progress = progress
        self.uid = None
        self.version = None
        self.mcu_rand = None
        self.n_sent = 0
        self.packet = None
        self.crc = 0

    def _get_uid(self):
        cmd = b'\x53\x2A\x7D\xAC'
        self.k.send(cmd)
        r = self.k.read_until(21, b'\x9B')
        if b'\x64' in r and b'\x9B' in r:
            r = r[r.index(b'\x64'):r.index(b'\x9B')]
            if len(r) >= 19 and r[1] == cmd[1] and r[2] == 0x10:
                self.uid = r[3:19]
                return True
        return False

    def _get_ver(self):
        self.k.send(b'down get_ver\r')
        r = self.k.read_until(5)
        if b'\r' in r[-2:]:
            self.version = r.split(b'\r')[0].decode()
            return True
        return False

    def _cmd_ok(self, cmd, n=3, ok=b'k\r'):
        self.k.send(cmd)
        r = self.k.read_until(n)
        if ok in r:
            return True
        if b'r\r' in r:
            raise FlashError('ESC answered error to ' + repr(cmd))
        return False

    def _retry(self, step, name):
        for _ in range(MAX_STATE_TRIES):
            if step():
                return
        raise FlashError('no answer at step ' + name + '. Check wiring.')

    def connect(self):
        self._retry(self._get_uid, 'UID')
        self.k.log('UID:', self.uid.decode())
        self._retry(self._get_ver, 'get_ver')
        self.k.log('MCU version:', self.version)

    def _rd_info(self):
        self.k.send(b'down rd_info\r\x00\x00\x00')
        return self.k.read_until(26).startswith(b'ok')

    def _ble_rand(self):
        self.k.send(b'down ble_rand ' + self.BLE_RAND + b'\r')
        r = self.k.read_until(20)
        if not r.startswith(b'ok'):
            return False
        if r[3:19] != sign_rand(self.uid, self.BLE_RAND, self.fw.lt0, self.fw.lt1):
            raise FlashError('BLE_KEY does not match: wrong firmware for this ESC?')
        return True

    def _mcu_rand(self):
        self.k.send(b'down mcu_rand\r')
        r = self.k.read_until(20)
        if r.startswith(b'ok'):
            self.mcu_rand = r[3:19]
            return True
        return False

    def _mcu_key(self):
        key = sign_rand(self.uid, self.mcu_rand, self.fw.lt0, self.fw.lt1)
        self.k.send(b'down mcu_key ' + key + b'\r')
        return self.k.read_until(3) == b'ok\r'

    def _nvm_write(self):
        self.packet = self.fw.packet(self.n_sent)
        loc = self.n_sent * PACKET_SIZE
        self.k.send(('down nvm_write %08X' % loc).encode() + b'\r')
        return b'k\r' in self.k.read_until(3)

    def _send_packet(self):
        if self.packet:
            self.packet += b'\xFF' * (PACKET_SIZE - len(self.packet))
            for n in range(CHUNKS_PER_PACKET):
                frame = build_frame(n + 1, self.packet[n * CHUNK_SIZE:(n + 1) * CHUNK_SIZE])
                for _ in range(MAX_REPEATS):
                    self.k.send(frame)
                    r = self.k.read_until(1, b'\x06')
                    if r == b'\x06':
                        break
                    if r == b'\x15':
                        raise FlashError('CRC fail')
                else:
                    raise FlashError('no ACK after %d retries' % MAX_REPEATS)
        self.k.send(b'\x04\x04\x04')
        self.k.read_until(3, b'\x06')
        self.n_sent += 1
        self.crc = binascii.crc32(self.packet, self.crc)

    def _wr_info(self):
        cmd = 'down wr_info %d %08x %d\r' % (self.n_sent, self.crc & 0xFFFFFFFF, self.n_sent * PACKET_SIZE)
        return self._cmd_ok(cmd.encode())

    def flash(self):
        self.connect()
        self._retry(self._rd_info, 'rd_info')
        self._retry(self._ble_rand, 'ble_rand')
        self._retry(self._mcu_rand, 'mcu_rand')
        self._retry(self._mcu_key, 'mcu_key')
        while True:
            self._retry(self._nvm_write, 'nvm_write')
            self._send_packet()
            if self.progress and self.packet:
                self.progress(min(self.n_sent, self.fw.total_packets), self.fw.total_packets)
            self._retry(self._wr_info, 'wr_info')
            if not self.packet:
                break
        self._retry(lambda: self._cmd_ok(b'down dfu_verify\r'), 'dfu_verify')
        self.k.log('verify OK')
        self._retry(lambda: self._cmd_ok(b'down dfu_active\r'), 'dfu_active')
        self.k.log('firmware update completed')
        self._retry(self._get_ver, 'get_ver')
        self.k.log('MCU version now:', self.version)


def _progress(done, total):
    print('packet %d/%d (%d%%)' % (done, total, done * 100 // total))


def info(debug=False):
    """Read UID and version only. Safe: writes nothing to the ESC."""
    f = Flasher(Link(open_uart(), debug=debug))
    f.connect()
    return f.uid, f.version


def flash(path, debug=False):
    fw = Firmware(path)
    print('firmware', path, fw.size, 'bytes,', fw.total_packets, 'packets')
    f = Flasher(Link(open_uart(), debug=debug), fw, _progress)
    t0 = time.time()
    f.flash()
    print('done in %d s' % (time.time() - t0))
