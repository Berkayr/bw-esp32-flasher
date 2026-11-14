# Offline, bounded HTTP UI for classic ESP32 / MicroPython 1.29.
# CC BY-NC-SA 4.0; see LICENSE.
import asyncio
import binascii
import gc
import json
import os
import _thread

import bwflash
import flasher

AP_PASSWORD = 'bw-flasher-setup'
IP = '192.168.4.1'
MAX_UPLOAD = 512 * 1024
CHUNK = 1024
TEMP_PATH = 'webui-upload.tmp'
IMAGE_PATH = 'webui-fw.bin'
PAGE_PATH = 'webui.html'
SAFE_PINS = (4, 5, 13, 14, 16, 17, 18, 19, 21, 22, 23, 25, 26, 27, 32, 33)
ROUTES = {'/': 'GET', '/api/status': 'GET', '/api/upload': 'POST',
          '/api/info': 'POST', '/api/flash': 'POST', '/api/probe': 'POST'}


class HTTPError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def remove_temp():
    try:
        os.remove(TEMP_PATH)
    except OSError as exc:
        if exc.args[0] != 2:
            raise


async def parse_request(reader):
    # Read no further than the terminator; never use unbounded readline().
    raw = bytearray()
    line_size = 0
    while not raw.endswith(b'\r\n\r\n'):
        value = await reader.read(1)
        if not value:
            raise HTTPError(400, 'Incomplete headers')
        raw.extend(value)
        line_size = 0 if value == b'\n' else line_size + 1
        if len(raw) > 2048 or line_size > 512:
            raise HTTPError(431, 'Headers too large')
    try:
        lines = bytes(raw).decode('ascii').split('\r\n')
        method, path, version = lines[0].split(' ')
        if version not in ('HTTP/1.0', 'HTTP/1.1'):
            raise ValueError()
        headers = {}
        if len(lines) > 27:
            raise ValueError()
        for line in lines[1:-2]:
            key, value = line.split(':', 1)
            if not key or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-' for c in key):
                raise ValueError()
            key = key.lower()
            if key in headers or any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise ValueError()
            headers[key] = value.strip()
        length = headers.get('content-length', '0')
        if not length or len(length) > 9 or not all('0' <= c <= '9' for c in length):
            raise ValueError()
        length = int(length)
    except (ValueError, UnicodeError):
        raise HTTPError(400, 'Malformed HTTP request')
    if path not in ROUTES:
        raise HTTPError(404, 'Unknown path')
    if method != ROUTES[path]:
        raise HTTPError(405, 'Unexpected method')
    if 'transfer-encoding' in headers or 'expect' in headers:
        raise HTTPError(400, 'Chunked transfer and Expect are unsupported')
    if headers.get('host') not in (IP, IP + ':80'):
        raise HTTPError(403, 'Unexpected Host')
    if method == 'GET' and length:
        raise HTTPError(400, 'GET body is unsupported')
    if method == 'POST' and 'content-length' not in headers:
        raise HTTPError(411, 'Content-Length is required')
    limit = MAX_UPLOAD if path == '/api/upload' else 512
    if length > limit:
        raise HTTPError(413, 'Body too large (firmware limit: 512 KiB)')
    return path, headers, length


async def read_body(reader, length):
    data = bytearray()
    while len(data) < length:
        part = await asyncio.wait_for(reader.read(min(CHUNK, length - len(data))), 15)
        if not part:
            raise HTTPError(400, 'Upload/body interrupted; send it again')
        data.extend(part)
    return data


async def stream_upload(reader, length):
    if not 4096 <= length <= MAX_UPLOAD:
        raise HTTPError(413, 'Select a complete raw .bin (4 KiB to 512 KiB)')
    fs = os.statvfs('.')
    if fs[0] * fs[4] < length + 8192:
        raise HTTPError(507, 'Not enough free filesystem space; remove unused files via USB')
    remove_temp()
    try:
        with open(TEMP_PATH, 'wb') as target:
            left = length
            while left:
                part = await asyncio.wait_for(reader.read(min(CHUNK, left)), 15)
                if not part:
                    raise HTTPError(400, 'Upload interrupted; send the .bin again')
                if target.write(part) != len(part):
                    raise OSError('Short filesystem write')
                left -= len(part)
    except BaseException:
        remove_temp()
        raise


