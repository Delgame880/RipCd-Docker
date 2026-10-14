/* RipCD web UI - no build step, no dependencies. */
(() => {
  "use strict";

  // ---------------------------------------------------------------- state --
  const state = {
    loaded: false,
    mock: false,
    version: "",
    system: {},
    settings: {},
    formats: [],
    paranoiaLevels: {},
    metadataProviders: {},
    mediaRoots: [],
    outputStatus: {},
    drives: [],
    disc: null,
    jobs: [],
    queue: {},
    history: [],
    logs: [],
    selected: new Set(),
    trackTitles: {},
    overrides: {},
    dirtyTitles: {},
    browserPath: null,
    browserListing: null,
    logOpen: false,
  };

  const $ = (id) => document.getElementById(id);
  const el = (tag, props = {}) => Object.assign(document.createElement(tag), props);
  const esc = (value) =>
    String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const fmtTime = (seconds) => {
    if (!seconds || seconds < 0) return "--:--";
    const s = Math.round(seconds);
    const m = Math.floor(s / 60);
    return `${m}:${String(s % 60).padStart(2, "0")}`;
  };
  const fmtBytes = (bytes) => {
    if (!bytes && bytes !== 0) return "unknown";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let value = bytes, i = 0;
    while (value >= 1024 && i < units.length - 1) { value /= 1024; i++; }
    return `${value.toFixed(value < 10 && i > 0 ? 1 : 0)} ${units[i]}`;
  };
  const fmtDate = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : "");

  // ------------------------------------------------------------------ api --
  const api = {
    async request(method, url, body) {
      const options = { method, headers: {} };
      if (body !== undefined) {
        options.headers["Content-Type"] = "application/json";
        options.body = JSON.stringify(body);
      }
      const response = await fetch(url, options);
      let data = null;
      const text = await response.text();
      if (text) { try { data = JSON.parse(text); } catch { data = { detail: text }; } }
      if (!response.ok) {
        const message = (data && (data.detail || data.error)) || `${response.status} ${response.statusText}`;
        throw new Error(typeof message === "string" ? message : JSON.stringify(message));
      }
      return data;
    },
    get: (url) => api.request("GET", url),
    post: (url, body) => api.request("POST", url, body),
    put: (url, body) => api.request("PUT", url, body),
    del: (url) => api.request("DELETE", url),
  };

  // -------------------------------------------------------------- toasts --
  function toast(title, message, level = "info", ttl = 5200) {
    const node = el("div", { className: `toast ${level}` });
    node.innerHTML = `<div class="t-title">${esc(title)}</div><div class="t-msg">${esc(message || "")}</div>`;
    $("toasts").appendChild(node);
    setTimeout(() => node.remove(), ttl);
  }

  // ------------------------------------------------------- formatting help --
  const formatInfo = (id) => state.formats.find((f) => f.id === id) || state.formats[0] || { qualities: {}, default_quality: "" };

  function formatOptions() {
    const select = $("formatSelect");
    select.innerHTML = state.formats
      .map((f) => `<option value="${esc(f.id)}">${esc(f.label)} (.${esc(f.extension)})</option>`)
      .join("");
    select.value = state.settings.format || "flac";
    renderQualityOptions();
  }

  function renderQualityOptions() {
    const format = formatInfo($("formatSelect").value);
    const quality = $("qualitySelect");
    const entries = Object.entries(format.qualities || {});
    quality.innerHTML = entries.map(([key, label]) => `<option value="${esc(key)}">${esc(label)}</option>`).join("");
    const wanted = state.settings.format === format.id ? state.settings.quality : "";
    quality.value = entries.some(([key]) => key === (wanted ?? "")) ? (wanted || "") : (format.default_quality || "");
    quality.disabled = entries.length <= 1;
  }

  function paranoiaOptions() {
    const select = $("paranoiaSelect");
    select.innerHTML = Object.entries(state.paranoiaLevels)
      .map(([key, label]) => `<option value="${esc(key)}">${esc(label)}</option>`)
      .join("");
    select.value = state.settings.paranoia || "full";
  }

  function providerOptions() {
    const select = $("providerSelect");
    select.innerHTML = Object.entries(state.metadataProviders)
      .map(([key, label]) => `<option value="${esc(key)}">${esc(label)}</option>`)
      .join("");
    select.value = state.settings.metadata_provider || "musicbrainz";
  }

  // ------------------------------------------------------------ rendering --
  function render() {
    renderTopbar();
    renderDrivePill();
    renderDisc();
    renderTracks();
    renderOptions();
    renderJob();
    renderHistory();
    renderMock();
    renderFooter();
    renderSettingsModal();
    renderLogModal();
    renderBrowser();
  }

  function renderTopbar() {
    const active = state.queue && state.queue.active;
    $("demoBadge").hidden = !state.mock;
    $("versionLabel").textContent = state.version ? `v${state.version}` : "";
  }

  function renderDrivePill() {
    const drives = state.drives || [];
    const disc = state.disc || {};
    const dot = $("driveDot");
    const text = $("driveText");
    if (!drives.length) {
      dot.className = "dot err";
      text.textContent = state.mock ? "no demo drive" : "no optical drive found";
      return;
    }
    const active = drives.find((d) => d.selected) || drives[0];
    if (disc.has_disc) {
      dot.className = "dot ok";
      text.textContent = `${active.label || active.device} · disc ready`;
    } else if (active.status === "tray_open") {
      dot.className = "dot warn";
      text.textContent = `${active.label || active.device} · tray open`;
    } else {
      dot.className = "dot";
      text.textContent = `${active.label || active.device} · no disc`;
    }
  }

  const DISC_ICON = `<svg viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="16" cy="16" r="13"/><circle cx="16" cy="16" r="4"/><path d="M16 3v4M16 25v4M3 16h4M25 16h4"/></svg>`;

  function renderDisc() {
    const card = $("discCard");
    const disc = state.disc || {};
    const job = state.queue && state.queue.active;

    if (!disc.device) {
      card.innerHTML = `<div class="empty-state">
        <div class="spinner"></div>
        <h2>Looking for a drive…</h2>
        <p class="muted">RipCD is scanning <code>/dev/sr*</code>. If this stays here, the optical drive is probably not
        passed into the container - see the Settings dialog for diagnostics.</p>
      </div>`;
      return;
    }

    if (!disc.has_disc) {
      const autoRip = state.settings.auto_rip;
      card.innerHTML = `<div class="empty-state">
        <div class="big-icon">${DISC_ICON}</div>
        <h2>Insert an audio CD</h2>
        <p class="muted">RipCD watches <code>${esc(disc.device)}</code> automatically - the disc will appear here the moment
        you close the tray.${autoRip ? " <strong>Auto-rip is on</strong>, so ripping starts by itself." : ""}</p>
        <div class="disc-actions" style="justify-content:center">
          <button class="btn" id="btnEmptyRescan">Scan now</button>
          <button class="btn ghost" id="btnEmptyClose">Close tray</button>
        </div>
      </div>`;
      $("btnEmptyRescan").onclick = () => rescan(true);
      $("btnEmptyClose").onclick = () => api.post("/api/drive/close").then(() => toast("Tray closed", "")).catch(showError);
      return;
    }

    const meta = disc.metadata || {};
    const pending = disc.metadata_pending;
    const kind = disc.kind;
    const isAudio = kind === "audio" || kind === "mixed";
    const toc = disc.toc || {};
    const known = meta.source && meta.source !== "none";
    const coverKey = state.coverStamp || Date.now();
    const cover = meta.release_id || meta.release_group_id
      ? `<img src="/api/cover/current?size=500&_=${coverKey}" alt="cover" title="Cover art from the Cover Art Archive" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'placeholder',innerHTML:'<span>no cover</span>'}))">`
      : `<div class="placeholder">${DISC_ICON}<span>no cover</span></div>`;

    const chips = [];
    if (meta.year) chips.push(`<span class="chip">${esc(meta.year)}</span>`);
    if (meta.genre) chips.push(`<span class="chip">${esc(meta.genre)}</span>`);
    if (meta.label) chips.push(`<span class="chip">${esc(meta.label)}</span>`);
    if (meta.disc_total > 1) chips.push(`<span class="chip">disc ${esc(meta.disc)}/${esc(meta.disc_total)}</span>`);
    chips.push(`<span class="chip">${esc(toc.tracks ? toc.tracks.length : 0)} tracks · ${esc(toc.total_time || "--:--")}</span>`);
    chips.push(`<span class="chip">${esc(disc.device)}</span>`);
    if (state.mock) chips.push(`<span class="chip warn">demo disc</span>`);
    if (pending) chips.push(`<span class="chip">looking up metadata…</span>`);
    else if (known) chips.push(`<span class="chip ok">${esc(meta.source)}${meta.confidence < 1 ? ` · ${Math.round((meta.confidence || 0) * 100)}%` : ""}</span>`);
    else chips.push(`<span class="chip warn">no metadata found</span>`);
    if (kind === "data") chips.push(`<span class="chip err">data CD - nothing to rip</span>`);
    if (kind === "mixed") chips.push(`<span class="chip warn">enhanced CD (data track ignored)</span>`);
    (meta.warnings || []).slice(0, 2).forEach((warning) => chips.push(`<span class="chip">${esc(warning)}</span>`));

    const autoRip = disc.auto_rip || {};
    let autoLine = "";
    if (autoRip.armed && !job) {
      autoLine = `<p class="hint good">Auto-rip starts in ${autoRip.seconds_left}s - press Cancel in the sidebar to stop it.</p>`;
    } else if (autoRip.enabled && autoRip.skipped) {
      autoLine = `<p class="hint">Auto-rip: ${esc(autoRip.skipped)}</p>`;
    }

    card.innerHTML = `
      <div class="disc-main">
        <div class="cover">${cover}</div>
        <div>
          <h2 class="disc-title">${esc(meta.title || (pending ? "Reading disc…" : "Unknown Album"))}</h2>
          <div class="disc-artist">${esc(meta.album_artist || (pending ? "" : "Unknown Artist"))}</div>
          <div class="chips">${chips.join("")}</div>
          ${autoLine}
          <div class="disc-actions">
            <button class="btn" id="btnRefreshMeta" ${pending ? "disabled" : ""}>${known ? "Re-lookup" : "Look up"}</button>
            <button class="btn ghost" id="btnEjectDisc">Eject</button>
            <button class="btn ghost" id="btnEditTags">${state.showTags ? "Hide tags" : "Edit tags"}</button>
            ${disc.disc_id ? `<span class="chip">disc id ${esc(disc.disc_id)}</span>` : ""}
          </div>
          ${state.showTags ? renderTagEditor(meta) : ""}
        </div>
      </div>`;

    $("btnRefreshMeta").onclick = refreshMetadata;
    $("btnEjectDisc").onclick = () => ejectDisc();
    $("btnEditTags").onclick = () => { state.showTags = !state.showTags; render(); };
    const resetTags = $("btnResetTags");
    if (resetTags) {
      resetTags.onclick = () => { state.overrides = {}; render(); recalcPreview(); };
    }
    card.querySelectorAll("[data-override]").forEach((input) => {
      input.oninput = () => {
        const key = input.dataset.override;
        const original = (disc.metadata || {})[key === "album" ? "title" : key] || "";
        if (input.value && input.value !== original) state.overrides[key] = input.value;
        else delete state.overrides[key];
        state.dirtyOverrides = true;
        renderTracks();
        recalcPreview();
      };
    });
  }

  function renderTagEditor(meta) {
    const fields = [
      ["album", "Album", meta.title || ""],
      ["album_artist", "Album artist", meta.album_artist || ""],
      ["year", "Year", meta.year || ""],
      ["genre", "Genre", meta.genre || ""],
      ["label", "Label", meta.label || ""],
    ];
    return `<div class="tag-editor">
      <div class="tag-grid">
        ${fields
          .map(([key, label, value]) => {
            const current = state.overrides[key] ?? value;
            return `<label class="field"><span class="label">${esc(label)}</span>
              <input type="text" data-override="${esc(key)}" value="${esc(current)}" placeholder="${esc(value)}"></label>`;
          })
          .join("")}
      </div>
      <div class="row" style="margin-top:10px">
        <button class="btn tiny" id="btnResetTags">Reset to MusicBrainz values</button>
        <span class="hint">Your edits are written into the tags and the folder name.</span>
      </div>
    </div>`;
  }

  function renderTracks() {
    const card = $("tracksCard");
    const disc = state.disc || {};
    const tracks = disc.tracks || [];
    const job = state.queue && state.queue.active;
    if (!tracks.length) { card.hidden = true; return; }
    card.hidden = false;

    const audioTracks = tracks.filter((t) => t.audio);
    const selected = audioTracks.filter((t) => state.selected.has(t.number));
    $("trackSummary").textContent =
      `${selected.length}/${audioTracks.length} selected · ${fmtTime(selected.reduce((sum, t) => sum + (t.seconds || 0), 0))}`;

    const jobTracks = {};
    if (job) (job.tracks || []).forEach((t) => { jobTracks[t.number] = t; });

    $("trackTable").innerHTML = tracks
      .map((track) => {
        const progress = jobTracks[track.number];
        const isSelected = state.selected.has(track.number);
        const title = state.trackTitles[track.number] ?? (state.overrides.album && !track.known ? track.title : track.title);
        const meta = (disc.metadata || {}).tracks || [];
        const metaTitle = (meta.find((t) => t.number === track.number) || {}).title || "";
        const value = state.trackTitles[track.number] ?? track.title;
        let status = "";
        if (!track.audio) status = `<span class="state">data track</span>`;
        else if (progress) {
          const cls = progress.state === "done" ? "ok" : progress.state === "error" ? "err" : "busy";
          const label = {
            pending: "waiting", ripping: "reading", encoding: "encoding",
            done: `${fmtBytes(progress.size_bytes)}`, error: "failed", canceled: "canceled", skipped: "skipped",
          }[progress.state] || progress.state;
          status = `<span class="state ${cls}">${esc(label)}</span><div class="mini-bar"><i style="width:${progress.percent}%"></i></div>`;
        } else {
          const length = Number((meta.find((t) => t.number === track.number) || {}).length_ms || 0) / 1000;
          status = `<span class="state">${length ? fmtTime(length) : ""}</span>`;
        }
        return `<div class="track-row ${isSelected ? "selected" : ""} ${progress && progress.state === "error" ? "failed" : ""}" data-track="${track.number}">
          <input type="checkbox" ${isSelected ? "checked" : ""} ${track.audio ? "" : "disabled"} data-check="${track.number}">
          <span class="num">${String(track.number).padStart(2, "0")}</span>
          <input class="title" data-title="${track.number}" value="${esc(value)}" placeholder="${esc(metaTitle || track.title)}" spellcheck="false">
          <span class="time">${esc(track.duration)}</span>
          <div>${status}</div>
        </div>`;
      })
      .join("");

    $("trackTable").querySelectorAll("[data-check]").forEach((box) => {
      box.onchange = () => {
        const number = Number(box.dataset.check);
        if (box.checked) state.selected.add(number); else state.selected.delete(number);
        renderTracks();
        recalcPreview();
      };
    });
    $("trackTable").querySelectorAll("[data-title]").forEach((input) => {
      input.oninput = () => {
        const number = Number(input.dataset.title);
        const original = ((disc.metadata || {}).tracks || []).find((t) => t.number === number);
        const originalTitle = (original && original.title) || `Track ${String(number).padStart(2, "0")}`;
        if (input.value && input.value !== originalTitle) state.trackTitles[number] = input.value;
        else delete state.trackTitles[number];
      };
      input.onblur = () => recalcPreview();
      input.onkeydown = (event) => { if (event.key === "Enter") input.blur(); };
    });
  }

  function renderOptions() {
    const settings = state.settings || {};
    const disc = state.disc || {};
    const hasDisc = Boolean(disc.has_disc) && (disc.tracks || []).some((t) => t.audio);

    const outputInput = $("outputDir");
    if (document.activeElement !== outputInput) outputInput.value = settings.output_dir || "";
    const status = state.outputStatus || {};
    const statusEl = $("outputStatus");
    if (status.ok) {
      statusEl.className = "hint good";
      statusEl.textContent = `writable${status.free_bytes ? ` · ${fmtBytes(status.free_bytes)} free` : ""}${status.filesystem && status.filesystem !== "unknown" ? ` · ${status.filesystem}` : ""}`;
    } else {
      statusEl.className = "hint bad";
      statusEl.textContent = status.error || "folder not usable";
    }
    if (document.activeElement !== $("speedSelect")) $("speedSelect").value = String(settings.read_speed ?? 0);
    if (document.activeElement !== $("albumTemplate")) $("albumTemplate").value = settings.album_folder_template || "";
    if (document.activeElement !== $("trackTemplate")) $("trackTemplate").value = settings.track_file_template || "";
    $("optCover").checked = settings.embed_cover !== false;
    $("optPlaylist").checked = settings.write_m3u !== false;
    $("optEject").checked = settings.eject_after_rip !== false;
    $("optKeepWav").checked = Boolean(settings.keep_wav);

    const button = $("btnStartRip");
    const active = state.queue && state.queue.active;
    const count = state.selected.size;
    button.disabled = !hasDisc || Boolean(active) || count === 0;
    if (active) button.textContent = "Ripping…";
    else if (!disc.device) button.textContent = "No drive";
    else if (!disc.has_disc) button.textContent = "Insert a CD to start";
    else if (!hasDisc) button.textContent = "Not an audio CD";
    else if (!count) button.textContent = "Select at least one track";
    else button.textContent = `Rip ${count} track${count === 1 ? "" : "s"}`;

    const hint = $("ripHint");
    if (hasDisc) {
      const seconds = (disc.tracks || []).filter((t) => state.selected.has(t.number)).reduce((sum, t) => sum + (t.seconds || 0), 0);
      hint.textContent = `${fmtTime(seconds)} of audio · estimated ${estimateSize(seconds)}`;
    } else {
      hint.textContent = "";
    }
  }

  function estimateSize(seconds) {
    const format = formatInfo($("formatSelect") ? $("formatSelect").value : state.settings.format);
    const quality = $("qualitySelect") ? $("qualitySelect").value : state.settings.quality;
    if (!format) return "unknown";
    if (format.kind === "lossless") {
      const kbps = format.id === "flac" ? 750 : format.id === "wav" ? 1411 : 1000;
      return fmtBytes((seconds * kbps * 1000) / 8);
    }
    const bitrates = { v0: 245, v2: 190, 320: 320, 192: 192, 128: 128, q8: 256, q6: 192, q4: 128, 256: 256 };
    const kbps = bitrates[quality] || 192;
    return fmtBytes((seconds * kbps * 1000) / 8);
  }

  async function recalcPreview() {
    const disc = state.disc || {};
    if (!disc.has_disc) { $("pathPreview").textContent = "select a disc to see the file names"; return; }
    const payload = {
      format: $("formatSelect").value,
      quality: $("qualitySelect").value,
      tracks: [...state.selected],
      overrides: state.overrides,
      track_titles: state.trackTitles,
      album_folder_template: $("albumTemplate").value,
      track_file_template: $("trackTemplate").value,
      output_dir: $("outputDir").value,
    };
    try {
      const result = await api.post("/api/preview", payload);
      state.preview = result;
      const lines = result.files.map((f) => f.name);
      $("pathPreview").textContent = `${result.directory}\n${lines.join("\n")}`;
    } catch (error) {
      $("pathPreview").textContent = `cannot preview: ${error.message}`;
    }
  }

  function renderJob() {
    const card = $("jobCard");
    const job = (state.queue && state.queue.active) || state.lastFinishedJob;
    if (!job) { card.hidden = true; return; }
    card.hidden = false;
    const finished = !["queued", "running"].includes(job.state);
    const stateClass = job.state === "error" ? "err" : job.state === "done" ? "done" : "active";
    const doneTracks = (job.tracks || []).filter((t) => t.state === "done").length;
    const elapsed = job.elapsed || 0;
    const remaining = job.percent > 2 ? (elapsed * (100 - job.percent)) / job.percent : 0;

    const logLines = (job.log || []).slice(-40);
    const files = (job.files || []).filter((f) => !f.endsWith(".m3u8") && !f.endsWith(".json"));

    card.innerHTML = `
      <div class="job-head">
        <span class="job-title">${esc((job.metadata || {}).title || "Rip")}</span>
        <span class="job-stage">${esc(job.message || job.stage || job.state)}</span>
        <div class="spacer"></div>
        <span class="chip ${job.state === "done" ? "ok" : job.state === "error" ? "err" : "accent"}">${esc(job.state)}</span>
        <span class="chip">${esc((job.format || "").toUpperCase())}</span>
        ${job.mock ? '<span class="chip warn">demo</span>' : ""}
      </div>
      <div class="progress ${stateClass}"><i style="width:${job.percent}%"></i></div>
      <div class="job-meta">
        <span>${job.percent.toFixed(0)}%</span>
        <span>${doneTracks}/${(job.tracks || []).length} tracks</span>
        <span>${fmtBytes(job.bytes_written)} written</span>
        <span>elapsed ${fmtTime(elapsed)}</span>
        ${!finished && remaining > 1 ? `<span>~${fmtTime(remaining)} left</span>` : ""}
        ${job.error ? `<span style="color:var(--err)">${esc(job.error)}</span>` : ""}
      </div>
      ${job.warnings && job.warnings.length ? `<p class="hint">${job.warnings.map(esc).join(" · ")}</p>` : ""}
      <div class="job-log">${logLines
        .map((line) => `<div class="${/ERROR/.test(line) ? "error" : /WARNING/.test(line) ? "warn" : ""}">${esc(line)}</div>`)
        .join("")}</div>
      ${files.length ? `<div class="job-files">${files.slice(0, 12).map((f) => `<span class="file">${esc(f)}</span>`).join("")}</div>` : ""}
      <div class="job-actions">
        ${!finished ? `<button class="btn danger" id="btnCancelJob">Cancel rip</button>` : ""}
        ${finished && job.state === "error" ? `<button class="btn" id="btnRetryJob">Retry</button>` : ""}
        ${finished ? `<button class="btn ghost" id="btnDismissJob">Hide</button>` : ""}
      </div>`;

    if ($("btnCancelJob")) $("btnCancelJob").onclick = () => api.post(`/api/jobs/${job.id}/cancel`).catch(showError);
    if ($("btnRetryJob")) $("btnRetryJob").onclick = () => api.post(`/api/jobs/${job.id}/retry`, {}).catch(showError);
    if ($("btnDismissJob")) $("btnDismissJob").onclick = () => { state.lastFinishedJob = null; renderJob(); };
  }

  function renderHistory() {
    const card = $("historyCard");
    const history = (state.history || []).filter((h) => h.state === "done" || h.state === "error");
    if (!history.length) { card.hidden = true; return; }
    card.hidden = false;
    card.innerHTML = `
      <div class="card-head">
        <h2>History</h2>
        <span class="muted small">${history.length} rip${history.length === 1 ? "" : "s"}</span>
        <div class="spacer"></div>
        <button class="btn tiny ghost" id="btnClearHistory">Clear</button>
      </div>
      <div class="history-list">
        ${history
          .map((item) => {
            const cover = item.cover_url
              ? `<img src="/api/cover/${esc((item.cover_url.match(/release\/([^/]+)\//) || [])[1] || "")}" alt="" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'thumb-ph',textContent:'♪'}))">`
              : `<div class="thumb-ph">♪</div>`;
            return `<div class="history-item">
              ${cover}
              <div>
                <div class="h-title">${esc(item.title || "Unknown Album")} ${item.state === "error" ? '<span class="chip err">failed</span>' : ""}</div>
                <div class="h-sub">${esc(item.album_artist || "")}${item.year ? ` · ${esc(item.year)}` : ""} · ${item.tracks_done}/${item.tracks_total} tracks · ${esc((item.format || "").toUpperCase())} · ${fmtDate(item.finished_at)}</div>
                <div class="h-sub" title="${esc(item.output_dir || "")}">${esc(item.output_dir || "")}</div>
              </div>
              <div class="h-actions">
                <button class="btn tiny" data-again="${esc(item.id)}">Rip again</button>
                <button class="btn tiny ghost" data-forget="${esc(item.id)}">Hide</button>
              </div>
            </div>`;
          })
          .join("")}
      </div>`;

    $("btnClearHistory").onclick = () => api.del("/api/history").then(() => toast("History cleared", "")).catch(showError);
    card.querySelectorAll("[data-again]").forEach((button) => {
      button.onclick = () => {
        const item = history.find((h) => h.id === button.dataset.again);
        if (!item) return;
        if (item.disc_id && state.disc && state.disc.disc_id && item.disc_id !== state.disc.disc_id) {
          toast("Different disc", "Insert the same CD again to repeat this rip.", "warning");
          return;
        }
        api.post(`/api/jobs/${item.id}/retry`, {}).catch(showError);
      };
    });
    card.querySelectorAll("[data-forget]").forEach((button) => {
      button.onclick = () => {
        state.history = state.history.filter((h) => h.id !== button.dataset.forget);
        renderHistory();
      };
    });
  }

  function renderMock() {
    const card = $("mockCard");
    card.hidden = !state.mock;
    if (!state.mock) return;
    const demos = state.mockDiscs || [];
    $("mockList").innerHTML = demos
      .map(
        (disc) => `<div class="mock-disc ${disc.key === state.mockInserted ? "active" : ""}" data-disc="${esc(disc.key)}">
          <span>${disc.audio ? "💿" : "📀"}</span>
          <div><div>${esc(disc.title)}</div><small>${esc(disc.artist || "—")} · ${disc.tracks} tracks</small></div>
        </div>`
      )
      .join("");
    $("mockList").querySelectorAll("[data-disc]").forEach((node) => {
      node.onclick = () => api.post("/api/mock/insert", { disc: node.dataset.disc }).then(() => refreshDisc()).catch(showError);
    });
    $("btnMockEject").onclick = () => api.post("/api/mock/eject").then(() => refreshDisc()).catch(showError);
    $("btnMockClose").onclick = () => api.post("/api/mock/close").then(() => refreshDisc()).catch(showError);
  }

  function renderFooter() {
    const system = state.system || {};
    const tools = system.tools || {};
    const missing = Object.values(tools).filter((t) => t.required && !t.available).map((t) => t.name);
    const parts = [`${system.machine || "?"}`, `python ${system.python || "?"}`];
    if (missing.length) parts.push(`missing: ${missing.join(", ")}`);
    else parts.push("tools ok");
    if (state.mock) parts.push("demo mode");
    $("footerMeta").textContent = `RipCD ${state.version} · ${parts.join(" · ")}`;
  }

  function renderSettingsModal() {
    const settings = state.settings || {};
    $("setAutoscan").checked = settings.autoscan !== false;
    $("setAutoRip").checked = Boolean(settings.auto_rip);
    if (document.activeElement !== $("setAutoRipDelay")) $("setAutoRipDelay").value = settings.auto_rip_delay ?? 20;
    if (document.activeElement !== $("setPoll")) $("setPoll").value = settings.poll_interval ?? 5;
    if (document.activeElement !== $("setToken")) $("setToken").value = "";
    $("setToken").placeholder = settings.api_token_set ? "a token is set - type a new one to replace it" : "empty = no password (LAN only)";

    const deviceSelect = $("setDevice");
    const current = settings.device || "";
    const drives = state.drives || [];
    deviceSelect.innerHTML =
      `<option value="">Automatic (first drive with a disc)</option>` +
      drives.map((d) => `<option value="${esc(d.device)}">${esc(d.label || d.device)} (${esc(d.device)})</option>`).join("");
    deviceSelect.value = current;

    const roots = state.mediaRoots || [];
    $("settingsRoots").innerHTML = roots.map((root) => `<span class="chip">${esc(root)}</span>`).join("") || '<span class="chip warn">none configured</span>';

    const tools = (state.system || {}).tools || {};
    const tree = state.deviceTree || {};
    const items = [
      ["Version", `RipCD ${state.version}${state.mock ? " (demo)" : ""}`, ""],
      ["cdparanoia", tools.cdparanoia && tools.cdparanoia.available ? tools.cdparanoia.version || "installed" : "missing", tools.cdparanoia && tools.cdparanoia.available ? "good" : "bad"],
      ["ffmpeg", tools.ffmpeg && tools.ffmpeg.available ? tools.ffmpeg.version || "installed" : "missing", tools.ffmpeg && tools.ffmpeg.available ? "good" : "bad"],
      ["Devices visible", Object.keys(tree.devices || {}).filter((d) => tree.devices[d]).join(", ") || "none", Object.keys(tree.devices || {}).some((d) => tree.devices[d]) ? "good" : "bad"],
      ["sr* in /sys", (tree.sr_in_sysfs || []).join(", ") || "none", (tree.sr_in_sysfs || []).length ? "good" : "bad"],
      ["sg* in /sys", (tree.sg_in_sysfs || []).join(", ") || "none", ""],
      ["Free space", (state.outputStatus && state.outputStatus.free_bytes) ? fmtBytes(state.outputStatus.free_bytes) : "unknown", ""],
    ];
    $("settingsDiag").innerHTML = items
      .map(([label, value, cls]) => `<div class="item"><b>${esc(label)}</b><span class="val ${cls}">${esc(value)}</span></div>`)
      .join("");
  }

  function renderLogModal() {
    if (!state.logOpen) return;
    const view = $("logView");
    const atBottom = view.scrollTop + view.clientHeight >= view.scrollHeight - 30;
    view.innerHTML = (state.logs || [])
      .slice(-400)
      .map((line) => {
        const time = new Date(line.time * 1000).toLocaleTimeString();
        return `<div class="log-line ${esc(line.level)}"><span class="t">${esc(time)}</span><span class="m"><span class="src">${esc(line.source)}:</span> ${esc(line.message)}</span></div>`;
      })
      .join("");
    if (atBottom) view.scrollTop = view.scrollHeight;
  }

  function renderBrowser() {
    const modal = $("browserModal");
    if (modal.hidden) return;
    const listing = state.browserListing;
    if (!listing) return;
    $("browserCrumbs").innerHTML =
      (listing.breadcrumbs || [])
        .map((crumb) => `<button data-crumb="${esc(crumb.path)}">${esc(crumb.name)}</button>`)
        .join('<span class="sep">/</span>') +
      `<span class="sep">·</span><span class="muted">${listing.writable ? "writable" : "read-only"}${listing.free_bytes ? ` · ${fmtBytes(listing.free_bytes)} free` : ""}</span>`;

    const entries = listing.entries || [];
    const rows = [];
    if (listing.parent) rows.push(`<div class="browser-entry" data-dir="${esc(listing.parent)}"><span class="ico">↩</span><span>..</span></div>`);
    rows.push(
      ...entries
        .filter((entry) => entry.is_dir)
        .map((entry) => `<div class="browser-entry" data-dir="${esc(entry.path)}"><span class="ico">📁</span><span>${esc(entry.name)}</span><span class="meta">${entry.is_dir ? "folder" : fmtBytes(entry.size)}</span></div>`)
    );
    $("browserList").innerHTML = rows.length ? rows.join("") : '<div class="browser-empty">No sub-folders here - create one below.</div>';

    $("browserList").querySelectorAll("[data-dir]").forEach((node) => {
      node.onclick = () => loadBrowser(node.dataset.dir);
    });
    $("browserCrumbs").querySelectorAll("[data-crumb]").forEach((node) => {
      node.onclick = () => loadBrowser(node.dataset.crumb);
    });
  }

  // -------------------------------------------------------------- actions --
  function showError(error) {
    toast("Something went wrong", error.message || String(error), "error", 8000);
  }

  async function loadState() {
    const data = await api.get("/api/state");
    applyState(data);
  }

  function applyState(data) {
    state.loaded = true;
    state.mock = Boolean(data.mock);
    state.version = data.version;
    state.system = data.system || {};
    state.settings = data.settings || {};
    state.formats = data.formats || [];
    state.paranoiaLevels = data.paranoia_levels || {};
    state.metadataProviders = data.metadata_providers || {};
    state.mediaRoots = data.media_roots || [];
    state.outputStatus = data.output_status || {};
    state.drives = data.drives || [];
    state.queue = data.queue || {};
    state.jobs = data.jobs || [];
    state.history = data.history || [];
    state.logs = data.logs || [];
    state.deviceTree = data.device_tree || {};
    if (!state.disc || (data.disc && data.disc.signature !== state.disc.signature)) {
      applyDisc(data.disc);
    } else {
      state.disc = data.disc || state.disc;
    }
    if (!state.formatsRendered) {
      formatOptions();
      paranoiaOptions();
      providerOptions();
      state.formatsRendered = true;
    }
    render();
  }

  function applyDisc(disc) {
    const previous = state.disc;
    state.disc = disc || null;
    if (!disc) return;
    if (!previous || previous.signature !== disc.signature) {
      // New disc: reset selection and per-disc edits.
      state.selected = new Set((disc.tracks || []).filter((t) => t.audio).map((t) => t.number));
      state.trackTitles = {};
      state.overrides = {};
      state.showTags = false;
      state.coverStamp = Date.now();
      state.lastFinishedJob = null;
    } else {
      // Keep the selection in sync when tracks appear later.
      const audioNumbers = (disc.tracks || []).filter((t) => t.audio).map((t) => t.number);
      if (!state.selected.size) state.selected = new Set(audioNumbers);
      [...state.selected].forEach((number) => { if (!audioNumbers.includes(number)) state.selected.delete(number); });
    }
  }

  async function refreshDisc(wait = 0) {
    const disc = await api.post(`/api/disc/refresh?wait=${wait}`);
    applyDisc(disc);
    render();
    recalcPreview();
    return disc;
  }

  async function rescan(force = false) {
    try {
      if (force) await api.post("/api/drives/rescan");
      await api.get("/api/disc?refresh=true").then(applyDisc);
      render();
      recalcPreview();
      toast("Rescanned", "Drive and disc state refreshed.");
    } catch (error) { showError(error); }
  }

  async function ejectDisc() {
    try {
      await api.post("/api/drive/eject");
      toast("Ejecting", "The tray should open now.");
    } catch (error) { showError(error); }
  }

  async function refreshMetadata() {
    const button = $("btnRefreshMeta");
    if (button) { button.disabled = true; button.textContent = "Looking up…"; }
    try {
      const result = await api.post("/api/metadata/refresh?wait=25");
      applyDisc(result.disc);
      const meta = result.metadata || {};
      toast(
        meta.source === "none" ? "No match found" : "Metadata updated",
        meta.source === "none" ? "Fill in the tags manually, they are written to the files." : `${meta.album_artist} - ${meta.title}`,
        meta.source === "none" ? "warning" : "success"
      );
      state.coverStamp = Date.now();
      render();
      recalcPreview();
    } catch (error) { showError(error); }
    finally { if (button) button.disabled = false; }
  }

  async function startRip() {
    const disc = state.disc || {};
    if (!disc.has_disc) return;
    const button = $("btnStartRip");
    button.disabled = true;
    try {
      const payload = {
        device: disc.device,
        output_dir: $("outputDir").value,
        format: $("formatSelect").value,
        quality: $("qualitySelect").value,
        tracks: [...state.selected],
        overrides: state.overrides,
        track_titles: state.trackTitles,
        paranoia: $("paranoiaSelect").value,
        read_speed: Number($("speedSelect").value),
        embed_cover: $("optCover").checked,
        write_m3u: $("optPlaylist").checked,
        eject_after_rip: $("optEject").checked,
        keep_wav: $("optKeepWav").checked,
        album_folder_template: $("albumTemplate").value,
        track_file_template: $("trackTemplate").value,
        metadata_provider: $("providerSelect").value,
      };
      const job = await api.post("/api/jobs", payload);
      toast("Rip started", `${(job.tracks || []).length} track(s) → ${job.output_dir}`, "success");
    } catch (error) {
      showError(error);
    } finally {
      renderOptions();
      render();
    }
  }

  async function saveSettings(patch, { quiet = true } = {}) {
    try {
      const result = await api.put("/api/settings", patch);
      state.settings = result.settings;
      state.outputStatus = result.output_status || state.outputStatus;
      state.mediaRoots = result.settings.media_roots || state.mediaRoots;
      if (!quiet) toast("Settings saved", "");
      render();
      return result;
    } catch (error) {
      showError(error);
      throw error;
    }
  }

  let saveTimer = null;
  function saveSettingsDebounced(patch) {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => saveSettings(patch).catch(() => {}), 350);
  }

  // folder browser
  async function openBrowser() {
    $("browserModal").hidden = false;
    await loadBrowser(state.browserPath || $("outputDir").value || (state.mediaRoots[0] || "/"));
  }

  async function loadBrowser(path) {
    try {
      const listing = await api.get(`/api/browse?path=${encodeURIComponent(path)}`);
      state.browserPath = listing.path;
      state.browserListing = listing;
      $("browserModal").hidden = false;
      renderBrowser();
    } catch (error) { showError(error); }
  }

  // ---------------------------------------------------------------- events --
  function connectEvents() {
    const source = new EventSource("/api/events");
    const indicator = $("liveIndicator");
    source.addEventListener("open", () => indicator.classList.remove("off"));
    source.addEventListener("error", () => indicator.classList.add("off"));
    source.addEventListener("state", (event) => { applyState(JSON.parse(event.data)); });
    source.addEventListener("disc", (event) => {
      applyDisc(JSON.parse(event.data));
      render();
      recalcPreview();
    });
    source.addEventListener("metadata", (event) => {
      const payload = JSON.parse(event.data);
      if (state.disc && payload.signature === state.disc.signature) refreshDisc();
    });
    source.addEventListener("job", (event) => {
      const job = JSON.parse(event.data);
      if (job.state === "queued" || job.state === "running") {
        state.queue = { ...state.queue, active: job };
      } else {
        state.queue = { ...state.queue, active: null };
        state.lastFinishedJob = job;
      }
      renderJob();
      renderOptions();
    });
    source.addEventListener("progress", (event) => {
      const job = JSON.parse(event.data);
      state.queue = { ...state.queue, active: job };
      renderJob();
      if (state.disc && state.disc.has_disc && job.state === "running") renderTracks();
    });
    source.addEventListener("queue", (event) => {
      const payload = JSON.parse(event.data);
      if (!payload.active) state.queue = { ...state.queue, active: null, ...payload };
      else state.queue = { ...state.queue, ...payload };
      renderOptions();
    });
    source.addEventListener("notification", (event) => {
      const payload = JSON.parse(event.data);
      toast(payload.title || "Notice", payload.message || "", payload.level || "info");
    });
    source.addEventListener("log", (event) => {
      state.logs.push(JSON.parse(event.data));
      if (state.logs.length > 800) state.logs.splice(0, 300);
      renderLogModal();
    });
    source.addEventListener("settings", (event) => {
      state.settings = JSON.parse(event.data);
      render();
    });
  }

  function bindUi() {
    $("btnRescan").onclick = () => rescan(true);
    $("btnEject").onclick = () => ejectDisc();
    $("btnStartRip").onclick = () => startRip();
    $("btnBrowse").onclick = () => openBrowser();
    $("btnAllTracks").onclick = () => { state.selected = new Set((state.disc.tracks || []).filter((t) => t.audio).map((t) => t.number)); renderTracks(); renderOptions(); recalcPreview(); };
    $("btnNoTracks").onclick = () => { state.selected = new Set(); renderTracks(); renderOptions(); recalcPreview(); };
    $("btnInvertTracks").onclick = () => {
      const audio = (state.disc.tracks || []).filter((t) => t.audio).map((t) => t.number);
      state.selected = new Set(audio.filter((n) => !state.selected.has(n)));
      renderTracks(); renderOptions(); recalcPreview();
    };

    $("formatSelect").onchange = () => { renderQualityOptions(); saveSettingsDebounced({ format: $("formatSelect").value }); recalcPreview(); renderOptions(); };
    $("qualitySelect").onchange = () => { saveSettingsDebounced({ format: $("formatSelect").value, quality: $("qualitySelect").value }); recalcPreview(); renderOptions(); };
    $("paranoiaSelect").onchange = () => saveSettingsDebounced({ paranoia: $("paranoiaSelect").value });
    $("speedSelect").onchange = () => saveSettingsDebounced({ read_speed: Number($("speedSelect").value) });
    $("providerSelect").onchange = () => saveSettingsDebounced({ metadata_provider: $("providerSelect").value });
    $("albumTemplate").onchange = () => { saveSettingsDebounced({ album_folder_template: $("albumTemplate").value }); recalcPreview(); };
    $("trackTemplate").onchange = () => { saveSettingsDebounced({ track_file_template: $("trackTemplate").value }); recalcPreview(); };
    $("optCover").onchange = () => saveSettingsDebounced({ embed_cover: $("optCover").checked });
    $("optPlaylist").onchange = () => saveSettingsDebounced({ write_m3u: $("optPlaylist").checked });
    $("optEject").onchange = () => saveSettingsDebounced({ eject_after_rip: $("optEject").checked });
    $("optKeepWav").onchange = () => saveSettingsDebounced({ keep_wav: $("optKeepWav").checked });
    $("outputDir").onchange = () => {
      saveSettings({ output_dir: $("outputDir").value }, { quiet: false })
        .then(() => recalcPreview())
        .catch(() => {});
    };

    // settings modal
    $("btnSettings").onclick = () => { $("settingsModal").hidden = false; renderSettingsModal(); };
    $("setAutoscan").onchange = () => saveSettings({ autoscan: $("setAutoscan").checked });
    $("setAutoRip").onchange = () => saveSettings({ auto_rip: $("setAutoRip").checked });
    $("setAutoRipDelay").onchange = () => saveSettings({ auto_rip_delay: Number($("setAutoRipDelay").value) });
    $("setPoll").onchange = () => saveSettings({ poll_interval: Number($("setPoll").value) });
    $("setDevice").onchange = () => saveSettings({ device: $("setDevice").value });
    $("setToken").onchange = () => saveSettings({ api_token: $("setToken").value }).then(() => toast("Token updated", "The UI will ask for the token from now on."));
    $("btnRecheckTools").onclick = async () => {
      const data = await api.get("/api/system?refresh=true").catch(showError);
      if (data) { state.system = data.system; state.deviceTree = data.device_tree; renderSettingsModal(); renderFooter(); toast("Tools re-checked", ""); }
    };
    $("btnResetSettings").onclick = () => api.post("/api/settings/reset").then((result) => { state.settings = result.settings; render(); toast("Settings reset", ""); }).catch(showError);

    // logs modal
    $("btnLogs").onclick = () => { state.logOpen = true; $("logModal").hidden = false; renderLogModal(); };
    $("btnClearLogs").onclick = () => api.del("/api/logs").then(() => { state.logs = []; renderLogModal(); });

    // modals close buttons
    document.querySelectorAll("[data-close]").forEach((node) => {
      node.onclick = () => {
        const modal = node.closest(".modal");
        modal.hidden = true;
        if (modal.id === "logModal") state.logOpen = false;
      };
    });
    document.querySelectorAll(".modal").forEach((modal) => {
      modal.addEventListener("click", (event) => {
        if (event.target === modal) {
          modal.hidden = true;
          if (modal.id === "logModal") state.logOpen = false;
        }
      });
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") document.querySelectorAll(".modal").forEach((m) => { m.hidden = true; });
    });

    // browser modal
    $("btnUseFolder").onclick = () => {
      if (!state.browserPath) return;
      $("outputDir").value = state.browserPath;
      $("browserModal").hidden = true;
      saveSettings({ output_dir: state.browserPath }, { quiet: false }).then(() => recalcPreview()).catch(() => {});
    };
    $("btnNewFolder").onclick = async () => {
      const name = $("newFolderName").value.trim();
      if (!name) return;
      try {
        const result = await api.post("/api/browse/mkdir", { path: state.browserPath, name });
        $("newFolderName").value = "";
        await loadBrowser(result.path);
      } catch (error) { showError(error); }
    };
  }

  // ------------------------------------------------------------------ boot --
  async function boot() {
    bindUi();
    try {
      await loadState();
      if (state.mock) {
        const mock = await api.get("/api/mock/status");
        state.mockDiscs = mock.discs;
        state.mockInserted = mock.inserted;
      }
      recalcPreview();
    } catch (error) {
      toast("Cannot reach RipCD", error.message, "error", 12000);
    }
    connectEvents();
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
