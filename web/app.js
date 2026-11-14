import {RawRepl, parseProgress, crc32} from './repl.js';
import {infoCode, dataCode, lightCode, batteryCode, probeCode, inspectCode, flashCode, resultFrom} from './device.js';
const el = id => document.getElementById(id);
const supported = window.isSecureContext && 'serial' in navigator &&
  !/Android|iPhone|iPad|iPod/i.test(navigator.userAgent) &&
  !(navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
let repl = null, busy = false, lightOn = false, info = null, image = null, data = {}, reviewing = false, installerReady = false;
const family = () => el('family').value;
const ready = () => !!repl?.raw && !repl.fault && !busy;
function clearReview() {
  reviewing = false;
  el('confirmBox').hidden = true;
  el('agree').checked = el('batteryOverride').checked = false;
}
function controls() {
  const idle = ready();
  for (const id of ['check', 'upload', 'probe', 'family', 'file', 'pins', 'probeSafe']) el(id).disabled = !idle;
  for (const id of ['light', 'refresh', 'autoRefresh']) el(id).disabled = !idle || family() !== 'Brightway';
  el('lightOff').disabled = !lightOn;
  el('connect').disabled = !supported || busy || !!repl;
  el('disconnect').disabled = busy || !repl;
  el('install').disabled = !supported || busy || !!repl || !installerReady;
  el('installer').hidden = !!repl || busy || !supported;
  el('review').disabled = !idle || !image || info?.family !== image.family;
  el('flash').disabled = !idle || !reviewing || !el('agree').checked ||
    (!el('overrideLabel').hidden && !el('batteryOverride').checked);
}
function renderData() {
  const value = (key, unit = '') => data[key] == null ? 'Unavailable' : data[key] + unit;
  el('batteryGauge').textContent = value('battery_percent', ' %');
  el('batteryMeter').hidden = data.battery_percent == null;
  el('batteryMeter').value = data.battery_percent ?? 0;
  el('liveData').textContent = 'Voltage: ' + value('voltage', ' V') + '\nBattery temperature: ' + value('battery_temperature', ' °C') +
    '\nESC temperature: ' + value('esc_temperature', ' °C') + '\nEvent: ' + (data.event === 1 ? 'Normal' : data.event === 16 ? 'No dashboard / host timeout' : value('event'));
  el('deviceData').textContent = 'ESC firmware: ' + value('esc_version') + ' (' + value('esc_date') + ')\nBMS firmware: ' +
    value('bms_version') + ' (' + value('bms_date') + ')\nBattery manufacture date: ' + value('battery_date') + '\nCharge cycles: ' + value('cycles');
  el('serials').hidden = !el('showSerials').checked;
  el('serials').textContent = el('showSerials').checked ? 'Battery: ' + value('battery_serial') + '\nESC: ' + value('esc_serial') +
    '\nScooter: ' + value('scooter_serial') + '\nUID: ' + (info?.uid ?? 'Unavailable') : '';
}
async function job(label, action) {
  if (busy) return;
  busy = true;
  el('error').textContent = '';
  el('status').textContent = label + ' · keep power connected';
  controls();
  try { await action(); }
  catch (e) { el('error').textContent = e.message; }
  finally {
    busy = false;
    el('status').textContent = repl?.fault ? 'USB connection lost or timed out. Keep power on; read the error before reconnecting.' :
      repl ? 'USB connected · Wi-Fi off · ready' : 'USB disconnected';
    controls();
  }
}
const execute = async (code, options) => resultFrom(await repl.exec(code, options));
function progress(done, total, text) {
  el('progress').max = total;
  el('progress').value = done;
  el('packets').textContent = `${text} ${done}/${total}` + (done === total ? ' · finalizing, keep power on' : '');
}
el('connect').onclick = () => job('Connecting to ESP32', async () => {
  const port = await navigator.serial.requestPort();
  const connection = new RawRepl(port);
  await connection.open();
  repl = connection;
  info = image = null; data = {}; clearReview(); renderData();
  el('device').textContent = 'No controller read yet.';
  el('image').textContent = 'No validated image.';
  // No ESC access until the radio is off, including after every reconnect.
  await repl.exec('import network\nnetwork.WLAN(network.WLAN.IF_AP).active(False)');
});
el('disconnect').onclick = () => job('Disconnecting USB', async () => {
  await repl.close(); repl = null; info = image = null; data = {}; clearReview(); renderData();
});
el('check').onclick = () => job('Checking controller', async () => {
  info = null; clearReview();
  el('device').textContent = 'Reading controller…';
  info = await execute(infoCode(family()));
  el('device').textContent = `${info.family} · Version: ${info.version}` + (info.region ? ' · Region: ' + info.region : '');
  renderData();
});
el('light').onclick = () => job('Tail light on (max 60 s)', async () => {
  clearReview(); lightOn = true; controls();
  try { el('result').textContent = await execute(lightCode(), {timeout: 70000}); }
  finally { lightOn = false; }
});
el('lightOff').onclick = () => repl?.poke();
async function readData(full) {
  const saved = Object.fromEntries(Object.entries(data).filter(([key]) =>
    !['battery_percent', 'voltage', 'battery_temperature', 'esc_temperature', 'event'].includes(key)));
  data = full ? {} : saved; renderData();
  data = {...(full ? {} : saved), ...await execute(dataCode(full))}; renderData();
}
el('refresh').onclick = () => job('Reading data', () => readData(true));
el('showSerials').onchange = renderData;
el('family').onchange = () => {
  info = null; data = {}; clearReview(); el('showSerials').checked = false;
  el('device').textContent = 'Check connection for the selected family.'; renderData(); controls();
};
el('file').onchange = () => { image = null; clearReview(); el('image').textContent = 'Upload and inspect the selected file.'; controls(); };
el('upload').onclick = () => job('Uploading firmware', async () => {
  image = null; clearReview(); el('image').textContent = 'No validated image.';
  const file = el('file').files[0];
  if (!file || !file.name.toLowerCase().endsWith('.bin') || file.size < 4096 || file.size > 524288)
    throw Error('Choose a raw .bin between 4 KiB and 512 KiB');
  const bytes = new Uint8Array(await file.arrayBuffer());
  await repl.writeFile(bytes, (done, total) => progress(done, total, 'Uploaded bytes'));
  try { image = {...await execute(inspectCode()), crc: crc32(bytes)}; }
  catch (e) {
    if (repl.raw && !repl.fault) await repl.exec("import os\ntry:\n os.remove('usb-upload.tmp')\nexcept OSError:\n pass");
    throw e;
  }
  el('image').textContent = `${image.family} · ${image.size} bytes · Model ID: ${image.model_id ?? 'not available'}. An ID does not prove compatibility.`;
  el('result').textContent = 'Upload CRC verified and family preflight passed. Check the model before flashing.';
});
el('review').onclick = () => job('Reading battery for review', async () => {
  clearReview();
  if (!image || image.family !== info?.family) throw Error('Upload firmware and check the matching controller family first');
  const warning = image.family === 'Brightway' ? (await execute(batteryCode())).warning : null;
  el('batteryWarning').textContent = warning || (image.family === 'Brightway' ? 'Battery check passed; it will be read again immediately before flash.' : 'Battery check is not supported for LEQI.');
  el('overrideLabel').hidden = !warning;
  el('reviewText').textContent = `Flash ${image.family}, ${image.size} bytes, model ${image.model_id ?? 'unknown'} to the connected controller?`;
  reviewing = true; el('confirmBox').hidden = false;
});
for (const id of ['agree', 'batteryOverride']) el(id).onchange = controls;
el('cancel').onclick = () => { clearReview(); controls(); };
el('flash').onclick = () => {
  if (!ready() || !reviewing || !image || info?.family !== image.family || !el('agree').checked ||
      (!el('overrideLabel').hidden && !el('batteryOverride').checked)) return;
  const code = flashCode(image, el('batteryOverride').checked);
  job('Flashing controller — do not interrupt', async () => {
    clearReview(); el('result').textContent = ''; progress(0, 1, 'Packet');
    el('result').textContent = await execute(code, {timeout: 600000, onLine: line => {
      const update = parseProgress(line);
      if (update) progress(update.done, update.total, 'Packet');
    }});
  });
};
el('probe').onclick = () => job('Probing measured wires', async () => {
  info = null; clearReview(); el('device').textContent = 'Restore wiring and check connection after probing.';
  if (!el('probeSafe').checked) throw Error('Measure the wires and confirm none exceeds 3.3 V first');
  const parts = el('pins').value.split(',').map(v => v.trim());
  if (parts.some(v => !/^\d+$/.test(v))) throw Error('Enter comma-separated GPIO numbers');
  el('probeResult').textContent = (await execute(probeCode(parts.map(Number)), {timeout: 30000})).join('\n');
});
setInterval(() => {
  if (ready() && !reviewing && el('autoRefresh').checked && family() === 'Brightway') job('Reading live data', () => readData(false));
}, 2000);
window.addEventListener('beforeunload', event => { if (busy) { event.preventDefault(); event.returnValue = ''; } });
if (supported) navigator.serial.addEventListener('disconnect', event => {
  if (repl && (event.target === repl.port || repl.fault || !repl.port.readable)) {
    repl.fault ||= Error('USB disconnected. Keep power connected: a running flash may continue.');
    repl.raw = false;
    clearReview(); info = image = null; data = {}; renderData();
    el('status').textContent = 'USB disconnected. Keep power on: a running flash may continue. Wait before reconnecting.';
    controls();
  }
});
el('unsupported').hidden = supported;
controls();
// Missing artifacts must not lead beginners into an installer that cannot complete.
fetch('firmware/bw-esp32-flasher.bin', {method: 'HEAD'}).then(response => {
  installerReady = response.ok;
  el('installHelp').textContent = response.ok ? 'Installer image is available. Skip installation if this board already has the flasher.' :
    'Installer image is not published yet. A maintainer must run tools/build_image.py and include web/firmware in the Pages deployment. Existing boards can still connect below.';
  controls();
}).catch(() => { el('installHelp').textContent = 'Cannot check installer image availability. Reload when the page is online.'; });
