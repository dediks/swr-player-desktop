/**
 * Audio Lab - Shared UI Facade & App Header / System Status Module
 * Strict DOM Manipulation (No innerHTML, strictly textContent, createElement & createElementNS)
 */

import { createSvgIcon, createExplorerIcon, createEditIcon, createTrashIcon } from './dom-icons.js';
import { apiGet, apiPost } from './api-client.js';
import {
  notify,
  playNotificationSound,
  createHeaderNotificationCenter,
} from './notifications.js';
import {
  groupFiles,
  groupFilesByFolder,
  createGroupHeaderBar,
  createFileNameCell,
  createGroupCollapseController,
} from './table-groups.js';
import { renderLogs, setLogFilter, createQueueMonitor } from './queue-monitor.js';
import { openRenameModal, openDeleteModal, openBatchRenameModal } from './rename-modal.js';
import { spaNavigate, initSpaRouter } from './spa-router.js';
import { initPersistentDock, ensureFohAudioElement } from './persistent-dock.js';

// Safe DOMContentLoaded hook for dynamically loaded SPA modules
if (typeof document !== 'undefined') {
  const nativeAddEventListener = EventTarget.prototype.addEventListener;
  document.addEventListener = function (type, listener, options) {
    if (type === 'DOMContentLoaded' && document.readyState !== 'loading') {
      queueMicrotask(() => {
        try {
          if (typeof listener === 'function') {
            listener.call(document, new Event('DOMContentLoaded'));
          } else if (listener && typeof listener.handleEvent === 'function') {
            listener.handleEvent(new Event('DOMContentLoaded'));
          }
        } catch (err) {
          console.error('[SPA Shell] Error in DOMContentLoaded callback:', err);
        }
      });
      return;
    }
    return nativeAddEventListener.call(document, type, listener, options);
  };
}

export {
  createSvgIcon,
  createExplorerIcon,
  createEditIcon,
  createTrashIcon,
  apiGet,
  apiPost,
  notify,
  playNotificationSound,
  groupFiles,
  groupFilesByFolder,
  createGroupHeaderBar,
  createFileNameCell,
  createGroupCollapseController,
  renderLogs,
  setLogFilter,
  createQueueMonitor,
  openRenameModal,
  openDeleteModal,
  openBatchRenameModal,
  spaNavigate,
  initSpaRouter,
  initPersistentDock,
  ensureFohAudioElement,
};

/**
 * Creates a compact Spectral Quality Badge element when spectral_status is available.
 * @param {Record<string, any> | null | undefined} qc
 * @returns {HTMLSpanElement | null}
 */
export function createSpectralBadge(qc) {
  if (!qc || !qc.spectral_status || qc.spectral_status === 'UNKNOWN') {
    return null;
  }
  const span = document.createElement('span');
  const status = String(qc.spectral_status).toUpperCase();
  const rawLabel = String(qc.spectral_label || '')
    .replace(/Asli\s*\(Full HQ\)/gi, 'Full-Range')
    .replace(/\bAsli\b/gi, 'Full-Range')
    .replace(/\(Standar\)/gi, 'Standard')
    .replace(/\bSpektrum\b/gi, 'Spectrum');

  if (status === 'AUTHENTIC') {
    span.className = 'spectral-badge authentic';
    span.textContent = rawLabel || '20 kHz Full-Range';
    span.title = 'Full-Range High-Frequency Spectrum (~20 kHz Authentic Lossless / HQ).';
  } else if (status === 'FAKE_HQ') {
    span.className = 'spectral-badge fake';
    span.textContent = rawLabel || '⚠️ Fake HQ';
    span.title = 'High-frequency spectral cutoff detected (<16 kHz) despite high bitrate — indicates upsampled/transcoded lossy source.';
  } else if (status === 'STANDARD') {
    span.className = 'spectral-badge standard';
    span.textContent = rawLabel || '16 kHz Standard';
    span.title = 'Standard high-frequency spectrum bandwidth (~16 kHz).';
  } else {
    span.className = 'spectral-badge low';
    span.textContent = rawLabel || '<14 kHz Low-BW';
    span.title = 'Limited high-frequency bandwidth (<14 kHz).';
  }
  return span;
}

/**
 * Returns true if the track metadata represents the highest-quality YouTube Premium stream
 * (Opus 774 ~256 kbps or AAC 141 256 kbps), excluding upsampled Fake HQ files.
 * @param {string | undefined} ext
 * @param {Record<string, any> | null | undefined} qc
 * @returns {boolean}
 */
export function isYoutubePremiumMax(ext, qc) {
  if (!qc) return false;
  if (String(qc.spectral_status || '').toUpperCase() === 'FAKE_HQ') return false;
  const codec = String(qc.codec || '').toLowerCase();
  const extLow = String(ext || '').toLowerCase();
  if (qc.quality_tier === 'yt_premium_max') return true;
  if (qc.is_youtube_premium === true && qc.is_highest_quality === true) return true;
  const srcFmt = String(qc.source_format_id || '');
  if (extLow !== '.mp3' && (srcFmt === '141' || srcFmt === '774')) return true;
  const brNum = qc.bitrate_kbps ? Math.round(parseFloat(qc.bitrate_kbps)) : 0;
  const isOpus = codec === 'opus' || extLow === '.opus' || extLow === '.webm';
  const isAac = codec === 'aac' || extLow === '.m4a' || extLow === '.aac';
  return (isOpus && brNum >= 195) || (isAac && brNum >= 220);
}

/**
 * Creates a concise stream quality tier badge (LOSSLESS, YT PREMIUM MAX, HQ, or STD).
 * @param {string | undefined} ext
 * @param {Record<string, any> | null | undefined} qc
 * @returns {HTMLSpanElement | null}
 */
