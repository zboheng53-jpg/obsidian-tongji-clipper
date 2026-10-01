/**
 * Dashboard Script: Permanent task center & system settings for Tongji Clipper.
 * Handles task monitoring, state transitions, actions, preview modal, mutually exclusive settings, and config validation.
 */

let allJobs = [];
let currentFilter = "all";
let currentSearch = "";
let pollTimer = null;
let isRefreshing = false;
let pendingActionJobId = null;

// Modal accessibility state
let activeModal = null;
let lastFocusedElementBeforeModal = null;
let lastFocusedJobTarget = null; // { jobId, action }

// DOM Elements Cache
const elements = {
  tabBtnTasks: document.getElementById("tab-btn-tasks"),
  tabBtnSettings: document.getElementById("tab-btn-settings"),
  paneTasks: document.getElementById("pane-tasks"),
  paneSettings: document.getElementById("pane-settings"),
  workerBadge: document.getElementById("worker-badge"),
  btnRefreshManual: document.getElementById("btn-refresh-manual"),

  // Stats
  statRunning: document.getElementById("stat-running-count"),
  statQueued: document.getElementById("stat-queued-count"),
  statAttention: document.getElementById("stat-attention-count"),
  statCompleted: document.getElementById("stat-completed-count"),

  // Tasks Filter & List
  taskSearchInput: document.getElementById("task-search-input"),
  taskStatusFilter: document.getElementById("task-status-filter"),
  syncStatusIndicator: document.getElementById("sync-status-indicator"),
  tasksContainer: document.getElementById("tasks-container"),

  // Settings
  radioModelPreset: document.getElementById("radio-model-preset"),
  radioModelCustom: document.getElementById("radio-model-custom"),
  selectModelPreset: document.getElementById("select-model-preset"),
  inputModelCustom: document.getElementById("input-model-custom"),
  rowModelPreset: document.getElementById("row-model-preset"),
  rowModelCustom: document.getElementById("row-model-custom"),
  lblEffectiveModel: document.getElementById("lbl-effective-model"),
  selectResourceProfile: document.getElementById("select-resource-profile"),
  chkOffline: document.getElementById("chk-offline"),
  inputOutputDir: document.getElementById("input-output-dir"),
  chkKeepAudio: document.getElementById("chk-keep-audio"),
  btnSaveSettings: document.getElementById("btn-save-settings"),
  btnValidateConfig: document.getElementById("btn-validate-config"),
  saveStatusText: document.getElementById("save-status-text"),
  validationBox: document.getElementById("validation-box"),
  validationChecksList: document.getElementById("validation-checks-list"),

  // Modals
  previewModal: document.getElementById("preview-modal"),
  previewModalTitle: document.getElementById("preview-modal-title"),
  previewSegmentsContainer: document.getElementById("preview-segments-container"),
  btnClosePreview: document.getElementById("btn-close-preview"),
  btnPreviewDone: document.getElementById("btn-preview-done"),

  exportModal: document.getElementById("export-modal"),
  inputExportDir: document.getElementById("input-export-dir"),
  exportFeedback: document.getElementById("export-feedback"),
  btnCloseExport: document.getElementById("btn-close-export"),
  btnCancelExport: document.getElementById("btn-cancel-export"),
  btnConfirmExport: document.getElementById("btn-confirm-export"),

  cleanupModal: document.getElementById("cleanup-modal"),
  cleanupFeedback: document.getElementById("cleanup-feedback"),
  btnCloseCleanup: document.getElementById("btn-close-cleanup"),
  btnCancelCleanup: document.getElementById("btn-cancel-cleanup"),
  btnConfirmCleanup: document.getElementById("btn-confirm-cleanup"),

  toast: document.getElementById("toast"),
};

// Toast notification helper
function showToast(message, isError = false) {
  if (!elements.toast) return;
  elements.toast.textContent = message;
  elements.toast.className = `toast ${isError ? "toast-error" : "toast-success"}`;
  elements.toast.style.display = "block";
  clearTimeout(elements.toast._timer);
  elements.toast._timer = setTimeout(() => {
    elements.toast.style.display = "none";
  }, 3500);
}

// Accessible Modal Helper with Focus Trap & Escape Restoration
function showModalDialog(modalEl, preferredFocusEl) {
  lastFocusedElementBeforeModal = document.activeElement;
  if (lastFocusedElementBeforeModal && lastFocusedElementBeforeModal.getAttribute) {
    const jId = lastFocusedElementBeforeModal.getAttribute("data-job-id");
    const act = lastFocusedElementBeforeModal.getAttribute("data-action");
    if (jId && act) {
      lastFocusedJobTarget = { jobId: jId, action: act };
    }
  }

  activeModal = modalEl;
  modalEl.style.display = "flex";

  const target = preferredFocusEl || modalEl.querySelector("button, input, [tabindex='0']");
  if (target && target.focus) {
    setTimeout(() => target.focus(), 20);
  }
}

function hideModalDialog(modalEl) {
  if (!modalEl) return;
  modalEl.style.display = "none";
  if (activeModal === modalEl) activeModal = null;

  // Restore focus to trigger element or matching job action button
  if (lastFocusedElementBeforeModal && document.body.contains(lastFocusedElementBeforeModal)) {
    try {
      lastFocusedElementBeforeModal.focus();
      return;
    } catch (e) {}
  }

  if (lastFocusedJobTarget) {
    const sel = `[data-job-id="${lastFocusedJobTarget.jobId}"][data-action="${lastFocusedJobTarget.action}"]`;
    const fallbackEl = document.querySelector(sel);
    if (fallbackEl && fallbackEl.focus) {
      try {
        fallbackEl.focus();
      } catch (e) {}
    }
  }
}

