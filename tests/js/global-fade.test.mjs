import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const root = path.resolve(import.meta.dirname, '../..');
const playerJs = fs.readFileSync(path.join(root, 'swararia/web/static/player.js'), 'utf8');
const playerHtml = fs.readFileSync(path.join(root, 'swararia/web/static/player.html'), 'utf8');
const appJs = fs.readFileSync(path.join(root, 'swararia/web/static/app.js'), 'utf8');

function extractBlock(startMarker, endMarker) {
  const start = playerJs.indexOf(startMarker);
  const end = playerJs.indexOf(endMarker, start);
  assert.ok(start >= 0, `Missing marker: ${startMarker}`);
  assert.ok(end > start, `Missing end marker: ${endMarker}`);
  return playerJs.slice(start, end);
}

function makeGlobalFadeHarness() {
  const clampSource = extractBlock('  function clampFadeSec(', '  function loadPlayerPrefs(');
  const fadeEngineSource = extractBlock('  function formatFadeSecShort(', '  function buildStreamUrl(');
  const transportSource = extractBlock('  function loadTrack(', '  function getAdjacentTrack(');

  const timers = new Map();
  let nextTimerId = 1;
  let fakeNowMs = 10_000;
  let audioTimeSec = 10.0;
  const gainEvents = [];
  const badges = [];
  const pauseCalls = [];
  const loadCalls = [];
  let activeTrack = { rel_path: 'a.mp3', name: 'Track A', size_mb: '4.0' };
  let paused = true;
  let currentTime = 12;

  const gainParam = {
    value: 1.0,
    cancelScheduledValues(at) {
      gainEvents.push({ op: 'cancel', at });
    },
    setValueAtTime(val, at) {
      this.value = val;
      gainEvents.push({ op: 'set', val, at });
    },
    linearRampToValueAtTime(val, at) {
      this.value = val;
      gainEvents.push({ op: 'ramp', val, at });
    },
  };

  const audioEl = {
    src: '',
    currentTime: 12,
    duration: 180,
    readyState: 1,
    get paused() {
      return paused;
    },
    get ended() {
      return false;
    },
    load() {
      paused = true;
      currentTime = 0;
      this.currentTime = 0;
    },
    async play() {
      paused = false;
    },
    pause() {
      paused = true;
      pauseCalls.push({ atMs: fakeNowMs, track: activeTrack?.rel_path });
    },
  };

  const sandbox = {
    Date: { now: () => fakeNowMs },
    Math,
    Number,
    Boolean,
    String,
    Array,
    setTimeout(fn, ms) {
      const id = nextTimerId++;
      timers.set(id, { fn, runAt: fakeNowMs + ms });
      return id;
    },
    clearTimeout(id) {
      timers.delete(id);
    },
    isRemoteController: false,
    visibleFiles: [activeTrack, { rel_path: 'b.mp3', name: 'Track B', size_mb: '5.0' }],
    allFiles: [activeTrack, { rel_path: 'b.mp3', name: 'Track B', size_mb: '5.0' }],
    get activeTrackObj() {
      return activeTrack;
    },
    set activeTrackObj(v) {
      activeTrack = v;
      loadCalls.push(v?.rel_path);
    },
    activeTrackRelPath: 'a.mp3',
    audioEl,
    audioCtx: {
      state: 'running',
      get currentTime() {
        return audioTimeSec;
      },
      async resume() {},
    },
    fadeGainNode: { gain: gainParam },
    chkAutoTrim: null,
    preampSlider: null,
    seekSlider: null,
    fadeTabBadge: { textContent: '', classList: { toggle() {} } },
    fadeInButtons: [],
    fadeOutButtons: [],
    isFadingOut: false,
    fadeTimerId: null,
    globalFadeInSec: 0,
    globalFadeOutSec: 0,
    fadePhase: 'none',
    fadeRampFromGain: 1.0,
    fadeRampToGain: 1.0,
    fadeRampDurationSec: 0,
    fadeRampStartedAtMs: 0,
    fadePendingAction: null,
    requestRtaWake: null,
    pendingAutoResumeOnGesture: false,
    buildStreamUrl: f => `/api/stream?rel_path=${f.rel_path}`,
    computeRecommendedAutoTrimDb: () => 0,
    applyGainAndEqNodes() {},
    updateNowPlayingCard() {},
    highlightActiveRowInTable() {},
    renderCuePads() {},
    savePlaybackSessionState() {},
    persistWorkspaceState() {},
    updatePeakBadge() {},
    maxPeakHoldDb: -Infinity,
    updateTimecodeUI() {},
    syncPlayPauseButton() {},
    setTransportStateBadge(s) {
      badges.push(s);
    },
    ensureAudioGraph() {},
    connectMediaElementToGraphIfReady() {},
    notify() {},
    formatTimecode: s => String(s),
    sendRemoteCommand() {},
  };

  vm.createContext(sandbox);
  vm.runInContext(`${clampSource}\n${fadeEngineSource}\n${transportSource}`, sandbox);

  function advanceMs(deltaMs) {
    const target = fakeNowMs + deltaMs;
    while (true) {
      const due = [...timers.entries()]
        .filter(([, entry]) => entry.runAt <= target)
        .sort((a, b) => a[1].runAt - b[1].runAt);
      if (due.length === 0) break;
      const [id, entry] = due[0];
      timers.delete(id);
      const step = Math.max(0, entry.runAt - fakeNowMs);
      fakeNowMs = entry.runAt;
      audioTimeSec += step / 1000;
      entry.fn();
    }
    const rest = Math.max(0, target - fakeNowMs);
    fakeNowMs = target;
    audioTimeSec += rest / 1000;
  }

  return {
    sandbox,
    audioEl,
    gainEvents,
    badges,
    pauseCalls,
    loadCalls,
    advanceMs,
    getActiveTrack: () => activeTrack,
  };
}

