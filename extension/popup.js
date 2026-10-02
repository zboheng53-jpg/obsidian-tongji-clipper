/**
 * Popup Script: Controls extension popup UI, tab capture detection, submit verification, and lightweight task listing.
 * All text rendering uses DOM textContent to prevent XSS.
 * Submission preserves strict course-switch verification before submitting.
 * Settings initialization & model resolution is strictly unified with dashboard settings.
 */

let currentCapture = null;
let tasksRefreshing = false;
let currentResolvedSettings = null;
let settingsInitPromise = null;

function taskProgress(job) {
  const info = (job && job.progress_info) || {};
  const status = (job && job.status) || "queued";
  const statuses = {
    queued: "排队等待",
    running: "处理中",
    cancelling: "正在取消",
    cancelled: "已取消",
    pausing: "正在暂停",
    paused: "已暂停",
    completed: "已完成",
    failed: "失败",
    needs_refresh: "等待刷新课程地址",
  };
  const stages = {
    preparing: "准备任务",
    acquisition: "获取音频",
    loading: "加载模型",
    transcription: "转写",
    exporting: "导出与核验",
  };
  const stage = status === "completed" ? "completed" : info.stage || "queued";
  const raw = info.chunk_percent;
  const percent =
    status === "completed"
      ? 100
      : stage === "transcription" &&
        info.total_chunks !== 0 &&
        typeof raw === "number" &&
        Number.isFinite(raw)
      ? Math.max(0, Math.min(100, raw))
      : null;

  return {
    stage,
    percent,
    active: status === "running",
    label:
      status === "running"
        ? stages[stage] || "处理中"
        : statuses[status] || status,
  };
}

function renderProgress(job, item) {
  const state = taskProgress(job);
  const block = document.createElement("div");
  block.className = "task-progress";

  const label = document.createElement("div");
  label.className = "progress-label";
  label.textContent =
    state.label +
    (state.percent !== null
      ? ` · ${state.percent}%${state.stage === "transcription" ? "（转写阶段）" : ""}`
      : "");
  block.appendChild(label);

  const track = document.createElement("div");
  track.className =
    "progress-track" +
    (state.active && state.percent === null ? " indeterminate" : "") +
    ` progress-${job.status}`;
  track.setAttribute("role", "progressbar");
  track.setAttribute("aria-label", state.label);
  track.setAttribute("aria-valuemin", "0");
  track.setAttribute("aria-valuemax", "100");
  if (state.percent !== null) {
    track.setAttribute("aria-valuenow", String(state.percent));
  }
  track.setAttribute("aria-valuetext", label.textContent);

  const fill = document.createElement("div");
  fill.className = "progress-fill";
  fill.style.width = `${state.percent ?? 0}%`;
  track.appendChild(fill);
  block.appendChild(track);

  const steps = document.createElement("div");
  steps.className = "progress-steps";
  const order = ["preparing", "acquisition", "transcription", "exporting", "completed"];
  const index = order.indexOf(state.stage);
  ["准备", "音频", "转写", "导出"].forEach((name, i) => {
    const step = document.createElement("span");
    step.textContent = (index > i ? "✓ " : "") + name;
    step.className = index > i ? "step-done" : index === i && state.active ? "step-current" : "";
    steps.appendChild(step);
  });
  block.appendChild(steps);

  const detail = document.createElement("div");
  detail.className = "task-meta";
  const info = job.progress_info || {};
  detail.textContent =
    job.status === "running" || job.status === "completed"
      ? info.message || ""
      : job.status === "queued"
      ? "按提交顺序执行，打开此窗口会自动刷新。"
      : "任务已暂停推进。";

  if (
    job.status === "running" &&
    state.stage === "transcription" &&
    Number.isFinite(info.total_chunks) &&
    info.total_chunks > 0
  ) {
    detail.textContent = `已完成 ${
      info.completed_chunks ?? Math.max(0, (info.current_chunk || 1) - 1)
    }/${info.total_chunks} 个分块 · ${detail.textContent}`;
  }
  block.appendChild(detail);
  item.appendChild(block);
}

function showMessage(text, isError = false) {
  const bar = document.getElementById("message-bar");
  if (!bar) return;
  bar.textContent = text;
  bar.classList.remove("hidden");
  if (isError) {
    bar.style.backgroundColor = "#fee2e2";
    bar.style.color = "#991b1b";
  } else {
    bar.style.backgroundColor = "#e0f2fe";
    bar.style.color = "#0369a1";
  }
}

