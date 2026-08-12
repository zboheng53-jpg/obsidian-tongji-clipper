import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";


const root = new URL("../", import.meta.url);

function makeElement() {
  return {
    dataset: {},
    disabled: false,
    hidden: false,
    checked: false,
    value: "",
    textContent: "",
    title: "",
    listeners: {},
    addEventListener(name, listener) { this.listeners[name] = listener; }
  };
}

function response(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() { return payload; }
  };
}

function makeOptionsContext(fetchImpl) {
  const selectors = [
    "service-chip", "service-label", "service-version", "vault-path", "vault-message",
    "select-vault", "notes-subdir", "model", "keep-media", "save-settings",
    "save-state", "test-write", "test-result", "retry-service", "toast"
  ];
  const elements = Object.fromEntries(selectors.map((id) => [`#${id}`, makeElement()]));
  const context = vm.createContext({
    document: { querySelector(selector) { return elements[selector]; } },
    fetch: fetchImpl,
    window: { setTimeout() { return 1; }, clearTimeout() {} },
    setTimeout,
    clearTimeout,
    console
  });
  vm.runInContext(fs.readFileSync(new URL("extension/options.js", root), "utf8"), context);
  return { context, elements };
}

async function testConfiguredSettingsFlow() {
  const settings = {
    vault_dir: "D:\\Obsidian\\Tongji",
    notes_subdir: "课程笔记",
    model: "small",
    keep_temporary_media: false,
    configured: true,
    vault_exists: true,
    vault_writable: true,
    looks_like_vault: true
  };
  const calls = [];
  const { context, elements } = makeOptionsContext(async (url, options = {}) => {
    calls.push({ url, options });
    if (url.endsWith("/health")) return response({ version: "0.2.0", configured: true });
    if (url.endsWith("/settings/test-write")) return response({ ok: true, output: "D:\\Obsidian\\Tongji\\课程笔记\\测试.md" });
    if (url.endsWith("/settings/select-vault")) return response({ ...settings, cancelled: false });
    if (url.endsWith("/settings")) return response(settings);
    throw new Error(`Unexpected URL: ${url}`);
  });

  await vm.runInContext("refresh()", context);
  assert.equal(elements["#service-chip"].dataset.state, "ready");
  assert.equal(elements["#vault-path"].textContent, settings.vault_dir);
  assert.equal(elements["#notes-subdir"].value, "课程笔记");
  assert.equal(elements["#test-write"].disabled, false);

  elements["#notes-subdir"].value = "课程笔记/自动转写";
  await vm.runInContext("saveSettings()", context);
  const put = calls.find((call) => call.options.method === "PUT");
  assert.equal(JSON.parse(put.options.body).notes_subdir, "课程笔记/自动转写");
  assert.equal(put.options.headers["X-Tongji-Clipper"], "1");

  await vm.runInContext("testWrite()", context);
  assert.match(elements["#test-result"].textContent, /测试成功/);
  assert.equal(elements["#test-result"].hidden, false);
}

async function testOfflineState() {
  const { context, elements } = makeOptionsContext(async () => {
    throw new Error("connection refused");
  });
  await vm.runInContext("refresh()", context);
  assert.equal(elements["#service-chip"].dataset.state, "error");
  assert.equal(elements["#select-vault"].disabled, true);
  assert.match(elements["#save-state"].textContent, /无法连接/);
}

await testConfiguredSettingsFlow();
await testOfflineState();
console.log("options tests: PASS");