export function createQualityTierBadge(ext, qc) {
  if (!qc) return null;
  if (String(qc.spectral_status || '').toUpperCase() === 'FAKE_HQ') return null;

  const codec = String(qc.codec || '').toLowerCase();
  const extLow = String(ext || '').toLowerCase();
  const isLossless =
    ['flac', 'alac', 'wavpack', 'ape', 'tta'].includes(codec) ||
    codec.startsWith('pcm_') ||
    ['.flac', '.wav', '.alac', '.aif', '.aiff'].includes(extLow);
  const brNum = qc.bitrate_kbps ? Math.round(parseFloat(qc.bitrate_kbps)) : 0;

  if (isLossless) {
    const span = document.createElement('span');
    span.className = 'quality-tier-badge lossless';
    span.textContent = 'LOSSLESS';
    span.title = 'Audio lossless (tanpa kompresi lossy)';
    return span;
  }

  if (isYoutubePremiumMax(ext, qc)) {
    const span = document.createElement('span');
    span.className = 'quality-tier-badge yt-premium-max';
    span.textContent = '★ YT PREMIUM MAX';
    const srcFmt = String(qc.source_format_id || '');
    const brLabel = brNum > 0 ? `${brNum} kbps` : '~256 kbps';
    span.title = srcFmt === '141' || srcFmt === '774'
      ? `Terverifikasi Kualitas Tertinggi YouTube Premium (Format ${srcFmt} • ${brLabel})`
      : `Kualitas Tertinggi YouTube Premium (Opus 774 / AAC 141 • ${brLabel})`;
    return span;
  }

  if (brNum >= 192) {
    const span = document.createElement('span');
    span.className = 'quality-tier-badge hq';
    span.textContent = `HQ ${brNum}k`;
    span.title = `Audio High Quality (${brNum} kbps)`;
    return span;
  }

  if (brNum >= 135) {
    const span = document.createElement('span');
    span.className = 'quality-tier-badge standard';
    const prefix = codec === 'opus' || extLow === '.opus' || extLow === '.webm' ? 'PUB' : 'STD';
    span.textContent = `${prefix} ${brNum}k`;
    span.title = `Stream Standar / Publik (${brNum} kbps, di bawah tier YouTube Premium 256 kbps)`;
    return span;
  }

  if (brNum > 0) {
    const span = document.createElement('span');
    span.className = 'quality-tier-badge low';
    span.textContent = `LOW ${brNum}k`;
    span.title = `Stream bitrate rendah (${brNum} kbps)`;
    return span;
  }

  return null;
}

/**
 * Applies YouTube session cookie tier status consistently to indicator + label + pill button.
 * @param {Record<string, any> | null | undefined} data
 * @param {{ dot?: HTMLElement | null, val?: HTMLElement | null, btn?: HTMLElement | null }} els
 */
export function applyCookieStatusUI(data, els = {}) {
  const status = String((data && data.cookies_status) || (data && data.cookies_present ? 'unchecked' : 'missing'));
  const label = String((data && data.cookies_label) || '');
  const note = String((data && data.cookies_note) || '');

  let dotCls = 'status-indicator';
  let btnMod = '';
  let text = label || 'Mode Publik';

  if (status === 'premium') {
    dotCls = 'status-indicator active';
    btnMod = 'is-premium';
    text = label || 'YT Music Premium';
  } else if (status === 'free') {
    dotCls = 'status-indicator active';
    btnMod = 'is-free';
    text = label || 'Akun Standar';
  } else if (status === 'unauthenticated') {
    dotCls = 'status-indicator error';
    btnMod = 'is-expired';
    text = 'Cookies Kadaluarsa';
  } else if (status === 'inconclusive' || status === 'unchecked') {
    dotCls = 'status-indicator warning';
    text = status === 'unchecked' ? 'Cek Sesi YT...' : 'Belum Terverifikasi';
  } else {
    text = 'Mode Publik';
  }

  if (els.dot) els.dot.className = dotCls;
  if (els.val) els.val.textContent = text;
  if (els.btn) {
    els.btn.className = 'header-version-btn header-cookie-btn' + (btnMod ? ' ' + btnMod : '');
    if (note) els.btn.title = `${text} — ${note} (Klik untuk verifikasi ulang)`;
  }
}

const WORKSPACE_STORAGE_KEY = 'swararia_workspace_state_v1';

/**
 * Reads shared workspace selection and source directory from sessionStorage.
 * @returns {{ sourceDir: string, activePlaylist: string, selectedPaths: string[], activeTrackRelPath: string }}
 */
export function getSharedWorkspaceState() {
  try {
    const raw = sessionStorage.getItem(WORKSPACE_STORAGE_KEY);
    if (!raw) return { sourceDir: '', activePlaylist: 'ALL', selectedPaths: [], activeTrackRelPath: '' };
    const parsed = JSON.parse(raw);
    return {
      sourceDir: typeof parsed.sourceDir === 'string' ? parsed.sourceDir : '',
      activePlaylist: typeof parsed.activePlaylist === 'string' ? parsed.activePlaylist : 'ALL',
      selectedPaths: Array.isArray(parsed.selectedPaths) ? parsed.selectedPaths : [],
      activeTrackRelPath: typeof parsed.activeTrackRelPath === 'string' ? parsed.activeTrackRelPath : '',
    };
  } catch (_e) {
    return { sourceDir: '', activePlaylist: 'ALL', selectedPaths: [], activeTrackRelPath: '' };
  }
}

/**
 * Persists partial workspace state (sourceDir, activePlaylist, selectedPaths, activeTrackRelPath) to sessionStorage.
 * @param {{ sourceDir?: string, activePlaylist?: string, selectedPaths?: string[] | Set<string>, activeTrackRelPath?: string }} partial
 */
export function saveSharedWorkspaceState(partial = {}) {
  try {
    const prev = getSharedWorkspaceState();
    const next = {
      sourceDir: partial.sourceDir !== undefined ? String(partial.sourceDir || '') : prev.sourceDir,
      activePlaylist: partial.activePlaylist !== undefined ? String(partial.activePlaylist || 'ALL') : prev.activePlaylist,
      selectedPaths:
        partial.selectedPaths !== undefined
          ? Array.from(partial.selectedPaths)
          : prev.selectedPaths,
      activeTrackRelPath:
        partial.activeTrackRelPath !== undefined
          ? String(partial.activeTrackRelPath || '')
          : prev.activeTrackRelPath,
    };
    sessionStorage.setItem(WORKSPACE_STORAGE_KEY, JSON.stringify(next));
  } catch (_e) {
    // Ignore storage quota errors
  }
}

const RAIL_COLLAPSED_KEY = 'swararia_rail_collapsed';

