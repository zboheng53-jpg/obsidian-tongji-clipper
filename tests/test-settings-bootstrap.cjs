const assert = require('node:assert/strict');
const {loadAndInitializeSettings, resolveSettings} = require('../extension/common-client.js');

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
  assert.equal(resolveSettings({modelSource:'preset',model:'large-v3-turbo',hotwords:'  Canvas、斜率  '}).actualModel, 'large-v3-turbo');
  assert.equal(resolveSettings({hotwords:'  Canvas、斜率  '}).hotwords, 'Canvas、斜率');
  console.log('Settings bootstrap retries after unavailable backend and preserves explicit settings.');
})().catch(error => {console.error(error); process.exitCode = 1;});