// 1. UI and preview isolation contracts
assert.ok(playerHtml.includes('id="dspPaneFade"'), 'Global Fade pane should be present in player.html');
assert.ok(playerHtml.includes('data-fade-in="2"') && playerHtml.includes('data-fade-out="2"'));
assert.ok(appJs.includes('ingPreviewAudio'), 'Preview search must keep using its dedicated audio element');

// 2. Fade In on Play & Proportional Interruption by Pause
{
  const h = makeGlobalFadeHarness();
  h.sandbox.globalFadeInSec = 2.0;
  h.sandbox.globalFadeOutSec = 2.0;
  await h.sandbox.playCurrentTrack();
  assert.equal(h.audioEl.paused, false);
  const rampIn = h.gainEvents.find(e => e.op === 'ramp' && e.val === 1.0);
  assert.ok(rampIn, 'playCurrentTrack must schedule ramp to 1.0');
  assert.equal(h.gainEvents.some(e => e.op === 'set' && e.val <= 0.001), true, 'must start Fade In near 0');

  // Interrupt Fade In at 1.0s (~50% gain) with Pause -> proportional Fade Out (~1.0s) without spiking to 1.0
  h.advanceMs(1000);
  h.sandbox.pauseCurrentTrack();
  assert.equal(h.audioEl.paused, false, 'pauseCurrentTrack with fadeOutSec > 0 fades before pausing');
  assert.equal(h.sandbox.isFadingOut, true);
  const lastSet = [...h.gainEvents].reverse().find(e => e.op === 'set');
  assert.ok(lastSet && lastSet.val > 0.4 && lastSet.val < 0.65, `expected ~0.5 start gain, got ${lastSet?.val}`);

  h.advanceMs(1050);
  assert.equal(h.audioEl.paused, true, 'pauses after proportional fade-out completes');
}

// 3. 2nd-tap Hard Cut on Pause
{
  const h = makeGlobalFadeHarness();
  h.sandbox.globalFadeOutSec = 3.0;
  await h.sandbox.playCurrentTrack();
  h.sandbox.pauseCurrentTrack();
  assert.equal(h.audioEl.paused, false);
  assert.equal(h.sandbox.isFadingOut, true);

  h.advanceMs(300);
  h.sandbox.pauseCurrentTrack();
  assert.equal(h.audioEl.paused, true, '2nd tap Pause during fade must cut immediately');
  assert.equal(h.sandbox.isFadingOut, false);
}

// 4. Track Switch Fade Out -> Switch -> Fade In
{
  const h = makeGlobalFadeHarness();
  h.sandbox.globalFadeInSec = 1.0;
  h.sandbox.globalFadeOutSec = 1.0;
  await h.sandbox.playCurrentTrack();
  h.advanceMs(1050);

  const trackB = { rel_path: 'b.mp3', name: 'Track B', size_mb: '5.0' };
  const switched = h.sandbox.loadTrack(trackB, true);
  assert.equal(switched, true);
  assert.equal(h.getActiveTrack().rel_path, 'a.mp3', 'current track stays while fading out');
  h.advanceMs(1050);
  assert.equal(h.getActiveTrack().rel_path, 'b.mp3', 'switches to Track B after Fade Out finishes');
  assert.equal(h.audioEl.paused, false);
}

// 5. Immediate / Preview bypass and manual fade fallback
{
  const h = makeGlobalFadeHarness();
  h.sandbox.globalFadeInSec = 2.0;
  h.sandbox.globalFadeOutSec = 0;
  assert.equal(h.sandbox.getManualFadeOutSec(), 3.0, 'manual fade falls back to 3s when global fadeOut=0');
  h.sandbox.globalFadeOutSec = 2.0;
  assert.equal(h.sandbox.getManualFadeOutSec(), 2.0, 'manual fade uses configured fadeOut when > 0');

  await h.sandbox.playCurrentTrack({ immediate: true });
  assert.equal(h.gainEvents.filter(e => e.op === 'ramp').length, 0, 'immediate play bypasses fade');
  h.sandbox.pauseCurrentTrack({ immediate: true });
  assert.equal(h.audioEl.paused, true, 'immediate pause bypasses fade');
}

// 6. Stop/Cue fade-out resets position to 0 after fading; resuming mid-fade-out ramps smoothly up
{
  const h = makeGlobalFadeHarness();
  h.sandbox.globalFadeInSec = 1.0;
  h.sandbox.globalFadeOutSec = 2.0;
  await h.sandbox.playCurrentTrack({ immediate: true });
  h.audioEl.currentTime = 42;
  h.sandbox.stopAndResetCue();
  assert.equal(h.audioEl.paused, false);
  assert.equal(h.audioEl.currentTime, 42, 'currentTime waits until Stop fade-out finishes');
  h.advanceMs(1000);
  // Reverse mid-fade-out with Play
  await h.sandbox.playCurrentTrack();
  assert.equal(h.sandbox.isFadingOut, false);
  assert.equal(h.audioEl.currentTime, 42, 'canceling Stop fade-out via Play keeps track playing');
  h.advanceMs(1200);
  assert.equal(h.audioEl.paused, false);

  // Clean Stop fade-out completion
  h.sandbox.stopAndResetCue();
  h.advanceMs(2050);
  assert.equal(h.audioEl.paused, true);
  assert.equal(h.audioEl.currentTime, 0);
}

console.log('PASS global fade: Fade In, Pause/Stop/Switch Fade Out, 2nd-tap hard cut, proportional interruption, reversal, bypass');