class Jobs:
    def __init__(self):
        self.gate = _thread.allocate_lock()
        self.mutex = _thread.allocate_lock()
        self.state = dict(busy=False, job=None, done=0, total=0, image=None,
                          info=None, error=None, result=None)

    def update(self, **values):
        with self.mutex:
            self.state.update(values)

    def snapshot(self):
        with self.mutex:
            return self.state.copy()

    def claim(self, name):
        if not self.gate.acquire(0):
            raise HTTPError(409, 'Busy: wait for the current upload or ESC operation')
        self.update(busy=True, job=name, done=0, total=0, error=None, result=None)

    def release(self):
        self.update(busy=False)
        self.gate.release()

    def progress(self, done, total):
        self.update(done=done, total=total)

    def start_owned(self, function, args=()):
        try:
            _thread.start_new_thread(self._work, (function, args))
        except Exception as exc:
            self.update(error=str(exc), result='Job could not start')
            self.release()
            raise

    def _work(self, function, args):
        try:
            self.update(result=function(*args))
        except Exception as exc:
            self.update(error=str(exc), result='Failed; read the error before retrying')
        finally:
            gc.collect()
            self.release()

    def validate(self):
        try:
            family = flasher.detect_firmware_type(TEMP_PATH)
            module = self.module(family)
            fw = module.Firmware(TEMP_PATH)
            image = dict(family=family, size=os.stat(TEMP_PATH)[6],
                         model_id=getattr(fw, 'model_id', None),
                         id=binascii.hexlify(os.urandom(8)).decode())
            os.rename(TEMP_PATH, IMAGE_PATH)
            self.update(image=image)
            return 'Image preflight passed. Check the model before flashing.'
        finally:
            remove_temp()

    @staticmethod
    def module(family):
        if family == 'Brightway':
            return bwflash
        if family == 'LEQI':
            import leqiflash
            return leqiflash
        raise HTTPError(400, 'Unknown firmware family; select a raw Brightway or LEQI .bin')

    def info(self, family):
        values = self.module(family).info()
        result = dict(family=family, uid=None, version=None)
        if family == 'Brightway':
            result.update(uid=binascii.hexlify(values[0]).decode(), version=values[1])
        else:
            result.update(version=values[0], region=values[1])
        self.update(info=result)
        return 'Connection checked (read-only).'

    def flash(self, family):
        self.module(family).flash(IMAGE_PATH, progress=self.progress)
        return ('Brightway: verify OK; firmware update completed.' if family == 'Brightway'
                else 'LEQI firmware update completed (no separate verify command).')

    def probe(self, pins):
        import probe
        from machine import Pin
        verdicts = []
        try:
            probe.run(pins, report=lambda pin, text: verdicts.append('GPIO%d: %s' % (pin, text)))
            return verdicts
        finally:
            if bwflash.KEEP_PIN is not None:
                Pin(bwflash.KEEP_PIN, Pin.OUT, value=1)