function updateWorkerStatus() {
  const badge = document.getElementById("worker-badge");
  if (!badge) return;
  chrome.runtime.sendMessage(
    { type: "NATIVE_ACTION", payload: { action: "system_info" } },
    (resp) => {
      if (resp && resp.status === "ok") {
        const runnerAlive = resp.worker ? resp.worker.runner_alive : true;
        if (runnerAlive) {
          badge.textContent = "服务已连接";
          badge.className = "badge badge-online";
        } else {
          badge.textContent = "后台服务离线";
          badge.className = "badge badge-offline";
        }
      } else {
        // Fallback to ping check if system_info is not implemented by legacy backend
        chrome.runtime.sendMessage(
          { type: "NATIVE_ACTION", payload: { action: "ping" } },
          (pingResp) => {
            if (pingResp && pingResp.status === "ok") {
              badge.textContent = "服务已连接";
              badge.className = "badge badge-online";
            } else {
              badge.textContent = "服务未连接";
              badge.className = "badge badge-offline";
            }
          }
        );
      }
    }
  );
}

function renderTasks(jobs) {
  const listEl = document.getElementById("tasks-list");
  if (!listEl) return;
  listEl.textContent = "";

  if (!jobs || jobs.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "暂无任务记录";
    listEl.appendChild(empty);
    return;
  }

  // Display top 5 recent tasks in lightweight popup
  jobs.slice(0, 5).forEach((job) => {
    const item = document.createElement("div");
    item.className = "task-item";

    const topDiv = document.createElement("div");
    topDiv.className = "task-top";

    const idSpan = document.createElement("span");
    idSpan.className = "task-id";
    idSpan.textContent = job.job_id || "";

    const rightGroup = document.createElement("div");
    rightGroup.style.display = "flex";
    rightGroup.style.gap = "6px";
    rightGroup.style.alignItems = "center";

    const statusSpan = document.createElement("span");
    const st = job.status || "queued";
    statusSpan.className = `task-status status-${st}`;
    statusSpan.textContent = taskProgress(job).label;
    rightGroup.appendChild(statusSpan);

    if (st === "queued" || st === "running") {
      const cancelBtn = document.createElement("button");
      cancelBtn.className = "btn-cancel-task";
      cancelBtn.textContent = "取消";
      cancelBtn.addEventListener("click", () => {
        chrome.runtime.sendMessage(
          { type: "NATIVE_ACTION", payload: { action: "cancel", job_id: job.job_id } },
          () => {
            refreshTasks();
          }
        );
      });
      rightGroup.appendChild(cancelBtn);
    }

    topDiv.appendChild(idSpan);
    topDiv.appendChild(rightGroup);
    item.appendChild(topDiv);

    const titleDiv = document.createElement("div");
    titleDiv.className = "task-title";
    const title = job.course_title || job.sub_title || "未知课程";
    titleDiv.textContent = title;
    titleDiv.title = title;
    item.appendChild(titleDiv);

    if (job.lecturer_name) {
      const metaDiv = document.createElement("div");
      metaDiv.className = "task-meta";
      metaDiv.textContent = `教师: ${job.lecturer_name}`;
      item.appendChild(metaDiv);
    }

    renderProgress(job, item);

    if (job.error_message) {
      const errDiv = document.createElement("div");
      errDiv.className = "task-err";
      errDiv.textContent = `错误: ${job.error_message}`;
      item.appendChild(errDiv);
    }

    if (job.output_dir) {
      const outDiv = document.createElement("div");
      outDiv.className = "task-out";
      outDiv.textContent = `产物: ${job.output_dir}`;
      item.appendChild(outDiv);
    }

    listEl.appendChild(item);
  });
}

function refreshTasks() {
  if (tasksRefreshing) return;
  tasksRefreshing = true;
  chrome.runtime.sendMessage(
    { type: "NATIVE_ACTION", payload: { action: "list_jobs" } },
    (resp) => {
      tasksRefreshing = false;
      const runtimeError = chrome.runtime.lastError;
      const sync = document.getElementById("tasks-sync-status");
      if (!sync) return;
      if (resp && resp.jobs) {
        renderTasks(resp.jobs);
        sync.textContent = `最近更新 ${new Date().toLocaleTimeString("zh-CN", {
          hour12: false,
        })}`;
        sync.style.color = "";
      } else if (resp && resp.error) {
        sync.textContent = `进度更新失败，显示可能已过期：${resp.error}`;
        sync.style.color = "#b91c1c";
      } else {
        sync.textContent = `进度更新失败，显示可能已过期：${
          runtimeError?.message || "本机服务未响应"
        }`;
        sync.style.color = "#b91c1c";
      }
    }
  );
}

