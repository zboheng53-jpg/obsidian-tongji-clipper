const SERVICE = "http://127.0.0.1:8765";
const REQUEST_HEADERS = { "X-Tongji-Clipper": "1" };

const elements = {
  relativePath: document.querySelector("#relative-path"),
  clipButton: document.querySelector("#clip-button"),
  status: document.querySelector("#status")
};

let settings = null;
let pollTimer = null;

async function api(path, options = {}) {
  const headers = { ...REQUEST_HEADERS, ...(options.headers || {}) };
  if (options.body) headers["Content-Type"] = "application/json";
  const response = await fetch(`${SERVICE}${path}`, { ...options, headers });
  let payload = {};
  try {
    payload = await response.json();
  } catch (_) {
    // 保留通用 HTTP 错误。
  }
  if (!response.ok) throw new Error(payload.error || `本地服务返回 ${response.status}`);
  return payload;
}

function normalizeOutputDirectory(value) {
  return value.trim();
}

function setStatus(message = "", state = "") {
  elements.status.textContent = message;
  elements.status.dataset.state = state;
}

function jobStatus(job) {
  const megabytes = job.downloaded_bytes
    ? `（视频 ${(job.downloaded_bytes / 1024 / 1024).toFixed(1)} MB）`
    : "";
  switch (job.state) {
    case "queued":
      return { message: "任务已排队…", state: "active" };
    case "downloading":
      return { message: "正在下载课程视频…", state: "active" };
    case "transcribing":
      return { message: `正在本地转写${megabytes}…`, state: "active" };
    case "writing":
      return { message: "正在写入 Markdown…", state: "active" };
    case "completed":
      return { message: `已完成：${job.output || job.title}`, state: "success" };
    case "failed":
      return { message: `失败：${job.error || "未知错误"}`, state: "error" };
    default:
      return { message: "", state: "" };
  }
}

function scheduleJobRefresh() {
  if (pollTimer) return;
  pollTimer = window.setTimeout(async () => {
    pollTimer = null;
    await refreshJobStatus();
  }, 2000);
}

async function refreshJobStatus() {
  try {
    const jobs = await api("/jobs");
    if (!Array.isArray(jobs) || jobs.length === 0) return;
    const latest = jobs[0];
    const status = jobStatus(latest);
    setStatus(status.message, status.state);
    if (["queued", "downloading", "transcribing", "writing"].includes(latest.state)) {
      scheduleJobRefresh();
    }
  } catch (error) {
    setStatus(`状态查询失败：${error.message || String(error)}`, "error");
  }
}

async function refresh() {
  elements.clipButton.disabled = true;
  setStatus("正在连接本地服务…");
  try {
    await api("/health");
    settings = await api("/settings");
    elements.relativePath.value = settings.output_dir || "";
    setStatus("");
    await refreshJobStatus();
  } catch (error) {
    setStatus(error.message || "无法连接本地服务", "error");
  } finally {
    elements.clipButton.disabled = false;
    elements.relativePath.focus();
    elements.relativePath.select();
  }
}

async function clipCurrentPage() {
  const outputDir = normalizeOutputDirectory(elements.relativePath.value);
  if (!/^(?:[a-zA-Z]:[\\/]|\\\\)/.test(outputDir)) {
    setStatus("请输入 Windows 绝对文件夹路径", "error");
    elements.relativePath.focus();
    return;
  }

  elements.clipButton.disabled = true;
  setStatus("正在准备剪藏…");
  try {
    if (!settings) {
      await api("/health");
      settings = await api("/settings");
    }
    settings = await api("/settings", {
      method: "PUT",
      body: JSON.stringify({ output_dir: outputDir })
    });
    elements.relativePath.value = settings.output_dir;

    const result = await chrome.runtime.sendMessage({ type: "clip-current-page" });
    if (!result || !result.ok) throw new Error(result?.error || "课程剪藏提交失败");
    setStatus("已加入本地转写队列", "active");
    elements.clipButton.disabled = false;
    scheduleJobRefresh();
  } catch (error) {
    setStatus(error.message || String(error), "error");
    elements.clipButton.disabled = false;
  }
}

elements.clipButton.addEventListener("click", clipCurrentPage);
elements.relativePath.addEventListener("keydown", (event) => {
  if (event.key === "Enter") clipCurrentPage();
});

refresh();