// Trap focus inside modal
document.addEventListener("keydown", (e) => {
  if (!activeModal) return;

  if (e.key === "Escape") {
    e.preventDefault();
    hideModalDialog(activeModal);
    return;
  }

  if (e.key === "Tab") {
    const focusables = Array.from(
      activeModal.querySelectorAll("button:not(:disabled), input:not(:disabled), select:not(:disabled), [tabindex='0']")
    );
    if (focusables.length === 0) return;

    const first = focusables[0];
    const last = focusables[focusables.length - 1];

    if (e.shiftKey) {
      if (document.activeElement === first) {
        e.preventDefault();
        last.focus();
      }
    } else {
      if (document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
  }
});

// Navigation Tabs Handling
function switchTab(tabId) {
  const isTasks = tabId === "tasks";

  elements.tabBtnTasks.classList.toggle("active", isTasks);
  elements.tabBtnTasks.setAttribute("aria-selected", isTasks ? "true" : "false");
  elements.tabBtnTasks.setAttribute("tabindex", isTasks ? "0" : "-1");
  elements.paneTasks.classList.toggle("active", isTasks);

  elements.tabBtnSettings.classList.toggle("active", !isTasks);
  elements.tabBtnSettings.setAttribute("aria-selected", !isTasks ? "true" : "false");
  elements.tabBtnSettings.setAttribute("tabindex", !isTasks ? "0" : "-1");
  elements.paneSettings.classList.toggle("active", !isTasks);

  const url = new URL(window.location.href);
  url.searchParams.set("tab", tabId);
  window.history.replaceState({}, "", url.toString());

  if (isTasks) {
    refreshTasksList();
  }
}

elements.tabBtnTasks.addEventListener("click", () => switchTab("tasks"));
elements.tabBtnSettings.addEventListener("click", () => switchTab("settings"));

// Keyboard Navigation for Tabs (Arrow keys)
[elements.tabBtnTasks, elements.tabBtnSettings].forEach((tabBtn) => {
  tabBtn.addEventListener("keydown", (e) => {
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      e.preventDefault();
      const target = e.target === elements.tabBtnTasks ? elements.tabBtnSettings : elements.tabBtnTasks;
      target.focus();
      target.click();
    }
  });
});

// Worker Status Check (Using runner_alive for true health)
function updateWorkerStatus() {
  sendNativeAction({ action: "system_info" }).then((resp) => {
    if (resp && resp.status === "ok") {
      const runnerAlive = resp.worker ? resp.worker.runner_alive : true;
      if (runnerAlive) {
        elements.workerBadge.textContent = "后台服务在线";
        elements.workerBadge.className = "badge badge-online";
      } else {
        elements.workerBadge.textContent = "执行器未就绪";
        elements.workerBadge.className = "badge badge-offline";
      }
    } else {
      // Fallback ping if system_info fails
      sendNativeAction({ action: "ping" }).then((pingResp) => {
        if (pingResp && pingResp.status === "ok") {
          elements.workerBadge.textContent = "后台服务在线";
          elements.workerBadge.className = "badge badge-online";
        } else {
          elements.workerBadge.textContent = "后台服务未连接";
          elements.workerBadge.className = "badge badge-offline";
        }
      });
    }
  });
}

// Mutually Exclusive Model Choice Logic
function getEffectiveModel() {
  const isCustom = elements.radioModelCustom.checked;
  if (isCustom) {
    return elements.inputModelCustom.value.trim();
  }
  return elements.selectModelPreset.value || "small";
}

function updateModelChoiceUI() {
  const isCustom = elements.radioModelCustom.checked;

  elements.radioModelPreset.checked = !isCustom;
  elements.rowModelPreset.classList.toggle("selected", !isCustom);
  elements.selectModelPreset.disabled = isCustom;

  elements.rowModelCustom.classList.toggle("selected", isCustom);
  elements.inputModelCustom.disabled = !isCustom;

  const effective = getEffectiveModel();
  if (isCustom && !effective) {
    elements.lblEffectiveModel.textContent = "（未填写模型路径，不可保存/自检）";
    elements.lblEffectiveModel.style.color = "var(--danger)";
  } else {
    elements.lblEffectiveModel.textContent = effective;
    elements.lblEffectiveModel.style.color = "";
  }
}

elements.radioModelPreset.addEventListener("change", () => {
  updateModelChoiceUI();
  autoSaveSettings();
});

elements.radioModelCustom.addEventListener("change", () => {
  updateModelChoiceUI();
  elements.inputModelCustom.focus();
  autoSaveSettings();
});

elements.selectModelPreset.addEventListener("change", () => {
  updateModelChoiceUI();
  autoSaveSettings();
});

elements.inputModelCustom.addEventListener("input", () => {
  updateModelChoiceUI();
  autoSaveSettings();
});

elements.selectResourceProfile.addEventListener("change", autoSaveSettings);
elements.chkOffline.addEventListener("change", autoSaveSettings);
elements.inputOutputDir.addEventListener("input", autoSaveSettings);
elements.chkKeepAudio.addEventListener("change", autoSaveSettings);

// Settings Persistence & Initialization
function loadSettings() {
  if (typeof loadAndInitializeSettings === "function") {
    loadAndInitializeSettings().then((resolved) => {
      applySettingsToUI(resolved);
    });
    return;
  }

  if (typeof chrome === "undefined" || !chrome.storage || !chrome.storage.local) return;
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
      applySettingsToUI(resolved);
    }
  );
}

