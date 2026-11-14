// Fixed MicroPython programs. Only validated enums, booleans and integers enter code.
export const SAFE_PINS = [4, 5, 13, 14, 16, 17, 18, 19, 21, 22, 23, 25, 26, 27, 32, 33];
const prefix = `import network, time, json, bwflash, flasher, telemetry, binascii, os
network.WLAN(network.WLAN.IF_AP).active(False)
time.sleep_ms(300)
`;
const emit = expression => `print('## '+json.dumps(${expression}))`;
function moduleFor(family) {
  if (!['Brightway', 'LEQI'].includes(family)) throw Error('Unknown controller family');
  return family === 'Brightway' ? 'bwflash' : 'leqiflash';
}
export const wifiCode = () => `import network, json, webui
network.WLAN(network.WLAN.IF_AP).active(False)
` + emit('webui.wifi_credentials()');
export function infoCode(family) {
  const module = moduleFor(family);
  return prefix + `import ${module}\nr=${module}.info()\n` + emit(family === 'Brightway' ?
    "{'family':'Brightway','uid':binascii.hexlify(r[0]).decode(),'version':r[1]}" :
    "{'family':'LEQI','version':r[0],'region':r[1]}");
}
export function dataCode(full = false) {
  return prefix + 'd=telemetry.read_status()\n' + (full ? 'd.update(telemetry.read_info())\n' : '') + emit('d');
}
export const lightCode = () => prefix + `import select, sys
p=select.poll()
p.register(sys.stdin, select.POLLIN)
def stop():
 if p.poll(0):
  sys.stdin.read(1)
  return True
 return False
` + emit('telemetry.light_test(telemetry.LIGHT_MAX_SECONDS, stop)');
export const batteryCode = () => prefix + `try:
 d=telemetry.read_status()
except Exception:
 d={}
` + emit("{'warning':telemetry.battery_warning(d)}");

export function probeCode(pins) {
  if (!Array.isArray(pins) || !pins.length || pins.length > 6 || new Set(pins).size !== pins.length ||
    pins.some(p => !Number.isInteger(p) || !SAFE_PINS.includes(p))) throw Error('Choose 1–6 distinct allowed GPIOs');
  return prefix + `import probe
from machine import Pin
r=[]
try:
 probe.run((${pins.join(',')},), report=lambda pin,text:r.append('GPIO%d: %s' % (pin,text)))
finally:
 if bwflash.KEEP_PIN is not None: Pin(bwflash.KEEP_PIN,Pin.OUT,value=1)
` + emit('r');
}

const inspect = path => `family=flasher.detect_firmware_type('${path}')
if family == 'Brightway':
 fw=bwflash.Firmware('${path}')
elif family == 'LEQI':
 import leqiflash
 fw=leqiflash.Firmware('${path}')
else:
 raise ValueError('Select a complete raw Brightway or LEQI .bin')
`;
export const inspectCode = () => prefix + inspect('usb-upload.tmp') +
  "image={'family':family,'size':os.stat('usb-upload.tmp')[6],'model_id':getattr(fw,'model_id',None)}\nos.rename('usb-upload.tmp','fw.bin')\n" + emit('image');

export function flashCode(image, override) {
  moduleFor(image.family);
  if (!Number.isInteger(image.size) || image.size < 4096 || image.size > 524288 ||
      !Number.isInteger(image.crc) || image.crc < 0 || image.crc > 0xffffffff) throw Error('Upload and inspect the image again');
  return prefix + `c=0
f=open('fw.bin','rb')
try:
 while True:
  b=f.read(256)
  if not b: break
  c=binascii.crc32(b,c)
finally:
 f.close()
if os.stat('fw.bin')[6] != ${image.size} or c & 0xffffffff != ${image.crc}: raise ValueError('Image changed; upload again')
` + inspect('fw.bin') + `if family != '${image.family}': raise ValueError('Image family changed')
if family == 'Brightway':
 try:
  d=telemetry.read_status()
 except Exception:
  d={}
 warning=telemetry.battery_warning(d)
 if warning and ${override === true ? 'False' : 'True'}: raise ValueError(warning+'. Review flash and check the override box.')
def progress(done,total):
 print('@@ '+json.dumps({'done':done,'total':total}))
flasher.flash('fw.bin',progress=progress)
` + emit("'Firmware update completed (Brightway: verify OK; LEQI: no separate verify command).'");
}

export function resultFrom(output) {
  const lines = output.split(/\r?\n/).filter(line => line.startsWith('## '));
  if (!lines.length) throw Error('No structured reply from board');
  return JSON.parse(lines.at(-1).slice(3));
}
