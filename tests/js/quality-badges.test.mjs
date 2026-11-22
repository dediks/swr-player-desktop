import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

const root = path.resolve(import.meta.dirname, '../..');
const uri = (text) => 'data:text/javascript;base64,' + Buffer.from(text).toString('base64');

// 1. Verify strict XSS safety (no innerHTML/outerHTML/insertAdjacentHTML/document.write) on modified pages
for (const rel of [
  'swararia/web/static/modules/ui-shared.js',
  'swararia/web/static/pages/ingest/index.js',
  'swararia/web/static/pages/qc/index.js',
  'swararia/web/static/pages/converter/index.js',
  'swararia/web/static/pages/player/index.js',
]) {
  const src = fs.readFileSync(path.join(root, rel), 'utf8');
  assert.ok(!/\.innerHTML\s*=|\.outerHTML\s*=|insertAdjacentHTML|document\.write\s*\(/.test(src), `${rel} must avoid unsafe HTML injection sinks`);
  assert.ok(!/\b(alert|confirm|prompt)\s*\(/.test(src), `${rel} must avoid blocking native alert/confirm/prompt dialogs`);
}

// 2. Verify HTML indicators & column headers
const ingestHtml = fs.readFileSync(path.join(root, 'swararia/web/static/pages/ingest/index.html'), 'utf8');
assert.match(ingestHtml, /id="opusFormatDesc"/);
assert.match(ingestHtml, /id="playlistInspectTierFlag"/);
assert.match(ingestHtml, /id="playlistInspectSpec"/);
assert.match(ingestHtml, /id="activeJobStreamBadges"/);

const qcHtml = fs.readFileSync(path.join(root, 'swararia/web/static/pages/qc/index.html'), 'utf8');
assert.match(qcHtml, /value="YT_PREMIUM"/);

const playerHtml = fs.readFileSync(path.join(root, 'swararia/web/static/pages/player/index.html'), 'utf8');
assert.match(playerHtml, /value="YT_PREMIUM"/);
assert.match(playerHtml, /Codec, Bitrate &amp; Sample Rate/);

// 3. Evaluate isYoutubePremiumMax & createQualityTierBadge from ui-shared.js
class MockEl {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.className = '';
    this.textContent = '';
    this.title = '';
  }
}

globalThis.document = {
  createElement: (tag) => new MockEl(tag),
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
  export function spaNavigate() {}
  export function initSpaRouter() {}
  export function initPersistentDock() {}
  export function ensureFohAudioElement() {}
`);

const uiSrc = fs.readFileSync(path.join(root, 'swararia/web/static/modules/ui-shared.js'), 'utf8')
  .replace("'./dom-icons.js'", JSON.stringify(domIconsUrl))
  .replace(/from '\.\/(api-client|notifications|table-groups|queue-monitor|rename-modal|spa-router|persistent-dock)\.js'/g, `from ${JSON.stringify(noopStubUrl)}`);

const { isYoutubePremiumMax, createQualityTierBadge } = await import(uri(uiSrc));

// YouTube Premium Opus 256k -> YT PREMIUM MAX
assert.equal(isYoutubePremiumMax('.opus', { codec: 'opus', bitrate_kbps: 254 }), true);
const premOpusBadge = createQualityTierBadge('.opus', { codec: 'opus', bitrate_kbps: 254 });
assert.equal(premOpusBadge.textContent, '★ YT PREMIUM MAX');
assert.equal(premOpusBadge.className, 'quality-tier-badge yt-premium-max');

// YouTube Premium AAC 256k (format 141) -> YT PREMIUM MAX
assert.equal(isYoutubePremiumMax('.m4a', { codec: 'aac', bitrate_kbps: 256, source_format_id: '141' }), true);
const premAacBadge = createQualityTierBadge('.m4a', { codec: 'aac', bitrate_kbps: 256, source_format_id: '141' });
assert.equal(premAacBadge.textContent, '★ YT PREMIUM MAX');
assert.match(premAacBadge.title, /Format 141/);

// Quiet Opus 774 track verified by source_format_id -> YT PREMIUM MAX
assert.equal(isYoutubePremiumMax('.opus', { codec: 'opus', bitrate_kbps: 185, source_format_id: '774' }), true);

// Public Opus 160k -> PUB 160k (NOT YouTube Premium)
assert.equal(isYoutubePremiumMax('.opus', { codec: 'opus', bitrate_kbps: 160 }), false);
const pubBadge = createQualityTierBadge('.opus', { codec: 'opus', bitrate_kbps: 160 });
assert.equal(pubBadge.textContent, 'PUB 160k');
assert.equal(pubBadge.className, 'quality-tier-badge standard');

// Transcoded MP3 320k -> HQ 320k (NOT YouTube Premium)
assert.equal(isYoutubePremiumMax('.mp3', { codec: 'mp3', bitrate_kbps: 320, source_format_id: '774' }), false);
const mp3Badge = createQualityTierBadge('.mp3', { codec: 'mp3', bitrate_kbps: 320, source_format_id: '774' });
assert.equal(mp3Badge.textContent, 'HQ 320k');
assert.equal(mp3Badge.className, 'quality-tier-badge hq');

// Lossless FLAC -> LOSSLESS
const flacBadge = createQualityTierBadge('.flac', { codec: 'flac', bitrate_kbps: 950 });
assert.equal(flacBadge.textContent, 'LOSSLESS');
assert.equal(flacBadge.className, 'quality-tier-badge lossless');

// Upsampled Fake HQ -> never gets YT Premium or HQ badge
assert.equal(isYoutubePremiumMax('.m4a', { codec: 'aac', bitrate_kbps: 256, spectral_status: 'FAKE_HQ', source_format_id: '141' }), false);
assert.equal(createQualityTierBadge('.m4a', { codec: 'aac', bitrate_kbps: 256, spectral_status: 'FAKE_HQ', source_format_id: '141' }), null);

console.log('PASS quality-badges: bitrate tiers, YT Premium Max flag, Fake-HQ guard, and XSS checks');
