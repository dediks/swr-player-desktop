import assert from 'node:assert/strict';
import fs from 'node:fs';
const uri = text => 'data:text/javascript;base64,' + Buffer.from(text).toString('base64');
const icons = uri(fs.readFileSync(new URL('../../swararia/web/static/modules/dom-icons.js', import.meta.url), 'utf8'));
const source = fs.readFileSync(new URL('../../swararia/web/static/modules/native-remote-admin.js', import.meta.url), 'utf8').replace("'./dom-icons.js'", JSON.stringify(icons));
const { initNativeRemoteAdmin } = await import(uri(source));
const html = fs.readFileSync(new URL('../../swararia/web/static/pages/player/index.html', import.meta.url), 'utf8');
class Element {
  textContent = ''; value = ''; disabled = false; children = []; listeners = {}; open = false; dataset = {}; attributes = {}; style = {};
  constructor(attrs = '') {
    this.className = /class="([^"]*)"/.exec(attrs)?.[1] || '';
    this.value = /value="([^"]*)"/.exec(attrs)?.[1] || '';
    this.classes = new Set(this.className.split(' '));
    this.classList = { add: x => this.classes.add(x), remove: x => this.classes.delete(x), contains: x => this.classes.has(x), toggle: (x, flag) => { const yes = flag ?? !this.classes.has(x); yes ? this.classes.add(x) : this.classes.delete(x); return yes; } };
  }
  set innerHTML(_) { throw new Error('unsafe innerHTML'); }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  appendChild(child) { child.parent = this; this.children.push(child); return child; }
  replaceChildren(...children) { this.children = children; }
  remove() { this.parent.children.splice(this.parent.children.indexOf(this), 1); }
  setAttribute(k,v) { this.attributes[k] = v; }
  focus() { this.owner.activeElement = this; }
  select() { this.selected = true; }
}
function harness() {
  const nodes = new Map([...html.matchAll(/<[^>]*\bid="([^"]+)"[^>]*>/g)].map(m => [m[1], new Element(m[0])]));
  const doc = { getElementById: id => nodes.get(id), createElement: tag => Object.assign(new Element(), { tagName: tag }), createElementNS: () => new Element() };
  for (const node of nodes.values()) node.owner = doc;
  globalThis.document = doc;
  let state = { enabled: false, devices: [] };
  const sent = []; const qr = [];
  let responder = async payload => {
    if (payload?.action === 'start') state = { ...state, enabled: true, url: 'https://127.0.0.1:8766', fingerprint_sha256: 'ab'.repeat(32) };
    if (payload?.action === 'invite_open') state = { ...state, invite: 'fixture', invite_expires_at: Date.now()/1000+90 };
    if (payload?.action === 'invite_close') state = { ...state, invite: null };
    if (payload?.action === 'stop') state = { enabled: false, devices: [] };
    return structuredClone(state);
  };
  const admin = initNativeRemoteAdmin({ document: doc, request: p => { sent.push(p); return responder(p); }, renderQr: (_,text) => { qr.push(text); return true; } });
  return { admin, nodes, doc, sent, qr, el: id => { assert.ok(nodes.has(id), `actual HTML needs ${id}`); return nodes.get(id); }, set state(v) { state = v; }, get state() { return state; }, set responder(v) { responder = v; } };
}
const tests = [];
const test = (name, fn) => tests.push([name, fn]);
const click = e => e.listeners.click();
const visible = e => !e.classList.contains('hidden');
test('disabled intro, progressive settings and missing IP stay truthful', async () => {
  const h = harness(); await h.admin.refresh();
  assert.ok(visible(h.el('nativeOff')));
  assert.ok(!visible(h.el('nativePairingPanel')));
  assert.ok(!visible(h.el('nativeDeviceSection')));
  assert.equal(h.el('nativeSettings').open, false);
  await click(h.el('nativeStart'));
  assert.equal(h.el('nativeSettings').open, true);
  assert.equal(h.doc.activeElement, h.el('nativeBindIp'));
  assert.ok(!h.sent.some(p => p?.action === 'start'));
  h.el('nativeBindIp').value = '127.0.0.1'; h.el('nativePort').value = '8766';
  await click(h.el('nativeStart'));
  assert.ok(h.sent.some(p => p?.action === 'start' && p.bind_ip === '127.0.0.1' && p.port === 8766));
  assert.ok(!h.el('nativeStatus').textContent.includes('terhubung'));
  assert.ok(h.qr.length > 0, 'primary flow starts listener then opens a real invitation');
});
test('real invitation expiry clears QR/manual and clipboard checks the live deadline', async () => {
  const h = harness(); const originalNow = Date.now; let now = originalNow(); Date.now = () => now;
  let copies = []; Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { clipboard: { writeText: async text => copies.push(text) } } });
  try {
    h.state = { enabled: true, url: 'https://127.0.0.1:8766', fingerprint_sha256: 'ab'.repeat(32), invite: 'fixture', invite_expires_at: now/1000+78, devices: [] };
    await h.admin.refresh();
    assert.equal(h.el('nativeStatus').textContent, 'Siap pairing');
    assert.equal(h.el('nativeCountdown').textContent, '01:18');
    assert.ok(visible(h.el('nativeQr'))); assert.ok(visible(h.el('nativePairingPanel')));
    assert.ok(h.el('nativePairing').value.startsWith('swararia1|127.0.0.1:8766|'));
    assert.ok(!h.el('nativePairing').value.includes('?'));
    await click(h.el('nativeCopy')); assert.equal(copies.length, 1);
    now += 79000;
    await click(h.el('nativeCopy')); assert.equal(copies.length, 1);
    assert.equal(h.el('nativePairing').value, ''); assert.equal(h.el('nativeCopy').disabled, true);
    assert.ok(!visible(h.el('nativeQr'))); assert.ok(!visible(h.el('nativePairingPanel')));
    assert.match(h.el('nativeInviteStatus').textContent, /kedaluwarsa/);
    assert.ok(visible(h.el('nativeInviteOpen')));
    h.state = { ...h.state, invite: null }; await h.admin.refresh();
    assert.ok(!visible(h.el('nativePairingPanel')));
  } finally { Date.now = originalNow; }
});
test('pending and active devices coexist, permissions are authoritative, rows stay stable and expired actions cannot run', async () => {
  const h = harness(); const expiry = Date.now()/1000+3600;
  h.state = { enabled: true, devices: Array.from({length:32}, (_,i) => ({ device_id: `d${i}`, label: i ? `Device ${i}` : '<img onerror=evil()>', status: i < 2 ? 'pending' : 'active', role: i < 2 ? null : 'control', expires_at: expiry })) };
  h.responder = async p => {
    if (p?.action === 'approve') { const d = h.state.devices.find(d => d.device_id === p.device_id); d.status = 'active'; d.role = p.role; }
    if (['deny','revoke'].includes(p?.action)) h.state.devices.find(d => d.device_id === p.device_id).status = p.action === 'deny' ? 'denied' : 'revoked';
    return structuredClone(h.state);
  };
  await h.admin.refresh();
  assert.equal(h.el('nativeStatus').textContent, 'Butuh persetujuan · 2');
  assert.equal(h.el('nativeDevices').children.length, 32);
  const first = h.el('nativeDevices').children[0];
  assert.equal(first.children[0].textContent, '<img onerror=evil()>');
  const active = h.el('nativeDevices').children[2];
  const select = active.children.find(e => e.tagName === 'select');
  assert.ok(select, 'active device needs a role selector');
  h.state.devices[31].label = 'Changed'; await h.admin.refresh();
  assert.equal(h.el('nativeDevices').children[0], first, 'unrelated changes cannot replace focused rows');
  select.value = 'view_only'; await select.listeners.change();
  assert.equal(h.sent.at(-1).role, 'view_only');
  assert.equal(select.value, 'view_only');
  await click(first.children.find(e => e.textContent === 'Izinkan kontrol'));
  assert.equal(h.sent.at(-1).action, 'approve');
  await click(h.el('nativeDevices').children.find(e => e.dataset.deviceId === 'd1').children.find(e => e.textContent === 'Tolak'));
  await click(active.children.find(e => e.textContent === 'Putus akses'));
  assert.ok(!h.el('nativeDevices').children.some(e => ['d1','d2'].includes(e.dataset.deviceId)));
  const old = h.el('nativeDevices').children.find(e => e.dataset.deviceId === 'd0').children.find(e => e.textContent === 'Putus akses');
  h.state.devices[0].expires_at = Date.now()/1000-1; await h.admin.refresh(); const before = h.sent.length;
  await click(old); assert.equal(h.sent.length, before, 'detached stale controls must revalidate');
});
test('silent polling preserves input, focus and feedback; mutations win races and block repeat clicks', async () => {
  const h = harness(); await h.admin.refresh();
  h.el('nativeBindIp').value = '127.0.0.1'; h.el('nativePort').value = '9876'; h.el('nativeBindIp').focus(); h.el('nativeSettings').open = true;
  h.el('nativeFeedback').textContent = 'Manual guidance';
  let resolvePoll; h.responder = () => new Promise(resolve => { resolvePoll = resolve; });
  const poll = h.admin.refresh();
  assert.equal(h.el('nativeFeedback').textContent, 'Manual guidance');
  h.responder = async p => { assert.equal(p.action, 'start'); throw new Error('listener_unavailable'); };
  await click(h.el('nativeStart'));
  assert.match(h.el('nativeFeedback').textContent, /port/);
  resolvePoll({ enabled: false, devices: [] }); await poll;
  assert.match(h.el('nativeFeedback').textContent, /port/);
  assert.equal(h.el('nativeBindIp').value, '127.0.0.1'); assert.equal(h.el('nativePort').value, '9876');
  assert.equal(h.doc.activeElement, h.el('nativeBindIp')); assert.equal(h.el('nativeSettings').open, true);
  h.state = { enabled: true, url: 'https://127.0.0.1:8766', fingerprint_sha256: 'ab'.repeat(32), invite: 'fixture', invite_expires_at: Date.now()/1000+90, devices: [{device_id:'a',label:'A',status:'active',role:'control',expires_at:Date.now()/1000+999}] };
  h.responder = async () => structuredClone(h.state); await h.admin.refresh();
  let late; h.responder = () => new Promise(resolve => { late = resolve; }); const stale = h.admin.refresh();
  let finish; h.responder = () => new Promise(resolve => { finish = resolve; }); const stop = click(h.el('nativeStop'));
  assert.equal(h.el('nativeStop').disabled, true); assert.equal(h.el('nativeCopy').disabled, true);
  assert.ok(h.el('nativeDevices').children[0].children.filter(e => ['button','select'].includes(e.tagName)).every(e => e.disabled));
  const before = h.sent.length; await click(h.el('nativeStop')); assert.equal(h.sent.length, before);
  finish({enabled:false,devices:[]}); await stop;
  late(structuredClone(h.state)); await stale;
  assert.equal(h.el('nativeStatus').textContent, 'Belum aktif'); assert.equal(h.el('nativePairing').value, '');
  assert.equal(h.el('nativeDevices').children.length, 0);
});
test('clipboard failure opens selectable manual fallback without exposing error text', async () => {
  const h = harness(); h.state = {enabled:true,url:'https://127.0.0.1:8766', fingerprint_sha256:'ab'.repeat(32),invite:'fixture',invite_expires_at:Date.now()/1000+90,devices:[]};
  await h.admin.refresh();
  Object.defineProperty(globalThis, 'navigator', {configurable:true,value:{clipboard:{writeText: async () => {throw new Error('sensitive');}}}});
  await click(h.el('nativeCopy'));
  assert.equal(h.el('nativeManual').open, true); assert.equal(h.el('nativePairing').selected, true);
  assert.ok(!h.el('nativeFeedback').textContent.includes('sensitive'));
});
test('approved composition reuses scoped styles and safe original SVG factory', async () => {
  const h = harness(); await h.admin.refresh();
  assert.ok(h.el('nativeTitleIcon').children.length > 0);
  assert.ok(h.el('nativeHeroIcon').children.length > 0);
  const css = fs.readFileSync(new URL('../../swararia/web/static/css/native-remote.css', import.meta.url), 'utf8');
  assert.ok(html.includes('href="/static/css/native-remote.css"'));
  assert.ok(css.includes('var(--font-heading)'));
  assert.ok(css.includes('var(--accent-amber)'));
  for (const rule of css.replace(/\/\*[\s\S]*?\*\//g,'').split('{').slice(0,-1)) {
    const selector = rule.slice(rule.lastIndexOf('}')+1).trim();
    if (!selector || selector.startsWith('@')) continue;
    assert.ok(selector.split(',').every(s => s.trim().startsWith('#androidRemoteModal')), `unscoped rule: ${selector}`);
  }
});
test('invalid pairing metadata never produces an empty QR-ready state', async () => {
  const h = harness();
  h.state = {enabled:true,url:'not-a-url',fingerprint_sha256:'ab'.repeat(32),invite:'fixture',invite_expires_at:Date.now()/1000+90,devices:[]};
  await h.admin.refresh();
  assert.ok(!visible(h.el('nativePairingPanel'))); assert.equal(h.el('nativePairing').value, '');
  assert.equal(h.el('nativeCopy').disabled, true);
  assert.ok(!h.el('nativeStatus').textContent.includes('Siap pairing'));
});
test('failed role updates roll back and later polls cannot be overwritten by earlier ones', async () => {
  const h = harness(); h.state = {enabled:true,devices:[{device_id:'a',label:'A',status:'active',role:'control',expires_at:Date.now()/1000+999}]};
  await h.admin.refresh();
  const select = h.el('nativeDevices').children[0].children.find(e => e.tagName === 'select');
  let fail; h.responder = () => new Promise((_,reject) => { fail = reject; });
  select.value = 'view_only'; const change = select.listeners.change();
  assert.equal(select.value, 'control'); assert.equal(select.disabled, true);
  fail(new Error('private-error-body')); await change;
  assert.equal(select.value, 'control'); assert.equal(select.disabled, false);
  assert.ok(!h.el('nativeFeedback').textContent.includes('private-error-body'));
  const resolves = []; h.responder = () => new Promise(resolve => resolves.push(resolve));
  const first = h.admin.refresh(); const second = h.admin.refresh();
  resolves[1]({...h.state,devices:[{...h.state.devices[0],role:'view_only'}]}); await second;
  resolves[0](h.state); await first;
  assert.equal(select.value, 'view_only');
});
test('normal approval and connected states omit irrelevant empty-invitation notices', async () => {
  const h = harness();
  h.state = { enabled: true, devices: [{device_id:'a',label:'Device',status:'pending',role:null,expires_at:Date.now()/1000+999}] };
  await h.admin.refresh();
  assert.equal(visible(h.el('nativeInviteStatus')), false, 'consumed invite is not a warning while approval is pending');
  h.state.devices[0].status='active'; h.state.devices[0].role='control';
  await h.admin.refresh();
  assert.equal(visible(h.el('nativeInviteStatus')), false, 'normal connected view must not imply an expired connection');
  assert.ok(visible(h.el('nativeDeviceSection')));
});
test('active connection settings reflect the real endpoint after reload without overwriting inactive edits', async () => {
  const h = harness();
  h.state = {enabled:true,url:'https://172.28.64.10:9443',devices:[]};
  await h.admin.refresh();
  assert.equal(h.el('nativeBindIp').value, '172.28.64.10', 'disabled settings must show the actual listener IP');
  assert.equal(h.el('nativePort').value, '9443', 'disabled settings must not show a misleading default port');
  h.state = {enabled:false,devices:[]};
  await h.admin.refresh(); // Server confirms disabled before the user can edit enabled fields.
  h.el('nativeBindIp').value='172.28.64.20'; h.el('nativePort').value='9876';
  await h.admin.refresh();
  assert.equal(h.el('nativeBindIp').value,'172.28.64.20'); assert.equal(h.el('nativePort').value,'9876');
});
test('pairing has one manual disclosure and compact invitation actions', async () => {
  const h = harness();
  h.state={enabled:true,url:'https://127.0.0.1:8766',fingerprint_sha256:'ab'.repeat(32),invite:'fixture',invite_expires_at:Date.now()/1000+90,devices:[]};
  await h.admin.refresh();
  assert.ok(visible(h.el('nativeInviteActions')), 'invitation actions share one contextual row');
  assert.equal(visible(h.el('nativeInviteStatus')), false, 'normal pairing must not repeat a technical paragraph');
  await click(h.el('nativeShowCode'));
  assert.equal(h.el('nativeManual').open,true); assert.equal(h.el('nativeShowCode').attributes['aria-expanded'],'true');
  const css=fs.readFileSync(new URL('../../swararia/web/static/css/native-remote.css',import.meta.url),'utf8');
  assert.match(css, /#androidRemoteModal #nativeManual\s*>\s*summary\s*\{\s*display:\s*none/,'do not show a duplicate manual-code trigger');
});
test('wsl2 defaults, bridge status banner and manual port reservation work cleanly', async () => {
  const nodes = new Map([...html.matchAll(/<[^>]*\bid="([^"]+)"[^>]*>/g)].map(m => [m[1], new Element(m[0])]));
  const doc = { getElementById: id => nodes.get(id), createElement: tag => Object.assign(new Element(), { tagName: tag }), createElementNS: () => new Element() };
  for (const node of nodes.values()) node.owner = doc;
  globalThis.document = doc;
  const el = id => nodes.get(id);
  const bridgeCalls = [];
  let bridgeState = {
    is_wsl: true, is_windows: false, needs_portproxy: true, bridge_active: false, has_portproxy: false,
    firewall_active: false, port_conflict: false, wifi_ip: '192.168.18.47', public_port: 8767, internal_port: 8768,
    tls_reachable: null, powershell_admin_cmd: 'netsh interface portproxy add v4tov4 listenport=8767 connectport=8768',
  };
  let remoteState = {
    enabled: false, lan_ips: ['192.168.18.47'], default_bind_ip: '192.168.18.47', default_port: 8767, is_wsl: true, devices: [],
  };
  const admin = initNativeRemoteAdmin({
    document: doc,
    renderQr: () => true,
    request: async payload => {
      if (payload?.action === 'start') {
        if (payload.port === 8766) throw new Error('wsl_port_reserved');
        remoteState = { ...remoteState, enabled: true, url: `https://${payload.bind_ip}:${payload.port}`, bind_url: 'https://127.0.0.1:8768', fingerprint_sha256: 'ab'.repeat(32) };
      }
      if (payload?.action === 'invite_open') remoteState = { ...remoteState, invite: 'fixture', invite_expires_at: Date.now() / 1000 + 90 };
      return structuredClone(remoteState);
    },
    bridgeRequest: async payload => {
      bridgeCalls.push(payload);
      if (payload.action === 'enable') {
        bridgeState = { ...bridgeState, bridge_active: true, has_portproxy: true, firewall_active: true, tls_reachable: true };
        return { ok: true, message: 'Bridge aktif!', bridge: structuredClone(bridgeState) };
      }
      return { ok: true, bridge: structuredClone(bridgeState) };
    },
  });
  await admin.refresh({ checkBridge: true });
  assert.equal(el('nativeBindIp').value, '192.168.18.47');
  assert.equal(el('nativePort').value, '8767');
  assert.ok(visible(el('nativeBridgeBox')));
  assert.ok(visible(el('nativeBridgeBanner')));
  el('nativePort').value = '8766';
  await click(el('nativeStart'));
  assert.match(el('nativeFeedback').textContent, /8767/);
  el('nativePort').value = '8767';
  await click(el('nativeStart'));
  assert.match(el('nativeConnection').textContent, /192\.168\.18\.47:8767/);
  assert.match(el('nativeConnection').textContent, /127\.0.0\.1:8768/);
  await click(el('nativeBridgeBannerEnable'));
  assert.ok(bridgeCalls.some(c => c.action === 'enable'));
  assert.ok(el('nativeBridgeBanner').classList.contains('ready'));
});
test('media transfer toggle defaults off, syncs when enabled, and rolls back on failure', async () => {
  const h = harness();
  await h.admin.refresh();
  assert.ok(!visible(h.el('nativeMediaBox')));
  assert.equal(h.el('nativeMediaTransfer').disabled, true);
  h.state = { enabled: true, media_transfer_enabled: false, url: 'https://127.0.0.1:8766', devices: [] };
  h.responder = async p => {
    if (p?.action === 'set_media_transfer') {
      h.state.media_transfer_enabled = p.enabled;
    }
    return structuredClone(h.state);
  };
  await h.admin.refresh();
  assert.ok(visible(h.el('nativeMediaBox')));
  assert.equal(h.el('nativeMediaTransfer').disabled, false);
  assert.equal(h.el('nativeMediaTransfer').checked, false);
  h.el('nativeMediaTransfer').checked = true;
  await h.el('nativeMediaTransfer').listeners.change();
  assert.equal(h.sent.at(-1).action, 'set_media_transfer');
  assert.equal(h.sent.at(-1).enabled, true);
  assert.equal(h.el('nativeMediaTransfer').checked, true);

  h.responder = async () => { throw new Error('local_origin_required'); };
  h.el('nativeMediaTransfer').checked = false;
  await h.el('nativeMediaTransfer').listeners.change();
  assert.equal(h.el('nativeMediaTransfer').checked, true);
});
test('pc folder sync toggle defaults off, syncs when enabled, and rolls back on failure', async () => {
  const h = harness();
  await h.admin.refresh();
  assert.ok(!visible(h.el('nativeSyncBox')));
  assert.equal(h.el('nativeFolderSync').disabled, true);
  h.state = { enabled: true, media_transfer_enabled: false, sync_enabled: false, url: 'https://127.0.0.1:8766', devices: [] };
  h.responder = async p => {
    if (p?.action === 'set_sync_enabled') {
      h.state.sync_enabled = p.enabled;
    }
    return structuredClone(h.state);
  };
  await h.admin.refresh();
  assert.ok(visible(h.el('nativeSyncBox')));
  assert.equal(h.el('nativeFolderSync').disabled, false);
  assert.equal(h.el('nativeFolderSync').checked, false);
  h.el('nativeFolderSync').checked = true;
  await h.el('nativeFolderSync').listeners.change();
  assert.equal(h.sent.at(-1).action, 'set_sync_enabled');
  assert.equal(h.sent.at(-1).enabled, true);
  assert.equal(h.el('nativeFolderSync').checked, true);

  h.responder = async () => { throw new Error('local_origin_required'); };
  h.el('nativeFolderSync').checked = false;
  await h.el('nativeFolderSync').listeners.change();
  assert.equal(h.el('nativeFolderSync').checked, true);
});
test('host sync source folder and android native upload controls dispatch expected actions', async () => {
  const nodes = new Map([...html.matchAll(/<[^>]*\bid="([^"]+)"[^>]*>/g)].map(m => [m[1], new Element(m[0])]));
  const doc = { getElementById: id => nodes.get(id), createElement: tag => Object.assign(new Element(), { tagName: tag }), createElementNS: () => new Element() };
  for (const node of nodes.values()) node.owner = doc;
  globalThis.document = doc;
  const el = id => nodes.get(id);
  const sent = [];
  let state = { enabled: true, sync_enabled: false, sync_source_label: 'library/inbox', upload_enabled: false, upload_dest_label: 'library/inbox', url: 'https://127.0.0.1:8766', devices: [] };
  let activeDir = '/music/StageShow';
  const admin = initNativeRemoteAdmin({
    document: doc,
    renderQr: () => true,
    getActiveSourceDir: () => activeDir,
    request: async p => {
      if (p) sent.push(p);
      if (p?.action === 'set_sync_enabled') {
        state = { ...state, sync_enabled: p.enabled, sync_source_label: p.source_dir === '' ? 'library/inbox' : (p.source_dir ? 'StageShow' : state.sync_source_label) };
      }
      if (p?.action === 'set_upload_enabled') {
        state = { ...state, upload_enabled: p.enabled, upload_dest_label: p.dest_dir === '' ? 'library/inbox' : (p.dest_dir ? 'StageShow' : state.upload_dest_label) };
      }
      return structuredClone(state);
    },
  });
  await admin.refresh();
  assert.equal(el('nativeSyncSourceLabel').textContent, 'library/inbox');
  assert.equal(el('nativeUploadDestLabel').textContent, 'library/inbox');
  await click(el('btnNativeSyncUseSource'));
  assert.equal(sent.at(-1).action, 'set_sync_enabled');
  assert.equal(sent.at(-1).source_dir, '/music/StageShow');
  assert.equal(el('nativeSyncSourceLabel').textContent, 'StageShow');

  el('nativeAndroidUpload').checked = true;
  await el('nativeAndroidUpload').listeners.change();
  assert.equal(sent.at(-1).action, 'set_upload_enabled');
  assert.equal(sent.at(-1).enabled, true);
  assert.equal(el('nativeAndroidUpload').checked, true);

  await click(el('btnNativeUploadUseSource'));
  assert.equal(sent.at(-1).action, 'set_upload_enabled');
  assert.equal(sent.at(-1).dest_dir, '/music/StageShow');
  assert.equal(el('nativeUploadDestLabel').textContent, 'StageShow');

  await click(el('btnNativeUploadResetInbox'));
  assert.equal(sent.at(-1).dest_dir, '');
  assert.equal(el('nativeUploadDestLabel').textContent, 'library/inbox');
});
let failed = 0;
for (const [name, fn] of tests) { try { await fn(); console.log('PASS',name); } catch(e) { failed++; console.error('FAIL',name,e); } }
console.log(`Summary: ${tests.length-failed}/${tests.length} passed.`);
if (failed) process.exitCode = 1;