function applySettingsToUI(resolved) {
  const isCustom = resolved.modelSource === "custom";
  elements.radioModelCustom.checked = isCustom;
  elements.radioModelPreset.checked = !isCustom;
  elements.selectModelPreset.value = resolved.model || "small";
  elements.inputModelCustom.value = resolved.customModelPath || "";
  elements.selectResourceProfile.value = resolved.resourceProfile || "balanced";
  elements.chkOffline.checked = !!resolved.isOffline;
  elements.inputOutputDir.value = resolved.outputDir || "";
  elements.chkKeepAudio.checked = !!resolved.keepAudio;

  updateModelChoiceUI();
}

let autoSaveTimer = null;
function autoSaveSettings() {
  clearTimeout(autoSaveTimer);
  autoSaveTimer = setTimeout(() => {
    saveSettingsInternal(false);
  }, 400);
}

function saveSettingsInternal(showFeedback = true) {
  if (typeof chrome === "undefined" || !chrome.storage || !chrome.storage.local) return;

  const isCustom = elements.radioModelCustom.checked;
  const customPath = elements.inputModelCustom.value.trim();

  // Guard: if custom is selected but path is empty, alert user and do not silently fall back
  if (isCustom && !customPath && showFeedback) {
    showToast("请填写本地模型绝对路径，或切换为预设模型！", true);
    return;
  }

  const payload = {
    modelSource: isCustom ? "custom" : "preset",
    model: elements.selectModelPreset.value,
    customModelPath: customPath,
    isOffline: elements.chkOffline.checked,
    outputDir: elements.inputOutputDir.value.trim(),
    keepAudio: elements.chkKeepAudio.checked,
    resourceProfile: elements.selectResourceProfile.value,
    settings_initialized: true,
  };

  chrome.storage.local.set(payload, () => {
    if (showFeedback) {
      elements.saveStatusText.textContent = "✓ 设置已保存，用于后续提交";
      showToast("设置已保存，用于后续提交");
      setTimeout(() => {
        elements.saveStatusText.textContent = "";
      }, 3000);
    }
  });
}

elements.btnSaveSettings.addEventListener("click", () => saveSettingsInternal(true));

// Validate Config Action
elements.btnValidateConfig.addEventListener("click", () => {
  const isCustom = elements.radioModelCustom.checked;
  const customPath = elements.inputModelCustom.value.trim();

  // Guard: do not allow empty custom path
  if (isCustom && !customPath) {
    elements.validationBox.style.display = "block";
    elements.validationChecksList.textContent = "";
    const errDiv = document.createElement("div");
    errDiv.className = "check-item check-error";
    errDiv.textContent = "❌ 校验失败：已选择本地自定义模型，但路径为空。请填写有效路径后再自检。";
    elements.validationChecksList.appendChild(errDiv);
    showToast("请填写本地模型路径后再自检", true);
    return;
  }

  const actualModel = isCustom ? customPath : elements.selectModelPreset.value;

  const config = {
    model: actualModel,
    offline: elements.chkOffline.checked,
    output_dir: elements.inputOutputDir.value.trim() || null,
    keep_audio: elements.chkKeepAudio.checked,
    resource_profile: elements.selectResourceProfile.value,
  };

  elements.btnValidateConfig.disabled = true;
  elements.btnValidateConfig.textContent = "正在自检...";

  sendNativeAction({ action: "validate_config", config }).then((resp) => {
    elements.btnValidateConfig.disabled = false;
    elements.btnValidateConfig.textContent = "环境与配置自检";
    elements.validationBox.style.display = "block";
    elements.validationChecksList.textContent = "";

    if (!resp || resp.error || resp.status !== "ok") {
      const errDiv = document.createElement("div");
      errDiv.className = "check-item check-error";
      errDiv.textContent = `自检调用失败: ${resp ? resp.error || "服务未返回成功响应" : "服务未响应"}`;
      elements.validationChecksList.appendChild(errDiv);
      showToast("自检失败，请查看检查结果", true);
      return;
    }

    const checks = resp.checks;
    // Guard against fabricating checks if backend returned empty array
    if (!Array.isArray(checks) || checks.length === 0) {
      const warnDiv = document.createElement("div");
      warnDiv.className = "check-item check-warn";
      warnDiv.textContent = "⚠️ 后台服务未返回具体检查项详情。";
      elements.validationChecksList.appendChild(warnDiv);
      return;
    }

    checks.forEach((chk) => {
      const row = document.createElement("div");
      row.className = `check-item ${chk.ok ? "check-ok" : "check-error"}`;
      const symbol = chk.ok ? "✓" : "✗";
      row.textContent = `${symbol} [${chk.name || "检查项"}] ${chk.message || (chk.ok ? "正常" : "异常")}`;
      elements.validationChecksList.appendChild(row);
    });

    const hasFailure = checks.some((c) => !c.ok);
    if (hasFailure) {
      showToast("自检发现异常，请核对失败项", true);
    } else {
      showToast("自检全部通过");
    }
  });
});

// Tasks List Rendering & Management
function formatETA(status, etaSeconds) {
  if (status === "completed") return "已完成";
  if (status !== "running") return "-"; // queued, paused, failed, cancelled do not show "计算中"
  if (typeof etaSeconds !== "number" || !Number.isFinite(etaSeconds) || etaSeconds <= 0) {
    return "计算中";
  }
  const s = Math.floor(etaSeconds);
  if (s < 60) return `约 ${s} 秒`;
  const m = Math.floor(s / 60);
  const remS = s % 60;
  return `约 ${m} 分 ${remS > 0 ? remS + " 秒" : ""}`;
}

