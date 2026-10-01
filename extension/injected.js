/**
 * MAIN World Script: Runs directly in target page context at document_start.
 * Intercepts /courseapi/v3/portal-home-setting/get-sub-info.
 * STRICT SECURITY: Extracts ONLY whitelisted metadata and preview_url/play_url.
 * NEVER forwards Authorization, tokens, or cookies.
 */

(function () {
  if (window.__TONGJI_CLIPPER_INJECTED__) return;
  window.__TONGJI_CLIPPER_INJECTED__ = true;

  const TARGET_API_PATH = "/courseapi/v3/portal-home-setting/get-sub-info";

  function isMatchingApiPath(inputUrl) {
    if (!inputUrl) return false;
    try {
      const u = new URL(inputUrl, window.location.origin);
      return u.origin === window.location.origin && u.pathname === TARGET_API_PATH;
    } catch (e) {
      return false;
    }
  }

  function capturePageIdentity() {
    try {
      const u = new URL(window.location.href);
      return {
        course_id: u.searchParams.get("course_id") || u.searchParams.get("courseId") || "",
        sub_id: u.searchParams.get("sub_id") || u.searchParams.get("subId") || "",
        tenant_code: u.searchParams.get("tenant_code") || "tongji",
      };
    } catch (e) {
      return { course_id: "", sub_id: "", tenant_code: "tongji" };
    }
  }

  // pushState/replaceState do not emit popstate. Notify only after the native
  // operation succeeds, preserving its return value and exception behavior.
  if (window.history) {
    for (const name of ["pushState", "replaceState"]) {
      const original = window.history[name];
      if (typeof original !== "function") continue;
      window.history[name] = function (...args) {
        const result = original.apply(this, args);
        window.postMessage({source: "TONGJI_CLIPPER_MAIN", type: "COURSE_NAVIGATED", identity: capturePageIdentity()}, window.location.origin);
        return result;
      };
    }
  }

  function extractRequestIdentity(inputUrl) {
    if (!inputUrl) return null;
    try {
      const u = new URL(inputUrl, window.location.origin);
      const params = u.searchParams;
      return {
        course_id: params.get("course_id") || params.get("courseId") || "",
        sub_id: params.get("sub_id") || params.get("subId") || "",
        tenant_code: params.get("tenant_code") || "",
      };
    } catch (e) {
      return null;
    }
  }

  function buildEffectiveRequestIdentity(inputUrl, pageIdentity) {
    const fromUrl = extractRequestIdentity(inputUrl) || {};
    const page = pageIdentity || capturePageIdentity();
    return {
      course_id: fromUrl.course_id || page.course_id || "",
      sub_id: fromUrl.sub_id || page.sub_id || "",
      tenant_code: fromUrl.tenant_code || page.tenant_code || "tongji",
    };
  }

  function parseSubInfoData(data, reqIdentity) {
    if (!data || typeof data !== "object") return null;
    const body = data.data || data;

    const respCourseId = body.course_id ? String(body.course_id) : "";
    const respSubId = (body.sub_id || body.id) ? String(body.sub_id || body.id) : "";
    const respTenantCode = body.tenant_code ? String(body.tenant_code) : "";

    const reqCourseId = (reqIdentity && reqIdentity.course_id) ? String(reqIdentity.course_id) : "";
    const reqSubId = (reqIdentity && reqIdentity.sub_id) ? String(reqIdentity.sub_id) : "";
    const reqTenantCode = (reqIdentity && reqIdentity.tenant_code) ? String(reqIdentity.tenant_code) : "";

    // If both response body and request identity have IDs and they conflict, reject to avoid masking errors
    if (respCourseId && reqCourseId && respCourseId !== reqCourseId) {
      return null;
    }
    if (respSubId && reqSubId && respSubId !== reqSubId) {
      return null;
    }
    if (respTenantCode && reqTenantCode && respTenantCode !== reqTenantCode) {
      return null;
    }

    // Response body identity is primary; fallback to request identity captured before network call
    const courseId = respCourseId || reqCourseId;
    const subId = respSubId || reqSubId;
    const tenantCode = respTenantCode || reqTenantCode || "tongji";

    // Normalize video_list (can be Array or Object with numeric keys)
    let rawList = [];
    if (Array.isArray(body.video_list)) {
      rawList = body.video_list;
    } else if (body.video_list && typeof body.video_list === "object") {
      rawList = Object.values(body.video_list);
    }

    const streams = rawList
      .map((item, idx) => {
        if (!item || typeof item !== "object") return null;
        // Look platform actual field is preview_url, fallback to play_url / video_url
        const signed_url = item.preview_url || item.play_url || item.video_url || item.url || "";
        if (!signed_url) return null;
        return {
          stream_id: String(item.id || item.stream_id || `stream_${idx}`),
          name: item.name || item.title || `画面 ${idx + 1}`,
          signed_url: signed_url,
          time_offset: Number(item.time_offset || 0),
        };
      })
      .filter(Boolean);

    // Whitelist only metadata (no credentials, no cookies)
    return {
      schema_version: 1,
      tenant_code: tenantCode,
      course_id: courseId,
      sub_id: subId,
      course_title: body.course_title || body.course_name || null,
      sub_title: body.sub_name || body.sub_title || body.title || null,
      lecturer_name: body.teacher_name || body.lecturer_name || null,
      room_name: body.room_name || body.classroom || null,
      media_streams: streams,
    };
  }

  function handleInterceptedData(jsonData, reqIdentity) {
    try {
      const parsed = parseSubInfoData(jsonData, reqIdentity);
      if (parsed && (parsed.course_id || parsed.sub_id)) {
        window.postMessage(
          {
            source: "TONGJI_CLIPPER_MAIN",
            type: "SUB_INFO_CAPTURED",
            payload: parsed,
            requestIdentity: reqIdentity || {
              course_id: parsed.course_id,
              sub_id: parsed.sub_id,
              tenant_code: parsed.tenant_code,
            },
          },
          window.location.origin
        );
      }
    } catch (e) {
      // Ignore parse failures
    }
  }

  // 1. Intercept fetch
  const originalFetch = window.fetch;
  window.fetch = async function (...args) {
    const url = (args[0] && typeof args[0] === "string") ? args[0] : (args[0]?.url || "");
    const isTarget = isMatchingApiPath(url);
    // CRITICAL: Capture page identity synchronously BEFORE awaiting network fetch!
    const reqIdentity = isTarget ? buildEffectiveRequestIdentity(url, capturePageIdentity()) : null;

    const response = await originalFetch.apply(this, args);
    try {
      if (isTarget && reqIdentity) {
        const cloned = response.clone();
        cloned.json().then(data => handleInterceptedData(data, reqIdentity)).catch(() => {});
      }
    } catch (err) {}
    return response;
  };

  // 2. Intercept XMLHttpRequest (safe for responseType=json and text)
  const originalXhrOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    this._interceptUrl = String(url);
    if (isMatchingApiPath(this._interceptUrl)) {
      this._reqIdentity = buildEffectiveRequestIdentity(this._interceptUrl, capturePageIdentity());
    } else {
      this._reqIdentity = null;
    }
    return originalXhrOpen.apply(this, [method, url, ...rest]);
  };

  const originalXhrSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function (...args) {
    if (this._interceptUrl && isMatchingApiPath(this._interceptUrl)) {
      const reqIdentity = this._reqIdentity;
      this.addEventListener("load", function () {
        try {
          let data = null;
          if (this.responseType === "json" && this.response) {
            data = this.response;
          } else if (!this.responseType || this.responseType === "text") {
            data = JSON.parse(this.responseText);
          }
          if (data) {
            handleInterceptedData(data, reqIdentity);
          }
        } catch (e) {
          // Prevent responseText access InvalidStateError or JSON parse error
        }
      });
    }
    return originalXhrSend.apply(this, args);
  };
})();
