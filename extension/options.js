const SERVICE = "http://127.0.0.1:8765";
const REQUEST_HEADERS = { "X-Tongji-Clipper": "1" };

const elements = {
  serviceChip: document.querySelector("#service-chip"),
  serviceLabel: document.querySelector("#service-label"),
  serviceVersion: document.querySelector("#service-version"),
  vaultPath: document.querySelector("#vault-path"),
  vaultMessage: document.querySelector("#vault-message"),
  selectVault: document.querySelector("#select-vault"),
  notesSubdir: document.querySelector("#notes-subdir"),
  model: document.querySelector("#model"),
  keepMedia: document.querySelector("#keep-media"),
  saveSettings: document.querySelector("#save-settings"),
  saveState: document.querySelector("#save-state"),
  testWrite: document.querySelector("#test-write"),
  testResult: document.querySelector("#test-result"),
  retryService: document.querySelector("#retry-service"),
  toast: document.querySelector("#toast")
};

let serviceOnline = false;
let currentSettings = null;
let toastTimer;

async function api(path, options = {}) {
  const headers = { ...REQUEST_HEADERS, ...(options.headers || {}) };
  if (options.body) headers["Content-Type"] = "application/json";
  const response = await fetch(`${SERVICE}${path}`, { ...options, headers });
  let payload;
  try {
    payload = await response.json();
  } catch (_) {
    payload = {};
  }
  if (!response.ok) throw new Error(payload.error || `本地服务返回 ${response.status}`);
  return payload;
}

function setServiceState(state, label) {
  elements.serviceChip.dataset.state = state;
  elements.serviceLabel.textContent = label;
}

function showToast(message, kind = "info") {
  window.clearTimeout(toastTimer);
  elements.toast.textContent = message;
  elements.toast.dataset.kind = kind;
  elements.toast.hidden = false;
  toastTimer = window.setTimeout(() => {
    elements.toast.hidden = true;
  }, 4200);
}

function setControlsEnabled(enabled) {
  elements.selectVault.disabled = !enabled;
  elements.notesSubdir.disabled = !enabled;
  elements.model.disabled = !enabled;
  elements.keepMedia.disabled = !enabled;
  elements.saveSettings.disabled = !enabled;
  elements.testWrite.disabled = !enabled || !currentSettings?.configured;
}

function renderSettings(settings) {
  currentSettings = settings;
  const path = settings.vault_dir || "尚未选择 Vault";
  elements.vaultPath.textContent = path;
  elements.vaultPath.title = path;
  elements.notesSubdir.value = settings.notes_subdir || "课程笔记";
  elements.model.value = settings.model || "small";
  elements.keepMedia.checked = Boolean(settings.keep_temporary_media);
  elements.vaultMessage.hidden = true;

  if (settings.vault_dir && !settings.vault_exists) {
    elements.vaultMessage.textContent = "这个文件夹已不存在，请重新选择 Vault。";
    elements.vaultMessage.hidden = false;
  } else if (settings.vault_dir && !settings.vault_writable) {
    elements.vaultMessage.textContent = "这个文件夹不可写，请检查权限或重新选择。";
    elements.vaultMessage.hidden = false;
  } else if (settings.vault_dir && !settings.looks_like_vault) {
    elements.vaultMessage.textContent = "未发现 .obsidian 文件夹。仍可使用，但请确认选择的是正确的 Vault。";
    elements.vaultMessage.hidden = false;
  }

  setServiceState(
    settings.configured ? "ready" : "checking",
    settings.configured ? "服务已就绪" : "等待完成配置"
  );
  setControlsEnabled(true);
}

async function refresh() {
  setServiceState("checking", "正在连接本地服务");
  elements.serviceVersion.textContent = "";
  serviceOnline = false;
  setControlsEnabled(false);
  try {
    const health = await api("/health");
    serviceOnline = true;
    elements.serviceVersion.textContent = `本地服务 v${health.version}`;
    renderSettings(await api("/settings"));
  } catch (error) {
    setServiceState("error", "本地服务未启动");
    elements.serviceVersion.textContent = "请运行 install.ps1 或 start.ps1";
    elements.saveState.textContent = "无法连接到 127.0.0.1:8765";
    showToast(error.message || "无法连接本地服务", "error");
  }
}

async function chooseVault() {
  if (!serviceOnline) return;
  elements.selectVault.disabled = true;
  elements.selectVault.textContent = "等待系统窗口…";
  elements.saveState.textContent = "请在弹出的窗口中选择文件夹";
  try {
    const settings = await api("/settings/select-vault", { method: "POST" });
    if (settings.cancelled) {
      elements.saveState.textContent = "已取消，原路径未更改";
    } else {
      renderSettings(settings);
      elements.saveState.textContent = "Vault 已保存";
      showToast("Obsidian Vault 已连接");
    }
  } catch (error) {
    showToast(error.message, "error");
    elements.saveState.textContent = "选择失败";
  } finally {
    elements.selectVault.textContent = "选择文件夹";
    elements.selectVault.disabled = false;
  }
}

async function saveSettings() {
  elements.saveSettings.disabled = true;
  elements.saveState.textContent = "正在保存…";
  try {
    const settings = await api("/settings", {
      method: "PUT",
      body: JSON.stringify({
        notes_subdir: elements.notesSubdir.value,
        model: elements.model.value,
        keep_temporary_media: elements.keepMedia.checked
      })
    });
    renderSettings(settings);
    elements.saveState.textContent = "设置已保存";
    showToast("设置已保存，新任务会立即使用这些设置");
  } catch (error) {
    elements.saveState.textContent = "保存失败";
    showToast(error.message, "error");
  } finally {
    elements.saveSettings.disabled = !serviceOnline;
  }
}

async function testWrite() {
  elements.testWrite.disabled = true;
  elements.testWrite.textContent = "正在写入…";
  elements.testResult.hidden = true;
  try {
    const result = await api("/settings/test-write", { method: "POST" });
    elements.testResult.textContent = `测试成功：${result.output}`;
    elements.testResult.hidden = false;
    showToast("测试笔记已写入 Obsidian Vault");
  } catch (error) {
    elements.testResult.textContent = `测试失败：${error.message}`;
    elements.testResult.hidden = false;
    showToast(error.message, "error");
  } finally {
    elements.testWrite.textContent = "生成测试笔记";
    elements.testWrite.disabled = !currentSettings?.configured;
  }
}

function markDirty() {
  if (serviceOnline) elements.saveState.textContent = "有未保存的更改";
}

elements.selectVault.addEventListener("click", chooseVault);
elements.saveSettings.addEventListener("click", saveSettings);
elements.testWrite.addEventListener("click", testWrite);
elements.retryService.addEventListener("click", refresh);
elements.notesSubdir.addEventListener("input", markDirty);
elements.model.addEventListener("change", markDirty);
elements.keepMedia.addEventListener("change", markDirty);

refresh();
