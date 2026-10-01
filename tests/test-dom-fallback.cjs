const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const listeners = {};
let receive, video = null;
const window = {
  location: new URL('https://look.tongji.edu.cn/livingroom?course_id=A&sub_id=1&tenant_code=222'),
  addEventListener: (name, fn) => { listeners[name] = fn; }
};
const document = {readyState: 'complete', title: 'fixture', querySelector: selector => selector === 'video' ? video : null};
vm.runInNewContext(fs.readFileSync('extension/content.js', 'utf8'), {window, document, URL, chrome: {runtime: {onMessage: {addListener: fn => {receive = fn;}}}}, setInterval, clearInterval});
function capture() { let result; receive({type: 'GET_CAPTURE_STATE'}, {}, r => {result = r;}); return result; }
assert.equal(capture().isReady, false);
video = {currentSrc: 'https://look.tongji.edu.cn/play/a.mp4'};
assert.equal(capture().isReady, true, 'initial document video can appear after document_start');
window.location = new URL('https://look.tongji.edu.cn/livingroom?course_id=B&sub_id=2&tenant_code=222');
assert.equal(capture().isReady, false, 'old video is not rebound to new course');
window.location = new URL('https://look.tongji.edu.cn/livingroom?course_id=A&sub_id=1&tenant_code=222');
assert.equal(capture().isReady, false, 'returning to original course still requires fresh binding');
listeners.message({source: window, origin: window.location.origin, data: {source: 'TONGJI_CLIPPER_MAIN', type: 'SUB_INFO_CAPTURED', payload: {course_id: 'A', sub_id: '1', tenant_code: '222', media_streams: [{stream_id:'main', signed_url:video.currentSrc}]}}});
assert.equal(capture().isReady, true);
console.log('Delayed initial video, navigation isolation, and fresh binding passed.');

window.location = new URL('https://look.tongji.edu.cn/livingroom?course_id=C&sub_id=3&tenant_code=222');
listeners.message({source: window, origin: window.location.origin, data: {source: 'TONGJI_CLIPPER_MAIN', type: 'SUB_INFO_CAPTURED', payload: {course_id: 'C', sub_id: '3', tenant_code: '222', media_streams: [{stream_id:'main', signed_url:'https://look.tongji.edu.cn/play/c.mp4'}]}}});
assert.equal(capture().isReady, true, 'fresh response arriving before first UI read must survive navigation cleanup');
assert.equal(capture().capture.course_id, 'C');
