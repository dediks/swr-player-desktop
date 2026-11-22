import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const root = path.resolve(import.meta.dirname, '../..');
const uri = (text) => 'data:text/javascript;base64,' + Buffer.from(text).toString('base64');

// ---------------------------------------------------------------------------
// 1. Verify queue-monitor dispatches swararia:library-changed on download done
// ---------------------------------------------------------------------------
{
  const sessionStore = new Map();
  const dispatched = [];
  const notifCalls = [];

  globalThis.sessionStorage = {
    getItem: (k) => (sessionStore.has(k) ? sessionStore.get(k) : null),
    setItem: (k, v) => sessionStore.set(k, String(v)),
  };
  globalThis.CustomEvent = class CustomEvent extends Event {
    constructor(type, init = {}) {
      super(type);
      this.detail = init.detail;
    }
  };
  const winTarget = new EventTarget();
  globalThis.window = {
    addEventListener: winTarget.addEventListener.bind(winTarget),
    dispatchEvent: (ev) => {
      dispatched.push(ev);
      return winTarget.dispatchEvent(ev);
    },
  };

  let queueResponse = { current: null, queue: [], history: [] };
  globalThis.__testQueueResp = () => queueResponse;
  globalThis.__testNotifs = notifCalls;

  const apiStubUrl = uri(`
    export async function apiGet(url) {
      if (url === '/api/queue') return { ok: true, data: globalThis.__testQueueResp() };
      return { ok: false, data: {} };
    }
  `);
  const notifStubUrl = uri(`
    export function notify(payload) { globalThis.__testNotifs.push(payload); }
  `);

  const qmSrc = fs
    .readFileSync(path.join(root, 'swararia/web/static/modules/queue-monitor.js'), 'utf8')
    .replace("'./api-client.js'", JSON.stringify(apiStubUrl))
    .replace("'./notifications.js'", JSON.stringify(notifStubUrl));

  const { createQueueMonitor } = await import(uri(qmSrc));
  const monitor = createQueueMonitor({});

  // Initial seed poll: should NOT fire notifications or library-changed
  await monitor.poll();
  assert.equal(dispatched.length, 0);

  // Completed download with 1 new file -> MUST dispatch swararia:library-changed
  queueResponse = {
    current: null,
    queue: [],
    history: [
      {
        id: 'job_dl_1',
        type: 'download',
        status: 'SUCCESS',
        outcome: 'SUCCESS',
        title: 'Unduhan Selesai',
        summary: 'Berhasil mengunduh: Track 1.opus',
        stats: { downloaded: 1, skipped: 0 },
      },
    ],
  };
  await monitor.poll();
  assert.equal(dispatched.length, 1);
  assert.equal(dispatched[0].type, 'swararia:library-changed');
  assert.equal(dispatched[0].detail.reason, 'task-finished');
  assert.equal(dispatched[0].detail.task.id, 'job_dl_1');

  // ALREADY_EXISTS or FAILED with 0 downloaded -> should NOT dispatch library-changed
  queueResponse = {
    current: null,
    queue: [],
    history: [
      {
        id: 'job_dl_2',
        type: 'download',
        status: 'SUCCESS',
        outcome: 'ALREADY_EXISTS',
        stats: { downloaded: 0, skipped: 1 },
      },
      {
        id: 'job_dl_3',
        type: 'download',
        status: 'FAILED',
        outcome: 'FAILED',
        stats: { downloaded: 0, skipped: 0 },
      },
      ...queueResponse.history,
    ],
  };
  await monitor.poll();
  assert.equal(dispatched.length, 1, 'skipped/failed tasks with 0 output must not spam refreshes');
}

