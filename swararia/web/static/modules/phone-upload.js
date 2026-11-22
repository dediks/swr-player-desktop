/**
 * Phone -> PC Audio Upload UI & PC Operator Configuration Module
 * Strict DOM manipulation only (createElement, textContent, replaceChildren — zero innerHTML).
 */

const ALLOWED_EXTS = new Set(['.mp3', '.m4a', '.mp4', '.aac', '.opus', '.ogg', '.webm']);
const EXCLUDED_PHASE_EXTS = new Set(['.wav', '.flac']);
const MAX_TRANSFER_BYTES = 150 * 1024 * 1024;
const MAX_FILES_PER_BATCH = 20;

function formatBytes(bytes) {
  const num = Number(bytes) || 0;
  if (num >= 1024 * 1024) return `${(num / (1024 * 1024)).toFixed(1)} MiB`;
  if (num >= 1024) return `${Math.round(num / 1024)} KiB`;
  return `${num} B`;
}

function getFileExt(name) {
  const idx = String(name || '').lastIndexOf('.');
  return idx > 0 ? String(name).slice(idx).toLowerCase() : '';
}

export function initPhoneUploadController({
  document,
  isRemoteController,
  getRemoteCredentials,
  getPlaylists,
  getCurrentSourceDir,
  notify,
  onUploadCompleted,
}) {
  const el = id => document.getElementById(id);

  // PC Operator Config DOM
  const btnOpenConfig = el('btnOpenPhoneUploadConfig');
  const configModal = el('phoneUploadConfigModal');
  const chkEnabled = el('chkPhoneUploadEnabled');
  const configDestLabel = el('phoneUploadConfigDestLabel');
  const configFeedback = el('phoneUploadConfigFeedback');
  const btnUseCurrentSource = el('btnPhoneUploadUseActiveSource');
  const btnResetInbox = el('btnPhoneUploadResetInbox');

  // Phone Remote Upload DOM
  const btnOpenUpload = el('btnOpenPhoneUpload');
  const uploadModal = el('phoneUploadModal');
  const uploadDestBadge = el('phoneUploadDestBadge');
  const playlistSelect = el('phoneUploadPlaylistSelect');
  const customSubfolderWrap = el('phoneUploadCustomSubfolderWrap');
  const customSubfolderInput = el('phoneUploadCustomSubfolder');
  const fileInput = el('phoneUploadFileInput');
  const previewList = el('phoneUploadFileList');
  const warningBanner = el('phoneUploadWarningBanner');
  const warningText = el('phoneUploadWarningText');
  const progressWrap = el('phoneUploadProgressWrap');
  const progressBar = el('phoneUploadProgressBar');
  const progressText = el('phoneUploadProgressText');
  const uploadFeedback = el('phoneUploadFeedback');
  const btnStartUpload = el('btnStartPhoneUpload');

  let uploadState = {
    upload_enabled: false,
    upload_dest_label: 'library/inbox',
    upload_dest_dir: '',
    role: 'control',
  };
  let configBusy = false;
  let uploading = false;

  function renderConfigState() {
    if (btnOpenConfig && !isRemoteController) {
      btnOpenConfig.classList.remove('hidden');
      btnOpenConfig.classList.toggle('connected', Boolean(uploadState.upload_enabled));
      btnOpenConfig.textContent = uploadState.upload_enabled ? 'Unggah HP: Aktif' : 'Unggah HP';
    }
    if (chkEnabled) {
      chkEnabled.checked = Boolean(uploadState.upload_enabled);
      chkEnabled.disabled = configBusy;
    }
    if (configDestLabel) {
      configDestLabel.textContent = uploadState.upload_dest_label || 'library/inbox';
    }
  }

  function renderPhoneBannerButton() {
    if (!btnOpenUpload) return;
    const canUpload = Boolean(
      isRemoteController && uploadState.upload_enabled && uploadState.role !== 'view_only'
    );
    btnOpenUpload.classList.toggle('hidden', !canUpload);
    if (uploadDestBadge) {
      uploadDestBadge.textContent = uploadState.upload_dest_label || 'library/inbox';
    }
  }

  async function refreshConfigFromServer() {
    if (isRemoteController) return;
    try {
      const res = await fetch('/api/remote/info', { cache: 'no-store' });
      if (!res.ok) return;
      const data = await res.json();
      uploadState.upload_enabled = Boolean(data.upload_enabled);
      uploadState.upload_dest_label = data.upload_dest_label || 'library/inbox';
      uploadState.upload_dest_dir = data.upload_dest_dir || '';
      renderConfigState();
    } catch (_) { /* Keep last state */ }
  }

  async function saveConfig(payload) {
    if (configBusy || isRemoteController) return;
    configBusy = true;
    renderConfigState();
    if (configFeedback) configFeedback.textContent = 'Menyimpan pengaturan upload…';
    try {
      const res = await fetch('/api/remote/upload-config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || data.ok === false) {
        throw new Error(data.error || 'Gagal menyimpan pengaturan upload.');
      }
      uploadState.upload_enabled = Boolean(data.upload_enabled);
      uploadState.upload_dest_label = data.upload_dest_label || 'library/inbox';
      uploadState.upload_dest_dir = data.upload_dest_dir || '';
      if (configFeedback) {
        configFeedback.textContent = uploadState.upload_enabled
          ? 'Upload dari browser HP aktif. Pastikan FOH Remote & PIN juga aktif.'
          : 'Upload dari HP dinonaktifkan.';
      }
    } catch (err) {
      if (configFeedback) configFeedback.textContent = err.message || 'Gagal memperbarui pengaturan.';
    } finally {
      configBusy = false;
      renderConfigState();
    }
  }

  function populatePlaylistDropdown() {
    if (!playlistSelect) return;
    const prev = playlistSelect.value;
    playlistSelect.replaceChildren();

    const optRoot = document.createElement('option');
    optRoot.value = '';
    optRoot.textContent = 'Root Folder (Tanpa Subfolder)';
    playlistSelect.appendChild(optRoot);

    const pls = typeof getPlaylists === 'function' ? (getPlaylists() || []) : [];
    for (const pl of pls) {
      if (!pl || !pl.name || pl.name === '__ROOT__') continue;
      const opt = document.createElement('option');
      opt.value = pl.name;
      opt.textContent = `Playlist: ${pl.name}`;
      playlistSelect.appendChild(opt);
    }

    const optNew = document.createElement('option');
    optNew.value = '__NEW__';
    optNew.textContent = '+ Buat Folder Playlist Baru…';
    playlistSelect.appendChild(optNew);

    if (prev && Array.from(playlistSelect.options).some(o => o.value === prev)) {
      playlistSelect.value = prev;
    }
    syncCustomSubfolderVisibility();
  }

  function syncCustomSubfolderVisibility() {
    if (!customSubfolderWrap || !playlistSelect) return;
    customSubfolderWrap.classList.toggle('hidden', playlistSelect.value !== '__NEW__');
  }

  function inspectSelectedFiles() {
    const rawFiles = fileInput && fileInput.files ? Array.from(fileInput.files) : [];
    const accepted = [];
    const warnings = [];
    let wavFlacCount = 0;

    if (previewList) previewList.replaceChildren();

    if (!rawFiles.length) {
      if (warningBanner) warningBanner.classList.add('hidden');
      if (btnStartUpload) btnStartUpload.disabled = true;
      return { accepted: [], warnings: [] };
    }

    rawFiles.forEach(file => {
      const ext = getFileExt(file.name);
      const row = document.createElement('div');
      row.className = 'transfer-file-row';

      const nameEl = document.createElement('span');
      nameEl.className = 'transfer-file-name';
      nameEl.textContent = `${file.name} (${formatBytes(file.size)})`;

      const statusEl = document.createElement('span');
      statusEl.className = 'transfer-file-status';

      if (EXCLUDED_PHASE_EXTS.has(ext)) {
        wavFlacCount += 1;
        row.classList.add('is-warn');
        statusEl.textContent = 'Dilewati (WAV/FLAC fase berikutnya)';
      } else if (!ALLOWED_EXTS.has(ext)) {
        row.classList.add('is-error');
        statusEl.textContent = `Format ${ext || '?'} ditolak`;
        warnings.push(`${file.name}: format tidak didukung.`);
      } else if (file.size <= 0 || file.size > MAX_TRANSFER_BYTES) {
        row.classList.add('is-error');
        statusEl.textContent = file.size <= 0 ? 'File kosong' : 'Melebihi 150 MiB';
        warnings.push(`${file.name}: melebihi batas 150 MiB.`);
      } else if (accepted.length >= MAX_FILES_PER_BATCH) {
        row.classList.add('is-warn');
        statusEl.textContent = `Melebihi ${MAX_FILES_PER_BATCH} file/batch`;
      } else {
        row.classList.add('is-ready');
        statusEl.textContent = 'Siap diunggah';
        accepted.push(file);
      }

      row.appendChild(nameEl);
      row.appendChild(statusEl);
      if (previewList) previewList.appendChild(row);
    });

    if (wavFlacCount > 0) {
      warnings.unshift(
        `Peringatan: ${wavFlacCount} file .wav/.flac dilewati karena WAV/FLAC belum didukung pada fase ini (khusus .mp3/.m4a/.aac/.opus/.ogg/.webm maks. 150 MiB).`
      );
    }

    if (warningBanner && warningText) {
      if (warnings.length > 0) {
        warningText.textContent = warnings.join(' ');
        warningBanner.classList.remove('hidden');
      } else {
        warningBanner.classList.add('hidden');
      }
    }

    if (btnStartUpload) {
      btnStartUpload.disabled = uploading || accepted.length === 0;
    }
    return { accepted, warnings };
  }

  function renderUploadResults(data) {
    if (!previewList || !Array.isArray(data?.files)) return;
    previewList.replaceChildren();
    for (const item of data.files) {
      const row = document.createElement('div');
      row.className = 'transfer-file-row';
      const nameEl = document.createElement('span');
      nameEl.className = 'transfer-file-name';
      nameEl.textContent = item.filename && item.renamed
        ? `${item.name} → ${item.filename}`
        : (item.filename || item.name || 'audio');

      const badge = document.createElement('span');
      badge.className = 'transfer-file-status';
      if (item.status === 'saved') {
        row.classList.add('is-ok');
        badge.textContent = item.renamed ? 'Tersimpan (Auto-Rename)' : 'Tersimpan';
      } else if (item.status === 'skipped_duplicate') {
        row.classList.add('is-warn');
        badge.textContent = 'Duplikat SHA-256 (Dilewati)';
      } else if (item.status === 'skipped_wav_flac') {
        row.classList.add('is-warn');
        badge.textContent = 'WAV/FLAC Dilewati';
      } else {
        row.classList.add('is-error');
        badge.textContent = item.message || 'Ditolak';
      }
      row.appendChild(nameEl);
      row.appendChild(badge);
      previewList.appendChild(row);
    }
  }

  function startUpload() {
    if (uploading || !isRemoteController) return;
    const { accepted, warnings } = inspectSelectedFiles();
    if (!accepted.length) {
      if (uploadFeedback) uploadFeedback.textContent = 'Pilih minimal satu file .mp3/.m4a/.aac/.opus/.ogg/.webm (maks. 150 MiB).';
      return;
    }

    const creds = typeof getRemoteCredentials === 'function' ? getRemoteCredentials() : { pin: '', clientId: 'mobile' };
    if (!creds.pin) {
      if (uploadFeedback) uploadFeedback.textContent = 'Masukkan 4-digit PIN FOH Remote terlebih dahulu.';
      return;
    }

    let subfolder = '';
    if (playlistSelect) {
      if (playlistSelect.value === '__NEW__') {
        subfolder = (customSubfolderInput?.value || '').trim();
        if (!subfolder) {
          if (uploadFeedback) uploadFeedback.textContent = 'Isi nama folder playlist baru terlebih dahulu.';
          return;
        }
      } else {
        subfolder = playlistSelect.value;
      }
    }

    const formData = new FormData();
    formData.append('subfolder', subfolder);
    for (const file of accepted) {
      formData.append('files', file, file.name);
    }

    uploading = true;
    if (btnStartUpload) btnStartUpload.disabled = true;
    if (progressWrap) progressWrap.classList.remove('hidden');
    if (progressBar) progressBar.style.width = '0%';
    if (progressText) progressText.textContent = '0%';
    if (uploadFeedback) uploadFeedback.textContent = `Mengunggah ${accepted.length} file ke PC…`;

    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/remote/upload', true);
    xhr.setRequestHeader('X-FOH-PIN', creds.pin);
    xhr.setRequestHeader('X-FOH-Client-ID', creds.clientId || 'mobile');
    if (subfolder) {
      xhr.setRequestHeader('X-Swararia-Subfolder', encodeURIComponent(subfolder));
    }

    xhr.upload.onprogress = evt => {
      if (!evt.lengthComputable) return;
      const pct = Math.max(0, Math.min(100, Math.round((evt.loaded / evt.total) * 100)));
      if (progressBar) progressBar.style.width = `${pct}%`;
      if (progressText) progressText.textContent = `${pct}%`;
    };

    xhr.onload = () => {
      uploading = false;
      let data = {};
      try {
        data = JSON.parse(xhr.responseText || '{}');
      } catch (_) { /* Fallback handled below */ }

      if (xhr.status >= 200 && xhr.status < 300 && data.ok) {
        if (progressBar) progressBar.style.width = '100%';
        if (progressText) progressText.textContent = '100%';
        renderUploadResults(data);
        const parts = [`${data.saved_count || 0} tersimpan`];
        if (data.duplicate_count) parts.push(`${data.duplicate_count} duplikat dilewati`);
        if (data.rejected_count) parts.push(`${data.rejected_count} ditolak`);
        const summary = `Selesai: ${parts.join(', ')}.`;
        if (uploadFeedback) uploadFeedback.textContent = summary;
        if (Array.isArray(data.warnings) && data.warnings.length && warningBanner && warningText) {
          warningText.textContent = [...warnings, ...data.warnings].join(' ');
          warningBanner.classList.remove('hidden');
        }
        if (fileInput) fileInput.value = '';
        if (typeof onUploadCompleted === 'function' && (data.saved_count || 0) > 0) {
          onUploadCompleted();
        }
        if (typeof notify === 'function') {
          notify({ type: 'success', title: 'Upload dari HP Selesai', message: summary });
        }
      } else {
        const hasPerFileResults = Array.isArray(data.files) && data.files.length > 0;
        if (hasPerFileResults) {
          renderUploadResults(data);
          if (btnStartUpload) btnStartUpload.disabled = true;
        } else {
          inspectSelectedFiles();
        }
        if (uploadFeedback) uploadFeedback.textContent = data.error || `Gagal mengunggah (HTTP ${xhr.status}).`;
      }
    };

    xhr.onerror = () => {
      uploading = false;
      if (uploadFeedback) uploadFeedback.textContent = 'Koneksi Wi-Fi terputus saat mengunggah.';
      inspectSelectedFiles();
    };

    xhr.send(formData);
  }

  // Event bindings
  if (btnOpenConfig && configModal && !isRemoteController) {
    btnOpenConfig.addEventListener('click', () => {
      configModal.classList.remove('hidden');
      refreshConfigFromServer();
    });
    for (const closeId of ['btnClosePhoneUploadConfig', 'btnCancelPhoneUploadConfig']) {
      const b = el(closeId);
      if (b) b.addEventListener('click', () => configModal.classList.add('hidden'));
    }
    configModal.addEventListener('click', e => {
      if (e.target === configModal) configModal.classList.add('hidden');
    });
  }

  if (chkEnabled) {
    chkEnabled.addEventListener('change', () => {
      saveConfig({ enabled: Boolean(chkEnabled.checked) });
    });
  }
  if (btnUseCurrentSource) {
    btnUseCurrentSource.addEventListener('click', () => {
      const src = typeof getCurrentSourceDir === 'function' ? getCurrentSourceDir() : '';
      saveConfig({ dest_dir: src || '' });
    });
  }
  if (btnResetInbox) {
    btnResetInbox.addEventListener('click', () => saveConfig({ dest_dir: '' }));
  }

  if (btnOpenUpload && uploadModal) {
    btnOpenUpload.addEventListener('click', () => {
      populatePlaylistDropdown();
      if (uploadFeedback) uploadFeedback.textContent = '';
      if (progressWrap) progressWrap.classList.add('hidden');
      inspectSelectedFiles();
      uploadModal.classList.remove('hidden');
    });
    for (const closeId of ['btnClosePhoneUpload', 'btnCancelPhoneUpload']) {
      const b = el(closeId);
      if (b) b.addEventListener('click', () => uploadModal.classList.add('hidden'));
    }
    uploadModal.addEventListener('click', e => {
      if (e.target === uploadModal) uploadModal.classList.add('hidden');
    });
  }

  if (playlistSelect) {
    playlistSelect.addEventListener('change', syncCustomSubfolderVisibility);
  }
  if (fileInput) {
    fileInput.addEventListener('change', inspectSelectedFiles);
  }
  if (btnStartUpload) {
    btnStartUpload.addEventListener('click', startUpload);
  }

  if (!isRemoteController) {
    renderConfigState();
    refreshConfigFromServer();
  }

  return {
    applyRemoteState(statePayload) {
      if (!statePayload) return;
      if (typeof statePayload.upload_enabled === 'boolean') {
        uploadState.upload_enabled = statePayload.upload_enabled;
      }
      if (typeof statePayload.upload_dest_label === 'string') {
        uploadState.upload_dest_label = statePayload.upload_dest_label || 'library/inbox';
      }
      if (typeof statePayload.role === 'string') {
        uploadState.role = statePayload.role;
      }
      renderPhoneBannerButton();
    },
  };
}
