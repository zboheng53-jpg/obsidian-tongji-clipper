/**
 * Extension Background Service Worker
 * Bridges popup and dashboard requests with Chrome Native Messaging Host "com.tongji.clipper"
 *
 * Key features:
 * - Persistent native port reuse via chrome.runtime.connectNative
 * - Correlation of concurrent requests and responses using request_id
 * - Disconnect cleanup (rejects all pending requests) and automatic reconnect on next call
 * - Per-request timeout protection to prevent locking the UI refresh loop
 * - Strict sender origin validation: permits only internal extension pages (popup, dashboard)
 * - Alarm-based task change sampling with notification deduplication and baseline storage
 * - Concurrency lock on pollJobsAndNotify
 * - Rejection & disconnect on legacy native host missing request_id to prevent misattribution
 */

const NATIVE_HOST_NAME = "com.tongji.clipper";
const DEFAULT_REQUEST_TIMEOUT_MS = 15000;

let nativePort = null;
let requestIdCounter = 0;
const pendingRequests = new Map(); // requestId -> { resolve, timer, action, createdAt }
let isPollingJobs = false;

function getOrCreateNativePort() {
  if (nativePort) {
    return nativePort;
  }

  if (typeof chrome === "undefined" || !chrome.runtime || !chrome.runtime.connectNative) {
    throw new Error("Chrome Native Messaging API 不可用");
  }

  try {
    nativePort = chrome.runtime.connectNative(NATIVE_HOST_NAME);

    const connectedPort = nativePort;
    connectedPort.onMessage.addListener((msg) => {
      if (nativePort === connectedPort) handleNativeMessage(msg);
    });

    connectedPort.onDisconnect.addListener(() => {
      if (nativePort === connectedPort) handleNativeDisconnect();
    });

    return nativePort;
  } catch (err) {
    nativePort = null;
    throw err;
  }
}

function handleNativeDisconnect() {
  const lastError = typeof chrome !== "undefined" && chrome.runtime ? chrome.runtime.lastError : null;
  const errMsg = `Native Messaging 通信中断: ${
    lastError ? lastError.message : "连接已断开"
  }。请确认已运行注册脚本并安装 Python 依赖。`;

  nativePort = null;

  for (const [id, req] of pendingRequests.entries()) {
    clearTimeout(req.timer);
    req.resolve({ error: errMsg, request_id: id });
  }
  pendingRequests.clear();
}

function handleNativeMessage(msg) {
  if (!msg || typeof msg !== "object") return;

  // 1. Direct match by request_id
  if (msg.request_id && pendingRequests.has(msg.request_id)) {
    const req = pendingRequests.get(msg.request_id);
    clearTimeout(req.timer);
    pendingRequests.delete(msg.request_id);
    req.resolve(msg);
    return;
  }

  // 2. Legacy backend response missing request_id:
  // Reject all pending and reset connection to prevent错配已超时回复 to a new request.
  if (!msg.request_id) {
    const errText =
      "本机后台服务未返回 request_id（服务版本过旧），为防止并发或超时消息错配，已中止并重置连接。请重启或更新本机服务。";
    const stalePort = nativePort;
    nativePort = null;
    if (stalePort && stalePort.disconnect) stalePort.disconnect();
    for (const [id, req] of pendingRequests.entries()) {
      clearTimeout(req.timer);
      req.resolve({ error: errText, request_id: id });
    }
    pendingRequests.clear();
    return;
  }
}

function sendToNativeHost(message, timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS) {
  return new Promise((resolve) => {
    try {
      const port = getOrCreateNativePort();
      const requestId = `req_${Date.now()}_${++requestIdCounter}`;
      const payloadWithId = Object.assign({}, message, { request_id: requestId });

      const timer = setTimeout(() => {
        if (pendingRequests.has(requestId)) {
          pendingRequests.delete(requestId);
          resolve({
            error: `请求超时（${Math.round(timeoutMs / 1000)}秒内本机服务未响应）`,
            request_id: requestId,
          });
        }
      }, timeoutMs);

      pendingRequests.set(requestId, {
        resolve,
        timer,
        action: message ? message.action : "unknown",
        createdAt: Date.now(),
      });

      try {
        port.postMessage(payloadWithId);
      } catch (postErr) {
        // Clean up pending immediately on postMessage failure
        clearTimeout(timer);
        pendingRequests.delete(requestId);
        resolve({
          error: `发送 Native 消息失败: ${postErr.message || String(postErr)}`,
          request_id: requestId,
        });
      }
    } catch (err) {
      resolve({
        error: `连接 Native 服务失败: ${err.message || String(err)}`,
      });
    }
  });
}

function isSenderTrusted(sender) {
  if (!sender) return false;
  if (typeof chrome === "undefined" || !chrome.runtime || !chrome.runtime.id) return false;
  if (sender.id !== chrome.runtime.id) return false;

  const extPrefix =
    (chrome.runtime.getURL && chrome.runtime.getURL("")) ||
    `chrome-extension://${chrome.runtime.id}/`;
  if (!sender.url || !sender.url.startsWith(extPrefix)) {
    return false;
  }
  return true;
}