function updateKPIStats(jobs) {
  const running = jobs.filter((j) => j.status === "running").length;
  const queued = jobs.filter((j) => j.status === "queued").length;
  const attention = jobs.filter(
    (j) => j.status === "needs_refresh" || j.status === "failed"
  ).length;
  const completed = jobs.filter((j) => j.status === "completed").length;

  elements.statRunning.textContent = String(running);
  elements.statQueued.textContent = String(queued);
  elements.statAttention.textContent = String(attention);
  elements.statCompleted.textContent = String(completed);
}

// KPI Stat Cards Click to Filter
document.querySelectorAll(".stat-card").forEach((card) => {
  card.addEventListener("click", () => {
    const filter = card.getAttribute("data-filter");
    elements.taskStatusFilter.value = filter;
    currentFilter = filter;
    document.querySelectorAll(".stat-card").forEach((c) => c.classList.remove("active-filter"));
    card.classList.add("active-filter");
    renderFilteredTasks();
  });
  card.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      card.click();
    }
  });
});

elements.taskStatusFilter.addEventListener("change", (e) => {
  currentFilter = e.target.value;
  document.querySelectorAll(".stat-card").forEach((c) => {
    c.classList.toggle("active-filter", c.getAttribute("data-filter") === currentFilter);
  });
  renderFilteredTasks();
});

elements.taskSearchInput.addEventListener("input", (e) => {
  currentSearch = e.target.value.trim().toLowerCase();
  renderFilteredTasks();
});

function sortJobsPriority(jobs) {
  const priorityWeight = (job) => {
    const st = job.status || "queued";
    if (st === "running") return 1;
    if (st === "needs_refresh" || st === "failed") return 2;
    if (st === "pausing" || st === "cancelling") return 3;
    if (st === "queued") return 4;
    if (st === "paused") return 5;
    if (st === "completed") return 6;
    return 7;
  };

  return [...jobs].sort((a, b) => {
    const wA = priorityWeight(a);
    const wB = priorityWeight(b);
    if (wA !== wB) return wA - wB;
    return (b.created_at || 0) - (a.created_at || 0);
  });
}

function renderFilteredTasks() {
  const container = elements.tasksContainer;

  // Capture currently focused element to preserve focus across 2-second polls
  let focusedJobId = null;
  let focusedAction = null;
  if (document.activeElement && container.contains(document.activeElement)) {
    focusedJobId = document.activeElement.getAttribute("data-job-id");
    focusedAction = document.activeElement.getAttribute("data-action");
  }

  container.textContent = "";

  const filtered = allJobs.filter((job) => {
    if (currentFilter === "running" && job.status !== "running") return false;
    if (currentFilter === "queued" && job.status !== "queued") return false;
    if (currentFilter === "attention" && job.status !== "needs_refresh" && job.status !== "failed") return false;
    if (currentFilter === "completed" && job.status !== "completed") return false;
    if (currentFilter === "paused" && job.status !== "paused") return false;
    if (currentFilter === "cancelled" && job.status !== "cancelled") return false;

    if (currentSearch) {
      const match =
        (job.course_title || "").toLowerCase().includes(currentSearch) ||
        (job.sub_title || "").toLowerCase().includes(currentSearch) ||
        (job.lecturer_name || "").toLowerCase().includes(currentSearch) ||
        (job.job_id || "").toLowerCase().includes(currentSearch);
      if (!match) return false;
    }
    return true;
  });

  if (filtered.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    const icon = document.createElement("div");
    icon.className = "empty-state-icon";
    icon.textContent = "🔍";
    const title = document.createElement("div");
    title.className = "empty-state-title";
    title.textContent = allJobs.length === 0 ? "暂无转写任务记录" : "未找到匹配条件的任务";
    const desc = document.createElement("div");
    desc.className = "empty-state-desc";
    desc.textContent = allJobs.length === 0 ? "请在同济课程播放页面通过扩展弹窗提交转写任务。" : "请尝试更换搜索关键字或状态筛选条件。";
    empty.appendChild(icon);
    empty.appendChild(title);
    empty.appendChild(desc);
    container.appendChild(empty);
    return;
  }

  const sorted = sortJobsPriority(filtered);
  sorted.forEach((job) => {
    container.appendChild(createTaskCardElement(job));
  });

  // Restore focus if previously focused action button still exists
  if (focusedJobId && focusedAction) {
    const restoreEl = container.querySelector(
      `[data-job-id="${focusedJobId}"][data-action="${focusedAction}"]`
    );
    if (restoreEl && restoreEl.focus) {
      try {
        restoreEl.focus();
      } catch (e) {}
    }
  }
}

