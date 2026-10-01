/**
 * Extension Background Service Worker
 * Bridges popup requests with Chrome Native Messaging Host "com.tongji.clipper"
 */

const NATIVE_HOST_NAME = "com.tongji.clipper";

function sendToNativeHost(message) {
  return new Promise((resolve) => {
    chrome.runtime.sendNativeMessage(NATIVE_HOST_NAME, message, (response) => {
      if (chrome.runtime.lastError) {
        resolve({
          error: `Native Messaging 通信错误: ${chrome.runtime.lastError.message}。请确认已运行注册脚本并安装 Python 依赖。`,
        });
      } else {
        resolve(response || { error: "Native host returned empty response" });
      }
    });
  });
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === "NATIVE_ACTION") {
    sendToNativeHost(message.payload).then((resp) => {
      sendResponse(resp);
    });
    return true; // Keep channel open for async response
  }
});
