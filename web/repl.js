// MicroPython standard raw REPL (no soft reset and no raw-paste negotiation).
const encode = value => new TextEncoder().encode(value);
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

export function parseProgress(line) {
  if (!line.startsWith('@@ ')) return null;
  try {
    const v = JSON.parse(line.slice(3));
    return Number.isInteger(v.done) && Number.isInteger(v.total) &&
      v.done >= 0 && v.total > 0 && v.done <= v.total ? v : null;
  } catch { return null; }
}

export function crc32(bytes) {
  let crc = 0xffffffff;
  for (const value of bytes) {
    crc ^= value;
    for (let i = 0; i < 8; i++) crc = (crc >>> 1) ^ ((crc & 1) ? 0xedb88320 : 0);
  }
  return (crc ^ 0xffffffff) >>> 0;
}

export class RawRepl {
  constructor(port) {
    this.port = port;
    this.buffer = '';
    this.busy = false;
    this.raw = false;
    this.fault = null;
  }

  async open() {
    await this.port.open({baudRate: 115200});
    this.reader = this.port.readable.getReader();
    this.writer = this.port.writable.getWriter();
    this.pump = this._pump();
    try {
      await this._send('\r\x03');
      await pause(100);
      await this._send('\x03');
      await pause(100);
      this.buffer = '';
      await this._send('\r\x01');
      await this._until('raw REPL; CTRL-B to exit\r\n>', Date.now() + 5000);
      this.raw = true;
    } catch (e) {
      await this.close();
      throw e;
    }
  }

  async _pump() {
    const decoder = new TextDecoder();
    try {
      while (true) {
        const {value, done} = await this.reader.read();
        if (done) break;
        this.buffer += decoder.decode(value, {stream: true});
        if (this.buffer.length > 131072) throw Error('Serial receive buffer overflow');
        this.wake?.();
      }
      this.fault ||= Error('Serial disconnected. A running flash may still continue; keep power on.');
    } catch (e) { this.fault = e; }
    finally { this.wake?.(); }
  }

  async _send(value) { await this.writer.write(encode(value)); }

  async _until(marker, deadline, consume) {
    let collected = '';
    while (true) {
      const index = this.buffer.indexOf(marker);
      if (index !== -1) {
        const value = this.buffer.slice(0, index);
        this.buffer = this.buffer.slice(index + marker.length);
        if (consume) consume(value); else collected += value;
        return collected;
      }
      if (this.fault) throw this.fault;
      if (consume && this.buffer.length >= marker.length) {
        const length = this.buffer.length - marker.length + 1;
        consume(this.buffer.slice(0, length));
        this.buffer = this.buffer.slice(length);
      }
      const remaining = deadline - Date.now();
      if (remaining <= 0) {
        this.raw = false;
        this.fault = Error('USB command timed out. Keep power connected: a flash may still be running. Disconnect this page and wait before reconnecting.');
        throw this.fault;
      }
      await new Promise(resolve => {
        const timer = setTimeout(resolve, remaining);
        this.wake = () => { clearTimeout(timer); resolve(); };
      });
      this.wake = null;
    }
  }

  async _exclusive(action) {
    if (this.busy) throw Error('Board busy');
    if (!this.raw || this.fault) throw this.fault || Error('Connect the board first');
    this.busy = true;
    try { return await action(); }
    finally { this.busy = false; }
  }

  exec(code, options = {}) { return this._exclusive(() => this._exec(code, options)); }

  async _exec(code, {timeout = 10000, onLine = () => {}} = {}) {
    const deadline = Date.now() + timeout;
    const bytes = encode(code);
    // Match mpremote's pacing; ESP32's normal raw REPL has a small input buffer.
    for (let i = 0; i < bytes.length; i += 256) {
      await this.writer.write(bytes.slice(i, i + 256));
      await pause(10);
    }
    await this._send('\x04');
    const ack = await this._until('OK', deadline);
    if (ack) { this.raw = false; throw Error('Unexpected raw REPL acknowledgement'); }
    let output = '', line = '';
    await this._until('\x04', deadline, chunk => {
      output = (output + chunk).slice(-65536);
      line += chunk;
      let end;
      while ((end = line.indexOf('\n')) !== -1) {
        onLine(line.slice(0, end).replace(/\r$/, ''));
        line = line.slice(end + 1);
      }
      if (line.length > 65536) throw Error('Serial output line too long');
    });
    if (line) onLine(line);
    const error = await this._until('\x04', deadline);
    const prompt = await this._until('>', deadline);
    if (prompt) { this.raw = false; throw Error('Unexpected raw REPL prompt'); }
    if (error) throw Error(error.trim());
    return output;
  }

  writeFile(bytes, onProgress = () => {}) {
    return this._exclusive(async () => {
      if (!(bytes instanceof Uint8Array) || bytes.length < 4096 || bytes.length > 524288)
        throw Error('Choose a raw .bin between 4 KiB and 512 KiB');
      try {
        await this._exec(`import os, binascii\ns=os.statvfs('/')\nif s[0]*s[3] < ${bytes.length + 4096}: raise OSError('Not enough free filesystem space')\nf=open('usb-upload.tmp','wb')`);
        for (let offset = 0; offset < bytes.length; offset += 256) {
          const chunk = bytes.slice(offset, offset + 256);
          const base64 = btoa(String.fromCharCode(...chunk));
          await this._exec(`if f.write(binascii.a2b_base64('${base64}')) != ${chunk.length}: raise OSError('Short file write')`);
          onProgress(Math.min(offset + 256, bytes.length), bytes.length);
        }
        await this._exec(`f.close()\nc=0\nf=open('usb-upload.tmp','rb')\nwhile True:\n b=f.read(256)\n if not b: break\n c=binascii.crc32(b,c)\nf.close()\nif os.stat('usb-upload.tmp')[6] != ${bytes.length} or c & 0xffffffff != ${crc32(bytes)}: raise ValueError('Upload size/CRC mismatch')`);
      } catch (e) {
        if (this.raw && !this.fault) {
          await this._exec("try:\n f.close()\nexcept Exception:\n pass\ntry:\n os.remove('usb-upload.tmp')\nexcept OSError:\n pass").catch(() => {});
        }
        throw e;
      }
    });
  }

  // Wakes code that polls stdin (the light loop). A newline is harmless if it reaches an idle raw REPL.
  async poke() {
    if (!this.busy || !this.raw || this.fault) return;
    await this._send('\n');
  }

  async close() {
    if (this.busy) throw Error('Wait for the current operation before disconnecting');
    if (this.raw && !this.fault) await this._send('\r\x02');
    this.raw = false;
    await this.reader?.cancel();
    await this.pump;
    this.reader?.releaseLock();
    this.writer?.releaseLock();
    await this.port.close();
  }
}
