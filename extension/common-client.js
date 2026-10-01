/**
 * Shared Client Utilities for Tongji Clipper Extension UI (Popup & Dashboard)
 */

const PRESET_MODELS = ["small", "base", "tiny", "medium"];

const JOB_STATUS_LABELS = {
  queued: "排队等待",
  running: "处理中",
  cancelling: "正在取消...",
  cancelled: "已取消",
  pausing: "正在暂停...",
  paused: "已暂停",
  completed: "已完成",
  failed: "失败",
  needs_refresh: "等待刷新课程地址",
};

const STAGE_LABELS = {
  preparing: "准备任务",
  acquisition: "获取音频",
  loading: "加载模型",
  transcription: "转写中",
  exporting: "导出与核验",
  completed: "已完成",
};

const PROFILE_LABELS = {
  balanced: "平衡模式（推荐）",
  low: "节能模式（低占用）",
  fast: "高速模式（性能优先）",
};

function isPathModel(modelStr) {
  if (!modelStr || typeof modelStr !== "string") return false;
  const s = modelStr.trim();
  if (PRESET_MODELS.includes(s)) return false;
  // Contains directory separators or starts with Windows drive letter / Unix root
  return /[\\/]/.test(s) || /^[a-zA-Z]:/.test(s);
}

function resolveSettings(saved = {}, defaults = {}) {
  // Determine modelSource
  let modelSource = "preset";
  if (saved.modelSource === "preset" || saved.modelSource === "custom") {
    modelSource = saved.modelSource;
  } else if (saved.customModelPath && saved.customModelPath.trim()) {
    modelSource = "custom";
  } else if (isPathModel(saved.model)) {
    modelSource = "custom";
  } else if (saved.model && PRESET_MODELS.includes(saved.model)) {
    modelSource = "preset";
  } else if (defaults.model && isPathModel(defaults.model)) {
    modelSource = "custom";
  } else {
    modelSource = "preset";
  }

  // Determine preset model
  let model = "small";
  if (saved.model && PRESET_MODELS.includes(saved.model)) {
    model = saved.model;
  } else if (defaults.model && PRESET_MODELS.includes(defaults.model)) {
    model = defaults.model;
  }

  // Determine customModelPath
  let customModelPath = "";
  if (saved.customModelPath !== undefined && saved.customModelPath !== null) {
    customModelPath = String(saved.customModelPath).trim();
  } else if (isPathModel(saved.model)) {
    customModelPath = String(saved.model).trim();
  } else if (defaults.model && isPathModel(defaults.model)) {
    customModelPath = String(defaults.model).trim();
  }

  // Environment and booleans (preserving explicit false values)
  const isOffline =
    typeof saved.isOffline === "boolean"
      ? saved.isOffline
      : typeof defaults.offline === "boolean"
      ? defaults.offline
      : false;

  const outputDir =
    typeof saved.outputDir === "string"
      ? saved.outputDir
      : typeof defaults.output_dir === "string"
      ? defaults.output_dir
      : "";

  const keepAudio =
    typeof saved.keepAudio === "boolean"
      ? saved.keepAudio
      : typeof defaults.keep_audio === "boolean"
      ? defaults.keep_audio
      : false;

  const resourceProfile =
    saved.resourceProfile || defaults.resource_profile || "balanced";

  // Compute effective actual model and validation
  let actualModel = null;
  let isValid = true;
  let validationError = "";

  if (modelSource === "custom") {
    if (!customModelPath) {
      isValid = false;
      validationError = "请指定本地自定义模型路径";
    } else {
      actualModel = customModelPath;
    }
  } else {
    actualModel = model;
  }

  return {
    modelSource,
    model,
    customModelPath,
    isOffline,
    outputDir,
    keepAudio,
    resourceProfile,
    actualModel,
    isValid,
    validationError,
  };
}