const STUDIO_STAGES = [
  {
    href: '/',
    key: 'ingest',
    num: '1',
    label: '1. Ingest',
    fullTitle: 'Stage 1 · Batch Audio Ingestion',
    icons: [
      { tag: 'path', attrs: { d: 'M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4' } },
      { tag: 'polyline', attrs: { points: '7 10 12 15 17 10' } },
      { tag: 'line', attrs: { x1: '12', y1: '15', x2: '12', y2: '3' } },
    ],
  },
  {
    href: '/qc',
    key: 'qc',
    num: '2',
    label: '2. QC Lab',
    fullTitle: 'Stage 2 · Soundsystem QC & Spectral Lab',
    icons: [
      { tag: 'path', attrs: { d: 'M2 10v3' } },
      { tag: 'path', attrs: { d: 'M6 6v11' } },
      { tag: 'path', attrs: { d: 'M10 3v18' } },
      { tag: 'path', attrs: { d: 'M14 8v7' } },
      { tag: 'path', attrs: { d: 'M18 5v13' } },
      { tag: 'path', attrs: { d: 'M22 10v3' } },
    ],
  },
  {
    href: '/converter',
    key: 'converter',
    num: '3',
    label: '3. Converter',
    fullTitle: 'Stage 3 · Sample Rate & EBU R128',
    icons: [
      { tag: 'polyline', attrs: { points: '17 1 21 5 17 9' } },
      { tag: 'path', attrs: { d: 'M3 11V9a4 4 0 0 1 4-4h14' } },
      { tag: 'polyline', attrs: { points: '7 23 3 19 7 15' } },
      { tag: 'path', attrs: { d: 'M21 13v2a4 4 0 0 1-4 4H3' } },
    ],
  },
  {
    href: '/organizer',
    key: 'organizer',
    num: '4',
    label: '4. Organizer',
    fullTitle: 'Stage 4 · Tag & File Organizer',
    icons: [
      { tag: 'line', attrs: { x1: '8', y1: '6', x2: '21', y2: '6' } },
      { tag: 'line', attrs: { x1: '8', y1: '12', x2: '21', y2: '12' } },
      { tag: 'line', attrs: { x1: '8', y1: '18', x2: '21', y2: '18' } },
      { tag: 'line', attrs: { x1: '3', y1: '6', x2: '3.01', y2: '6' } },
      { tag: 'line', attrs: { x1: '3', y1: '12', x2: '3.01', y2: '12' } },
      { tag: 'line', attrs: { x1: '3', y1: '18', x2: '3.01', y2: '18' } },
    ],
  },
  {
    href: '/multitrack',
    key: 'multitrack',
    num: '5',
    label: '5. Multitrack',
    fullTitle: 'Stage 5 · Multitrack Stem Splitter',
    icons: [
      { tag: 'line', attrs: { x1: '4', y1: '21', x2: '4', y2: '14' } },
      { tag: 'line', attrs: { x1: '4', y1: '10', x2: '4', y2: '3' } },
      { tag: 'line', attrs: { x1: '12', y1: '21', x2: '12', y2: '12' } },
      { tag: 'line', attrs: { x1: '12', y1: '8', x2: '12', y2: '3' } },
      { tag: 'line', attrs: { x1: '20', y1: '21', x2: '20', y2: '16' } },
      { tag: 'line', attrs: { x1: '20', y1: '12', x2: '20', y2: '3' } },
      { tag: 'circle', attrs: { cx: '4', cy: '12', r: '2' } },
      { tag: 'circle', attrs: { cx: '12', cy: '10', r: '2' } },
      { tag: 'circle', attrs: { cx: '20', cy: '14', r: '2' } },
    ],
  },
  {
    href: '/video-split',
    key: 'video-split',
    num: '6',
    label: '6. Video Split',
    fullTitle: 'Stage 6 · Concert Video Splitter',
    icons: [
      { tag: 'rect', attrs: { x: '2', y: '2', width: '20', height: '20', rx: '2.18', ry: '2.18' } },
      { tag: 'line', attrs: { x1: '7', y1: '2', x2: '7', y2: '22' } },
      { tag: 'line', attrs: { x1: '17', y1: '2', x2: '17', y2: '22' } },
      { tag: 'line', attrs: { x1: '2', y1: '12', x2: '22', y2: '12' } },
      { tag: 'line', attrs: { x1: '2', y1: '7', x2: '7', y2: '7' } },
      { tag: 'line', attrs: { x1: '2', y1: '17', x2: '7', y2: '17' } },
      { tag: 'line', attrs: { x1: '17', y1: '7', x2: '22', y2: '7' } },
      { tag: 'line', attrs: { x1: '17', y1: '17', x2: '22', y2: '17' } },
    ],
  },
  {
    href: '/player',
    key: 'player',
    num: '7',
    label: '7. FOH Player',
    fullTitle: 'Stage 7 · FOH Live Player & Cue Deck',
    icons: [{ tag: 'polygon', attrs: { points: '5 3 19 12 5 21 5 3' } }],
  },
];

let shellEventsBound = false;

function getStageMeta(pageKey) {
  return STUDIO_STAGES.find((s) => s.key === pageKey) || STUDIO_STAGES[0];
}

function isRemoteClientContext() {
  if (typeof document !== 'undefined' && document.body?.classList?.contains('remote-controller-mode')) {
    return true;
  }
  if (typeof window === 'undefined' || !window.location) return false;
  const params = new URLSearchParams(window.location.search || '');
  const host = window.location.hostname || '127.0.0.1';
  return params.get('remote') === '1' || !['127.0.0.1', 'localhost', '::1', ''].includes(host);
}

function readRailCollapsedPref() {
  try {
    return typeof localStorage !== 'undefined' && localStorage.getItem(RAIL_COLLAPSED_KEY) === '1';
  } catch (_e) {
    return false;
  }
}

export function setStudioRailCollapsed(collapsed) {
  const next = Boolean(collapsed);
  if (typeof document !== 'undefined' && document.body) {
    document.body.classList.toggle('rail-collapsed', next);
  }
  const btn = typeof document !== 'undefined' ? document.getElementById('btnToggleRail') : null;
  if (btn) {
    btn.setAttribute('aria-expanded', next ? 'false' : 'true');
    btn.title = next ? 'Perluas sidebar stage (Alt+B)' : 'Ciutkan sidebar stage (Alt+B)';
    const lbl = btn.querySelector('.rail-toggle-text');
    if (lbl) lbl.textContent = next ? 'Expand' : 'Sidebar';
  }
  try {
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem(RAIL_COLLAPSED_KEY, next ? '1' : '0');
    }
  } catch (_e) {
    // Ignore storage quota errors
  }
}

