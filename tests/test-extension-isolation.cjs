const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

(async () => {
  // Test 1: 页内切课 (In-page course switch isolates media and purges stale streams)
  {
    const listeners = {};
    let currentHref = 'https://look.tongji.edu.cn/livingroom?course_id=100&sub_id=200&tenant_code=tongji';
    const location = {
      get href() { return currentHref; },
      set href(v) { currentHref = v; },
      get origin() { return new URL(currentHref).origin; },
      get search() { return new URL(currentHref).search; },
      get hash() { return new URL(currentHref).hash; },
    };

    let onMessage;
    const body200 = {
      data: {
        course_id: '100',
        sub_id: '200',
        tenant_code: 'tongji',
        course_title: '第200讲',
        video_list: [{ id: 'v200', preview_url: 'https://look.tongji.edu.cn/play/v200.mp4' }],
      },
    };
    const body201 = {
      data: {
        course_id: '100',
        sub_id: '201',
        tenant_code: 'tongji',
        course_title: '第201讲',
        video_list: [{ id: 'v201', preview_url: 'https://look.tongji.edu.cn/play/v201.mp4' }],
      },
    };

    const mockVideo = { currentSrc: 'https://look.tongji.edu.cn/play/v200.mp4', src: 'https://look.tongji.edu.cn/play/v200.mp4' };
    const document = {
      readyState: 'complete',
      title: '同济课程',
      querySelector: (sel) => (sel === 'video' ? mockVideo : null),
    };
    const window = {
      location,
      addEventListener: (type, fn) => {
        if (!listeners[type]) listeners[type] = [];
        listeners[type].push(fn);
      },
      postMessage: data => {
        (listeners.message || []).forEach(fn => fn({ source: window, origin: location.origin, data }));
      },
      fetch: async (url) => ({
        clone: () => ({
          json: async () => (String(url).includes('sub_id=201') ? body201 : body200),
        }),
      }),
    };
    function Xhr() {}
    Xhr.prototype.open = () => {};
    Xhr.prototype.send = () => {};

    const context = vm.createContext({
      window,
      document,
      chrome: { runtime: { onMessage: { addListener: fn => (onMessage = fn) } } },
      URL,
      URLSearchParams,
      XMLHttpRequest: Xhr,
      console,
      setInterval,
      clearInterval,
    });

    vm.runInContext(fs.readFileSync('extension/content.js', 'utf8'), context);
    vm.runInContext(fs.readFileSync('extension/injected.js', 'utf8'), context);

    // Fetch sub 200
    await window.fetch('/courseapi/v3/portal-home-setting/get-sub-info?course_id=100&sub_id=200&tenant_code=tongji');
    await new Promise(resolve => setImmediate(resolve));

    let res200;
    onMessage({ type: 'GET_CAPTURE_STATE' }, {}, v => (res200 = v));
    assert.equal(res200.isReady, true);
    assert.equal(res200.capture.sub_id, '200');
    assert.equal(res200.capture.media_streams[0].signed_url, 'https://look.tongji.edu.cn/play/v200.mp4');

    // User navigates in-page to sub 201 BEFORE sub 201 stream arrives
    currentHref = 'https://look.tongji.edu.cn/livingroom?course_id=100&sub_id=201&tenant_code=tongji';
    (listeners.popstate || []).forEach(fn => fn());

    let resSwitch;
    onMessage({ type: 'GET_CAPTURE_STATE' }, {}, v => (resSwitch = v));
    // Old media streams from sub 200 MUST NOT be merged with sub 201!
    assert.equal(resSwitch.capture.sub_id, '201');
    assert.equal(resSwitch.isReady, false);
    assert.equal(resSwitch.capture.media_streams.length, 0);

    // Fetch sub 201
    await window.fetch('/courseapi/v3/portal-home-setting/get-sub-info?course_id=100&sub_id=201&tenant_code=tongji');
    await new Promise(resolve => setImmediate(resolve));

    let res201;
    onMessage({ type: 'GET_CAPTURE_STATE' }, {}, v => (res201 = v));
    assert.equal(res201.isReady, true);
    assert.equal(res201.capture.sub_id, '201');
    assert.equal(res201.capture.media_streams[0].signed_url, 'https://look.tongji.edu.cn/play/v201.mp4');
    console.log('✓ Test passed: In-page course switch purges old streams and isolates new course');
  }

  // Test 2: 乱序 / 迟到响应 (Out-of-order response for old course is rejected)
  {
    const listeners = {};
    let currentHref = 'https://look.tongji.edu.cn/livingroom?course_id=100&sub_id=205&tenant_code=tongji';
    const location = {
      get href() { return currentHref; },
      get origin() { return new URL(currentHref).origin; },
      get search() { return new URL(currentHref).search; },
      get hash() { return new URL(currentHref).hash; },
    };

    let onMessage;
    const document = { readyState: 'complete', title: '同济课程', querySelector: () => null };
    const window = {
      location,
      addEventListener: (type, fn) => {
        if (!listeners[type]) listeners[type] = [];
        listeners[type].push(fn);
      },
      postMessage: data => {
        (listeners.message || []).forEach(fn => fn({ source: window, origin: location.origin, data }));
      },
    };
    function Xhr() {}
    Xhr.prototype.open = () => {};
    Xhr.prototype.send = () => {};

    const context = vm.createContext({
      window,
      document,
      chrome: { runtime: { onMessage: { addListener: fn => (onMessage = fn) } } },
      URL,
      URLSearchParams,
      XMLHttpRequest: Xhr,
      console,
      setInterval,
      clearInterval,
    });

    vm.runInContext(fs.readFileSync('extension/content.js', 'utf8'), context);

    // Stale response from older lesson 204 arrives late
    const staleData = {
      source: 'TONGJI_CLIPPER_MAIN',
      type: 'SUB_INFO_CAPTURED',
      requestIdentity: { course_id: '100', sub_id: '204', tenant_code: 'tongji' },
      payload: {
        schema_version: 1,
        course_id: '100',
        sub_id: '204',
        tenant_code: 'tongji',
        media_streams: [{ stream_id: 's204', signed_url: 'https://look.tongji.edu.cn/play/v204.mp4' }],
      },
    };

    (listeners.message || []).forEach(fn => fn({ source: window, origin: location.origin, data: staleData }));

    let resStale;
    onMessage({ type: 'GET_CAPTURE_STATE' }, {}, v => (resStale = v));
    // Stale response must be rejected: sub_id is 205, streams must NOT contain 204
    assert.equal(resStale.capture.sub_id, '205');
    assert.equal(resStale.isReady, false);
    assert.equal(resStale.capture.media_streams.length, 0);
    console.log('✓ Test passed: Stale out-of-order async response rejected');
  }

  // Test 3: 提交前切课 (In-page switch before submit refreshes and validates snapshot)
  {
    let submitActionSent = null;
    let messageBarText = '';
    let captureInTab = {
      isReady: true,
      capture: {
        schema_version: 1,
        tenant_code: 'tongji',
        course_id: '100',
        sub_id: '301',
        source_url: 'https://look.tongji.edu.cn/livingroom?course_id=100&sub_id=301&tenant_code=tongji',
        media_streams: [{ stream_id: 's301', signed_url: 'https://look.tongji.edu.cn/play/301.mp4' }],
      },
    };

    class MockElement {
      constructor() { this.children = []; this.style = {}; this.classList = { add() {}, remove() {} }; }
      set textContent(v) { this._text = v; this.children = []; }
      get textContent() { return this._text || ''; }
      appendChild(c) { this.children.push(c); }
      addEventListener(evt, fn) { this['on' + evt] = fn; }
      click() { if (this.onclick) this.onclick(); }
    }

    const els = new Map();
    function getEl(id) {
      if (!els.has(id)) els.set(id, new MockElement());
      return els.get(id);
    }

    const context = vm.createContext({
      document: {
        hidden: false,
        createElement: () => new MockElement(),
        getElementById: id => getEl(id),
      },
      chrome: {
        runtime: {
          sendMessage: (msg, cb) => {
            if (msg.type === 'NATIVE_ACTION' && msg.payload && msg.payload.action === 'submit') {
              submitActionSent = msg.payload;
              cb({ status: 'ok', job: { job_id: 'job_new' } });
            } else {
              cb({ status: 'ok', jobs: [] });
            }
          },
        },
        tabs: {
          query: (opt, cb) => cb([{ id: 1, url: 'https://look.tongji.edu.cn/livingroom?course_id=100&sub_id=301&tenant_code=tongji' }]),
          sendMessage: (tabId, msg, cb) => {
            if (msg.type === 'GET_CAPTURE_STATE') cb(captureInTab);
          },
        },
        storage: { local: { get: (k, cb) => cb({}), set: () => {} } },
      },
      setInterval: () => {},
      clearInterval: () => {},
    });

    vm.runInContext(fs.readFileSync('extension/popup.js', 'utf8'), context);

    // Initial detection found 301
    // Now simulate user navigated to 302 inside page right before submitting
    captureInTab = {
      isReady: true,
      capture: {
        schema_version: 1,
        tenant_code: 'tongji',
        course_id: '100',
        sub_id: '302',
        source_url: 'https://look.tongji.edu.cn/livingroom?course_id=100&sub_id=302&tenant_code=tongji',
        media_streams: [{ stream_id: 's302', signed_url: 'https://look.tongji.edu.cn/play/302.mp4' }],
      },
    };

    // User clicks submit
    getEl('btn-submit').click();

    // The submit action must NOT have been sent to native host because course changed!
    assert.equal(submitActionSent, null, 'Submission of stale course must be intercepted and aborted');
    const bar = getEl('message-bar');
    assert.match(bar.textContent, /检测到课程已切换/);
    console.log('✓ Test passed: Pre-submit course change verification intercepted stale submission');
  }

  // Test 4: Injected.js captures page identity BEFORE async fetch
  {
    const listeners = {};
    let currentHref = 'https://look.tongji.edu.cn/livingroom?course_id=100&sub_id=401&tenant_code=tongji';
    const location = {
      get href() { return currentHref; },
      set href(v) { currentHref = v; },
      get origin() { return new URL(currentHref).origin; },
      get search() { return new URL(currentHref).search; },
      get hash() { return new URL(currentHref).hash; },
    };

    let capturedMessages = [];
    const window = {
      location,
      addEventListener: (type, fn) => {
        if (!listeners[type]) listeners[type] = [];
        listeners[type].push(fn);
      },
      postMessage: data => capturedMessages.push(data),
      fetch: async (url) => {
        // Simulate slow network during which user navigates to 402!
        currentHref = 'https://look.tongji.edu.cn/livingroom?course_id=100&sub_id=402&tenant_code=tongji';
        return {
          clone: () => ({
            json: async () => ({
              data: {
                course_title: '第401讲',
                video_list: [{ id: 'v401', preview_url: 'https://look.tongji.edu.cn/play/401.mp4' }],
              },
            }),
          }),
        };
      },
    };
    function Xhr() {}
    Xhr.prototype.open = () => {};
    Xhr.prototype.send = () => {};

    const context = vm.createContext({
      window,
      document: { readyState: 'complete', title: '同济课程' },
      URL,
      URLSearchParams,
      XMLHttpRequest: Xhr,
      console,
    });

    vm.runInContext(fs.readFileSync('extension/injected.js', 'utf8'), context);

    // Call API without query identity; should bind to 401 (captured before fetch, not 402)
    await window.fetch('/courseapi/v3/portal-home-setting/get-sub-info');
    await new Promise(resolve => setImmediate(resolve));

    assert.equal(capturedMessages.length, 1);
    const msg = capturedMessages[0];
    assert.equal(msg.payload.sub_id, '401', 'Must use identity captured before fetch started');
    assert.equal(msg.requestIdentity.sub_id, '401');
    console.log('✓ Test passed: Injected.js captures page identity synchronously before network initiation');
  }

  // Test 5: Conflicting response body identity rejected
  {
    const listeners = {};
    let currentHref = 'https://look.tongji.edu.cn/livingroom?course_id=100&sub_id=501&tenant_code=tongji';
    const location = {
      get href() { return currentHref; },
      set href(v) { currentHref = v; },
      get origin() { return new URL(currentHref).origin; },
      get search() { return new URL(currentHref).search; },
      get hash() { return new URL(currentHref).hash; },
    };

    let capturedMessages = [];
    const window = {
      location,
      addEventListener: (type, fn) => {
        if (!listeners[type]) listeners[type] = [];
        listeners[type].push(fn);
      },
      postMessage: data => capturedMessages.push(data),
      fetch: async () => ({
        clone: () => ({
          json: async () => ({
            data: {
              course_id: '100',
              sub_id: '999', // Conflicting sub_id!
              video_list: [{ id: 'v_conflict', preview_url: 'https://look.tongji.edu.cn/play/conflict.mp4' }],
            },
          }),
        }),
      }),
    };
    function Xhr() {}
    Xhr.prototype.open = () => {};
    Xhr.prototype.send = () => {};

    const context = vm.createContext({
      window,
      document: { readyState: 'complete', title: '同济课程' },
      URL,
      URLSearchParams,
      XMLHttpRequest: Xhr,
      console,
    });

    vm.runInContext(fs.readFileSync('extension/injected.js', 'utf8'), context);

    // Fetch with sub_id 501 in query
    await window.fetch('/courseapi/v3/portal-home-setting/get-sub-info?course_id=100&sub_id=501&tenant_code=tongji');
    await new Promise(resolve => setImmediate(resolve));

    // Must be rejected due to conflict!
    assert.equal(capturedMessages.length, 0, 'Conflicting response body identity must be rejected and not masked');
    console.log('✓ Test passed: Conflicting response body identity is rejected');
  }

  console.log('\nAll in-page isolation and fresh capture verification tests passed!');
})().catch(err => {
  console.error(err);
  process.exitCode = 1;
});
