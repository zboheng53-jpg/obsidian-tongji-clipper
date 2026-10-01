/**
 * Popup Script: Controls extension UI, queries tab capture state, submits jobs, and lists task status.
 * All text rendering uses DOM textContent to prevent XSS.
 */

let currentCapture = null;
let tasksRefreshing = false;

function taskProgress(job) {
  const info = job.progress_info || {};
  const status = job.status || 'queued';
  const statuses = {queued: '排队等待', completed: '已完成', failed: '失败', cancelled: '已取消', needs_refresh: '等待刷新课程地址'};
  const stages = {preparing: '准备任务', acquisition: '获取音频', transcription: '转写', exporting: '导出与核验'};
  const stage = status === 'completed' ? 'completed' : info.stage || 'queued';
  const raw = info.chunk_percent;
  const percent = status === 'completed' ? 100 : stage === 'transcription' && info.total_chunks !== 0 && typeof raw === 'number' && Number.isFinite(raw) ? Math.max(0, Math.min(100, raw)) : null;
  return {stage, percent, active: status === 'running', label: status === 'running' ? stages[stage] || '处理中' : statuses[status] || status};
}

function renderProgress(job, item) {
  const state = taskProgress(job);
  const block = document.createElement('div');
  block.className = 'task-progress';
  const label = document.createElement('div');
  label.className = 'progress-label';
  label.textContent = state.label + (state.percent !== null ? ` · ${state.percent}%${state.stage === 'transcription' ? '（转写阶段）' : ''}` : '');
  block.appendChild(label);
  const track = document.createElement('div');
  track.className = 'progress-track' + (state.active && state.percent === null ? ' indeterminate' : '') + ` progress-${job.status}`;
  track.setAttribute('role', 'progressbar');
  track.setAttribute('aria-label', state.label);
  track.setAttribute('aria-valuemin', '0');
  track.setAttribute('aria-valuemax', '100');
  if (state.percent !== null) track.setAttribute('aria-valuenow', String(state.percent));
  track.setAttribute('aria-valuetext', label.textContent);
  const fill = document.createElement('div');
  fill.className = 'progress-fill';
  fill.style.width = `${state.percent ?? 0}%`;
  track.appendChild(fill);
  block.appendChild(track);
  const steps = document.createElement('div');
  steps.className = 'progress-steps';
  const order = ['preparing', 'acquisition', 'transcription', 'exporting', 'completed'];
  const index = order.indexOf(state.stage);
  ['准备', '音频', '转写', '导出'].forEach((name, i) => {
    const step = document.createElement('span');
    step.textContent = (index > i ? '✓ ' : '') + name;
    step.className = index > i ? 'step-done' : index === i && state.active ? 'step-current' : '';
    steps.appendChild(step);
  });
  block.appendChild(steps);
  const detail = document.createElement('div');
  detail.className = 'task-meta';
  const info = job.progress_info || {};
  detail.textContent = job.status === 'running' || job.status === 'completed' ? info.message || '' : job.status === 'queued' ? '按提交顺序执行，打开此窗口会自动刷新。' : '任务已暂停推进。';
  if (job.status === 'running' && state.stage === 'transcription' && Number.isFinite(info.total_chunks) && info.total_chunks > 0) {
    detail.textContent = `已完成 ${info.completed_chunks ?? Math.max(0, (info.current_chunk || 1) - 1)}/${info.total_chunks} 个分块 · ${detail.textContent}`;
  }
  block.appendChild(detail);
  item.appendChild(block);
}

function showMessage(text, isError = false) {
  const bar = document.getElementById("message-bar");
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
  chrome.runtime.sendMessage(
    { type: "NATIVE_ACTION", payload: { action: "ping" } },
    (resp) => {
      if (resp && resp.status === "ok") {
        badge.textContent = `Worker 在线 (端口 ${resp.port})`;
        badge.className = "badge badge-online";
      } else {
        badge.textContent = "Worker 未连接";
        badge.className = "badge badge-offline";
      }
    }
  );
}

