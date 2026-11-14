import {test} from 'node:test';
import assert from 'node:assert/strict';
import {RawRepl, crc32, parseProgress} from '../repl.js';

class FakePort {
  constructor(execute = () => ({out: 'hello\r\n'})) {
    this.execute = execute; this.commands = []; this.writes = []; this.command = '';
    this.readable = new ReadableStream({start: controller => { this.rx = controller; }});
    this.writable = new WritableStream({write: bytes => {
      const value = new TextDecoder().decode(bytes); this.writes.push(value);
      if (value === '\r\x01') return this.reply('raw REPL; CTRL-B to exit\r\n>');
      if (value === '\x04') {
        this.commands.push(this.command);
        const response = this.execute(this.command); this.command = '';
        if (response === null) return;
        this.reply('OK' + response.out + '\x04' + (response.error ?? '') + '\x04>');
      } else if (!value.includes('\x03') && value !== '\r\x02') this.command += value;
    }});
  }
  async open(options) { this.options = options; }
  async close() { this.closed = true; }
  reply(text) {
    // Split every marker, including OK, CRLF and both EOFs, across reads.
    for (const char of text) this.rx.enqueue(new TextEncoder().encode(char));
  }
}
async function connect(execute) { const port = new FakePort(execute); const repl = new RawRepl(port); await repl.open(); return {port, repl}; }

test('raw framing, paced command chunks, streamed lines, and Ctrl-B on close', async () => {
  const {port, repl} = await connect(() => ({out: 'one\r\n@@ {"done":1,"total":2}\nlast'}));
  const lines = [];
  assert.equal(await repl.exec('x'.repeat(600), {onLine: line => lines.push(line)}), 'one\r\n@@ {"done":1,"total":2}\nlast');
  assert.deepEqual(lines, ['one', '@@ {"done":1,"total":2}', 'last']);
  assert.deepEqual(port.options, {baudRate: 115200});
  assert.deepEqual(port.writes.slice(0, 3), ['\r\x03', '\x03', '\r\x01']);
  assert.deepEqual(port.writes.slice(3, 6).map(v => v.length), [256, 256, 88]);
  assert.equal(port.commands[0].length, 600);
  await repl.close(); assert.equal(port.writes.at(-1), '\r\x02'); assert.ok(port.closed);
});

test('stderr is surfaced after complete framing; next command remains synchronized', async () => {
  let count = 0;
  const {repl} = await connect(() => ++count === 1 ? {out: '', error: 'ValueError: wrong image\r\n'} : {out: 'ok\n'});
  await assert.rejects(repl.exec('bad'), /wrong image/);
  assert.equal(await repl.exec('good'), 'ok\n'); await repl.close();
});

test('timeout poisons session, does not interrupt a possibly running flash, and releases locks', async () => {
  const {port, repl} = await connect(() => null);
  await assert.rejects(repl.exec('flash()', {timeout: 25}), /timed out/);
  await assert.rejects(repl.exec('again'), /timed out/);
  await repl.close(); assert.notEqual(port.writes.at(-1), '\r\x02'); assert.ok(port.closed);
});

test('concurrent commands are rejected', async () => {
  const {repl} = await connect(() => null);
  const pending = repl.exec('flash()', {timeout: 30});
  await assert.rejects(repl.exec('interrupt()'), /busy/);
  await assert.rejects(repl.close(), /current operation/);
  await assert.rejects(pending, /timed out/); await repl.close();
});

test('chunked upload uses base64, fixed temporary path, final size and CRC check', async () => {
  const bytes = Uint8Array.from({length: 4107}, (_, n) => n % 251);
  const {port, repl} = await connect(() => ({out: ''}));
  const progress = [];
  await repl.writeFile(bytes, (done, total) => progress.push([done, total]));
  const chunks = port.commands.filter(c => c.includes('a2b_base64(')).map(c =>
    Buffer.from(c.match(/a2b_base64\('([^']+)'\)/)[1], 'base64'));
  assert.deepEqual(Buffer.concat(chunks), Buffer.from(bytes));
  assert.ok(chunks.every(c => c.length <= 256));
  assert.match(port.commands[0], /statvfs/);
  assert.match(port.commands.at(-1), new RegExp('!= ' + bytes.length));
  assert.match(port.commands.at(-1), new RegExp('!= ' + crc32(bytes)));
  assert.match(port.commands.at(-1), /f\.close\(\)/);
  assert.deepEqual(progress.at(-1), [4107, 4107]);
  assert.ok(!port.commands.join('').includes("open('fw.bin','wb')"));
  await repl.close();
});

test('upload validation and CRC failure clean up temporary files and retain previous firmware', async () => {
  const {port, repl} = await connect(code => code.includes('c=0') ? {out: '', error: 'ValueError: Upload size/CRC mismatch'} : {out: ''});
  await assert.rejects(repl.writeFile(new Uint8Array(1)), /4 KiB/);
  await assert.rejects(repl.writeFile(new Uint8Array(4096)), /CRC mismatch/);
  assert.match(port.commands.at(-1), /os.remove\('usb-upload.tmp'\)/);
  assert.ok(!port.commands.some(c => c.includes("os.remove('fw.bin')")));
  await repl.close();
});

test('progress parser accepts only bounded integer counters', () => {
  assert.deepEqual(parseProgress('@@ {"done":1,"total":2}'), {done: 1, total: 2});
  for (const line of ['noise', '@@ bad', '@@ null', '@@ {"done":3,"total":2}', '@@ {"done":true,"total":2}', '@@ {"done":0,"total":0}'])
    assert.equal(parseProgress(line), null);
  assert.equal(crc32(new TextEncoder().encode('123456789')), 0xcbf43926);
});

test('progress streams before the final EOF; USB loss is surfaced without an interrupt', async () => {
  const {port, repl} = await connect(() => null);
  let firstLine;
  const streamed = new Promise(resolve => { firstLine = resolve; });
  const running = repl.exec('flash()', {onLine: firstLine, timeout: 1000});
  while (!port.commands.length) await new Promise(resolve => setTimeout(resolve, 1));
  port.reply('OK@@ {"done":1,"total":2}\n');
  assert.equal(await streamed, '@@ {"done":1,"total":2}');
  port.rx.close();
  await assert.rejects(running, /disconnected/);
  assert.equal(port.writes.filter(s => s.includes('\x03')).length, 2);
  await repl.close();
});
