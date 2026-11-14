# Runs at boot: hold the keep-alive wire high so the ESC stays on (see README).
from machine import Pin
from bwflash import KEEP_PIN

if KEEP_PIN is not None:
    Pin(KEEP_PIN, Pin.OUT, value=1)

if __name__ == '__main__':
    import webui
    webui.run()