function createTaskCardElement(job) {
  const card = document.createElement("article");
  const st = job.status || "queued";

  let priorityClass = "";
  if (st === "running") priorityClass = "priority-running";
  else if (st === "failed") priorityClass = "priority-failed";
  else if (st === "needs_refresh") priorityClass = "priority-attention";
  card.className = `task-card ${priorityClass}`;
  card.setAttribute("tabindex", "0");

  // Header: Titles & Status Pill
  const header = document.createElement("div");
  header.className = "task-card-header";

  const titles = document.createElement("div");
  titles.className = "task-titles";

  const mainTitle = document.createElement("h3");
  mainTitle.className = "task-main-title";
  mainTitle.textContent = job.course_title || "未知课程";

  const subTitle = document.createElement("div");
  subTitle.className = "task-sub-title";
  subTitle.textContent = job.sub_title || "默认课时";

  titles.appendChild(mainTitle);
  titles.appendChild(subTitle);
  header.appendChild(titles);

  const headerMeta = document.createElement("div");
  headerMeta.className = "task-header-meta";

  const statusPill = document.createElement("span");
  statusPill.className = `task-status-pill status-${st}`;
  const progressState = taskProgress(job);

  // Honest intermediate states: cancelling and pausing must explicitly display ongoing transition
  statusPill.textContent =
    st === "cancelling"
      ? "正在取消..."
      : st === "pausing"
      ? "正在暂停..."
      : progressState.label;
  headerMeta.appendChild(statusPill);

  header.appendChild(headerMeta);
  card.appendChild(header);

  // Meta Row (Lecturer, Room, Details ID)
  const metaRow = document.createElement("div");
  metaRow.className = "task-meta-row";

  if (job.lecturer_name) {
    const item = document.createElement("span");
    item.className = "task-meta-item";
    item.textContent = `👨‍🏫 教师: ${job.lecturer_name}`;
    metaRow.appendChild(item);
  }

  if (job.room_name) {
    const item = document.createElement("span");
    item.className = "task-meta-item";
    item.textContent = `🏫 教室: ${job.room_name}`;
    metaRow.appendChild(item);
  }

  // ID is put in details so it does not clutter the main line
  const idDetails = document.createElement("details");
  idDetails.className = "task-details-id";
  const idSummary = document.createElement("summary");
  idSummary.textContent = "任务 ID";
  const idCode = document.createElement("code");
  idCode.textContent = job.job_id || "-";
  idDetails.appendChild(idSummary);
  idDetails.appendChild(idCode);
  metaRow.appendChild(idDetails);

  card.appendChild(metaRow);

  // Progress Bar & Stages
  const progressBox = document.createElement("div");
  progressBox.className = "task-progress-box";

  const progressHeader = document.createElement("div");
  progressHeader.className = "progress-header-row";

  const progressTitle = document.createElement("span");
  progressTitle.textContent = `阶段：${STAGE_LABELS[progressState.stage] || progressState.stage}`;
  progressHeader.appendChild(progressTitle);

  const progressPercentText = document.createElement("span");
  if (st === "completed") {
    progressPercentText.textContent = "100%";
  } else if (progressState.percent !== null) {
    progressPercentText.textContent = `${progressState.percent}%`;
  }
  progressHeader.appendChild(progressPercentText);
  progressBox.appendChild(progressHeader);

  const track = document.createElement("div");
  track.className =
    "progress-track" +
    (progressState.active && progressState.percent === null ? " indeterminate" : "") +
    ` progress-${st}`;
  track.setAttribute("role", "progressbar");
  track.setAttribute("aria-label", progressState.label);
  track.setAttribute("aria-valuemin", "0");
  track.setAttribute("aria-valuemax", "100");
  if (progressState.percent !== null) {
    track.setAttribute("aria-valuenow", String(progressState.percent));
  }
  const fill = document.createElement("div");
  fill.className = "progress-fill";
  fill.style.width = `${progressState.percent ?? 0}%`;
  track.appendChild(fill);
  progressBox.appendChild(track);

  // Stage steps row
  const stepsRow = document.createElement("div");
  stepsRow.className = "stage-steps-row";
  const order = ["preparing", "acquisition", "loading", "transcription", "exporting", "completed"];
  const stageLabels = ["准备", "获取音频", "加载模型", "转写", "导出", "完成"];
  const currentIndex = order.indexOf(progressState.stage);

  order.forEach((stepName, i) => {
    const span = document.createElement("span");
    span.className = `stage-step ${
      currentIndex > i ? "done" : currentIndex === i && progressState.active ? "current" : ""
    }`;
    span.textContent = `${currentIndex > i ? "✓ " : ""}${stageLabels[i]}`;
    stepsRow.appendChild(span);
  });
  progressBox.appendChild(stepsRow);
  card.appendChild(progressBox);

  // Metrics Grid (Separate message and last_activity so time is not obscured)
  const info = job.progress_info || {};
  const metricsGrid = document.createElement("div");
  metricsGrid.className = "task-metrics-grid";

  // 1. Audio Duration & Acquisition
  const mAudio = document.createElement("div");
  mAudio.className = "metric-item";
  const mAudioLbl = document.createElement("span");
  mAudioLbl.className = "metric-label";
  mAudioLbl.textContent = "音频进度";
  const mAudioVal = document.createElement("span");
  mAudioVal.className = "metric-value";

  const processedSec =
    info.stage === "acquisition" && typeof info.audio_seconds === "number"
      ? info.audio_seconds
      : typeof info.processed_seconds === "number"
      ? info.processed_seconds
      : typeof info.audio_seconds === "number"
      ? info.audio_seconds
      : null;
  const totalSec = typeof info.total_seconds === "number" ? info.total_seconds : null;

  let audioStr = "-";
  if (processedSec !== null && totalSec !== null) {
    audioStr = `${formatDuration(processedSec)} / ${formatDuration(totalSec)}`;
  } else if (processedSec !== null) {
    audioStr = formatDuration(processedSec);
  }
  if (info.speed) {
    audioStr += ` (${info.speed})`;
  }
  mAudioVal.textContent = audioStr;
  mAudio.appendChild(mAudioLbl);
  mAudio.appendChild(mAudioVal);
  metricsGrid.appendChild(mAudio);

  // 2. Elapsed Time
  const mElapsed = document.createElement("div");
  mElapsed.className = "metric-item";
  const mElapsedLbl = document.createElement("span");
  mElapsedLbl.className = "metric-label";
  mElapsedLbl.textContent = "已耗时";
  const mElapsedVal = document.createElement("span");
  mElapsedVal.className = "metric-value";
  mElapsedVal.textContent = typeof info.elapsed_seconds === "number" ? formatDuration(info.elapsed_seconds) : "-";
  mElapsed.appendChild(mElapsedLbl);
  mElapsed.appendChild(mElapsedVal);
  metricsGrid.appendChild(mElapsed);

  // 3. Reliable ETA (Never fabricate missing values; inactive states display "-")
  const mETA = document.createElement("div");
  mETA.className = "metric-item";
  const mETALbl = document.createElement("span");
  mETALbl.className = "metric-label";
  mETALbl.textContent = "预估剩余时间";
  const mETAVal = document.createElement("span");
  mETAVal.className = "metric-value";
  mETAVal.textContent = formatETA(st, info.eta_seconds);
  mETA.appendChild(mETALbl);
  mETA.appendChild(mETAVal);
  metricsGrid.appendChild(mETA);

  // 4. Latest Activity Timestamp (Separate from message!)
  const mLastTime = document.createElement("div");
  mLastTime.className = "metric-item";
  const mLastTimeLbl = document.createElement("span");
  mLastTimeLbl.className = "metric-label";
  mLastTimeLbl.textContent = "最近活动时间";
  const mLastTimeVal = document.createElement("span");
  mLastTimeVal.className = "metric-value";
  mLastTimeVal.textContent = info.last_activity || (st === "completed" ? "已完成" : "-");
  mLastTime.appendChild(mLastTimeLbl);
  mLastTime.appendChild(mLastTimeVal);
  metricsGrid.appendChild(mLastTime);

  // 5. Latest Message
  const mMsg = document.createElement("div");
  mMsg.className = "metric-item";
  const mMsgLbl = document.createElement("span");
  mMsgLbl.className = "metric-label";
  mMsgLbl.textContent = "最新进展";
  const mMsgVal = document.createElement("span");
  mMsgVal.className = "metric-value";
  mMsgVal.textContent = info.message || (st === "completed" ? "导出完毕" : st === "queued" ? "排队等待后台处理" : JOB_STATUS_LABELS[st] || st);
  mMsg.appendChild(mMsgLbl);
  mMsg.appendChild(mMsgVal);
  metricsGrid.appendChild(mMsg);

  card.appendChild(metricsGrid);

  // Hints & Alert Badges
  const hints = document.createElement("div");
  hints.className = "task-hints";

  if (job.audio_ready === true) {
    const hint = document.createElement("div");
    hint.className = "hint-tag hint-audio-ready";
    hint.textContent = "✓ 音频已保存就绪，可安全关闭同济课程页面";
    hints.appendChild(hint);
  } else if (job.page_required === true) {
    const hint = document.createElement("div");
    hint.className = "hint-tag hint-page-required";
    hint.textContent = "⚠️ 正在采集音频数据，请保持同济课程页面开启";
    hints.appendChild(hint);
  }

  if (st === "completed") {
    const hint = document.createElement("div");
    hint.className = "hint-tag hint-safe-leave";
    hint.textContent = "✓ 转写已完成，产物已就绪，可安全离开或关闭课程页面";
    hints.appendChild(hint);
  }

  if (st === "needs_refresh") {
    const hint = document.createElement("div");
    hint.className = "hint-tag hint-needs-refresh";
    hint.textContent = "⚠️ 课程流签名已过期，请在课程页刷新后重新提交";
    hints.appendChild(hint);
  }

  if (st === "failed") {
    const hint = document.createElement("div");
    hint.className = "hint-tag hint-error";
    hint.textContent = `❌ 失败原因: ${job.error_message || "未知执行错误"}`;
    hints.appendChild(hint);
  }

  if (hints.children.length > 0) {
    card.appendChild(hints);
  }

  // Action Buttons
  const actions = document.createElement("div");
  actions.className = "task-actions";

  // Helper to create buttons with data attributes for focus restoration
  function createActionButton(label, cls, actName, onClick) {
    const b = document.createElement("button");
    b.className = cls;
    b.textContent = label;
    b.setAttribute("data-job-id", job.job_id);
    b.setAttribute("data-action", actName);
    b.addEventListener("click", onClick);
    return b;
  }

  // 1. Running
  if (st === "running") {
    actions.appendChild(createActionButton("暂停", "btn btn-secondary", "pause", () => handleJobAction("pause", job.job_id)));
    actions.appendChild(createActionButton("取消", "btn btn-danger", "cancel", () => handleJobAction("cancel", job.job_id)));
  }

  // 2. Queued: supports pause & cancel
  else if (st === "queued") {
    actions.appendChild(createActionButton("暂停排队", "btn btn-secondary", "pause", () => handleJobAction("pause", job.job_id)));
    actions.appendChild(createActionButton("取消排队", "btn btn-danger", "cancel", () => handleJobAction("cancel", job.job_id)));
  }

  // 3. Pausing: can be cancelled
  else if (st === "pausing") {
    const btnP = document.createElement("button");
    btnP.className = "btn btn-secondary";
    btnP.disabled = true;
    btnP.textContent = "正在暂停...";
    actions.appendChild(btnP);

    actions.appendChild(createActionButton("取消任务", "btn btn-danger", "cancel", () => handleJobAction("cancel", job.job_id)));
  }

  // 4. Paused: can resume & cancel
  else if (st === "paused") {
    actions.appendChild(createActionButton("继续", "btn btn-primary", "resume", () => handleJobAction("resume", job.job_id)));
    actions.appendChild(createActionButton("取消", "btn btn-danger", "cancel", () => handleJobAction("cancel", job.job_id)));
  }

  // 5. Cancelling: honest state, cannot repeatedly cancel
  else if (st === "cancelling") {
    const btnC = document.createElement("button");
    btnC.className = "btn btn-danger";
    btnC.disabled = true;
    btnC.textContent = "正在取消...";
    actions.appendChild(btnC);
  }

  // 6. Cancelled: MUST have retry
  else if (st === "cancelled") {
    actions.appendChild(createActionButton("重试任务", "btn btn-primary", "retry", () => handleJobAction("retry", job.job_id)));
  }

  // 7. Failed: retry
  else if (st === "failed") {
    actions.appendChild(createActionButton("重试任务", "btn btn-primary", "retry", () => handleJobAction("retry", job.job_id)));
  }

  // 8. Needs Refresh
  else if (st === "needs_refresh") {
    actions.appendChild(
      createActionButton("打开课程页刷新", "btn btn-primary", "open-course", () => {
        if (job.source_url && chrome.tabs && chrome.tabs.create) {
          chrome.tabs.create({ url: job.source_url });
          showToast("已打开课程页面，请刷新页面后在扩展弹窗中重新提交");
        } else {
          showToast("未检测到课程原始页面地址，请在 Look 平台手动打开", true);
        }
      })
    );
  }

  // Completed specific actions
  if (st === "completed") {
    actions.appendChild(
      createActionButton("打开笔记", "btn btn-secondary", "open-note", () => {
        sendNativeAction({ action: "open_output", job_id: job.job_id, kind: "markdown" }).then((resp) => {
          if (!resp || resp.error || resp.status !== "ok") {
            showToast(`打开笔记失败: ${resp ? resp.error || "未知响应" : "服务未响应"}`, true);
          } else {
            showToast("已在本地打开 Markdown 笔记");
          }
        });
      })
    );

    actions.appendChild(
      createActionButton("打开目录", "btn btn-secondary", "open-folder", () => {
        sendNativeAction({ action: "open_output", job_id: job.job_id, kind: "folder" }).then((resp) => {
          if (!resp || resp.error || resp.status !== "ok") {
            showToast(`打开目录失败: ${resp ? resp.error || "未知响应" : "服务未响应"}`, true);
          } else {
            showToast("已打开产物目录");
          }
        });
      })
    );

    actions.appendChild(
      createActionButton("重新导出目录", "btn btn-secondary", "reexport", () => openExportModal(job.job_id, job.output_dir))
    );

    actions.appendChild(
      createActionButton("预览逐字稿", "btn btn-primary", "preview", () => openPreviewModal(job))
    );
  }

  // Inactive tasks (completed, failed, cancelled, paused) can clean cache if cache_bytes > 0
  const isInactive = st === "completed" || st === "failed" || st === "cancelled" || st === "paused";
  if (isInactive && typeof job.cache_bytes === "number" && job.cache_bytes > 0) {
    actions.appendChild(
      createActionButton(
        `清理缓存 (${formatBytes(job.cache_bytes)})`,
        "btn btn-secondary",
        "cleanup",
        () => openCleanupModal(job.job_id)
      )
    );
  }

  if (actions.children.length > 0) {
    card.appendChild(actions);
  }

  return card;
}

