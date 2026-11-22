import assert from 'node:assert/strict';
import fs from 'node:fs';
import { initPhoneUploadController } from '../../swararia/web/static/modules/phone-upload.js';
import { initPcSyncPanel } from '../../swararia/web/static/modules/pc-sync-panel.js';

const playerHtml = fs.readFileSync(new URL('../../swararia/web/static/pages/player/index.html', import.meta.url), 'utf8');
const organizerHtml = fs.readFileSync(new URL('../../swararia/web/static/pages/organizer/index.html', import.meta.url), 'utf8');

class MockElement {
  textContent = '';
  value = '';
  disabled = false;
  checked = false;
  files = [];
  children = [];
  listeners = {};
  options = [];
  style = {};
  constructor(tag = 'div', attrs = '') {
    this.tagName = tag.toUpperCase();
    this.className = /class="([^"]*)"/.exec(attrs)?.[1] || '';
    this.value = /value="([^"]*)"/.exec(attrs)?.[1] || '';
    this.classes = new Set(this.className.split(/\s+/).filter(Boolean));
    this.classList = {
      add: (...xs) => xs.forEach(x => this.classes.add(x)),
      remove: (...xs) => xs.forEach(x => this.classes.delete(x)),
      contains: x => this.classes.has(x),
      toggle: (x, flag) => {
        const yes = flag ?? !this.classes.has(x);
        if (yes) this.classes.add(x); else this.classes.delete(x);
        return yes;
      },
    };
  }
  set innerHTML(_) { throw new Error('unsafe innerHTML must never be used'); }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  appendChild(child) {
    child.parent = this;
    this.children.push(child);
    if (this.tagName === 'SELECT' && child.tagName === 'OPTION') {
      this.options.push(child);
    }
    return child;
  }
  replaceChildren(...children) {
    this.children = [];
    this.options = [];
    children.forEach(c => this.appendChild(c));
  }
}

function makeDoc(html) {
  const nodes = new Map(
    [...html.matchAll(/<([a-z0-9-]+)[^>]*\bid="([^"]+)"[^>]*>/gi)].map(m => [m[2], new MockElement(m[1], m[0])])
  );
  return {
    nodes,
    el(id) {
      assert.ok(nodes.has(id), `Expected element #${id}`);
      return nodes.get(id);
    },
    getElementById: id => nodes.get(id) || null,
    createElement: tag => new MockElement(tag),
  };
}

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test('phone upload gates visibility by upload_enabled and role, and warns + skips WAV/FLAC', () => {
  const doc = makeDoc(playerHtml);
  const ctrl = initPhoneUploadController({
    document: doc,
    isRemoteController: true,
    getRemoteCredentials: () => ({ pin: '1234', clientId: 'hp1' }),
    getPlaylists: () => [{ name: 'Opening', total: 2 }],
    getCurrentSourceDir: () => '/tmp/inbox',
    notify: () => {},
    onUploadCompleted: () => {},
  });

  const btnOpen = doc.el('btnOpenPhoneUpload');
  assert.ok(btnOpen.classList.contains('hidden'));

  ctrl.applyRemoteState({ upload_enabled: true, upload_dest_label: 'library/inbox', role: 'view_only' });
  assert.ok(btnOpen.classList.contains('hidden'), 'view_only phones must not see Upload button');

  ctrl.applyRemoteState({ upload_enabled: true, upload_dest_label: 'library/inbox', role: 'control' });
  assert.ok(!btnOpen.classList.contains('hidden'), 'control phones see Upload when enabled');

  btnOpen.listeners.click();
  const sel = doc.el('phoneUploadPlaylistSelect');
  assert.ok(sel.options.some(o => o.value === 'Opening'));

  doc.el('phoneUploadFileInput').files = [
    { name: 'Song.mp3', size: 1024 * 500 },
    { name: 'Master.wav', size: 2048 * 1024 },
    { name: 'Studio.flac', size: 4096 * 1024 },
  ];
  doc.el('phoneUploadFileInput').listeners.change();

  assert.equal(doc.el('btnStartPhoneUpload').disabled, false);
  assert.ok(!doc.el('phoneUploadWarningBanner').classList.contains('hidden'));
  assert.match(doc.el('phoneUploadWarningText').textContent, /\.wav\/\.flac/i);

  // Only WAV selected -> upload button stays disabled with warning
  doc.el('phoneUploadFileInput').files = [{ name: 'OnlyRaw.wav', size: 1024 * 1024 }];
  doc.el('phoneUploadFileInput').listeners.change();
  assert.equal(doc.el('btnStartPhoneUpload').disabled, true);
  assert.match(doc.el('phoneUploadWarningText').textContent, /1 file \.wav\/\.flac/i);
});

