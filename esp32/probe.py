# Wire probe: find out what each dashboard-cable wire is, without trusting colour tables.
#
#   import probe
#   probe.run((4, 16, 17, 18))   # GPIOs the unknown wires are connected to
#
# Verify ground with a multimeter before connecting anything. Measure signal
# voltages first: never connect a supply or a signal above 3.3 V to a GPIO.
# Verdicts are hints, not proof of wire identity or electrical safety.

import time
from machine import Pin, UART

import bwflash


def _level(pin, pull, n=200):
    p = Pin(pin, Pin.IN, pull)
    time.sleep_ms(5)
    return sum(p.value() for _ in range(n)) / n


def _edges(pin, ms=300):
    p = Pin(pin, Pin.IN, Pin.PULL_UP)
    last, edges = p.value(), 0
    t0 = time.ticks_ms()
    while time.ticks_diff(time.ticks_ms(), t0) < ms:
        v = p.value()
        if v != last:
            edges += 1
            last = v
    return edges


def _frames(pin, ms=600):
    """Count Brightway status frames (61 .. 9E) on a pin at 19200 8N1."""
    tx = 5 if pin != 5 else 18
    u = UART(2, baudrate=19200, tx=tx, rx=pin, rxbuf=2048)
    if pin in bwflash.IO_MUX:
        import machine
        a = 0x3FF49000 + bwflash.IO_MUX[pin]
        machine.mem32[a] = machine.mem32[a] | 0x100
    try:
        u.read()
        time.sleep_ms(ms)
        data = u.read() or b''
    finally:
        u.deinit()
        Pin(tx, Pin.IN)
    return data.count(b'\x61\x30') + data.count(b'\x61\x31'), data[:16]


def classify(pin):
    free = _level(pin, None)
    up = _level(pin, Pin.PULL_UP)
    down = _level(pin, Pin.PULL_DOWN)
    edges = _edges(pin)
    if edges > 4:
        n, sample = _frames(pin)
        if n:
            return 'ESC TX (data from ESC, %d status frames) -> ESP32 RX' % n
        return 'activity but no Brightway frames (%d edges), sample %s' % (edges, sample)
    if up < 0.1:
        return 'held low even with pull-up -> GND (or driven low)'
    if down > 0.9:
        return 'held high even with pull-down -> ESC RX (has its own pull-up) -> ESP32 TX; if not, a supply wire'
    return 'floating (follows pull, free=%.2f) -> ESC input: RX or keep-alive line' % free


def run(pins=(4, 16, 17, 18), report=None):
    try:
        for p in pins:
            try:
                verdict = classify(p)
                if report is None:
                    print('GPIO%d: %s' % (p, verdict))
                else:
                    report(p, verdict)
            finally:
                Pin(p, Pin.IN)
    finally:
        if bwflash.KEEP_PIN is not None:
            Pin(bwflash.KEEP_PIN, Pin.OUT, value=1)
