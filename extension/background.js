const SERVICE = "http://127.0.0.1:8765";

async function notify(title, message) {
  await chrome.notifications.create({
    type: "basic",
    iconUrl: "icon-128.png",
    title,
    message
  });
}

async function setBadge(tabId, text, color) {
  await chrome.action.setBadgeText({ tabId, text });
  await chrome.action.setBadgeBackgroundColor({ tabId, color });
}

async function getServiceHealth() {
  try {
    const response = await fetch(`${SERVICE}/health`, {
      headers: { "X-Tongji-Clipper": "1" }
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return await response.json();
  } catch (_) {
    throw new Error("本地服务未启动，请运行 install.ps1 或 start.ps1");
  }
}

async function submitCurrentPage(tab) {
  if (!tab.id || !tab.url || !tab.url.startsWith("https://look.tongji.edu.cn/livingroom")) {
    throw new Error("请先打开同济课程回放页面");
  }
  const result = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    files: ["extractor.js"]
  });
  const course = result[0] && result[0].result;
  if (!course) throw new Error("课程信息提取失败");

  const response = await fetch(`${SERVICE}/jobs`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Tongji-Clipper": "1" },
    body: JSON.stringify(course)
  });
  const job = await response.json();
  if (!response.ok) throw new Error(job.error || `本机服务返回 ${response.status}`);
  const stored = await chrome.storage.local.get("jobs");
  const jobs = stored.jobs || {};
  jobs[job.id] = { id: job.id, tabId: tab.id, title: course.title };
  await chrome.storage.local.set({ jobs });
  await chrome.alarms.create("poll-tongji-jobs", { periodInMinutes: 0.5 });
  return job;
}

async function clipCurrentPage() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) throw new Error("找不到当前页面");
  try {
    await setBadge(tab.id, "…", "#9a7b42");
    const health = await getServiceHealth();
    if (health.configured === false) {
      throw new Error("Obsidian Vault 尚未配置");
    }
    await setBadge(tab.id, "ASR", "#7c3aed");
    const job = await submitCurrentPage(tab);
    await notify("同济课程剪藏已开始", `${job.title}\n正在后台下载并转写。`);
    return job;
  } catch (error) {
    await setBadge(tab.id, "ERR", "#dc2626");
    await notify("同济课程剪藏失败", error.message || String(error));
    throw error;
  }
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || message.type !== "clip-current-page") return false;
  clipCurrentPage()
    .then((job) => sendResponse({ ok: true, job }))
    .catch((error) => sendResponse({ ok: false, error: error.message || String(error) }));
  return true;
});

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name !== "poll-tongji-jobs") return;
  const stored = await chrome.storage.local.get("jobs");
  const jobs = stored.jobs || {};
  for (const [id, localJob] of Object.entries(jobs)) {
    try {
      const response = await fetch(`${SERVICE}/jobs/${id}`, {
        headers: { "X-Tongji-Clipper": "1" }
      });
      if (response.status === 404) {
        await setBadge(localJob.tabId, "ERR", "#dc2626");
        await notify(
          "同济课程剪藏失败",
          `${localJob.title}\n任务状态已丢失，可能是本地服务曾重启，请重新提交。`
        );
        delete jobs[id];
        continue;
      }
      if (!response.ok) continue;
      const job = await response.json();
      if (job.state === "completed") {
        await setBadge(localJob.tabId, "OK", "#16a34a");
        await notify("同济课程剪藏完成", `${localJob.title}\n已写入目标文件夹。`);
        delete jobs[id];
      } else if (job.state === "failed") {
        await setBadge(localJob.tabId, "ERR", "#dc2626");
        await notify("同济课程剪藏失败", job.error || localJob.title);
        delete jobs[id];
      }
    } catch (_) {
      // 本机服务短暂不可用时保留任务，下次轮询继续检查。
    }
  }
  await chrome.storage.local.set({ jobs });
  if (Object.keys(jobs).length === 0) await chrome.alarms.clear("poll-tongji-jobs");
});
