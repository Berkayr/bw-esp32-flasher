# Runs at boot: hold the keep-alive wire high so the ESC stays on (see README).
from machine import Pin

Pin(17, Pin.OUT, value=1)
