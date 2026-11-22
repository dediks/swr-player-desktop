import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

const root = path.resolve(import.meta.dirname, '../..');
const uri = (text) => 'data:text/javascript;base64,' + Buffer.from(text).toString('base64');

const PAGES = ['ingest', 'qc', 'converter', 'organizer', 'multitrack', 'video-split', 'player'];

// 1. Verify HTML & JS security/offline invariants across all 7 desktop pages
for (const page of PAGES) {
  const html = fs.readFileSync(path.join(root, `swararia/web/static/pages/${page}/index.html`), 'utf8');
  assert.ok(!html.includes('fonts.googleapis.com'), `${page} must not require external Google Fonts CDN`);
  assert.ok(!html.includes('pipeline-stage-grid'), `${page} must not render web-dashboard promo card grids`);
  assert.match(html, /id="appHeader"/, `${page} needs #appHeader`);
  assert.match(html, /<main class="app-container/, `${page} needs <main class="app-container">`);
  assert.match(html, /<footer class="app-footer">/, `${page} keeps <footer class="app-footer"> for spa-router parity`);
}

const styleCss = fs.readFileSync(path.join(root, 'swararia/web/static/css/style.css'), 'utf8');
assert.match(styleCss, /--bg-core:\s*#14161a/, 'style.css should use neutral studio graphite desktop background');
assert.match(styleCss, /--shadow-card:\s*none/, 'style.css should disable web-card drop shadows');
assert.match(styleCss, /gap:\s*1px/, 'style.css should use 1px docked split-pane dividers');

for (const mod of ['ui-shared.js', 'spa-router.js', 'persistent-dock.js']) {
  const src = fs.readFileSync(path.join(root, `swararia/web/static/modules/${mod}`), 'utf8');
  assert.ok(!/\.innerHTML\s*=|\.outerHTML\s*=|insertAdjacentHTML|document\.write\s*\(/.test(src), `${mod} must avoid unsafe HTML injection sinks`);
  assert.ok(!/\b(alert|confirm|prompt)\s*\(/.test(src), `${mod} must avoid blocking native alert/confirm/prompt dialogs`);
}

// 2. Verify live initAppHeader() shell mounting, rail collapse, diagnostics, and Alt+1..7 shortcuts
const navigated = [];
const storage = new Map();
globalThis.localStorage = {
  getItem: (k) => (storage.has(k) ? storage.get(k) : null),
  setItem: (k, v) => storage.set(k, String(v)),
};

class MockElement {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase();
    this.id = '';
    this.className = '';
    this.textContent = '';
    this.children = [];
    this.attributes = {};
    this.dataset = {};
    this.listeners = {};
    this.parentElement = null;
    this.classes = new Set();
    const self = this;
    this.classList = {
      add: (...xs) => { xs.forEach((x) => self.classes.add(x)); self._syncClass(); },
      remove: (...xs) => { xs.forEach((x) => self.classes.delete(x)); self._syncClass(); },
      contains: (x) => self.classes.has(x),
      toggle: (x, force) => {
        const next = force === undefined ? !self.classes.has(x) : Boolean(force);
        if (next) self.classes.add(x);
        else self.classes.delete(x);
        self._syncClass();
        return next;
      },
    };
  }
  _syncClass() {
    this.className = Array.from(this.classes).join(' ');
  }
  set className(val) {
    this._className = String(val || '');
    this.classes = new Set(this._className.split(/\s+/).filter(Boolean));
  }
  get className() {
    return this._className;
  }
  set innerHTML(_) {
    throw new Error('Unsafe innerHTML assignment blocked');
  }
  setAttribute(k, v) {
    this.attributes[k] = String(v);
    if (k === 'id') this.id = String(v);
  }
  appendChild(child) {
    if (child && typeof child === 'object') {
      child.parentElement = this;
      this.children.push(child);
    }
    return child;
  }
  after(node) {
    if (!this.parentElement) return;
    node.parentElement = this.parentElement;
    const idx = this.parentElement.children.indexOf(this);
    this.parentElement.children.splice(idx + 1, 0, node);
  }
  replaceChildren(...nodes) {
    this.children = [];
    nodes.forEach((n) => this.appendChild(n));
  }
  addEventListener(type, fn) {
    this.listeners[type] = this.listeners[type] || [];
    this.listeners[type].push(fn);
  }
  querySelector(sel) {
    return this.querySelectorAll(sel)[0] || null;
  }
  querySelectorAll(sel) {
    const out = [];
    const walk = (node) => {
      for (const ch of node.children || []) {
        if (matchesSimpleSelector(ch, sel)) out.push(ch);
        walk(ch);
      }
    };
    walk(this);
    return out;
  }
}

function matchesSimpleSelector(el, sel) {
  if (sel === '.app-nav .nav-tab') return el.classes?.has('nav-tab');
  if (sel === '.rail-toggle-text') return el.classes?.has('rail-toggle-text');
  if (sel === '.modal-backdrop:not(.hidden)') return false;
  if (sel.startsWith('#')) return el.id === sel.slice(1);
  if (sel.startsWith('.')) return el.classes?.has(sel.slice(1));
  return el.tagName?.toLowerCase() === sel.toLowerCase();
}

const body = new MockElement('body');
const appHeader = new MockElement('header');
appHeader.id = 'appHeader';
appHeader.className = 'app-header';
appHeader.dataset.page = 'qc';
body.appendChild(appHeader);

const winTarget = new EventTarget();
const mockDoc = Object.assign(new EventTarget(), {
  readyState: 'complete',
  body,
  activeElement: null,
  createElement: (tag) => new MockElement(tag),
  createElementNS: (_ns, tag) => new MockElement(tag),
  createTextNode: (text) => ({ nodeType: 3, textContent: String(text) }),
  getElementById: (id) => {
    const walk = (node) => {
      if (node.id === id) return node;
      for (const ch of node.children || []) {
        const hit = walk(ch);
        if (hit) return hit;
      }
      return null;
    };
    return walk(body);
  },
  querySelector: (sel) => body.querySelector(sel),
  querySelectorAll: (sel) => body.querySelectorAll(sel),
});

globalThis.document = mockDoc;
globalThis.window = {
  location: { hostname: '127.0.0.1', host: '127.0.0.1:8765', pathname: '/qc', search: '' },
  addEventListener: winTarget.addEventListener.bind(winTarget),
  dispatchEvent: winTarget.dispatchEvent.bind(winTarget),
};
globalThis.CustomEvent = class CustomEvent extends Event {
  constructor(type, init = {}) {
    super(type);
    this.detail = init.detail;
  }
};

const domIconsUrl = uri(fs.readFileSync(path.join(root, 'swararia/web/static/modules/dom-icons.js'), 'utf8'));
const noopStubUrl = uri(`
  export async function apiGet() { return { ok: false }; }
  export async function apiPost() { return { ok: false }; }
  export function notify() {}
  export function playNotificationSound() {}
  export function createHeaderNotificationCenter() { return document.createElement('div'); }
  export function groupFiles() { return []; }
  export function groupFilesByFolder() { return []; }
  export function createGroupHeaderBar() { return document.createElement('tr'); }
  export function createFileNameCell() { return document.createElement('td'); }
  export function createGroupCollapseController() { return {}; }
  export function renderLogs() {}
  export function setLogFilter() {}
  export function createQueueMonitor() { return {}; }
  export function openRenameModal() {}
  export function openDeleteModal() {}
  export function openBatchRenameModal() {}
  export function initSpaRouter() {}
  export function initPersistentDock() {}
  export function ensureFohAudioElement() {}
`);
const routerStubUrl = uri(`
  export const navigated = [];
  export function spaNavigate(href) { globalThis.__testNavigated.push(href); }
  export function initSpaRouter() {}
`);
globalThis.__testNavigated = navigated;

let uiSrc = fs.readFileSync(path.join(root, 'swararia/web/static/modules/ui-shared.js'), 'utf8')
  .replace("'./dom-icons.js'", JSON.stringify(domIconsUrl))
  .replace("'./spa-router.js'", JSON.stringify(routerStubUrl))
  .replace(/from '\.\/(api-client|notifications|table-groups|queue-monitor|rename-modal|persistent-dock)\.js'/g, `from ${JSON.stringify(noopStubUrl)}`);

const origSetTimeout = globalThis.setTimeout;
globalThis.setTimeout = () => 0;
try {
  const { initAppHeader, setStudioRailCollapsed } = await import(uri(uiSrc));
  initAppHeader({ activePage: 'qc' });

  const rail = mockDoc.getElementById('studioRail');
  const statusbar = mockDoc.getElementById('studioStatusbar');
  const stageChip = mockDoc.getElementById('headerStageChip');
  assert.ok(rail, 'studioRail should be mounted');
  assert.ok(statusbar, 'studioStatusbar should be mounted');
  assert.equal(stageChip.textContent, 'Stage 2 · Soundsystem QC & Spectral Lab');

  const navTabs = mockDoc.querySelectorAll('.app-nav .nav-tab');
  assert.equal(navTabs.length, 7, 'left rail should mount all 7 stages');
  assert.equal(navTabs[1].classList.contains('active'), true, 'active stage tab should be highlighted');

  setStudioRailCollapsed(true);
  assert.equal(body.classList.contains('rail-collapsed'), true);
  assert.equal(storage.get('swararia_rail_collapsed'), '1');
  setStudioRailCollapsed(false);
  assert.equal(body.classList.contains('rail-collapsed'), false);

  window.dispatchEvent(new CustomEvent('swararia:system-status', {
    detail: { ffmpeg: true, ffprobe: true, yt_dlp: true, python: '3.12.3' },
  }));
  assert.equal(mockDoc.getElementById('sbFfmpegVal').textContent, 'FFmpeg Ready');
  assert.equal(mockDoc.getElementById('sbYtdlpVal').textContent, 'yt-dlp Ready');
  assert.equal(mockDoc.getElementById('sbPythonVal').textContent, 'Py 3.12.3');

  window.dispatchEvent(new CustomEvent('swararia:route-changed', { detail: { key: 'player' } }));
  assert.equal(stageChip.textContent, 'Stage 7 · FOH Live Player & Cue Deck');
  assert.equal(mockDoc.getElementById('statusBarStage').textContent, 'Stage 7 · FOH Live Player & Cue Deck');

  // Alt+3 switches to /converter when not in remote mode
  const keyEvt = (key) => Object.assign(new Event('keydown'), { altKey: true, ctrlKey: false, metaKey: false, key });
  mockDoc.dispatchEvent(keyEvt('3'));
  assert.deepEqual(navigated, ['/converter']);

  // Remote controller mode suppresses global stage shortcuts
  body.classList.add('remote-controller-mode');
  mockDoc.dispatchEvent(keyEvt('1'));
  assert.deepEqual(navigated, ['/converter']);

  console.log('PASS desktop-shell: 7-stage rail, statusbar diagnostics, collapse persistence, Alt+1..7, and XSS safety');
} finally {
  globalThis.setTimeout = origSetTimeout;
}
