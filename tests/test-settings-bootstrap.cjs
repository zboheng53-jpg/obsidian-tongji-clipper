const assert = require('node:assert/strict');
const {loadAndInitializeSettings} = require('../extension/common-client.js');

(async () => {
  let stored = {};
  let response = {error: 'backend unavailable'};
  global.chrome = {
    storage: {local: {
      get: (_, callback) => callback({...stored}),
      set: (values, callback) => {Object.assign(stored, values); callback();},
    }},
    runtime: {sendMessage: (_, callback) => callback(response)},
  };
  const unavailable = await loadAndInitializeSettings();
  assert.equal(unavailable.isValid, false);
  assert.equal(stored.settings_initialized, undefined);
  response = {status: 'ok', defaults: {model: 'D:/fixture/model', offline: true}};
  const recovered = await loadAndInitializeSettings();
  assert.equal(recovered.actualModel, 'D:/fixture/model');
  assert.equal(recovered.isOffline, true);
  assert.equal(stored.settings_initialized, true);
  stored.isOffline = false;
  response = {error: 'backend unavailable'};
  assert.equal((await loadAndInitializeSettings()).isOffline, false);
  console.log('Settings bootstrap retries after unavailable backend and preserves explicit settings.');
})().catch(error => {console.error(error); process.exitCode = 1;});