function handleJobAction(action, jobId) {
  sendNativeAction({ action, job_id: jobId }).then((resp) => {
    if (!resp || resp.error || resp.status !== "ok") {
      showToast(`操作失败: ${resp ? resp.error || "服务未返回成功响应" : "服务未响应"}`, true);
    } else {
      showToast("操作已执行");
      refreshTasksList();
    }
  });
}

// Transcript Preview Modal Logic (Replacing hash with #clip-t=...)
function openPreviewModal(job) {
  elements.previewModalTitle.textContent = `逐字稿预览: ${job.course_title || job.sub_title || job.job_id}`;
  elements.previewSegmentsContainer.textContent = "正在读取逐字稿片段...";
  showModalDialog(elements.previewModal, elements.btnClosePreview);

  sendNativeAction({ action: "preview", job_id: job.job_id }).then((resp) => {
    elements.previewSegmentsContainer.textContent = "";

    if (!resp || resp.error || resp.status !== "ok") {
      elements.previewSegmentsContainer.textContent = `读取逐字稿失败: ${
        resp ? resp.error || "服务未返回有效逐字稿" : "服务未响应"
      }`;
      return;
    }

    const segments = resp.segments || [];
    if (segments.length === 0) {
      elements.previewSegmentsContainer.textContent = "暂无可预览的逐字稿段落。";
      return;
    }

    if (resp.total_segments > segments.length) {
      const notice = document.createElement("p");
      notice.textContent = `当前预览前 ${segments.length} 段，共 ${resp.total_segments} 段；完整内容请点击“打开笔记”。`;
      elements.previewSegmentsContainer.appendChild(notice);
    }
    const sourceUrl = resp.source_url || job.source_url || "";

    segments.forEach((seg) => {
      const segItem = document.createElement("div");
      segItem.className = "segment-item";

      const segMeta = document.createElement("div");
      segMeta.className = "segment-meta";

      const timeBtn = document.createElement("button");
      timeBtn.className = "segment-time-btn";
      const startSec = typeof seg.start === "number" ? Math.floor(seg.start) : 0;
      const endSec = typeof seg.end === "number" ? Math.floor(seg.end) : 0;
      timeBtn.textContent = `⏱ ${formatDuration(startSec)} - ${formatDuration(endSec)}`;
      timeBtn.title = "点击定位原课程播放时间点";

      timeBtn.addEventListener("click", () => {
        // Point 2: strictly use #clip-t=${startSec} and replace hash cleanly
        const clipUrl = buildClipUrl(sourceUrl, startSec);
        if (clipUrl && typeof chrome !== "undefined" && chrome.tabs && chrome.tabs.create) {
          chrome.tabs.create({ url: clipUrl });
        } else {
          showToast(`已生成定位地址: ${clipUrl || "原视频时间点 " + formatDuration(startSec)}`);
        }
      });
      segMeta.appendChild(timeBtn);

      // Point 4: needs_review only recommends review, NEVER says accuracy percentage!
      if (seg.needs_review) {
        const reviewTag = document.createElement("span");
        reviewTag.className = "segment-review-tag";
        reviewTag.textContent = "建议复核";
        segMeta.appendChild(reviewTag);
      }

      segItem.appendChild(segMeta);

      const textEl = document.createElement("div");
      textEl.className = "segment-text";
      textEl.textContent = seg.text || "";
      segItem.appendChild(textEl);

      elements.previewSegmentsContainer.appendChild(segItem);
    });
  });
}