function loadAndInitializeSettings() {
  return new Promise((resolve) => {
    if (typeof chrome === "undefined" || !chrome.storage || !chrome.storage.local) {
      resolve(resolveSettings({}, {}));
      return;
    }

    chrome.storage.local.get(
      [
        "modelSource",
        "model",
        "customModelPath",
        "isOffline",
        "outputDir",
        "keepAudio",
        "resourceProfile",
        "settings_initialized",
      ],
      (saved) => {
        // If already initialized with valid settings, return resolved settings
        if (saved && saved.settings_initialized) {
          resolve(resolveSettings(saved, {}));
          return;
        }

        // Query system_info from backend to fetch first-time defaults
        sendNativeAction({ action: "system_info" })
          .then((sysInfo) => {
            if (!sysInfo || sysInfo.status !== "ok" || !sysInfo.defaults) {
              resolve({ ...resolveSettings(saved || {}, {}), isValid: false,
                validationError: "无法读取后台默认设置，请连接服务后刷新，或在设置中明确保存配置。" });
              return;
            }
            const defaults = sysInfo.defaults;
            const resolved = resolveSettings(saved || {}, defaults);

            // Persist initialized defaults so popup & dashboard share identical configuration
            chrome.storage.local.set(
              {
                modelSource: resolved.modelSource,
                model: resolved.model,
                customModelPath: resolved.customModelPath,
                isOffline: resolved.isOffline,
                outputDir: resolved.outputDir,
                keepAudio: resolved.keepAudio,
                resourceProfile: resolved.resourceProfile,
                settings_initialized: true,
              },
              () => {
                resolve(resolved);
              }
            );
          })
          .catch(() => {
            const fallback = resolveSettings(saved || {}, {});
            resolve({ ...fallback, isValid: false,
              validationError: "无法读取后台默认设置，请连接服务后刷新。" });
          });
      }
    );
  });
}

function buildClipUrl(sourceUrl, startSec) {
  if (!sourceUrl || typeof sourceUrl !== "string") return "";
  const sec = Math.max(0, Math.floor(startSec || 0));
  try {
    const u = new URL(sourceUrl);
    u.hash = `clip-t=${sec}`;
    return u.toString();
  } catch (e) {
    const base = sourceUrl.split("#")[0];
    return `${base}#clip-t=${sec}`;
  }
}

function formatDuration(seconds) {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) {
    return "-";
  }
  const s = Math.floor(seconds);
  const hrs = Math.floor(s / 3600);
  const mins = Math.floor((s % 3600) / 60);
  const secs = s % 60;
  if (hrs > 0) {
    return `${String(hrs).padStart(2, "0")}:${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
  }
  return `${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
}

function formatBytes(bytes) {
  if (typeof bytes !== "number" || !Number.isFinite(bytes) || bytes <= 0) {
    return "0 B";
  }
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let val = bytes;
  while (val >= 1024 && i < units.length - 1) {
    val /= 1024;
    i++;
  }
  return `${val.toFixed(val >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function taskProgress(job) {
  const info = (job && job.progress_info) || {};
  const status = (job && job.status) || "queued";
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
        ? STAGE_LABELS[stage] || "处理中"
        : JOB_STATUS_LABELS[status] || status,
  };
}

function sendNativeAction(payload) {
  return new Promise((resolve) => {
    if (typeof chrome === "undefined" || !chrome.runtime || !chrome.runtime.sendMessage) {
      resolve({ error: "Chrome runtime messaging not available" });
      return;
    }
    chrome.runtime.sendMessage({ type: "NATIVE_ACTION", payload }, (resp) => {
      if (chrome.runtime.lastError) {
        resolve({ error: chrome.runtime.lastError.message });
      } else {
        resolve(resp || { error: "Empty response" });
      }
    });
  });
}

if (typeof globalThis !== "undefined") {
  globalThis.PRESET_MODELS = PRESET_MODELS;
  globalThis.JOB_STATUS_LABELS = JOB_STATUS_LABELS;
  globalThis.STAGE_LABELS = STAGE_LABELS;
  globalThis.PROFILE_LABELS = PROFILE_LABELS;
  globalThis.isPathModel = isPathModel;
  globalThis.resolveSettings = resolveSettings;
  globalThis.loadAndInitializeSettings = loadAndInitializeSettings;
  globalThis.buildClipUrl = buildClipUrl;
  globalThis.formatDuration = formatDuration;
  globalThis.formatBytes = formatBytes;
  globalThis.taskProgress = taskProgress;
  globalThis.sendNativeAction = sendNativeAction;
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    PRESET_MODELS,
    JOB_STATUS_LABELS,
    STAGE_LABELS,
    PROFILE_LABELS,
    isPathModel,
    resolveSettings,
    loadAndInitializeSettings,
    buildClipUrl,
    formatDuration,
    formatBytes,
    taskProgress,
    sendNativeAction,
  };
}
