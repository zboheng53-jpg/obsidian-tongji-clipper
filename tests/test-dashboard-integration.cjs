// Runs the real dashboard against an isolated real worker with mock ASR only.
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const readline = require('node:readline');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  const root = path.resolve(__dirname, '..');
  const python = process.env.CLIPPER_PYTHON || path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
  const child = spawn(python, ['-u', path.join(__dirname, 'fixtures/ui_worker.py')], {cwd: root, env: {...process.env, PYTHONPATH: path.join(root, 'src')}});
  let stderr = '';
  child.stderr.on('data', data => {stderr += data;});
  let browser;
  const exited = new Promise(resolve => child.on('exit', resolve));
  try {
    const fixture = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(`Fixture timed out: ${stderr}`)), 20000);
      child.on('error', reject);
      child.on('exit', code => {clearTimeout(timer); reject(new Error(`Fixture exited ${code}: ${stderr}`));});
      readline.createInterface({input: child.stdout}).on('line', line => {
        try {const data = JSON.parse(line); if (data.url) {clearTimeout(timer); resolve(data);}} catch {}
      });
    });
    const routes = {system_info: '/system_info', list_jobs: '/jobs', preview: '/preview', export: '/export', cleanup_cache: '/cleanup_cache', validate_config: '/validate_config', pause: '/pause', resume: '/resume', retry: '/retry', cancel: '/cancel'};
    const calls = [];
    browser = await chromium.launch({headless: true, ...(process.env.BROWSER_EXECUTABLE ? {executablePath: process.env.BROWSER_EXECUTABLE} : {})});
    const page = await browser.newPage({viewport: {width: 1280, height: 900}});
    page.setDefaultTimeout(10000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.exposeFunction('__fixtureNative', async payload => {
      calls.push(payload);
      if (payload.action === 'ping') return {status: 'ok', port: 0};
      if (!routes[payload.action]) return {error: 'Unsupported fixture action'};
      const method = ['system_info', 'list_jobs'].includes(payload.action) ? 'GET' : 'POST';
      const response = await fetch(fixture.url + routes[payload.action], {method, headers: {'Authorization': 'Bearer synthetic-ui-token', 'Content-Type': 'application/json'}, ...(method === 'POST' ? {body: JSON.stringify(payload)} : {})});
      return response.json();
    });
    await page.addInitScript(() => {
      const storage = {};
      window.chrome = {
        runtime: {id: 'fixture', getURL: value => new URL(value, location.href).href, sendMessage: (message, callback) => {window.__fixtureNative(message.payload).then(callback).catch(error => callback({error: error.message}));}},
        storage: {local: {get: (keys, callback) => {const value = Object.fromEntries(keys.filter(key => key in storage).map(key => [key, storage[key]])); if (callback) callback(value); return Promise.resolve(value);}, set: (values, callback) => {Object.assign(storage, values); if (callback) callback(); return Promise.resolve();}}, onChanged: {addListener() {}}},
        tabs: {create: () => Promise.resolve({})}
      };
    });
    await page.goto(pathToFileURL(path.join(root, 'extension/dashboard.html')).href);
    await page.getByText('合成接口验收课程', {exact: true}).waitFor();
    await page.getByRole('button', {name: '预览逐字稿', exact: true}).click();
    await page.getByText('这是接口验收的合成文本。', {exact: true}).waitFor();
    await page.keyboard.press('Escape');
    await page.getByRole('button', {name: '重新导出目录', exact: true}).click();
    await page.locator('#input-export-dir').fill(fixture.export_dir);
    await page.locator('#btn-confirm-export').click();
    await page.waitForFunction(() => document.querySelector('#export-modal').style.display === 'none');
    assert.ok(calls.some(call => call.action === 'export' && call.output_dir === fixture.export_dir));
    const fs = require('node:fs');
    assert.ok(fs.readdirSync(fixture.export_dir).some(name => name.endsWith('.md')), 're-export must create a real Markdown artifact');
    assert.deepEqual(errors, []);
    console.log('Real dashboard → worker → actual transcript preview and revision-safe re-export passed.');
  } finally {
    if (browser) await browser.close();
    child.stdin.end('\n');
    await Promise.race([exited, new Promise(resolve => setTimeout(resolve, 5000))]);
    if (child.exitCode === null) child.kill();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