function closePreviewModal() {
  hideModalDialog(elements.previewModal);
}
elements.btnClosePreview.addEventListener("click", closePreviewModal);
elements.btnPreviewDone.addEventListener("click", closePreviewModal);

// Re-export Modal Logic
function openExportModal(jobId, currentOutputDir) {
  pendingActionJobId = jobId;
  elements.inputExportDir.value = currentOutputDir || "";
  elements.exportFeedback.textContent = "";
  showModalDialog(elements.exportModal, elements.inputExportDir);
}

function closeExportModal() {
  hideModalDialog(elements.exportModal);
  pendingActionJobId = null;
}

elements.btnCloseExport.addEventListener("click", closeExportModal);
elements.btnCancelExport.addEventListener("click", closeExportModal);

elements.btnConfirmExport.addEventListener("click", () => {
  if (!pendingActionJobId) return;
  const targetDir = elements.inputExportDir.value.trim();
  elements.btnConfirmExport.disabled = true;
  elements.btnConfirmExport.textContent = "正在导出...";

  sendNativeAction({
    action: "export",
    job_id: pendingActionJobId,
    output_dir: targetDir || null,
  }).then((resp) => {
    elements.btnConfirmExport.disabled = false;
    elements.btnConfirmExport.textContent = "执行导出";

    if (!resp || resp.error || resp.status !== "ok") {
      elements.exportFeedback.style.color = "var(--danger)";
      elements.exportFeedback.textContent = `导出失败: ${resp ? resp.error || "服务未返回成功响应" : "服务未响应"}`;
    } else {
      closeExportModal();
      showToast("重新导出成功！文件已生成至目标目录。");
      refreshTasksList();
    }
  });
});