function initTabDetection() {
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    if (!tabs || tabs.length === 0) return;
    const tab = tabs[0];
    const url = tab.url || "";

    const notCourseCard = document.getElementById("not-course-page");
    const notReadyCard = document.getElementById("media-not-ready");
    const captureCard = document.getElementById("capture-container");

    if (!url.startsWith("https://look.tongji.edu.cn/livingroom")) {
      if (notCourseCard) notCourseCard.classList.remove("hidden");
      if (notReadyCard) notReadyCard.classList.add("hidden");
      if (captureCard) captureCard.classList.add("hidden");
      return;
    }

    if (notCourseCard) notCourseCard.classList.add("hidden");

    chrome.tabs.sendMessage(tab.id, { type: "GET_CAPTURE_STATE" }, (resp) => {
      if (chrome.runtime.lastError || !resp) {
        if (notReadyCard) notReadyCard.classList.remove("hidden");
        if (captureCard) captureCard.classList.add("hidden");
        return;
      }

      if (!resp.isReady) {
        if (notReadyCard) notReadyCard.classList.remove("hidden");
        if (captureCard) captureCard.classList.add("hidden");
        return;
      }

      if (notReadyCard) notReadyCard.classList.add("hidden");
      if (captureCard) captureCard.classList.remove("hidden");
      currentCapture = resp.capture;

      const courseTitle = document.getElementById("course-title");
      if (courseTitle) courseTitle.textContent = currentCapture.course_title || "-";
      const subTitle = document.getElementById("sub-title");
      if (subTitle) subTitle.textContent = currentCapture.sub_title || "-";
      const lecturerName = document.getElementById("lecturer-name");
      if (lecturerName) lecturerName.textContent = currentCapture.lecturer_name || "-";
      const roomName = document.getElementById("room-name");
      if (roomName) roomName.textContent = currentCapture.room_name || "-";

      const select = document.getElementById("stream-select");
      if (select) {
        select.textContent = "";
        (currentCapture.media_streams || []).forEach((stream, idx) => {
          const opt = document.createElement("option");
          opt.value = stream.stream_id;
          opt.textContent = stream.name || `画面流 ${idx + 1}`;
          select.appendChild(opt);
        });
      }
    });
  });
}

function openDashboard(tabName) {
  const url = tabName
    ? `dashboard.html?tab=${encodeURIComponent(tabName)}`
    : "dashboard.html";
  if (chrome.tabs && chrome.tabs.create) {
    chrome.tabs.create({ url: chrome.runtime.getURL(url) });
  }
}

function loadConfigSummary() {
  if (typeof loadAndInitializeSettings === "function") {
    settingsInitPromise = loadAndInitializeSettings().then((resolved) => {
      currentResolvedSettings = resolved;
      renderConfigSummary(resolved);
      return resolved;
    });
    return settingsInitPromise;
  }

  // Fallback if common-client is not loaded (e.g. basic unit test environment)
  settingsInitPromise = new Promise((resolve) => {
    chrome.storage.local.get(
      [
        "modelSource",
        "model",
        "customModelPath",
        "isOffline",
        "outputDir",
        "keepAudio",
        "resourceProfile",
      ],
      (saved) => {
        const resolved = typeof resolveSettings === "function" ? resolveSettings(saved, {}) : saved;
        currentResolvedSettings = resolved;
        renderConfigSummary(resolved);
        resolve(resolved);
      }
    );
  });
  return settingsInitPromise;
}

function renderConfigSummary(settings) {
  const summaryModel = document.getElementById("summary-model");
  const summaryProfile = document.getElementById("summary-profile");
  const summaryFlags = document.getElementById("summary-flags");

  const isCustom = settings.modelSource === "custom";
  let modelText = "";
  if (isCustom) {
    modelText = settings.customModelPath
      ? `本地模型: ${settings.customModelPath}`
      : "本地模型: （未指定路径）";
  } else {
    modelText = `${settings.model || "small"} (通用推荐)`;
  }

  const profiles = {
    balanced: "平衡模式（推荐）",
    low: "节能模式（低占用）",
    fast: "高速模式（性能优先）",
  };
  const profileText = profiles[settings.resourceProfile] || "平衡模式（推荐）";

  const offlineText = settings.isOffline ? "严格离线" : "在线模式";
  const audioText = settings.keepAudio ? "保留音频" : "清理音频";
  const outText = settings.outputDir ? "自定义目录" : "默认目录";

  if (summaryModel) summaryModel.textContent = modelText;
  if (summaryProfile) summaryProfile.textContent = profileText;
  if (summaryFlags) summaryFlags.textContent = `${offlineText} · ${audioText} · ${outText}`;
}