function updateShellStageReadouts(pageKey) {
  const meta = getStageMeta(pageKey);
  if (typeof document === 'undefined') return;
  const headerChip = document.getElementById('headerStageChip');
  if (headerChip) {
    headerChip.textContent = meta.fullTitle;
  }
  const statusStage = document.getElementById('statusBarStage');
  if (statusStage) {
    statusStage.textContent = meta.fullTitle;
  }
  document.querySelectorAll('.app-nav .nav-tab').forEach((tab) => {
    const isMatch = tab.dataset?.stageKey === meta.key;
    tab.classList.toggle('active', isMatch);
  });
}

function updateStatusBarDiagnostics(data) {
  if (!data || typeof document === 'undefined') return;
  const ffDot = document.getElementById('sbFfmpegDot');
  const ffVal = document.getElementById('sbFfmpegVal');
  if (ffDot && ffVal) {
    const ready = Boolean(data.ffmpeg && data.ffprobe);
    ffDot.className = ready ? 'status-indicator active' : 'status-indicator error';
    ffVal.textContent = ready ? 'FFmpeg Ready' : data.ffmpeg ? 'No ffprobe' : 'No FFmpeg';
  }
  const ytDot = document.getElementById('sbYtdlpDot');
  const ytVal = document.getElementById('sbYtdlpVal');
  if (ytDot && ytVal) {
    ytDot.className = data.yt_dlp ? 'status-indicator active' : 'status-indicator error';
    ytVal.textContent = data.yt_dlp ? 'yt-dlp Ready' : 'No yt-dlp';
  }
  const pyVal = document.getElementById('sbPythonVal');
  if (pyVal && data.python) {
    pyVal.textContent = 'Py ' + data.python;
  }
}

function ensureStudioRail(headerEl, activePage) {
  if (typeof document === 'undefined' || !document.body) return null;
  let rail = document.getElementById('studioRail');
  const isNew = !rail;
  if (!rail) {
    rail = document.createElement('aside');
    rail.id = 'studioRail';
    rail.className = 'studio-rail';
    rail.setAttribute('aria-label', 'Workstation Stages');
  }
  rail.replaceChildren();

  const railHead = document.createElement('div');
  railHead.className = 'studio-rail-head';

  const railCaption = document.createElement('span');
  railCaption.className = 'studio-rail-caption';
  railCaption.textContent = 'MODULES';

  const toggleBtn = document.createElement('button');
  toggleBtn.type = 'button';
  toggleBtn.id = 'btnToggleRail';
  toggleBtn.className = 'rail-toggle-btn';
  toggleBtn.appendChild(createSvgIcon([
    { tag: 'rect', attrs: { x: '3', y: '3', width: '18', height: '18', rx: '2' } },
    { tag: 'line', attrs: { x1: '9', y1: '3', x2: '9', y2: '21' } },
  ]));
  const toggleText = document.createElement('span');
  toggleText.className = 'rail-toggle-text';
  toggleText.textContent = 'Sidebar';
  toggleBtn.appendChild(toggleText);
  toggleBtn.addEventListener('click', () => {
    const collapsed = document.body.classList.contains('rail-collapsed');
    setStudioRailCollapsed(!collapsed);
  });

  railHead.appendChild(railCaption);
  railHead.appendChild(toggleBtn);

  const nav = document.createElement('nav');
  nav.className = 'app-nav';
  nav.setAttribute('aria-label', 'Main Navigation');

  STUDIO_STAGES.forEach((stage) => {
    const a = document.createElement('a');
    a.href = stage.href;
    a.className = 'nav-tab' + (activePage === stage.key ? ' active' : '');
    a.dataset.stageKey = stage.key;
    a.title = `${stage.fullTitle} (Alt+${stage.num})`;
    a.appendChild(createSvgIcon(stage.icons));

    const labelSpan = document.createElement('span');
    labelSpan.className = 'nav-tab-label';
    labelSpan.textContent = stage.label;
    a.appendChild(labelSpan);

    const kbd = document.createElement('kbd');
    kbd.className = 'nav-tab-kbd';
    kbd.textContent = `Alt+${stage.num}`;
    a.appendChild(kbd);

    a.addEventListener('click', (e) => {
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      e.preventDefault();
      spaNavigate(stage.href);
    });
    nav.appendChild(a);
  });

  rail.appendChild(railHead);
  rail.appendChild(nav);

  if (isNew) {
    if (typeof headerEl.after === 'function') {
      headerEl.after(rail);
    } else {
      document.body.appendChild(rail);
    }
  }
  setStudioRailCollapsed(readRailCollapsedPref());
  return rail;
}