function renderTasks(jobs) {
  const listEl = document.getElementById("tasks-list");
  listEl.textContent = "";

  if (!jobs || jobs.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "暂无任务记录";
    listEl.appendChild(empty);
    return;
  }

  jobs.slice(0, 10).forEach((job) => {
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
      const sync = document.getElementById('tasks-sync-status');
      if (resp && resp.jobs) {
        renderTasks(resp.jobs);
        sync.textContent = `最近更新 ${new Date().toLocaleTimeString('zh-CN', {hour12: false})}`;
        sync.style.color = '';
      } else if (resp && resp.error) {
        sync.textContent = `进度更新失败，显示可能已过期：${resp.error}`;
        sync.style.color = '#b91c1c';
      } else {
        sync.textContent = `进度更新失败，显示可能已过期：${runtimeError?.message || '本机服务未响应'}`;
        sync.style.color = '#b91c1c';
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
      notCourseCard.classList.remove("hidden");
      notReadyCard.classList.add("hidden");
      captureCard.classList.add("hidden");
      return;
    }

    notCourseCard.classList.add("hidden");

    chrome.tabs.sendMessage(tab.id, { type: "GET_CAPTURE_STATE" }, (resp) => {
      if (chrome.runtime.lastError || !resp) {
        notReadyCard.classList.remove("hidden");
        captureCard.classList.add("hidden");
        return;
      }

      if (!resp.isReady) {
        notReadyCard.classList.remove("hidden");
        captureCard.classList.add("hidden");
        return;
      }

      notReadyCard.classList.add("hidden");
      captureCard.classList.remove("hidden");
      currentCapture = resp.capture;

      document.getElementById("course-title").textContent = currentCapture.course_title || "-";
      document.getElementById("sub-title").textContent = currentCapture.sub_title || "-";
      document.getElementById("lecturer-name").textContent = currentCapture.lecturer_name || "-";
      document.getElementById("room-name").textContent = currentCapture.room_name || "-";

      const select = document.getElementById("stream-select");
      select.textContent = "";
      currentCapture.media_streams.forEach((stream, idx) => {
        const opt = document.createElement("option");
        opt.value = stream.stream_id;
        opt.textContent = stream.name || `画面流 ${idx + 1}`;
        select.appendChild(opt);
      });
    });
  });
}

document.getElementById("btn-submit").addEventListener("click", () => {
  if (!currentCapture) return;

  const btn = document.getElementById("btn-submit");
  btn.disabled = true;
  btn.textContent = "正在提交...";

  const streamId = document.getElementById("stream-select").value;
  const model = document.getElementById("model-select").value;
  const customModelPath = document.getElementById("custom-model-path").value.trim();
  const isOffline = document.getElementById("chk-offline").checked;
  const outputDir = document.getElementById('output-dir').value.trim();
  const keepAudio = document.getElementById('keep-audio').checked;
  chrome.storage.local.set({model, customModelPath, isOffline, outputDir, keepAudio});

  const msgPayload = {
    action: "submit",
    payload: currentCapture,
    stream_id: streamId,
    config: {
      model: customModelPath || model,
      device: "cpu",
      compute_type: "int8",
      language: "zh",
      offline: isOffline,
      output_dir: outputDir || null,
      keep_audio: keepAudio,
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

document.getElementById("btn-refresh-tasks").addEventListener("click", () => {
  refreshTasks();
  updateWorkerStatus();
});

const btnRefreshCap = document.getElementById("btn-refresh-capture");
if (btnRefreshCap) {
  btnRefreshCap.addEventListener("click", initTabDetection);
}
const btnRefreshCapAlt = document.getElementById("btn-refresh-capture-alt");
if (btnRefreshCapAlt) {
  btnRefreshCapAlt.addEventListener("click", initTabDetection);
}

// Initial boot
chrome.storage.local.get(['model', 'customModelPath', 'isOffline', 'outputDir', 'keepAudio'], settings => {
  if (settings.model) document.getElementById('model-select').value = settings.model;
  document.getElementById('custom-model-path').value = settings.customModelPath || '';
  document.getElementById('chk-offline').checked = !!settings.isOffline;
  document.getElementById('output-dir').value = settings.outputDir || '';
  document.getElementById('keep-audio').checked = !!settings.keepAudio;
});
updateWorkerStatus();
initTabDetection();
refreshTasks();
// The popup owns this timer; closing it leaves the independent worker running.
setInterval(() => {
  if (!document.hidden) refreshTasks();
}, 2000);
