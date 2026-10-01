const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor() { this.children = []; this.style = {}; this.attributes = {}; this.classList = {add() {}, remove() {}}; }
  set textContent(value) { this.text = value; this.children = []; }
  get textContent() { return this.text || ''; }
  appendChild(child) { this.children.push(child); }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener() {}
}
const elements = new Map();
let timer, requests = 0;
const context = vm.createContext({
  document: {
    hidden: false,
    createElement: () => new Element(),
    getElementById: id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); },
  },
  chrome: {
    runtime: {sendMessage(message, callback) { requests++; callback(message.payload.action === 'ping' ? {status: 'ok', port: 48721} : {jobs: []}); }},
    tabs: {query(options, callback) { callback([]); }},
    storage: {local: {get(keys, callback) { callback({}); }}},
  },
  setInterval(callback, delay) { assert.equal(delay, 2000); timer = callback; },
});
vm.runInContext(fs.readFileSync('extension/popup.js', 'utf8'), context);
function render(status, progress_info) {
  context.job = {job_id: 'test', status, progress_info};
  vm.runInContext('renderTasks([job])', context);
  const item = elements.get('tasks-list').children[0];
  return item.children.find(child => child.className === 'task-progress');
}
let block = render('running', {stage: 'transcription', total_chunks: 10, completed_chunks: 4, chunk_percent: 40});
assert.match(block.children[0].textContent, /40%/);
assert.equal(block.children[1].attributes['aria-valuenow'], '40');
assert.equal(block.children[1].children[0].style.width, '40%');
assert.match(block.children[3].textContent, /4\/10/);
block = render('running', {stage: 'acquisition'});
assert.match(block.children[1].className, /indeterminate/);
assert.equal(block.children[1].attributes['aria-valuenow'], undefined);
block = render('running', {stage: 'transcription', total_chunks: 0, chunk_percent: 0});
assert.match(block.children[1].className, /indeterminate/);
for (const [status, label] of [['failed', '失败'], ['cancelled', '已取消'], ['needs_refresh', '等待刷新课程地址'], ['queued', '排队等待']]) {
  block = render(status, {stage: 'transcription', chunk_percent: 40, message: '正在转写'});
  assert.match(block.children[0].textContent, new RegExp(label));
  assert.doesNotMatch(block.children[1].className, /indeterminate/);
  assert.doesNotMatch(block.children[3].textContent, /正在转写/);
}
block = render('completed', {stage: 'completed'});
assert.equal(block.children[1].attributes['aria-valuenow'], '100');
assert.equal(block.children[1].children[0].style.width, '100%');
const before = requests;
timer();
assert.equal(requests, before + 1);
context.document.hidden = true;
timer();
assert.equal(requests, before + 1);
console.log('Popup progress: rendering, terminal states, accessibility and polling passed.');
