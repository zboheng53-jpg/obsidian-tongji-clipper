/**
 * Test Suite: Background Messaging Architecture & Service Worker
 * Tests:
 * 1. Concurrent requests correlation with request_id out-of-order replies
 * 2. Port disconnect handling & pending promise cleanup
 * 3. Request timeout protection
 * 4. Legacy host response compatibility (single vs multiple collision protection)
 * 5. Sender security validation (content script & external origin rejection)
 * 6. Notification deduplication and baseline storage
 * 7. Action badge updates
 */

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

(async () => {
  console.log("--- Starting Background Messaging Tests ---");

  // Mock Chrome APIs
  let nativeMessageListeners = [];
  let nativeDisconnectListeners = [];
  let runtimeMessageListeners = [];
  let postedNativeMessages = [];
  let storageState = {};
  let createdNotifications = [];
  let currentBadge = { text: "", color: "" };

  const mockPort = {
    postMessage(msg) {
      postedNativeMessages.push(msg);
    },
    onMessage: {
      addListener(fn) {
        nativeMessageListeners.push(fn);
      },
    },
    onDisconnect: {
      addListener(fn) {
        nativeDisconnectListeners.push(fn);
      },
    },
  };

  const mockChrome = {
    runtime: {
      id: "tongji-clipper-ext-id",
      lastError: null,
      getURL(path) {
        return `chrome-extension://tongji-clipper-ext-id/${path || ""}`;
      },
      connectNative(hostName) {
        assert.equal(hostName, "com.tongji.clipper");
        return mockPort;
      },
      onMessage: {
        addListener(fn) {
          runtimeMessageListeners.push(fn);
        },
      },
      onInstalled: { addListener() {} },
    },
    storage: {
      local: {
        get(keys, cb) {
          const res = {};
          (Array.isArray(keys) ? keys : [keys]).forEach((k) => {
            if (storageState[k] !== undefined) res[k] = storageState[k];
          });
          cb(res);
        },
        set(data, cb) {
          Object.assign(storageState, data);
          if (cb) cb();
        },
      },
    },
    action: {
      setBadgeText({ text }) {
        currentBadge.text = text;
      },
      setBadgeBackgroundColor({ color }) {
        currentBadge.color = color;
      },
    },
    notifications: {
      create(id, options) {
        createdNotifications.push({ id, ...options });
      },
      onClicked: { addListener() {} },
    },
    alarms: {
      create() {},
      onAlarm: { addListener() {} },
    },
  };

  const context = vm.createContext({
    chrome: mockChrome,
    console,
    setTimeout,
    clearTimeout,
    Date,
    Array,
    Object,
    String,
    Math,
    Promise,
    module: { exports: {} },
  });

  const bgCode = fs.readFileSync("extension/background.js", "utf8");
  vm.runInContext(bgCode, context);
  const bg = context.module.exports;

  // -------------------------------------------------------------
  // Test 1: Concurrency and out-of-order response matching by request_id
  // -------------------------------------------------------------
  console.log("Test 1: Concurrent requests & out-of-order matching with request_id");
  postedNativeMessages = [];
  const p1 = bg.sendToNativeHost({ action: "test_action_1" });
  const p2 = bg.sendToNativeHost({ action: "test_action_2" });
  const p3 = bg.sendToNativeHost({ action: "test_action_3" });

  assert.equal(postedNativeMessages.length, 3);
  const [req1, req2, req3] = postedNativeMessages;
  assert.ok(req1.request_id && req2.request_id && req3.request_id);
  assert.notEqual(req1.request_id, req2.request_id);
  assert.notEqual(req2.request_id, req3.request_id);

  // Dispatch responses out of order: Req 3, then Req 1, then Req 2
  bg.handleNativeMessage({ request_id: req3.request_id, data: "result_3" });
  bg.handleNativeMessage({ request_id: req1.request_id, data: "result_1" });
  bg.handleNativeMessage({ request_id: req2.request_id, data: "result_2" });

  const [res1, res2, res3] = await Promise.all([p1, p2, p3]);
  assert.equal(res1.data, "result_1");
  assert.equal(res2.data, "result_2");
  assert.equal(res3.data, "result_3");
  assert.equal(bg.getPendingRequests().size, 0);
  console.log("✓ Concurrent requests matched correctly out of order");

  // -------------------------------------------------------------
  // Test 2: Port disconnect handling & pending cleanup
  // -------------------------------------------------------------
  console.log("Test 2: Port disconnect & pending cleanup");
  bg.resetState();
  postedNativeMessages = [];
  const pDisconnect1 = bg.sendToNativeHost({ action: "pending_before_disconnect" });
  const pDisconnect2 = bg.sendToNativeHost({ action: "pending_before_disconnect_2" });
  assert.equal(bg.getPendingRequests().size, 2);

  mockChrome.runtime.lastError = { message: "Native host has exited." };
  bg.handleNativeDisconnect();

  const [discRes1, discRes2] = await Promise.all([pDisconnect1, pDisconnect2]);
  assert.match(discRes1.error, /Native Messaging 通信中断/);
  assert.match(discRes1.error, /Native host has exited/);
  assert.match(discRes2.error, /Native Messaging 通信中断/);
  assert.equal(bg.getPendingRequests().size, 0);

  // Verify next request reconnects properly
  mockChrome.runtime.lastError = null;
  const pAfterDisc = bg.sendToNativeHost({ action: "after_disconnect" });
  assert.equal(bg.getPendingRequests().size, 1);
  const newReq = postedNativeMessages[postedNativeMessages.length - 1];
  bg.handleNativeMessage({ request_id: newReq.request_id, status: "reconnected_ok" });
  const afterRes = await pAfterDisc;
  assert.equal(afterRes.status, "reconnected_ok");
  console.log("✓ Disconnect handled cleanly, pending rejected, reconnect succeeded");

  // -------------------------------------------------------------
  // Test 3: Request timeout protection
  // -------------------------------------------------------------
  console.log("Test 3: Request timeout protection");
  bg.resetState();
  const pTimeout = bg.sendToNativeHost({ action: "will_timeout" }, 40); // 40ms timeout
  assert.equal(bg.getPendingRequests().size, 1);
  const timeoutRes = await pTimeout;
  assert.match(timeoutRes.error, /请求超时/);
  assert.equal(bg.getPendingRequests().size, 0);
  console.log("✓ Request timeout triggered without blocking");

  // -------------------------------------------------------------
  // Test 4: Legacy host compatibility (no request_id)
  // -------------------------------------------------------------
  console.log("Test 4: Legacy backend fallback without request_id");
  bg.resetState();
  // 4a. Single pending request receives response without request_id -> OK
  const pLegacySingle = bg.sendToNativeHost({ action: "legacy_single" });
  bg.handleNativeMessage({ status: "legacy_ok_single" }); // Missing request_id
  const resLegacySingle = await pLegacySingle;
  assert.match(resLegacySingle.error, /未返回 request_id/);
  assert.equal(bg.getPendingRequests().size, 0);

  // 4b. Multiple pending requests receive response without request_id -> collision prevented!
  const pLegacyMulti1 = bg.sendToNativeHost({ action: "legacy_multi_1" });
  const pLegacyMulti2 = bg.sendToNativeHost({ action: "legacy_multi_2" });
  assert.equal(bg.getPendingRequests().size, 2);
  bg.handleNativeMessage({ status: "legacy_ambiguous" }); // Missing request_id with 2 pending!
  const [resLM1, resLM2] = await Promise.all([pLegacyMulti1, pLegacyMulti2]);
  assert.match(resLM1.error, /未返回 request_id/);
  assert.match(resLM2.error, /未返回 request_id/);
  assert.equal(bg.getPendingRequests().size, 0);
  console.log("✓ Legacy host handled safely without response mismatch");

  // -------------------------------------------------------------
  // Test 5: Sender security origin check
  // -------------------------------------------------------------
  console.log("Test 5: Sender security check (rejecting content scripts & untrusted origins)");
  // 5a. Extension internal page (e.g. popup or dashboard)
  assert.equal(
    bg.isSenderTrusted({
      id: "tongji-clipper-ext-id",
      url: "chrome-extension://tongji-clipper-ext-id/dashboard.html",
    }),
    true
  );
  // 5b. External extension/app ID mismatch
  assert.equal(
    bg.isSenderTrusted({
      id: "malicious-extension-id",
      url: "chrome-extension://malicious-extension-id/popup.html",
    }),
    false
  );
  // 5c. Content script running on web page tab (sender.tab is populated with web URL)
  assert.equal(
    bg.isSenderTrusted({
      id: "tongji-clipper-ext-id",
      tab: { id: 101 },
      url: "https://look.tongji.edu.cn/livingroom?course_id=1",
    }),
    false
  );
  console.log("✓ Sender origin security restrictions verified");

  // -------------------------------------------------------------
  // Test 6: Notification deduplication & baseline state
  // -------------------------------------------------------------
  console.log("Test 6: Notification deduplication & baseline initialization");
  bg.resetState();
  storageState = {};
  createdNotifications = [];

  // Simulate initial poll where tasks are already completed / failed
  const initialJobs = [
    { job_id: "j1", status: "completed", course_title: "线性代数" },
    { job_id: "j2", status: "failed", course_title: "大学物理", error_message: "解码错误" },
  ];

  // When pollJobsAndNotify sends list_jobs, answer it via handleNativeMessage
  bg.pollJobsAndNotify();
  const listReq1 = postedNativeMessages[postedNativeMessages.length - 1];
  assert.equal(listReq1.action, "list_jobs");
  bg.handleNativeMessage({ request_id: listReq1.request_id, jobs: initialJobs });
  await new Promise((r) => setTimeout(r, 20));

  // Baseline should be stored, 0 notifications triggered!
  assert.equal(createdNotifications.length, 0);
  assert.equal(storageState.baseline_initialized, true);
  assert.equal(storageState.known_job_states["j1"], "completed");
  assert.equal(storageState.known_job_states["j2"], "failed");

  // Second poll: a new job transitions to completed, another to needs_refresh
  currentMockJobs = [
    { job_id: "j1", status: "completed", course_title: "线性代数" },
    { job_id: "j2", status: "failed", course_title: "大学物理", error_message: "解码错误" },
    { job_id: "j3", status: "completed", course_title: "高等数学" },
    { job_id: "j4", status: "needs_refresh", course_title: "算法分析" },
  ];

  bg.pollJobsAndNotify();
  const listReq2 = postedNativeMessages[postedNativeMessages.length - 1];
  assert.equal(listReq2.action, "list_jobs");
  bg.handleNativeMessage({ request_id: listReq2.request_id, jobs: currentMockJobs });
  await new Promise((r) => setTimeout(r, 20));

  // Only the transitions should trigger notifications (j3 completed, j4 needs_refresh)
  assert.equal(createdNotifications.length, 2);
  const n1 = createdNotifications.find((n) => n.id.includes("j3"));
  const n2 = createdNotifications.find((n) => n.id.includes("j4"));
  assert.ok(n1);
  assert.match(n1.title, /任务已完成/);
  assert.ok(n2);
  assert.match(n2.title, /课程地址需刷新/);

  // Third poll with same status -> 0 new notifications
  const countBefore = createdNotifications.length;
  bg.pollJobsAndNotify();
  const listReq3 = postedNativeMessages[postedNativeMessages.length - 1];
  bg.handleNativeMessage({ request_id: listReq3.request_id, jobs: currentMockJobs });
  await new Promise((r) => setTimeout(r, 20));
  assert.equal(createdNotifications.length, countBefore);
  console.log("✓ Baseline initialization & notification deduplication passed");

  // -------------------------------------------------------------
  // Test 7: Badge count updates
  // -------------------------------------------------------------
  console.log("Test 7: Action badge updates");
  bg.updateBadge([{ job_id: "j1", status: "running" }, { job_id: "j2", status: "running" }]);
  assert.equal(currentBadge.text, "2");
  assert.equal(currentBadge.color, "#2563eb");

  bg.updateBadge([{ job_id: "j1", status: "completed" }, { job_id: "j2", status: "failed" }]);
  assert.equal(currentBadge.text, "!");
  assert.equal(currentBadge.color, "#dc2626");

  bg.updateBadge([{ job_id: "j1", status: "completed" }]);
  assert.equal(currentBadge.text, "");
  console.log("✓ Action badge display states passed");

  console.log("All background messaging tests passed successfully!");
})().catch((err) => {
  console.error("Test failed:", err);
  process.exitCode = 1;
});
