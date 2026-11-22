/**
 * PC <-> PC HTTPS Pinned Folder Sync Modal Controller (Receiver UI)
 * Strict DOM manipulation only (createElement, textContent, replaceChildren — zero innerHTML).
 * Session bearer tokens stay strictly in Python RAM — never written to localStorage/sessionStorage.
 */

function formatSize(bytes) {
  const n = Number(bytes) || 0;
  if (n >= 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MiB`;
  if (n >= 1024) return `${Math.round(n / 1024)} KiB`;
  return `${n} B`;
}

export function initPcSyncPanel({
  document,
  apiGet,
  apiPost,
  notify,
  getTargetDestDir,
  getTargetDestLabel,
  onSyncQueued,
}) {
  const el = id => document.getElementById(id);

  const btnOpen = el('btnOpenPcSyncModal');
  const modal = el('pcSyncModal');
  const stepPair = el('pcSyncPairSection');
  const stepBrowse = el('pcSyncBrowseSection');
  const codeInput = el('syncPairingCodeInput');
  const labelInput = el('syncDeviceLabelInput');
  const btnPair = el('btnStartPcSyncPair');
  const btnRefresh = el('btnRefreshPcSyncPeer');
  const btnDisconnect = el('btnDisconnectPcSyncPeer');
  const peerStatusText = el('pcSyncPeerStatus');
  const peerFingerprintText = el('pcSyncPeerFingerprint');
  const foldersList = el('pcSyncFoldersList');
  const btnSelectAll = el('btnPcSyncSelectAll');
  const btnClearFolders = el('btnPcSyncClearFolders');
  const destLabelEl = el('pcSyncDestLabel');
  const btnPreview = el('btnPreviewPcSync');
  const btnStart = el('btnExecutePcSync');
  const diffBox = el('pcSyncDiffBox');
  const diffSummary = el('pcSyncDiffSummary');
  const diffPreviewList = el('pcSyncDiffPreviewList');
  const warningBox = el('pcSyncWarningBanner');
  const warningText = el('pcSyncWarningText');
  const feedbackEl = el('pcSyncFeedback');

  if (!btnOpen || !modal) return;

  let activePeer = null;
  let remoteFolders = [];
  const selectedFolderIds = new Set();
  let pollTimer = null;
  let busy = false;

  function setFeedback(msg) {
    if (feedbackEl) feedbackEl.textContent = msg || '';
  }

  function setWarning(warningsList) {
    if (!warningBox || !warningText) return;
    const msgs = Array.isArray(warningsList) ? warningsList.filter(Boolean) : [];
    if (!msgs.length) {
      warningBox.classList.add('hidden');
      warningText.textContent = '';
    } else {
      warningText.textContent = msgs.join(' ');
      warningBox.classList.remove('hidden');
    }
  }

  function stopPolling() {
    if (pollTimer !== null) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  function renderFolders() {
    if (!foldersList) return;
    foldersList.replaceChildren();
    if (!remoteFolders.length) {
      const empty = document.createElement('div');
      empty.className = 'notif-empty';
      empty.textContent = activePeer?.status === 'pending'
        ? 'Menunggu operator di PC sumber menyetujui perangkat ini…'
        : !activePeer?.sync_capable
          ? 'Di PC sumber, aktifkan "Izinkan sinkronisasi folder PC ↔ PC (HTTPS)" pada modal Android Remote.'
          : 'Tidak ada folder audio terkompresi (.mp3/.m4a/.opus/.webm) di root sinkronisasi PC sumber.';
      foldersList.appendChild(empty);
      return;
    }

    for (const folder of remoteFolders) {
      const row = document.createElement('label');
      row.className = 'sync-folder-item';

      const chk = document.createElement('input');
      chk.type = 'checkbox';
      chk.checked = selectedFolderIds.has(folder.folder_id);
      chk.addEventListener('change', () => {
        if (chk.checked) selectedFolderIds.add(folder.folder_id);
        else selectedFolderIds.delete(folder.folder_id);
        resetDiff();
        syncActionButtons();
      });

      const info = document.createElement('div');
      info.className = 'sync-folder-meta';
      const title = document.createElement('strong');
      title.textContent = folder.name;
      const sub = document.createElement('small');
      const parts = [`${folder.track_count} lagu`, formatSize(folder.total_bytes)];
      if (folder.excluded_wav_flac_count > 0) {
        parts.push(`${folder.excluded_wav_flac_count} WAV/FLAC dilewati`);
      }
      if (folder.oversized_count > 0) {
        parts.push(`${folder.oversized_count} >150 MiB dilewati`);
      }
      sub.textContent = parts.join(' • ');

      info.appendChild(title);
      info.appendChild(sub);
      row.appendChild(chk);
      row.appendChild(info);
      foldersList.appendChild(row);
    }
  }

  function resetDiff() {
    if (diffBox) diffBox.classList.add('hidden');
    if (diffPreviewList) diffPreviewList.replaceChildren();
    if (btnStart) btnStart.disabled = true;
  }

  function syncActionButtons() {
    const canBrowse = Boolean(
      activePeer && activePeer.status === 'active' && activePeer.role === 'control' && activePeer.sync_capable
    );
    if (btnPreview) btnPreview.disabled = busy || !canBrowse || selectedFolderIds.size === 0;
    if (btnSelectAll) btnSelectAll.disabled = busy || !remoteFolders.length;
    if (btnClearFolders) btnClearFolders.disabled = busy || !remoteFolders.length;
    if (btnStart && (!canBrowse || selectedFolderIds.size === 0)) {
      btnStart.disabled = true;
    }
  }

  function renderPeerState() {
    const paired = Boolean(activePeer);
    if (stepPair) stepPair.classList.toggle('hidden', paired);
    if (stepBrowse) stepBrowse.classList.toggle('hidden', !paired);
    if (destLabelEl) {
      destLabelEl.textContent = typeof getTargetDestLabel === 'function' ? getTargetDestLabel() : 'library/inbox';
    }

    if (!paired) {
      stopPolling();
      setWarning([]);
      resetDiff();
      return;
    }

    if (peerFingerprintText) {
      peerFingerprintText.textContent = activePeer.fingerprint_sha256 || '—';
    }
    if (peerStatusText) {
      if (activePeer.status === 'pending') {
        peerStatusText.textContent = `Terhubung ke ${activePeer.host}:${activePeer.port} · Menunggu persetujuan PC sumber`;
      } else if (activePeer.status !== 'active') {
        peerStatusText.textContent = `Status sesi: ${activePeer.status} (${activePeer.host}:${activePeer.port})`;
      } else if (activePeer.role !== 'control') {
        peerStatusText.textContent = `Disetujui sebagai Pantau Saja · Ubah ke Kontrol Penuh di PC sumber`;
      } else if (!activePeer.sync_capable) {
        peerStatusText.textContent = `Terhubung ke ${activePeer.host}:${activePeer.port} · Sinkronisasi belum diaktifkan di PC sumber`;
      } else {
        peerStatusText.textContent = `Siap sinkronisasi dari ${activePeer.host}:${activePeer.port}`;
      }
    }
    renderFolders();
    syncActionButtons();
  }

  async function refreshActivePeer() {
    if (!activePeer) return;
    try {
      const { ok, data, error } = await apiPost('/api/sync/refresh', { peer_id: activePeer.peer_id });
      if (!ok || !data?.peer) {
        setFeedback(error || data?.error || 'Gagal memeriksa status PC sumber.');
        return;
      }
      activePeer = data.peer;
      const info = data.folders_info;
      if (info && Array.isArray(info.folders)) {
        remoteFolders = info.folders;
        setWarning(info.warnings || []);
        stopPolling();
      } else if (activePeer.status === 'active') {
        remoteFolders = [];
        setWarning([]);
        stopPolling();
      }
      renderPeerState();
    } catch (_) {
      setFeedback('Koneksi ke PC sumber gagal.');
    }
  }

  async function loadExistingPeers() {
    const { ok, data } = await apiGet('/api/sync/status');
    if (ok && Array.isArray(data?.peers) && data.peers.length > 0) {
      activePeer = data.peers[data.peers.length - 1];
      renderPeerState();
      await refreshActivePeer();
      if (activePeer?.status === 'pending' && !modal.classList.contains('hidden')) {
        stopPolling();
        pollTimer = setInterval(refreshActivePeer, 1500);
      }
    } else {
      activePeer = null;
      remoteFolders = [];
      selectedFolderIds.clear();
      renderPeerState();
    }
  }

  async function handlePair() {
    if (busy) return;
    const pairingCode = (codeInput?.value || '').trim();
    if (!pairingCode) {
      setFeedback('Tempel kode pairing swararia1|... dari modal Android Remote di PC sumber.');
      return;
    }
    busy = true;
    if (btnPair) btnPair.disabled = true;
    setFeedback('Memverifikasi fingerprint TLS & pairing ke PC sumber…');
    try {
      const { ok, data, error } = await apiPost('/api/sync/pair', {
        pairing_code: pairingCode,
        label: (labelInput?.value || '').trim() || 'Swararia PC Sync',
      });
      if (!ok || !data?.peer) {
        setFeedback(data?.error || error || 'Pairing gagal.');
        return;
      }
      activePeer = data.peer;
      remoteFolders = [];
      selectedFolderIds.clear();
      if (codeInput) codeInput.value = '';
      setFeedback('Pairing terkirim! Klik "Izinkan kontrol" pada PC sumber.');
      renderPeerState();
      stopPolling();
      pollTimer = setInterval(refreshActivePeer, 1500);
    } finally {
      busy = false;
      if (btnPair) btnPair.disabled = false;
    }
  }

  async function handlePreview() {
    if (busy || !activePeer || selectedFolderIds.size === 0) return;
    busy = true;
    syncActionButtons();
    setFeedback('Mengambil manifest SHA-256 & membandingkan folder lokal…');
    resetDiff();
    try {
      const destDir = typeof getTargetDestDir === 'function' ? getTargetDestDir() : '';
      const { ok, data, error } = await apiPost('/api/sync/preview', {
        peer_id: activePeer.peer_id,
        folder_ids: Array.from(selectedFolderIds),
        dest_dir: destDir || null,
      });
      if (!ok || !data) {
        setFeedback(data?.error || error || 'Gagal membandingkan folder.');
        return;
      }
      setWarning(data.warnings || []);
      if (diffBox && diffSummary && diffPreviewList) {
        diffBox.classList.remove('hidden');
        diffSummary.textContent =
          `${data.new_count} lagu baru · ${data.rename_collision_count} bentrok (auto-rename) · ${data.duplicate_skip_count} duplikat SHA-256 dilewati`;
        diffPreviewList.replaceChildren();
        for (const item of (data.items_preview || []).slice(0, 80)) {
          const row = document.createElement('div');
          row.className = 'diff-preview-row';
          const lbl = document.createElement('span');
          lbl.textContent = item.rel_path || item.name;
          const act = document.createElement('span');
          act.className = `diff-action-tag diff-${item.action}`;
          act.textContent = item.action === 'skip_duplicate'
            ? 'Skip (SHA-256 identik)'
            : item.action === 'rename_collision'
              ? 'Simpan + Rename'
              : `Baru (${formatSize(item.size_bytes)})`;
          row.appendChild(lbl);
          row.appendChild(act);
          diffPreviewList.appendChild(row);
        }
      }
      if (btnStart) {
        btnStart.disabled = (data.transfer_count || 0) === 0;
      }
      setFeedback((data.transfer_count || 0) > 0
        ? `Siap menyinkronkan ${data.transfer_count} lagu (${formatSize(data.transfer_bytes)}).`
        : 'Semua lagu terkompresi terpilih sudah ada secara identik di folder lokal.');
    } finally {
      busy = false;
      syncActionButtons();
    }
  }

  async function handleStartSync() {
    if (busy || !activePeer || selectedFolderIds.size === 0) return;
    busy = true;
    syncActionButtons();
    setFeedback('Menambahkan tugas sinkronisasi folder ke antrian…');
    try {
      const destDir = typeof getTargetDestDir === 'function' ? getTargetDestDir() : '';
      const { ok, data, error } = await apiPost('/api/sync/start', {
        peer_id: activePeer.peer_id,
        folder_ids: Array.from(selectedFolderIds),
        dest_dir: destDir || null,
      });
      if (!ok || !data) {
        setFeedback(data?.error || error || 'Gagal memulai sinkronisasi.');
        return;
      }
      setFeedback(data.message || 'Sinkronisasi dimulai di latar belakang.');
      if (typeof notify === 'function') {
        notify({
          type: 'success',
          title: 'Sinkronisasi PC ↔ PC Dimulai',
          message: data.message || 'Pantau progres di ikon Antrian Tugas di pojok kanan atas.',
        });
      }
      if (typeof onSyncQueued === 'function') onSyncQueued(data.job_id || null);
      modal.classList.add('hidden');
      stopPolling();
    } finally {
      busy = false;
      syncActionButtons();
    }
  }

  async function handleDisconnect() {
    stopPolling();
    if (activePeer) {
      await apiPost('/api/sync/disconnect', { peer_id: activePeer.peer_id });
    }
    activePeer = null;
    remoteFolders = [];
    selectedFolderIds.clear();
    setFeedback('Sesi pairing diputus.');
    renderPeerState();
  }

  btnOpen.addEventListener('click', () => {
    setFeedback('');
    modal.classList.remove('hidden');
    loadExistingPeers();
  });
  for (const cid of ['btnClosePcSyncModal', 'btnCancelPcSyncModal']) {
    const b = el(cid);
    if (b) {
      b.addEventListener('click', () => {
        stopPolling();
        modal.classList.add('hidden');
      });
    }
  }
  modal.addEventListener('click', e => {
    if (e.target === modal) {
      stopPolling();
      modal.classList.add('hidden');
    }
  });

  if (btnPair) btnPair.addEventListener('click', handlePair);
  if (btnRefresh) btnRefresh.addEventListener('click', refreshActivePeer);
  if (btnDisconnect) btnDisconnect.addEventListener('click', handleDisconnect);
  if (btnPreview) btnPreview.addEventListener('click', handlePreview);
  if (btnStart) btnStart.addEventListener('click', handleStartSync);

  if (btnSelectAll) {
    btnSelectAll.addEventListener('click', () => {
      remoteFolders.forEach(f => selectedFolderIds.add(f.folder_id));
      renderFolders();
      resetDiff();
      syncActionButtons();
    });
  }
  if (btnClearFolders) {
    btnClearFolders.addEventListener('click', () => {
      selectedFolderIds.clear();
      renderFolders();
      resetDiff();
      syncActionButtons();
    });
  }
}