function ensureStudioStatusBar(activePage) {
  if (typeof document === 'undefined' || !document.body) return null;
  let bar = document.getElementById('studioStatusbar');
  if (bar) {
    updateShellStageReadouts(activePage);
    return bar;
  }

  bar = document.createElement('div');
  bar.id = 'studioStatusbar';
  bar.className = 'studio-statusbar';
  bar.setAttribute('role', 'status');
  bar.setAttribute('aria-label', 'System Diagnostics');

  const left = document.createElement('div');
  left.className = 'statusbar-group';

  const hostItem = document.createElement('span');
  hostItem.className = 'statusbar-item statusbar-host';
  const hostDot = document.createElement('span');
  hostDot.className = 'status-indicator active';
  const hostCode = document.createElement('code');
  hostCode.textContent = (typeof window !== 'undefined' && window.location && window.location.host)
    ? window.location.host
    : '127.0.0.1:8765';
  hostItem.appendChild(hostDot);
  hostItem.appendChild(hostCode);

  const stageItem = document.createElement('span');
  stageItem.id = 'statusBarStage';
  stageItem.className = 'statusbar-item statusbar-stage';
  stageItem.textContent = getStageMeta(activePage).fullTitle;

  left.appendChild(hostItem);
  left.appendChild(stageItem);

  const right = document.createElement('div');
  right.className = 'statusbar-group';

  const ffItem = document.createElement('span');
  ffItem.className = 'statusbar-item';
  const ffDot = document.createElement('span');
  ffDot.id = 'sbFfmpegDot';
  ffDot.className = 'status-indicator';
  const ffVal = document.createElement('span');
  ffVal.id = 'sbFfmpegVal';
  ffVal.textContent = 'FFmpeg...';
  ffItem.appendChild(ffDot);
  ffItem.appendChild(ffVal);

  const ytItem = document.createElement('span');
  ytItem.className = 'statusbar-item';
  const ytDot = document.createElement('span');
  ytDot.id = 'sbYtdlpDot';
  ytDot.className = 'status-indicator';
  const ytVal = document.createElement('span');
  ytVal.id = 'sbYtdlpVal';
  ytVal.textContent = 'yt-dlp...';
  ytItem.appendChild(ytDot);
  ytItem.appendChild(ytVal);

  const pyItem = document.createElement('span');
  pyItem.id = 'sbPythonVal';
  pyItem.className = 'statusbar-item statusbar-muted';
  pyItem.textContent = 'Py 3';

  const hintItem = document.createElement('span');
  hintItem.className = 'statusbar-item statusbar-hint';
  hintItem.textContent = 'Alt+1–7 Stage · Alt+B Sidebar';

  right.appendChild(ffItem);
  right.appendChild(ytItem);
  right.appendChild(pyItem);
  right.appendChild(hintItem);

  bar.appendChild(left);
  bar.appendChild(right);
  document.body.appendChild(bar);
  return bar;
}

function bindShellGlobalEvents() {
  if (shellEventsBound || typeof window === 'undefined' || typeof document === 'undefined') return;
  shellEventsBound = true;

  window.addEventListener('swararia:route-changed', (e) => {
    const key = e?.detail?.key;
    if (key) updateShellStageReadouts(key);
  });

  window.addEventListener('swararia:system-status', (e) => {
    updateStatusBarDiagnostics(e?.detail);
  });

  document.addEventListener('keydown', (e) => {
    if (!e.altKey || e.ctrlKey || e.metaKey || isRemoteClientContext()) return;
    const active = document.activeElement;
    const tag = active ? active.tagName : '';
    if (['INPUT', 'TEXTAREA', 'SELECT'].includes(tag) || active?.isContentEditable) return;
    if (document.querySelector('.modal-backdrop:not(.hidden)')) return;

    const lower = String(e.key || '').toLowerCase();
    if (lower === 'b') {
      e.preventDefault();
      setStudioRailCollapsed(!document.body.classList.contains('rail-collapsed'));
      return;
    }
    const idx = Number(e.key) - 1;
    if (Number.isInteger(idx) && idx >= 0 && idx < STUDIO_STAGES.length) {
      e.preventDefault();
      spaNavigate(STUDIO_STAGES[idx].href);
    }
  });
}

/**
 * Shared Studio App Shell:
 * Mounts the compact 40px top command bar, persistent left stage rail, and bottom diagnostics statusbar,
 * and fetches /api/system to update status indicators.
 *
 * @param {{
 *   activePage?: 'ingest' | 'qc' | 'converter' | 'organizer' | 'multitrack' | 'video-split' | 'player',
 *   onSystemData?: (data: any) => void,
 *   onSystemError?: (err: Error) => void
 * }} opts
 */
