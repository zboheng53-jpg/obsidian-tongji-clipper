/**
 * Content Script: Runs in isolated world on https://look.tongji.edu.cn/*
 * Handles DOM fallback, metadata merging, timestamp seeking #clip-t=, and popup messaging.
 */

(function () {
  let interceptedData = null;
  let boundMediaIdentity = null;

  // On initialization, if a video is already playing/ready, bind it to initial URL params
  const initialParams = parseUrlParams();
  let observedIdentity = initialParams;
  let initialDocument = true;
  const initialVideo = document.querySelector("video");
  if (initialVideo && (initialVideo.currentSrc || initialVideo.src)) {
    boundMediaIdentity = { ...initialParams };
  }

  // 1. Parse course_id, sub_id, tenant_code from URL
  function parseUrlParams() {
    const url = new URL(window.location.href);
    const course_id = url.searchParams.get("course_id") || url.searchParams.get("courseId") || "";
    const sub_id = url.searchParams.get("sub_id") || url.searchParams.get("subId") || "";
    const tenant_code = url.searchParams.get("tenant_code") || "tongji";

    return {
      course_id: String(course_id),
      sub_id: String(sub_id),
      tenant_code: String(tenant_code),
    };
  }

  function isIdentityMatch(idA, idB) {
    if (!idA || !idB) return false;
    const cA = String(idA.course_id || "");
    const cB = String(idB.course_id || "");
    const sA = String(idA.sub_id || "");
    const sB = String(idB.sub_id || "");
    const tA = String(idA.tenant_code || "");
    const tB = String(idB.tenant_code || "");
    if (cA && cB && cA !== cB) return false;
    if (sA && sB && sA !== sB) return false;
    if (tA && tB && tA !== tB) return false;
    return true;
  }

  function purgeMismatchedInterceptedData() {
    const current = parseUrlParams();
    if (!isIdentityMatch(observedIdentity, current)) {
      initialDocument = false;
      observedIdentity = current;
      interceptedData = null;
      boundMediaIdentity = null;
    }
    if (interceptedData && !isIdentityMatch(interceptedData, current)) {
      interceptedData = null;
    }
    if (boundMediaIdentity && !isIdentityMatch(boundMediaIdentity, current)) {
      boundMediaIdentity = null;
    }
  }

  // 2. Listen for messages from MAIN world injected.js
  window.addEventListener("message", function (event) {
    if (event.source !== window) return;
    if (event.origin !== window.location.origin) return;
    if (!event.data || event.data.source !== "TONGJI_CLIPPER_MAIN") return;

    if (event.data.type === "COURSE_NAVIGATED") {
      // A route notification may arrive after another navigation; either change
      // invalidates the initial-document DOM fallback.
      if (!isIdentityMatch(event.data.identity, observedIdentity)) {
        initialDocument = false;
        interceptedData = null;
        boundMediaIdentity = null;
      }
      purgeMismatchedInterceptedData();
      return;
    }

    if (event.data.type === "SUB_INFO_CAPTURED") {
      purgeMismatchedInterceptedData();
      const payload = event.data.payload;
      if (!payload) return;
      const current = parseUrlParams();
      const reqId = event.data.requestIdentity;

      // Reject late or out-of-order responses if request identity does not match current URL
      if (reqId && !isIdentityMatch(reqId, current)) {
        return;
      }
      // Also reject if payload identity does not match current URL
      if (!isIdentityMatch(payload, current)) {
        return;
      }

      interceptedData = payload;
      boundMediaIdentity = { ...current };
    }
  });

  window.addEventListener("popstate", purgeMismatchedInterceptedData);
  window.addEventListener("hashchange", purgeMismatchedInterceptedData);

  // 3. Build stable source_url with query parameters
  function getStableSourceUrl() {
    const params = parseUrlParams();
    const origin = window.location.origin;
    return `${origin}/livingroom?course_id=${encodeURIComponent(params.course_id)}&sub_id=${encodeURIComponent(params.sub_id)}&tenant_code=${encodeURIComponent(params.tenant_code)}`;
  }

  // 4. Merge API metadata with DOM video stream
  function getBestCapture() {
    purgeMismatchedInterceptedData();
    const params = parseUrlParams();
    const stableUrl = getStableSourceUrl();

    const video = document.querySelector("video");
    const videoSrc = video ? (video.currentSrc || video.src || "") : "";
    if (!boundMediaIdentity && initialDocument && videoSrc.startsWith("https://")) {
      boundMediaIdentity = { ...params };
    }

    const titleEl = document.querySelector(".course_name, .course-title");
    const domTitle = titleEl ? titleEl.textContent.replace(/\s+/g, ' ').replace(/^回放\s*/, '').trim() : document.title;

    let streams = [];
    if (interceptedData && Array.isArray(interceptedData.media_streams) && interceptedData.media_streams.length > 0) {
      streams = interceptedData.media_streams.map((s, i) => ({
        stream_id: i === 0 ? 'stream_main' : String(s.stream_id),
        name: String(s.name || '画面'),
        signed_url: String(s.signed_url || ''),
        time_offset: Number(s.time_offset || 0),
      }));
    } else if (boundMediaIdentity && isIdentityMatch(boundMediaIdentity, params) && videoSrc && videoSrc.startsWith("https://")) {
      // DOM fallback is strictly allowed ONLY if boundMediaIdentity matches current course identity
      streams.push({
        stream_id: "stream_main",
        name: "当前播放画面 (DOM)",
        signed_url: videoSrc,
        time_offset: 0.0,
      });
    }

    return {
      schema_version: 1,
      tenant_code: params.tenant_code || "tongji",
      course_id: params.course_id || "course",
      sub_id: params.sub_id || "sub",
      source_url: stableUrl,
      course_title: (interceptedData && interceptedData.course_title) || domTitle || document.title,
      sub_title: (interceptedData && interceptedData.sub_title) || document.title,
      lecturer_name: (interceptedData && interceptedData.lecturer_name) || null,
      room_name: (interceptedData && interceptedData.room_name) || null,
      media_streams: streams,
    };
  }

  // 5. Timestamp seeking: #clip-t=seconds
  function parseClipTime() {
    const hash = window.location.hash || "";
    const match = hash.match(/#clip-t=([0-9]+(?:\.[0-9]+)?)/);
    return match ? parseFloat(match[1]) : null;
  }

  function seekVideoToTime(targetSeconds) {
    if (targetSeconds === null || isNaN(targetSeconds)) return;

    function applySeek(video) {
      if (video.readyState >= 1) {
        video.currentTime = targetSeconds;
        video.play().catch(() => {});
      } else {
        video.addEventListener(
          "loadedmetadata",
          function () {
            video.currentTime = targetSeconds;
            video.play().catch(() => {});
          },
          { once: true }
        );
      }
    }

    const video = document.querySelector("video");
    if (video) {
      applySeek(video);
    } else {
      let attempts = 0;
      const interval = setInterval(() => {
        attempts++;
        const v = document.querySelector("video");
        if (v) {
          clearInterval(interval);
          applySeek(v);
        } else if (attempts > 20) {
          clearInterval(interval);
        }
      }, 300);
    }
  }

  function checkAndSeekHash() {
    const t = parseClipTime();
    if (t !== null) {
      seekVideoToTime(t);
    }
  }

  window.addEventListener("hashchange", checkAndSeekHash);
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", checkAndSeekHash);
  } else {
    checkAndSeekHash();
  }

  // 6. Handle messages from popup
  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message.type === "GET_CAPTURE_STATE") {
      const video = document.querySelector("video");
      const capture = getBestCapture();
      const hasStreams = capture.media_streams && capture.media_streams.length > 0;

      sendResponse({
        isReady: hasStreams,
        hasVideoElement: !!video,
        capture: capture,
        url: window.location.href,
      });
      return true;
    }

    if (message.type === "SEEK_VIDEO") {
      seekVideoToTime(Number(message.seconds));
      sendResponse({ success: true });
      return true;
    }
  });
})();
