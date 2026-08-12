import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";


const root = new URL("../", import.meta.url);

function makeElement() {
  return {
    dataset: {},
    disabled: false,
    hidden: false,
    value: "",
    textContent: "",
    listeners: {},
    addEventListener(name, listener) { this.listeners[name] = listener; },
    focus() {},
    select() {}
  };
}

function response(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() { return payload; }
  };
}

async function testCompactClipFlow() {
  const elements = Object.fromEntries(
    ["relative-path", "clip-button", "status"].map((id) => [`#${id}`, makeElement()])
  );
  const calls = [];
  let message;
  const settings = {
    output_dir: "C:\\Vault\\课程笔记",
    configured: true
  };
  const context = vm.createContext({
    document: { querySelector(selector) { return elements[selector]; } },
    fetch: async (url, options = {}) => {
      calls.push({ url, options });
      if (url.endsWith("/health")) return response({ status: "ok" });
      if (url.endsWith("/jobs")) return response([]);
      if (url.endsWith("/settings")) return response(settings);
      throw new Error(`Unexpected URL: ${url}`);
    },
    chrome: {
      runtime: {
        async sendMessage(value) {
          message = value;
          return { ok: true, job: { title: "自动控制原理" } };
        }
      }
    },
    window: { setTimeout() { return 1; }, close() {} },
    console
  });

  vm.runInContext(fs.readFileSync(new URL("extension/popup.js", root), "utf8"), context);
  await vm.runInContext("refresh()", context);
  assert.equal(elements["#relative-path"].value, "C:\\Vault\\课程笔记");

  elements["#relative-path"].value = "D:\\Obsidian\\竞赛库\\11_比赛现场速查包";
  await vm.runInContext("clipCurrentPage()", context);
  const put = calls.find((call) => call.options.method === "PUT");
  assert.equal(
    JSON.parse(put.options.body).output_dir,
    "D:\\Obsidian\\竞赛库\\11_比赛现场速查包"
  );
  assert.equal(message.type, "clip-current-page");
  assert.equal(elements["#status"].dataset.state, "active");
}

async function testDirectPathNeverLooksForVault() {
  const elements = Object.fromEntries(
    ["relative-path", "clip-button", "status"].map((id) => [`#${id}`, makeElement()])
  );
  const calls = [];
  const configured = {
    output_dir: "D:\\Obsidian\\竞赛库\\11_比赛现场速查包",
    configured: true,
  };
  const context = vm.createContext({
    document: { querySelector(selector) { return elements[selector]; } },
    fetch: async (url, options = {}) => {
      calls.push({ url, options });
      if (url.endsWith("/health")) return response({ status: "ok" });
      if (url.endsWith("/jobs")) return response([]);
      if (url.endsWith("/settings") && options.method === "PUT") return response(configured);
      if (url.endsWith("/settings")) return response(configured);
      throw new Error(`Unexpected URL: ${url}`);
    },
    chrome: { runtime: { async sendMessage() { return { ok: true, job: {} }; } } },
    window: { setTimeout() { return 1; }, close() {} },
    console
  });

  vm.runInContext(fs.readFileSync(new URL("extension/popup.js", root), "utf8"), context);
  await vm.runInContext("refresh()", context);
  await vm.runInContext("clipCurrentPage()", context);

  assert.equal(calls.some((call) => call.url.includes("select-vault")), false);
  assert.equal(elements["#status"].dataset.state, "active");
}

async function testReopenedPopupShowsLatestFailure() {
  const elements = Object.fromEntries(
    ["relative-path", "clip-button", "status"].map((id) => [`#${id}`, makeElement()])
  );
  const context = vm.createContext({
    document: { querySelector(selector) { return elements[selector]; } },
    fetch: async (url) => {
      if (url.endsWith("/health")) return response({ status: "ok" });
      if (url.endsWith("/settings")) {
        return response({ output_dir: "C:\\Vault\\课程笔记", configured: true });
      }
      if (url.endsWith("/jobs")) {
        return response([
          {
            id: "failed-job",
            state: "failed",
            title: "自动控制原理",
            error: "视频下载失败：HTTP 403",
            created_at: 200
          }
        ]);
      }
      throw new Error(`Unexpected URL: ${url}`);
    },
    chrome: { runtime: { async sendMessage() { return { ok: true }; } } },
    window: { setTimeout() { return 1; }, close() {} },
    console
  });

  vm.runInContext(fs.readFileSync(new URL("extension/popup.js", root), "utf8"), context);
  await vm.runInContext("refresh()", context);

  assert.equal(elements["#status"].dataset.state, "error");
  assert.match(elements["#status"].textContent, /视频下载失败：HTTP 403/);
}

await testCompactClipFlow();
await testDirectPathNeverLooksForVault();
await testReopenedPopupShowsLatestFailure();
console.log("popup tests: PASS");