export function initAppHeader(opts = {}) {
  const headerEl = document.getElementById('appHeader') || document.querySelector('.app-header');
  if (!headerEl) return {};

  const activePage = opts.activePage || headerEl.dataset.page || 'ingest';
  if (typeof document !== 'undefined' && document.body) {
    document.body.dataset.page = activePage;
  }
  headerEl.replaceChildren();

  // 1. Brand Block & Active Stage Breadcrumb
  const brand = document.createElement('div');
  brand.className = 'brand';

  const brandIcon = document.createElement('div');
  brandIcon.className = 'brand-icon';
  const brandLogoImg = document.createElement('img');
  brandLogoImg.src = '/static/assets/swararia-logo.jpg';
  brandLogoImg.alt = 'SWARARIA AUDIO LAB';
  brandLogoImg.className = 'brand-logo-img';
  brandIcon.appendChild(brandLogoImg);

  const brandInfo = document.createElement('div');
  brandInfo.className = 'brand-info';

  const brandTitle = document.createElement('h1');
  brandTitle.className = 'brand-title';
  brandTitle.appendChild(document.createTextNode('SWARARIA AUDIO LAB '));
  const studioBadge = document.createElement('span');
  studioBadge.className = 'badge-studio';
  studioBadge.textContent = 'WORKSTATION';
  brandTitle.appendChild(studioBadge);

  const brandSubtitle = document.createElement('p');
  brandSubtitle.className = 'brand-subtitle';
  brandSubtitle.textContent = 'Pro Audio & Video Production Suite';

  brandInfo.appendChild(brandTitle);
  brandInfo.appendChild(brandSubtitle);
  brand.appendChild(brandIcon);
  brand.appendChild(brandInfo);

  const stageChip = document.createElement('span');
  stageChip.id = 'headerStageChip';
  stageChip.className = 'header-stage-chip';
  stageChip.textContent = getStageMeta(activePage).fullTitle;

  // 2. Compact Header Actions (YouTube Session Tier + Version + Notification Center)
  const statusGroup = document.createElement('div');
  statusGroup.className = 'header-actions-group';
  statusGroup.id = 'systemStatusGroup';

  const cookieBtn = document.createElement('button');
  cookieBtn.type = 'button';
  cookieBtn.className = 'header-version-btn header-cookie-btn';
  cookieBtn.id = 'headerCookieBtn';
  cookieBtn.title = 'Status sesi YouTube Music Premium (klik untuk cek ulang)';

  const cookieDot = document.createElement('span');
  cookieDot.className = 'status-indicator';
  cookieDot.id = 'headerCookieDot';

  const cookieVal = document.createElement('span');
  cookieVal.id = 'headerCookieVal';
  cookieVal.textContent = 'Cek Sesi...';

  cookieBtn.appendChild(cookieDot);
  cookieBtn.appendChild(cookieVal);
  statusGroup.appendChild(cookieBtn);

  const updateBtn = document.createElement('button');
  updateBtn.type = 'button';
  updateBtn.className = 'header-version-btn';
  updateBtn.id = 'statusUpdate';
  updateBtn.title = 'Swararia Audio Lab — Klik untuk periksa pembaruan';

  const updateDot = document.createElement('span');
  updateDot.className = 'status-indicator active';
  updateDot.id = 'updateDot';

  const updateVal = document.createElement('span');
  updateVal.id = 'updateVal';
  updateVal.textContent = 'v1.0.0';

  updateBtn.appendChild(updateDot);
  updateBtn.appendChild(updateVal);

  updateBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    openUpdateModal(true);
  });

  statusGroup.appendChild(updateBtn);

  // Notification Bell & History Popover in Header
  const notifWrapper = createHeaderNotificationCenter();
  statusGroup.appendChild(notifWrapper);

  headerEl.appendChild(brand);
  headerEl.appendChild(stageChip);
  headerEl.appendChild(statusGroup);

  // 3. Persistent Left Stage Rail & Bottom Statusbar
  ensureStudioRail(headerEl, activePage);
  ensureStudioStatusBar(activePage);
  bindShellGlobalEvents();

  let backgroundVerifyStarted = false;

  function applyAndBroadcast(data) {
    if (data.app_version) {
      updateVal.textContent = 'v' + data.app_version;
    }
    if (typeof window !== 'undefined' && typeof CustomEvent === 'function') {
      window.dispatchEvent(new CustomEvent('swararia:system-status', { detail: data }));
    }
    if (typeof opts.onSystemData === 'function') {
      opts.onSystemData(data);
    }
  }

  function refreshStatus(verifyCookies = false) {
    return checkSystemStatus({
      updateVal: updateVal,
      cookiesDot: cookieDot,
      cookiesVal: cookieVal,
      cookiesBtn: cookieBtn,
      verifyCookies: verifyCookies,
      onData: (data) => {
        applyAndBroadcast(data);
        if (!verifyCookies && !backgroundVerifyStarted && data.cookies_present && data.cookies_status === 'unchecked') {
          backgroundVerifyStarted = true;
          setTimeout(() => {
            refreshStatus(true);
          }, 120);
        }
      },
      onError: opts.onSystemError,
    });
  }

  cookieBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    cookieVal.textContent = 'Memeriksa...';
    refreshStatus(true).then((data) => {
      if (!data) return;
      if (typeof window !== 'undefined' && typeof CustomEvent === 'function') {
        window.dispatchEvent(new CustomEvent('swararia:header-cookie-click', { detail: data }));
      }
      const t = data.cookies_status === 'premium' ? 'success'
        : data.cookies_status === 'unauthenticated' ? 'error'
        : 'info';
      notify({
        type: t,
        title: data.cookies_label || 'Status Cookies YouTube',
        message: data.cookies_note || 'Verifikasi selesai.',
        duration: 4200,
      });
    });
  });

  refreshStatus(false);
  initSpaRouter();

  // Background check for GitHub updates shortly after startup
  setTimeout(() => {
    checkForUpdatesUI({ val: updateVal, dot: updateDot, pill: updateBtn }, false);
  }, 2500);

  return { refreshStatus };
}

/**
 * Appends a single log line to a terminal container element.
 * @param {HTMLElement | null} terminalEl
 * @param {'info' | 'warn' | 'error'} level
 * @param {string} message
 */
export function appendLog(terminalEl, level, message) {
  if (!terminalEl) return;
  const p = document.createElement('p');
  p.className = 'log-line ' + level;
  p.textContent = message;
  terminalEl.appendChild(p);
  terminalEl.scrollTop = terminalEl.scrollHeight;
}

/**
 * Fetches /api/system and updates system status indicators across pages.
 * @param {{
 *   pythonVal?: HTMLElement | null,
 *   ytdlpDot?: HTMLElement | null,
 *   ytdlpVal?: HTMLElement | null,
 *   ffmpegDot?: HTMLElement | null,
 *   ffmpegVal?: HTMLElement | null,
 *   ffmpegReadyLabel?: string,
 *   requireFfprobe?: boolean,
 *   cookiesDot?: HTMLElement | null,
 *   cookiesVal?: HTMLElement | null,
 *   onData?: (data: any) => void,
 *   onError?: (err: Error) => void
 * }} opts
 */
export async function checkSystemStatus(opts = {}) {
  const endpoint = opts.verifyCookies ? '/api/system?verify_cookies=1' : '/api/system';
  const { ok, data, error } = await apiGet(endpoint);
  if (!ok) {
    if (error && typeof opts.onError === 'function') {
      opts.onError(new Error(error));
    }
    return null;
  }

  if (opts.pythonVal) {
    opts.pythonVal.textContent = data.python || '3.12';
  }

  if (opts.ytdlpDot && opts.ytdlpVal) {
    if (data.yt_dlp) {
      opts.ytdlpDot.className = 'status-indicator active';
      opts.ytdlpVal.textContent = 'Installed';
    } else {
      opts.ytdlpDot.className = 'status-indicator error';
      opts.ytdlpVal.textContent = 'Missing';
    }
  }

  if (opts.ffmpegDot && opts.ffmpegVal) {
    const ffmpegOk = opts.requireFfprobe ? Boolean(data.ffmpeg && data.ffprobe) : Boolean(data.ffmpeg);
    if (ffmpegOk) {
      opts.ffmpegDot.className = 'status-indicator active';
      opts.ffmpegVal.textContent = opts.ffmpegReadyLabel || 'Installed';
    } else {
      opts.ffmpegDot.className = 'status-indicator error';
      opts.ffmpegVal.textContent = 'Missing';
    }
  }

  if (opts.cookiesDot || opts.cookiesVal || opts.cookiesBtn) {
    applyCookieStatusUI(data, {
      dot: opts.cookiesDot || null,
      val: opts.cookiesVal || null,
      btn: opts.cookiesBtn || null,
    });
  }

  if (opts.updateVal && data.app_version) {
    opts.updateVal.textContent = 'v' + data.app_version;
  }

  if (typeof opts.onData === 'function') {
    opts.onData(data);
  }
  return data;
}

/**
 * Sends a request to open a folder in Windows File Explorer (or host file manager).
 * @param {string} [folderPath] - Relative subfolder or full path.
 * @param {string} [sourceDir] - Base source directory.
 * @returns {Promise<boolean>}
 */