// ---------------------------------------------------------------------------
// 2. Verify FOH Player refreshes on SPA activation, library-changed, and
//    upload_library_version without interrupting active playback
// ---------------------------------------------------------------------------
{
  const playerSrc = fs.readFileSync(path.join(root, 'swararia/web/static/pages/player/index.js'), 'utf8');

  const actStart = playerSrc.indexOf("  window.addEventListener('swararia:player-activated',");
  const actEnd = playerSrc.indexOf('  // ---------------------------------------------------------------------------\n  // 5. Audio Element Events', actStart);
  assert.ok(actStart > 0 && actEnd > actStart, 'player-activated & library-changed slice must exist');
  const activationSlice = playerSrc.slice(actStart, actEnd);

  const libStart = playerSrc.indexOf('  function refreshPlayerLibrary(');
  const libEnd = playerSrc.indexOf('  function renderPlaylists()', libStart);
  assert.ok(libStart > 0 && libEnd > libStart, 'refreshPlayerLibrary & fetchLibrary slice must exist');
  const librarySlice = playerSrc.slice(libStart, libEnd);

  const pollStart = playerSrc.indexOf('  let lastUploadLibraryVersion = null;');
  const pollEnd = playerSrc.indexOf('  function applyMasterStateToRemoteUI(', pollStart);
  assert.ok(pollStart > 0 && pollEnd > pollStart, 'pushMasterStateAndPollCommands slice must exist');
  const pollSlice = playerSrc.slice(pollStart, pollEnd);

  const winTarget = new EventTarget();
  const apiCalls = [];
  const loadedTracks = [];
  const nowPlayingUpdates = [];
  let libraryPayload = {
    source_dir: '/music/SwarariaAudioLab',
    default_inbox: '/music/SwarariaAudioLab',
    default_inbox_env_override: false,
    files: [],
    playlists: [],
  };
  let remoteRevision = 0;
  let sharedState = { sourceDir: '', activePlaylist: 'ALL', activeTrackRelPath: '' };
  let pendingApiResolvers = [];
  let holdNextApiGet = false;

  const ctx = {
    window: {
      addEventListener: winTarget.addEventListener.bind(winTarget),
      dispatchEvent: winTarget.dispatchEvent.bind(winTarget),
    },
    CustomEvent: class CustomEvent extends Event {
      constructor(type, init = {}) {
        super(type);
        this.detail = init.detail;
      }
    },
    isRemoteController: false,
    isMasterSyncInFlight: false,
    isPageUnloading: false,
    currentSourceDir: '',
    defaultInboxDir: '',
    defaultInboxEnvOverride: false,
    isFollowingDefaultInbox: true,
    libraryFetchSeq: 0,
    activePlaylist: 'ALL',
    activeTrackRelPath: '',
    allFiles: [],
    allPlaylists: [],
    visibleFiles: [],
    activeTrackObj: null,
    hasRestoredInitialSession: false,
    lastAutoProbedSignature: '',
    sourceDirInput: { value: '' },
    nativeAcks: [],
    nativeMasterInstance: 'test-master',
    lastAckedNativeCatalogSig: '',
    remotePin: '1234',
    btnOpenRemoteModal: null,
    remoteHeaderLabel: null,
    remoteModalClientCount: null,
    remoteModal: null,
    requestRtaWake: null,
    getSharedWorkspaceState: () => ({ ...sharedState }),
    syncSourceLocationUI: () => {},
    renderPlaylists: () => {},
    applyFiltersAndRenderTable: () => {
      ctx.visibleFiles = ctx.allFiles.slice();
    },
    persistWorkspaceState: () => {
      sharedState.sourceDir = ctx.currentSourceDir;
      sharedState.activePlaylist = ctx.activePlaylist;
      sharedState.activeTrackRelPath = ctx.activeTrackRelPath;
    },
    loadPlaybackSessionState: () => null,
    loadTrack: (track, autoPlay) => {
      loadedTracks.push({ rel_path: track.rel_path, autoPlay });
      ctx.activeTrackObj = track;
      ctx.activeTrackRelPath = track.rel_path;
    },
    updateNowPlayingCard: (track) => {
      nowPlayingUpdates.push(track.rel_path);
    },
    updateTimecodeUI: () => {},
    highlightActiveRowInTable: () => {},
    syncPlayPauseButton: () => {},
    setTransportStateBadge: () => {},
    isDeckPlaying: () => false,
    notify: () => {},
    buildMasterStateSnapshot: () => ({}),
    getNativeTrackId: (rel) => 'tid:' + rel,
    buildNativeCatalogSnapshot: () => null,
    consumeNativeCommand: async () => null,
    executeRemoteCommandOnMaster: () => {},
    renderConnectedRemoteClients: () => {},
    apiGet: async (url) => {
      apiCalls.push(url);
      if (holdNextApiGet) {
        holdNextApiGet = false;
        return new Promise((resolve) => {
          pendingApiResolvers.push(() => resolve({ ok: true, data: structuredClone(libraryPayload) }));
        });
      }
      return { ok: true, data: structuredClone(libraryPayload) };
    },
    apiPost: async () => ({ ok: false, data: {} }),
    fetch: async () => ({
      ok: true,
      json: async () => ({
        ok: true,
        commands: [],
        active_remotes_count: 0,
        active_remotes: [],
        blocked_remotes: [],
        pin: '1234',
        enabled: true,
        upload_library_version: remoteRevision,
      }),
    }),
  };

  vm.createContext(ctx);
  vm.runInContext(
    `${librarySlice}\n${activationSlice}\n${pollSlice}\n` +
      `globalThis.__runFetch = (s) => fetchLibrary(s);\n` +
      `globalThis.__runPoll = () => pushMasterStateAndPollCommands();\n` +
      `globalThis.__getState = () => ({ allFiles, visibleFiles, activeTrackObj, currentSourceDir, defaultInboxDir, isFollowingDefaultInbox, activePlaylist });`,
    ctx
  );

  // Initial load with empty library + baseline poll (revision 0)
  await ctx.__runFetch('');
  await ctx.__runPoll();
  assert.equal(ctx.__getState().allFiles.length, 0);
  assert.equal(loadedTracks.length, 0);

  // Song #1 downloaded -> Ingest dispatches swararia:library-changed
  libraryPayload = {
    source_dir: '/music/SwarariaAudioLab',
    default_inbox: '/music/SwarariaAudioLab',
    default_inbox_env_override: false,
    files: [
      {
        name: 'Song One [abc12345678].opus',
        rel_path: 'Song One [abc12345678].opus',
        folder: '',
        ext: '.opus',
        qc: { codec: 'opus', bitrate_kbps: 160 },
      },
    ],
    playlists: [{ name: '__ROOT__', total: 1, mp3_count: 0, other_count: 1 }],
  };
  ctx.window.dispatchEvent(new ctx.CustomEvent('swararia:library-changed', { detail: { reason: 'task-finished' } }));
  await new Promise((r) => setTimeout(r, 10));

  let st = ctx.__getState();
  assert.equal(st.allFiles.length, 1, 'library-changed must reload downloaded song into FOH Player');
  assert.equal(loadedTracks.length, 1, 'first downloaded song must auto-cue when deck was empty');
  assert.equal(loadedTracks[0].rel_path, 'Song One [abc12345678].opus');
  assert.equal(loadedTracks[0].autoPlay, false);

  // Song #2 downloaded while Song #1 is loaded -> backend bumps upload_library_version to 1
  libraryPayload.files.push({
    name: 'Song Two [xyz98765432].opus',
    rel_path: 'Song Two [xyz98765432].opus',
    folder: '',
    ext: '.opus',
    qc: { codec: 'opus', bitrate_kbps: 160 },
  });
  remoteRevision = 1;
  await ctx.__runPoll();
  await new Promise((r) => setTimeout(r, 10));

  st = ctx.__getState();
  assert.equal(st.allFiles.length, 2, 'upload_library_version increment must refresh FOH Player automatically');
  assert.equal(loadedTracks.length, 1, 'refreshing library must NOT interrupt already loaded/playing track');
  assert.equal(nowPlayingUpdates.at(-1), 'Song One [abc12345678].opus');

  // Song #3 downloaded -> user navigates back to FOH Player via SPA (swararia:player-activated)
  libraryPayload.files.push({
    name: 'Song Three [def45678901].opus',
    rel_path: 'Song Three [def45678901].opus',
    folder: '',
    ext: '.opus',
    qc: { codec: 'opus', bitrate_kbps: 160 },
  });
  const callsBeforeActivate = apiCalls.length;
  ctx.window.dispatchEvent(new ctx.CustomEvent('swararia:player-activated'));
  await new Promise((r) => setTimeout(r, 10));

  st = ctx.__getState();
  assert.ok(apiCalls.length > callsBeforeActivate, 'swararia:player-activated must trigger library refresh');
  assert.equal(st.allFiles.length, 3);
  assert.equal(apiCalls.at(-1), '/api/library', 'following default inbox must fetch /api/library without stale source_dir');

  // Default library folder changed externally -> FOH Player follows new default automatically
  libraryPayload = {
    source_dir: '/music/NewDefaultFOH',
    default_inbox: '/music/NewDefaultFOH',
    default_inbox_env_override: false,
    files: [
      {
        name: 'New Default Track.opus',
        rel_path: 'New Default Track.opus',
        folder: '',
        ext: '.opus',
        qc: { codec: 'opus', bitrate_kbps: 160 },
      },
    ],
    playlists: [{ name: '__ROOT__', total: 1, mp3_count: 0, other_count: 1 }],
  };
  ctx.window.dispatchEvent(
    new ctx.CustomEvent('swararia:library-changed', {
      detail: { reason: 'default-library-changed', libraryDir: '/music/NewDefaultFOH' },
    })
  );
  await new Promise((r) => setTimeout(r, 10));
  st = ctx.__getState();
  assert.equal(st.currentSourceDir, '/music/NewDefaultFOH');
  assert.equal(st.allFiles[0].name, 'New Default Track.opus');

  // Out-of-order fetch guard: older slow response must NOT overwrite newer download list
  const oldSnapshot = structuredClone(libraryPayload);
  holdNextApiGet = true;
  const slowFetchPromise = ctx.__runFetch('');
  libraryPayload.files.push({
    name: 'Fresh Track.opus',
    rel_path: 'Fresh Track.opus',
    folder: '',
    ext: '.opus',
    qc: { codec: 'opus', bitrate_kbps: 160 },
  });
  await ctx.__runFetch('');
  assert.equal(ctx.__getState().allFiles.length, 2);
  // Now resolve the older slow request with stale 1-track payload
  const staleData = oldSnapshot;
  libraryPayload = staleData;
  pendingApiResolvers.forEach((fn) => fn());
  await slowFetchPromise;
  assert.equal(ctx.__getState().allFiles.length, 2, 'stale out-of-order response must be ignored');
}

console.log('PASS player-auto-refresh: queue-monitor event, SPA activation, revision sync, default-dir follow, and race guard');