test('pc sync modal pairs, renders folders with WAV/FLAC warning, previews diff, and clears on disconnect', async () => {
  const doc = makeDoc(organizerHtml);
  const posts = [];
  const notifications = [];
  const peerObj = {
    peer_id: 'peer_1',
    host: '192.168.1.55',
    port: 8766,
    fingerprint_sha256: 'cd'.repeat(32),
    status: 'active',
    role: 'control',
    sync_capable: true,
  };

  initPcSyncPanel({
    document: doc,
    apiGet: async url => {
      if (url === '/api/sync/status') return { ok: true, data: { peers: [] } };
      return { ok: false };
    },
    apiPost: async (url, payload) => {
      posts.push({ url, payload });
      if (url === '/api/sync/pair') return { ok: true, data: { peer: { ...peerObj, status: 'pending', sync_capable: false } } };
      if (url === '/api/sync/refresh') {
        return {
          ok: true,
          data: {
            peer: peerObj,
            folders_info: {
              folders: [{
                folder_id: 'fld_1234567890abcdef',
                rel_folder: 'Setlist1',
                name: 'Setlist1',
                track_count: 4,
                total_bytes: 12 * 1024 * 1024,
                excluded_wav_flac_count: 2,
                oversized_count: 0,
              }],
              warnings: ['2 file .wav/.flac dilewati karena WAV/FLAC belum didukung pada fase sinkronisasi ini.'],
            },
          },
        };
      }
      if (url === '/api/sync/preview') {
        return {
          ok: true,
          data: {
            new_count: 3,
            rename_collision_count: 1,
            duplicate_skip_count: 0,
            wav_flac_skipped_count: 2,
            transfer_count: 4,
            transfer_bytes: 12 * 1024 * 1024,
            warnings: ['2 file .wav/.flac di PC sumber dilewati.'],
            items_preview: [{ name: 'Intro.mp3', rel_path: 'Setlist1/Intro.mp3', size_bytes: 2048, action: 'new' }],
          },
        };
      }
      if (url === '/api/sync/start') return { ok: true, data: { message: 'Queued sync job', job_id: 'sync_1' } };
      return { ok: true, data: {} };
    },
    notify: n => notifications.push(n),
    getTargetDestDir: () => '/tmp/inbox',
    getTargetDestLabel: () => 'library/inbox',
    onSyncQueued: jobId => { posts.push({ url: 'callback:onSyncQueued', jobId }); },
  });

  doc.el('btnOpenPcSyncModal').listeners.click();
  doc.el('syncPairingCodeInput').value = 'swararia1|192.168.1.55:8766|' + 'A'.repeat(43) + '|invite1234567890123';
  await doc.el('btnStartPcSyncPair').listeners.click();
  await doc.el('btnRefreshPcSyncPeer').listeners.click();

  assert.equal(doc.el('pcSyncFoldersList').children.length, 1);
  assert.ok(!doc.el('pcSyncWarningBanner').classList.contains('hidden'));
  assert.match(doc.el('pcSyncWarningText').textContent, /wav\/\.flac/i);

  // Check folder checkbox and preview diff
  await doc.el('btnPcSyncSelectAll').listeners.click();
  await doc.el('btnPreviewPcSync').listeners.click();
  assert.match(doc.el('pcSyncDiffSummary').textContent, /3 lagu baru · 1 bentrok/);
  assert.equal(doc.el('btnExecutePcSync').disabled, false);

  await doc.el('btnExecutePcSync').listeners.click();
  assert.ok(posts.some(p => p.url === '/api/sync/start'));
  assert.ok(posts.some(p => p.url === 'callback:onSyncQueued' && p.jobId === 'sync_1'));
  assert.equal(notifications.length, 1);

  await doc.el('btnDisconnectPcSyncPeer').listeners.click();
  assert.ok(posts.some(p => p.url === '/api/sync/disconnect'));
});

let failed = 0;
for (const [name, fn] of tests) {
  try {
    await fn();
    console.log('PASS', name);
  } catch (err) {
    failed += 1;
    console.error('FAIL', name, err);
  }
}
console.log(`Summary: ${tests.length - failed}/${tests.length} passed.`);
if (failed) process.exitCode = 1;