// Course-switch pre-submission verification
const submitBtn = document.getElementById("btn-submit");
if (submitBtn) {
  submitBtn.addEventListener("click", () => {
    if (!currentCapture) return;

    const btn = document.getElementById("btn-submit");
    btn.disabled = true;
    btn.textContent = "正在核验提交...";

    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      if (!tabs || tabs.length === 0) {
        btn.disabled = false;
        btn.textContent = "提交转写任务";
        return;
      }
      const tab = tabs[0];
      chrome.tabs.sendMessage(tab.id, { type: "GET_CAPTURE_STATE" }, (resp) => {
        if (chrome.runtime.lastError || !resp || !resp.isReady || !resp.capture) {
          btn.disabled = false;
          btn.textContent = "提交转写任务";
          showMessage("页面媒体状态已变更，请重新确认当前课程！", true);
          initTabDetection();
          return;
        }

        const freshCapture = resp.capture;
        if (
          freshCapture.course_id !== currentCapture.course_id ||
          freshCapture.sub_id !== currentCapture.sub_id ||
          freshCapture.tenant_code !== currentCapture.tenant_code
        ) {
          btn.disabled = false;
          btn.textContent = "提交转写任务";
          showMessage("检测到课程已切换，已自动刷新当前课程信息，请确认后重新提交！", true);
          currentCapture = freshCapture;
          initTabDetection();
          return;
        }

        currentCapture = freshCapture;

        const streamSelect = document.getElementById("stream-select");
        const streamId = streamSelect ? streamSelect.value : "";

        // Wait for settings initialization to ensure submission matches summary
        const initTask = settingsInitPromise || loadConfigSummary();
        initTask.then((settings) => {
          if (settings.isValid === false) {
            btn.disabled = false;
            btn.textContent = "提交转写任务";
            showMessage(settings.validationError || "请检查设置后重新提交", true);
            return;
          }
          // Strictly prevent submission if custom model path is empty
          if (settings.modelSource === "custom" && !settings.customModelPath) {
            btn.disabled = false;
            btn.textContent = "提交转写任务";
            showMessage("已选择本地自定义模型，但尚未填写模型绝对路径！请前往设置填写后再提交。", true);
            return;
          }

          const actualModel =
            settings.modelSource === "custom" ? settings.customModelPath : settings.model || "small";

          const msgPayload = {
            action: "submit",
            payload: currentCapture,
            stream_id: streamId,
            config: {
              model: actualModel,
              device: "cpu",
              compute_type: "int8",
              language: "zh",
              offline: !!settings.isOffline,
              output_dir: settings.outputDir ? settings.outputDir.trim() : null,
              keep_audio: !!settings.keepAudio,
              resource_profile: settings.resourceProfile || "balanced",
              hotwords: settings.hotwords || "",
            },
          };

          chrome.runtime.sendMessage(
            { type: "NATIVE_ACTION", payload: msgPayload },
            (resp) => {
              btn.disabled = false;
              btn.textContent = "提交转写任务";

              if (resp && resp.error) {
                showMessage(`提交失败: ${resp.error}`, true);
              } else if (resp && resp.status === "ok") {
                showMessage(`任务提交成功！Job ID: ${resp.job?.job_id}`);
                refreshTasks();
              } else {
                showMessage("未知响应", true);
              }
            }
          );
        });
      });
    });
  });
}

// Event listeners for dashboard buttons
const btnOpenDash = document.getElementById("btn-open-dashboard");
if (btnOpenDash) btnOpenDash.addEventListener("click", () => openDashboard());

const btnOpenDashAlt = document.getElementById("btn-open-dashboard-alt");
if (btnOpenDashAlt) btnOpenDashAlt.addEventListener("click", () => openDashboard());

const btnOpenDashTasks = document.getElementById("btn-open-dashboard-tasks");
if (btnOpenDashTasks) btnOpenDashTasks.addEventListener("click", () => openDashboard("tasks"));

const btnOpenSettings = document.getElementById("btn-open-settings");
if (btnOpenSettings) btnOpenSettings.addEventListener("click", () => openDashboard("settings"));

const btnRefreshTasks = document.getElementById("btn-refresh-tasks");
if (btnRefreshTasks) {
  btnRefreshTasks.addEventListener("click", () => {
    refreshTasks();
    updateWorkerStatus();
    loadConfigSummary();
  });
}

const btnRefreshCap = document.getElementById("btn-refresh-capture");
if (btnRefreshCap) btnRefreshCap.addEventListener("click", initTabDetection);

const btnRefreshCapAlt = document.getElementById("btn-refresh-capture-alt");
if (btnRefreshCapAlt) btnRefreshCapAlt.addEventListener("click", initTabDetection);

// Listen for storage changes to keep summary updated
if (typeof chrome !== "undefined" && chrome.storage && chrome.storage.onChanged) {
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "local") {
      loadConfigSummary();
    }
  });
}

// Initial boot
loadConfigSummary();
updateWorkerStatus();
initTabDetection();
refreshTasks();

// Popup auto-refresh timer (2 seconds)
setInterval(() => {
  if (!document.hidden) refreshTasks();
}, 2000);