export async function openFolderInExplorer(folderPath = '', sourceDir = '') {
  notify({
    type: 'info',
    title: 'Membuka File Explorer...',
    message: 'Mengirim perintah buka direktori ke Windows Explorer...',
    duration: 2500,
  });

  const { ok, data, error } = await apiPost('/api/open-folder', {
    source_dir: sourceDir || '',
    folder: folderPath || '',
  });

  if (ok) {
    notify({
      type: 'success',
      title: 'Windows Explorer Dibuka',
      message: data?.message || 'Folder berhasil dibuka di File Explorer.',
      duration: 3800,
    });
    return true;
  } else {
    notify({
      type: 'error',
      title: 'Gagal Membuka Explorer',
      message: error || 'Gagal membuka folder di Windows Explorer.',
      duration: 5000,
      playSound: true,
    });
    return false;
  }
}

/**
 * Formats the source folder badge text truthfully across all pages.
 * @param {string} sourceDir
 * @param {boolean} isDefault
 * @returns {string}
 */
export function formatSourceBadgeText(sourceDir, isDefault) {
  const clean = String(sourceDir || '').trim();
  if (!clean) return 'SwarariaAudioLab (Koleksi Default)';
  return isDefault ? `${clean} (Default)` : clean;
}

/**
 * Persists a folder path as the server-wide default audio library (or resets to OS default).
 * @param {string} libraryDir
 * @param {{ reset?: boolean }} [opts]
 * @returns {Promise<{ ok: boolean, data: any, error?: string }>}
 */
export async function saveDefaultLibraryDir(libraryDir = '', opts = {}) {
  const payload = opts.reset ? { reset: true } : { library_dir: String(libraryDir || '').trim() };
  const res = await apiPost('/api/settings/library', payload);
  if (res.ok) {
    if (res.data?.library_dir) {
      saveSharedWorkspaceState({ sourceDir: res.data.library_dir });
    }
    if (typeof window !== 'undefined' && typeof CustomEvent === 'function') {
      window.dispatchEvent(new CustomEvent('swararia:library-changed', {
        detail: { reason: 'default-library-changed', libraryDir: res.data?.library_dir || '' },
      }));
    }
    notify({
      type: 'success',
      title: 'Folder Koleksi Default Disimpan',
      message: res.data?.message || 'Folder koleksi default berhasil diperbarui.',
      duration: 4200,
    });
  } else {
    notify({
      type: 'error',
      title: 'Gagal Menyimpan Folder Default',
      message: res.error || res.data?.error || 'Tidak dapat menyimpan folder koleksi default.',
      playSound: true,
    });
  }
  return res;
}

// ==============================================================================
// GITHUB RELEASE UPDATE CHECKER & IN-APP AUTO-INSTALLER
// ==============================================================================

let cachedUpdateData = null;
let isCheckingUpdate = false;

/**
 * Checks for software updates from GitHub Releases API and updates header status pill.
 * @param {{ val?: HTMLElement, dot?: HTMLElement, pill?: HTMLElement } | null} updatePill
 * @param {boolean} [isManual=false]
 * @returns {Promise<any>}
 */
export async function checkForUpdatesUI(updatePill, isManual = false) {
  if (isCheckingUpdate) return cachedUpdateData;
  isCheckingUpdate = true;
  if (updatePill && isManual && updatePill.val) {
    updatePill.val.textContent = 'checking...';
  }

  try {
    const { ok, data } = await apiGet('/api/check-update');
    isCheckingUpdate = false;
    if (ok && data) {
      cachedUpdateData = data;
      if (updatePill) {
        if (data.has_update) {
          if (updatePill.val) updatePill.val.textContent = (data.latest_version || 'new') + ' ⚡';
          if (updatePill.dot) updatePill.dot.className = 'status-indicator update-available';
          if (updatePill.pill) {
            updatePill.pill.classList.add('has-update');
            updatePill.pill.title = 'Versi baru ' + data.latest_version + ' tersedia! Klik untuk perbarui.';
          }
          if (!isManual) {
            notify({
              type: 'info',
              title: 'Pembaruan Tersedia',
              message: 'Swararia Audio Lab ' + data.latest_version + ' telah dirilis. Klik badge versi untuk detail.',
              duration: 8000,
            });
          }
        } else {
          if (updatePill.val) updatePill.val.textContent = 'v' + (data.current_version || '1.0.0');
          if (updatePill.dot) updatePill.dot.className = 'status-indicator active';
          if (updatePill.pill) {
            updatePill.pill.classList.remove('has-update');
            updatePill.pill.title = 'Swararia Audio Lab v' + (data.current_version || '1.0.0') + ' (Versi Terbaru)';
          }
        }
      }
      return data;
    }
  } catch (err) {
    isCheckingUpdate = false;
    console.error('[Update Checker] Gagal memeriksa update:', err);
  }
  return null;
}

/**
 * Opens the release update modal dialog with changelog and 1-click installer.
 * @param {boolean} [isManual=true]
 */