// Runtime message listener for UI commands
if (typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.onMessage) {
  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message && message.type === "NATIVE_ACTION") {
      if (!isSenderTrusted(sender)) {
        sendResponse({
          error: "权限拒绝：仅限扩展内部受信任页面调用系统原生接口",
        });
        return false;
      }

      sendToNativeHost(message.payload).then((resp) => {
        sendResponse(resp);
      });
      return true; // Keep channel open for async response
    }
  });
}

// Task sampling, notification deduplication and badge management
function pollJobsAndNotify() {
  if (isPollingJobs) return;
  isPollingJobs = true;

  sendToNativeHost({ action: "list_jobs" })
    .then((resp) => {
      if (!resp || !Array.isArray(resp.jobs)) return;

      const jobs = resp.jobs;
      if (typeof chrome === "undefined" || !chrome.storage || !chrome.storage.local) return;

      return new Promise((done) => chrome.storage.local.get(
        ["known_job_states", "baseline_initialized"],
        (storageData) => {
          const knownStates = Object.assign({}, storageData.known_job_states || {});
          const baselineInitialized = !!storageData.baseline_initialized;

          if (!baselineInitialized) {
            // Record initial baseline without triggering bulk notifications
            for (const job of jobs) {
              if (job.job_id) {
                knownStates[job.job_id] = job.status || "queued";
              }
            }
            chrome.storage.local.set({
              known_job_states: knownStates,
              baseline_initialized: true,
            }, done);
            updateBadge(jobs);
            return;
          }

          // Compare status transitions and trigger deduplicated notifications
          for (const job of jobs) {
            if (!job.job_id) continue;
            const prevStatus = knownStates[job.job_id];
            const currStatus = job.status || "queued";
            const courseTitle = job.course_title || job.sub_title || "课程转写";

            if (prevStatus !== currStatus) {
              if (currStatus === "completed" && prevStatus !== "completed") {
                triggerNotification(
                  `job_${job.job_id}_completed`,
                  "转写任务已完成",
                  `【${courseTitle}】转写与笔记已生成完毕。`
                );
              } else if (currStatus === "failed" && prevStatus !== "failed") {
                triggerNotification(
                  `job_${job.job_id}_failed`,
                  "转写任务失败",
                  `【${courseTitle}】处理失败: ${job.error_message || "未知错误"}`
                );
              } else if (currStatus === "needs_refresh" && prevStatus !== "needs_refresh") {
                triggerNotification(
                  `job_${job.job_id}_needs_refresh`,
                  "课程地址需刷新",
                  `【${courseTitle}】媒体流地址已失效，请重新打开课程页刷新后提交。`
                );
              }
            }
            knownStates[job.job_id] = currStatus;
          }

          chrome.storage.local.set({ known_job_states: knownStates }, done);
          updateBadge(jobs);
        }
      ));
    })
    .finally(() => {
      isPollingJobs = false;
    });
}

function updateBadge(jobs) {
  if (typeof chrome === "undefined" || !chrome.action || !chrome.action.setBadgeText) return;

  const runningCount = jobs.filter((j) => j.status === "running").length;
  const attentionCount = jobs.filter(
    (j) => j.status === "needs_refresh" || j.status === "failed"
  ).length;

  if (runningCount > 0) {
    chrome.action.setBadgeText({ text: String(runningCount) });
    chrome.action.setBadgeBackgroundColor({ color: "#2563eb" });
  } else if (attentionCount > 0) {
    chrome.action.setBadgeText({ text: "!" });
    chrome.action.setBadgeBackgroundColor({ color: "#dc2626" });
  } else {
    chrome.action.setBadgeText({ text: "" });
  }
}

function triggerNotification(notifId, title, message) {
  if (typeof chrome === "undefined" || !chrome.notifications || !chrome.notifications.create) {
    return;
  }
  chrome.notifications.create(notifId, {
    type: "basic",
    iconUrl: "icons/icon48.png",
    title,
    message,
    priority: 1,
  });
}

if (typeof chrome !== "undefined" && chrome.notifications && chrome.notifications.onClicked) {
  chrome.notifications.onClicked.addListener(() => {
    if (chrome.tabs && chrome.tabs.create) {
      chrome.tabs.create({ url: chrome.runtime.getURL("dashboard.html") });
    }
  });
}

if (typeof chrome !== "undefined" && chrome.alarms) {
  chrome.alarms.create("poll_jobs_alarm", { periodInMinutes: 0.5 });
  chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm && alarm.name === "poll_jobs_alarm") {
      pollJobsAndNotify();
    }
  });
}

if (typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.onInstalled) {
  chrome.runtime.onInstalled.addListener(() => {
    pollJobsAndNotify();
  });
}

// Export for node unit testing
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    sendToNativeHost,
    handleNativeMessage,
    handleNativeDisconnect,
    isSenderTrusted,
    pollJobsAndNotify,
    updateBadge,
    getPendingRequests: () => pendingRequests,
    setNativePort: (port) => {
      nativePort = port;
    },
    resetState: () => {
      nativePort = null;
      requestIdCounter = 0;
      pendingRequests.clear();
      isPollingJobs = false;
    },
  };
}
