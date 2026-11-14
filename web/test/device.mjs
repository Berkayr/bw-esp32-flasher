import {test} from 'node:test';
import assert from 'node:assert/strict';
import {infoCode, dataCode, lightCode, batteryCode, probeCode, inspectCode, flashCode, resultFrom} from '../device.js';

test('every ESC operation disables AP before controller access', () => {
  for (const code of [infoCode('Brightway'), infoCode('LEQI'), dataCode(true), lightCode(), batteryCode(), probeCode([4,16]), flashCode({family:'Brightway',size:4096,crc:0}, false)]) {
    assert.ok(code.indexOf('active(False)') < code.indexOf('time.sleep_ms(300)'));
    assert.ok(code.includes('network.WLAN(network.WLAN.IF_AP)'));
  }
});
test('untrusted code parameters cannot enter Python', () => {
  for (const pins of [[], [4,4], [6], [true], [34], [1,3], [4,16,17,18,19,21,22]]) assert.throws(() => probeCode(pins));
  assert.throws(() => infoCode("Brightway';reset()"));
  assert.throws(() => flashCode({family:'Brightway',size:4096,crc:'0;reset()'}, false));
  assert.ok(flashCode({family:'Brightway',size:4096,crc:0}, 'true').includes('if warning and True:'));
  assert.ok(flashCode({family:'Brightway',size:4096,crc:0}, true).includes('if warning and False:'));
});
test('preflight precedes atomic promotion, probe always restores keep-alive', () => {
  assert.ok(inspectCode().indexOf('Firmware(') < inspectCode().indexOf('os.rename('));
  assert.match(probeCode([17]), /finally:\n if bwflash.KEEP_PIN/);
  assert.deepEqual(resultFrom('unstructured log\n## {"family":"Brightway"}\r\n'), {family:'Brightway'});
  assert.throws(() => resultFrom('no structured result'));
});