export function openUpdateModal(isManual = true) {
  const existing = document.getElementById('updateModalBackdrop');
  if (existing) existing.remove();

  const backdrop = document.createElement('div');
  backdrop.className = 'modal-backdrop';
  backdrop.id = 'updateModalBackdrop';

  const card = document.createElement('div');
  card.className = 'modal-card modal-update-card';

  // 1. Header
  const header = document.createElement('div');
  header.className = 'modal-header';

  const h3 = document.createElement('h3');
  h3.textContent = 'Pembaruan Swararia Audio Lab';

  const closeBtn = document.createElement('button');
  closeBtn.type = 'button';
  closeBtn.className = 'modal-close-btn';
  closeBtn.title = 'Tutup';
  closeBtn.appendChild(createSvgIcon([
    { tag: 'line', attrs: { x1: '18', y1: '6', x2: '6', y2: '18' } },
    { tag: 'line', attrs: { x1: '6', y1: '6', x2: '18', y2: '18' } },
  ]));
  closeBtn.addEventListener('click', () => backdrop.remove());

  header.appendChild(h3);
  header.appendChild(closeBtn);

  // 2. Body
  const body = document.createElement('div');
  body.className = 'modal-body';

  const loadingWrap = document.createElement('div');
  loadingWrap.className = 'update-loading-wrap';
  loadingWrap.textContent = 'Memeriksa rilis terbaru dari GitHub dediks/swr-player-desktop...';
  body.appendChild(loadingWrap);

  // 3. Footer
  const footer = document.createElement('div');
  footer.className = 'modal-footer';

  const btnClose = document.createElement('button');
  btnClose.type = 'button';
  btnClose.className = 'btn-modal-cancel';
  btnClose.textContent = 'Tutup';
  btnClose.addEventListener('click', () => backdrop.remove());
  footer.appendChild(btnClose);

  card.appendChild(header);
  card.appendChild(body);
  card.appendChild(footer);
  backdrop.appendChild(card);
  document.body.appendChild(backdrop);

  backdrop.addEventListener('click', (e) => {
    if (e.target === backdrop) backdrop.remove();
  });

  const onKeyDown = (e) => {
    if (e.key === 'Escape') {
      backdrop.remove();
      window.removeEventListener('keydown', onKeyDown);
    }
  };
  window.addEventListener('keydown', onKeyDown);

  const renderData = (data) => {
    body.replaceChildren();

    if (!data || !data.ok) {
      const banner = document.createElement('div');
      banner.className = 'update-banner-card error';

      const title = document.createElement('h4');
      title.className = 'update-banner-title';
      title.textContent = '⚠️ Tidak Dapat Memeriksa Rilis';

      const desc = document.createElement('p');
      desc.className = 'update-banner-sub';
      desc.textContent = (data && data.error) ? data.error : 'Pastikan komputer terhubung ke internet atau periksa repositori GitHub.';

      banner.appendChild(title);
      banner.appendChild(desc);
      body.appendChild(banner);

      const actions = document.createElement('div');
      actions.className = 'update-actions-bar';

      const ghLink = document.createElement('a');
      ghLink.className = 'btn-update-secondary';
      ghLink.href = (data && data.html_url) || 'https://github.com/dediks/swr-player-desktop';
      ghLink.target = '_blank';
      ghLink.rel = 'noopener noreferrer';
      ghLink.textContent = '🌐 Buka Repository GitHub';
      actions.appendChild(ghLink);

      body.appendChild(actions);
      return;
    }

    if (data.has_update) {
      const banner = document.createElement('div');
      banner.className = 'update-banner-card has-update';

      const title = document.createElement('h4');
      title.className = 'update-banner-title';
      title.textContent = '⚡ Versi Baru Tersedia: ' + data.latest_version;

      const sub = document.createElement('p');
      sub.className = 'update-banner-sub';
      const pubDate = data.published_at ? data.published_at.substring(0, 10) : '';
      sub.textContent = 'Versi Anda saat ini: v' + data.current_version + (pubDate ? ' • Dirilis pada: ' + pubDate : '');

      banner.appendChild(title);
      banner.appendChild(sub);
      body.appendChild(banner);

      if (data.release_name && data.release_name !== data.latest_version) {
        const relName = document.createElement('div');
        relName.className = 'update-section-title';
        relName.textContent = data.release_name;
        body.appendChild(relName);
      }

      const notesTitle = document.createElement('div');
      notesTitle.className = 'update-section-title';
      notesTitle.textContent = 'Catatan Rilis (Changelog):';
      body.appendChild(notesTitle);

      const changelog = document.createElement('pre');
      changelog.className = 'update-changelog';
      changelog.textContent = data.release_notes || 'Tidak ada catatan rilis tertulis.';
      body.appendChild(changelog);

      const actions = document.createElement('div');
      actions.className = 'update-actions-bar';

      if (data.download_url) {
        const btnInstall = document.createElement('button');
        btnInstall.type = 'button';
        btnInstall.className = 'btn-update-primary';
        btnInstall.textContent = '⚡ Unduh & Pasang Pembaruan';
        btnInstall.addEventListener('click', async () => {
          btnInstall.disabled = true;
          btnInstall.textContent = 'Mengunduh... Mohon tunggu (aplikasi akan restart)';
          try {
            const res = await apiPost('/api/install-update', { download_url: data.download_url });
            if (res.ok) {
              btnInstall.textContent = 'Installer sedang dibuka... Menutup aplikasi.';
            } else {
              btnInstall.disabled = false;
              btnInstall.textContent = 'Gagal memasang: ' + (res.data?.message || res.error || 'Unknown error');
            }
          } catch (err) {
            btnInstall.disabled = false;
            btnInstall.textContent = 'Gagal: ' + err.message;
          }
        });
        actions.appendChild(btnInstall);
      }

      const ghLink = document.createElement('a');
      ghLink.className = 'btn-update-secondary';
      ghLink.href = data.html_url || 'https://github.com/dediks/swr-player-desktop/releases';
      ghLink.target = '_blank';
      ghLink.rel = 'noopener noreferrer';
      ghLink.textContent = '🌐 Buka Rilis di GitHub';
      actions.appendChild(ghLink);

      body.appendChild(actions);
    } else {
      const banner = document.createElement('div');
      banner.className = 'update-banner-card up-to-date';

      const title = document.createElement('h4');
      title.className = 'update-banner-title';
      title.textContent = '✅ Anda Menggunakan Versi Terbaru (v' + data.current_version + ')';

      const sub = document.createElement('p');
      sub.className = 'update-banner-sub';
      sub.textContent = data.message || 'Sistem telah memeriksa rilis resmi terbaru di GitHub dediks/swr-player-desktop.';

      banner.appendChild(title);
      banner.appendChild(sub);
      body.appendChild(banner);

      const actions = document.createElement('div');
      actions.className = 'update-actions-bar';

      const ghLink = document.createElement('a');
      ghLink.className = 'btn-update-secondary';
      ghLink.href = data.html_url || 'https://github.com/dediks/swr-player-desktop';
      ghLink.target = '_blank';
      ghLink.rel = 'noopener noreferrer';
      ghLink.textContent = '🌐 Kunjungi Repository GitHub';
      actions.appendChild(ghLink);

      body.appendChild(actions);
    }
  };

  const pill = document.getElementById('statusUpdate');
  checkForUpdatesUI(pill ? { val: document.getElementById('updateVal'), dot: document.getElementById('updateDot'), pill } : null, true)
    .then((data) => {
      renderData(data);
    })
    .catch((err) => {
      renderData({ ok: false, error: String(err) });
    });
}