// Cache Cleanup Modal Logic
function openCleanupModal(jobId) {
  pendingActionJobId = jobId;
  elements.cleanupFeedback.textContent = "";
  showModalDialog(elements.cleanupModal, elements.btnConfirmCleanup);
}

function closeCleanupModal() {
  hideModalDialog(elements.cleanupModal);
  pendingActionJobId = null;
}

elements.btnCloseCleanup.addEventListener("click", closeCleanupModal);
elements.btnCancelCleanup.addEventListener("click", closeCleanupModal);

elements.btnConfirmCleanup.addEventListener("click", () => {
  if (!pendingActionJobId) return;
  elements.btnConfirmCleanup.disabled = true;
  elements.btnConfirmCleanup.textContent = "正在清理...";

  sendNativeAction({
    action: "cleanup_cache",
    job_id: pendingActionJobId,
  }).then((resp) => {
    elements.btnConfirmCleanup.disabled = false;
    elements.btnConfirmCleanup.textContent = "确认清理缓存";

    if (!resp || resp.error || resp.status !== "ok") {
      elements.cleanupFeedback.style.color = "var(--danger)";
      elements.cleanupFeedback.textContent = `清理失败: ${resp ? resp.error || "服务未返回成功响应" : "服务未响应"}`;
    } else {
      closeCleanupModal();
      const freed = typeof resp.freed_bytes === "number" ? formatBytes(resp.freed_bytes) : "部分";
      showToast(`缓存清理完成，成功释放 ${freed} 磁盘空间`);
      refreshTasksList();
    }
  });
});

// Refresh Tasks List
function refreshTasksList() {
  if (isRefreshing) return;
  isRefreshing = true;

  sendNativeAction({ action: "list_jobs" })
    .then((resp) => {
      isRefreshing = false;
      const nowStr = new Date().toLocaleTimeString("zh-CN", { hour12: false });

      if (resp && Array.isArray(resp.jobs)) {
        allJobs = resp.jobs;
        elements.syncStatusIndicator.textContent = `最近同步: ${nowStr}`;
        elements.syncStatusIndicator.style.color = "";
        updateKPIStats(allJobs);
        renderFilteredTasks();
      } else if (resp && resp.error) {
        elements.syncStatusIndicator.textContent = `同步失败: ${resp.error}`;
        elements.syncStatusIndicator.style.color = "var(--danger)";
      } else {
        elements.syncStatusIndicator.textContent = "后台服务未连接，显示可能已过期";
        elements.syncStatusIndicator.style.color = "var(--danger)";
      }
    })
    .catch((err) => {
      isRefreshing = false;
      elements.syncStatusIndicator.textContent = `连接失败: ${err.message || String(err)}`;
      elements.syncStatusIndicator.style.color = "var(--danger)";
    });
}

elements.btnRefreshManual.addEventListener("click", () => {
  updateWorkerStatus();
  refreshTasksList();
});

// Visibility handling: 2 seconds refresh when visible, pause when hidden
function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(() => {
    if (!document.hidden && elements.paneTasks.classList.contains("active")) {
      refreshTasksList();
    }
  }, 2000);
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    refreshTasksList();
    updateWorkerStatus();
  }
});

// Initial boot
const initialTab = new URL(window.location.href).searchParams.get("tab");
if (initialTab === "settings") {
  switchTab("settings");
} else {
  switchTab("tasks");
}

loadSettings();
updateWorkerStatus();
refreshTasksList();
startPolling();