class WebUI:
    def __init__(self):
        self.jobs = Jobs()
        self.token = binascii.hexlify(os.urandom(16)).decode()
        self.clients = 0

    async def dispatch(self, reader):
        path, headers, length = await asyncio.wait_for(parse_request(reader), 10)
        if path == '/':
            return 200, None
        if path == '/api/status':
            data = self.jobs.snapshot()
            data['token'] = self.token
            data['pins'] = [bwflash.TX_PIN, bwflash.RX_PIN, bwflash.KEEP_PIN]
            return 200, data
        if headers.get('origin', 'http://' + IP) not in ('http://' + IP, 'http://' + IP + ':80'):
            raise HTTPError(403, 'Unexpected Origin')
        if headers.get('x-flasher-token') != self.token:
            raise HTTPError(403, 'Reload the page before starting an operation')
        if path == '/api/upload':
            if headers.get('content-type') != 'application/octet-stream':
                raise HTTPError(415, 'Send the raw .bin as application/octet-stream')
            return await self.upload(reader, length)
        if headers.get('content-type') != 'application/json':
            raise HTTPError(415, 'Expected application/json')
        try:
            data = json.loads(bytes(await read_body(reader, length)))
            if not isinstance(data, dict):
                raise ValueError()
        except (ValueError, UnicodeError):
            raise HTTPError(400, 'Expected a JSON object')
        return self.operation(path, data)

    async def upload(self, reader, length):
        self.jobs.claim('upload/preflight')
        owned = True
        try:
            self.jobs.update(image=None)
            await asyncio.wait_for(stream_upload(reader, length), 180)
            owned = False
            self.jobs.start_owned(self.jobs.validate)
            return 202, {'accepted': True}
        except Exception as exc:
            self.jobs.update(error=str(exc) or 'Upload timed out; send it again')
            raise
        finally:
            if owned:
                self.jobs.release()

    def operation(self, path, data):
        self.jobs.claim(path.rsplit('/', 1)[1])
        owned = True
        try:
            state = self.jobs.snapshot()
            if path == '/api/info':
                family = data.get('family')
                self.jobs.module(family)
                self.jobs.update(info=None)
                function, args = self.jobs.info, (family,)
            elif path == '/api/flash':
                image, info = state['image'], state['info']
                if not image or data.get('confirm') != image['id']:
                    raise HTTPError(409, 'Upload, inspect and confirm the current image first')
                if not info or info['family'] != image['family']:
                    raise HTTPError(409, 'Check the connection for the image family first')
                function, args = self.jobs.flash, (image['family'],)
            else:
                pins = data.get('pins')
                if (not isinstance(pins, list) or not 1 <= len(pins) <= 6 or
                        any(type(p) is not int or p not in SAFE_PINS for p in pins) or
                        len(set(pins)) != len(pins)):
                    raise HTTPError(400, 'Choose 1 to 6 distinct safe GPIOs: ' + str(SAFE_PINS))
                self.jobs.update(info=None)
                function, args = self.jobs.probe, (tuple(pins),)
            owned = False
            self.jobs.start_owned(function, args)
            return 202, {'accepted': True}
        finally:
            if owned:
                self.jobs.release()

    async def respond(self, writer, status, data):
        page = status == 200 and data is None
        body = b'' if page else json.dumps(data).encode()
        size = os.stat(PAGE_PATH)[6] if page else len(body)
        kind = 'text/html; charset=utf-8' if page else 'application/json'
        writer.write(('HTTP/1.1 %d Response\r\nContent-Type: %s\r\nContent-Length: %d\r\n'
                      'Connection: close\r\nCache-Control: no-store\r\n'
                      'X-Content-Type-Options: nosniff\r\n'
                      'Content-Security-Policy: default-src \'self\'; script-src \'unsafe-inline\'; '
                      'style-src \'unsafe-inline\'; connect-src \'self\'; frame-ancestors \'none\'\r\n\r\n'
                      % (status, kind, size)).encode())
        await asyncio.wait_for(writer.drain(), 10)
        if page:
            with open(PAGE_PATH, 'rb') as source:
                while True:
                    part = source.read(CHUNK)
                    if not part:
                        break
                    writer.write(part)
                    await asyncio.wait_for(writer.drain(), 10)
        else:
            writer.write(body)
            await asyncio.wait_for(writer.drain(), 10)

    async def handle(self, reader, writer):
        if self.clients >= 3:
            writer.close()
            return
        self.clients += 1
        try:
            try:
                status, data = await self.dispatch(reader)
            except HTTPError as exc:
                status, data = exc.status, {'error': exc.message}
            except asyncio.TimeoutError:
                status, data = 408, {'error': 'Request timed out; reconnect and check status'}
            except Exception as exc:
                status, data = 500, {'error': str(exc)}
            await self.respond(writer, status, data)
        except OSError:
            # A socket failure must never cancel the independent ESC worker.
            return
        except asyncio.TimeoutError:
            return
        finally:
            self.clients -= 1
            writer.close()
            try:
                await writer.wait_closed()
            except OSError as exc:
                # Peer may already have disappeared from the AP.
                print('HTTP close:', exc)


def start_ap():
    import network
    network.WLAN(network.WLAN.IF_STA).active(False)
    ap = network.WLAN(network.WLAN.IF_AP)
    ap.active(True)
    suffix = binascii.hexlify(ap.config('mac')[-2:]).decode().upper()
    # authmode/password are the ESP32 aliases retained in MicroPython 1.29.
    ap.config(essid='bw-flasher-' + suffix, authmode=3, password=AP_PASSWORD, max_clients=2)
    ap.ifconfig((IP, '255.255.255.0', IP, IP))
    return ap


def disabled():
    from machine import Pin
    try:
        os.stat('webui.disabled')
        return True
    except OSError as exc:
        if exc.args[0] != 2:
            raise
    return Pin(0, Pin.IN, Pin.PULL_UP).value() == 0


async def serve(ui):
    server = await asyncio.start_server(ui.handle, '0.0.0.0', 80, backlog=3)
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        server.close()
        await server.wait_closed()


def run():
    if disabled():
        print('Wi-Fi UI disabled; plain USB REPL mode')
        return
    remove_temp()
    ap = start_ap()
    print('Connect to', ap.config('essid'), 'and open http://' + IP)
    gc.collect()
    asyncio.run(serve(WebUI()))
