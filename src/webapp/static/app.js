(() => {
  const logEl = document.getElementById("log");
  const statusEl = document.getElementById("job-status");
  const resultEl = document.getElementById("result");
  const retryBtn = document.getElementById("retry-job");
  const editList = document.getElementById("edit-list");
  const edlSelect = document.getElementById("edl-select");
  const createForm = document.getElementById("create-form");
  const reviseForm = document.getElementById("revise-form");
  const refreshBtn = document.getElementById("refresh-edits");
  const clearOutputsBtn = document.getElementById("clear-outputs");
  const libraryStatus = document.getElementById("library-status");
  const refreshMediaBtn = document.getElementById("refresh-media");
  const mediaGrid = document.getElementById("media-grid");
  const mediaMeta = document.getElementById("media-meta");
  const createMeta = document.getElementById("create-meta");
  const uploadForm = document.getElementById("upload-form");
  const folderInput = document.getElementById("folder-input");
  const filesInput = document.getElementById("files-input");
  const uploadBtn = document.getElementById("upload-btn");
  const uploadStatus = document.getElementById("upload-status");
  const previewDialog = document.getElementById("preview-dialog");
  const previewTitle = document.getElementById("preview-title");
  const previewBody = document.getElementById("preview-body");

  let busy = false;
  let source = null;
  let pendingFiles = [];
  let lastFailedJobId = null;

  const ASPECT_PRESETS = [
    { id: "9:16", w: 1080, h: 1920, label: "9:16 · Vertical" },
    { id: "16:9", w: 1920, h: 1080, label: "16:9 · Landscape" },
    { id: "1:1", w: 1080, h: 1080, label: "1:1 · Square" },
    { id: "4:5", w: 1080, h: 1350, label: "4:5 · Portrait" },
  ];

  function setBusy(next) {
    busy = next;
    createForm.querySelector("button[type=submit]").disabled = next;
    const reviseBtn = reviseForm.querySelector("button[type=submit]");
    if (edlSelect.options.length && edlSelect.value) {
      reviseBtn.disabled = next;
    }
    if (clearOutputsBtn) {
      const hasEdits = editList && !editList.querySelector(".empty");
      clearOutputsBtn.disabled = next || !hasEdits;
    }
    if (retryBtn) {
      retryBtn.disabled = next;
      if (next) retryBtn.classList.add("hidden");
    }
  }

  function setStatus(text, cls) {
    statusEl.textContent = text;
    statusEl.className = `status ${cls || text}`;
  }

  function appendLog(line) {
    logEl.textContent += (logEl.textContent ? "\n" : "") + line;
    logEl.scrollTop = logEl.scrollHeight;
  }

  function clearJobUi() {
    logEl.textContent = "";
    resultEl.classList.add("hidden");
    resultEl.innerHTML = "";
    lastFailedJobId = null;
    if (retryBtn) retryBtn.classList.add("hidden");
  }

  function showRetry(jobId) {
    lastFailedJobId = jobId || null;
    if (!retryBtn) return;
    if (lastFailedJobId) {
      retryBtn.classList.remove("hidden");
      retryBtn.disabled = false;
    } else {
      retryBtn.classList.add("hidden");
    }
  }

  function showResult(result) {
    if (!result) return;
    resultEl.classList.remove("hidden");
    const parts = [];
    if (result.edl) {
      const name = result.edl.split("/").pop();
      parts.push(`<a href="/api/download/edl/${encodeURIComponent(name)}">Download EDL</a>`);
    }
    if (result.fcpxml) {
      const name = result.fcpxml.split("/").pop();
      parts.push(`<a href="/api/download/fcpxml/${encodeURIComponent(name)}">Download FCPXML</a>`);
    }
    resultEl.innerHTML = parts.join("");
  }

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function escapeAttr(value) {
    return escapeHtml(value).replaceAll("'", "&#39;");
  }

  function formatDuration(value) {
    if (value === null || value === undefined || value === "") return "";
    const num = Number(value);
    if (!Number.isFinite(num)) return "";
    return ` · ${num.toFixed(1)}s`;
  }

  function updatePendingFiles() {
    const fromFolder = folderInput.files ? [...folderInput.files] : [];
    const fromFiles = filesInput.files ? [...filesInput.files] : [];
    pendingFiles = fromFolder.length ? fromFolder : fromFiles;
    uploadBtn.disabled = pendingFiles.length === 0 || busy;
    if (!pendingFiles.length) {
      uploadStatus.textContent = "";
      return;
    }
    uploadStatus.textContent = `${pendingFiles.length} file${pendingFiles.length === 1 ? "" : "s"} selected`;
  }

  function renderMedia(payload) {
    const media = payload.media || [];
    mediaMeta.textContent = `${payload.videos_count || 0} videos · ${payload.photos_count || 0} photos · ${payload.originals_count || 0} total`;
    if (createMeta) {
      const analyzed = createMeta.textContent.split("·")[1] || "";
      createMeta.textContent = `${payload.originals_count || 0} originals ·${analyzed}`;
    }

    if (!media.length) {
      mediaGrid.innerHTML = `<p class="empty-media">No media yet — upload a folder to get started.</p>`;
      return;
    }

    mediaGrid.innerHTML = media
      .map((item) => {
        const thumb = item.thumb
          ? `<img src="${escapeAttr(item.thumb)}" alt="" loading="lazy" />`
          : `<span class="media-placeholder">${escapeHtml(String(item.ext || "").toUpperCase())}</span>`;
        return `<article
          class="media-card"
          data-name="${escapeAttr(item.name)}"
          data-rel="${escapeAttr(item.rel)}"
          data-kind="${escapeAttr(item.kind)}"
          data-preview="${escapeAttr(item.preview)}"
          data-thumb="${escapeAttr(item.thumb || "")}"
          data-size="${escapeAttr(item.size_label)}"
          data-duration="${escapeAttr(item.duration ?? "")}"
        >
          <button type="button" class="media-open" aria-label="Preview ${escapeAttr(item.name)}">
            <span class="media-thumb">
              ${thumb}
              <span class="media-badge">${escapeHtml(item.kind)}</span>
            </span>
            <span class="media-name">${escapeHtml(item.name)}</span>
            <span class="media-sub">${escapeHtml(item.size_label)}${escapeHtml(formatDuration(item.duration))}</span>
          </button>
          <button type="button" class="media-delete" data-rel="${escapeAttr(item.rel)}" data-name="${escapeAttr(item.name)}" aria-label="Delete ${escapeAttr(item.name)}">Delete</button>
        </article>`;
      })
      .join("");
  }

  async function refreshMedia() {
    const res = await fetch("/api/media");
    const data = await res.json();
    renderMedia(data);
  }

  function setLibraryStatus(text) {
    if (libraryStatus) libraryStatus.textContent = text || "";
  }

  function updateClearOutputsState(edits) {
    if (!clearOutputsBtn) return;
    clearOutputsBtn.disabled = !edits.length || busy;
  }

  async function refreshEdits() {
    const res = await fetch("/api/edits");
    const data = await res.json();
    const edits = data.edits || [];

    if (!edits.length) {
      editList.innerHTML = `<li class="empty">No edits yet — create one.</li>`;
      edlSelect.innerHTML = `<option value="" disabled selected>No edits yet</option>`;
      reviseForm.querySelector("button[type=submit]").disabled = true;
      updateClearOutputsState(edits);
      return;
    }

    editList.innerHTML = edits
      .map((edit) => {
        const note = edit.notes ? `<span class="note">${escapeHtml(edit.notes)}</span>` : "";
        const fcpxml = edit.fcpxml
          ? `<a href="/api/download/fcpxml/${encodeURIComponent(edit.stem)}.fcpxml">FCPXML</a>`
          : "";
        return `<li data-id="${escapeAttr(edit.id)}">
          <div>
            <strong>${escapeHtml(edit.title)}</strong>
            <span class="muted">${edit.cuts} cuts · ${edit.duration}s</span>
            ${note}
          </div>
          <div class="actions">
            <a href="/api/download/edl/${encodeURIComponent(edit.id)}">EDL</a>
            ${fcpxml}
            <button type="button" class="linkish danger edit-delete" data-id="${escapeAttr(edit.id)}" data-title="${escapeAttr(edit.title)}">Delete</button>
          </div>
        </li>`;
      })
      .join("");

    const previous = edlSelect.value;
    edlSelect.innerHTML = edits
      .map(
        (edit) =>
          `<option value="${escapeAttr(edit.id)}">${escapeHtml(edit.title)} (${escapeHtml(edit.stem)})</option>`
      )
      .join("");
    if ([...edlSelect.options].some((o) => o.value === previous)) {
      edlSelect.value = previous;
    }
    if (!busy) {
      reviseForm.querySelector("button[type=submit]").disabled = false;
    }
    updateClearOutputsState(edits);
  }

  function openPreview(card) {
    const name = card.dataset.name || "Media";
    const kind = card.dataset.kind || "";
    const preview = card.dataset.preview || "";
    const thumb = card.dataset.thumb || "";
    const size = card.dataset.size || "";
    const duration = formatDuration(card.dataset.duration).replace(/^ · /, "");

    previewTitle.textContent = name;
    previewBody.innerHTML = "";

    if (kind === "video") {
      const video = document.createElement("video");
      video.controls = true;
      video.src = preview;
      video.playsInline = true;
      previewBody.appendChild(video);
    } else if (thumb || preview) {
      const img = document.createElement("img");
      img.src = thumb || preview;
      img.alt = name;
      img.onerror = () => {
        previewBody.innerHTML = `<p class="preview-fallback">Preview unavailable for this still.<br>${escapeHtml(size)}${duration ? ` · ${escapeHtml(duration)}` : ""}</p>`;
      };
      previewBody.appendChild(img);
    } else {
      previewBody.innerHTML = `<p class="preview-fallback">No preview for ${escapeHtml(name)}.</p>`;
    }
    previewDialog.showModal();
  }

  function watchJob(jobId) {
    if (source) {
      source.close();
    }
    setBusy(true);
    setStatus("running", "running");
    source = new EventSource(`/api/jobs/${jobId}/events`);

    source.onmessage = (event) => {
      let payload;
      try {
        payload = JSON.parse(event.data);
      } catch {
        return;
      }
      if (payload.type === "log" && payload.line) {
        appendLog(payload.line);
      }
      if (payload.type === "done") {
        source.close();
        source = null;
        setBusy(false);
        setStatus(payload.status, payload.status);
        if (payload.error) {
          appendLog(`failed: ${payload.error}`);
        }
        if (payload.status === "succeeded") {
          showResult(payload.result);
          refreshEdits().catch(console.error);
        } else if (payload.can_retry) {
          showRetry(payload.job_id || jobId);
        }
      }
    };

    source.onerror = () => {
      if (!busy) return;
      appendLog("Connection lost — check job status.");
      source.close();
      source = null;
      setBusy(false);
      setStatus("failed", "failed");
      showRetry(jobId);
    };
  }

  async function startJob(url, body) {
    clearJobUi();
    setStatus("queued", "running");
    appendLog("Starting…");
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      setBusy(false);
      setStatus("failed", "failed");
      appendLog(data.detail || `Request failed (${res.status})`);
      return;
    }
    if (data.resume_from) {
      appendLog(`Will resume from: ${data.resume_from}`);
    }
    watchJob(data.job_id);
  }

  if (retryBtn) {
    retryBtn.addEventListener("click", () => {
      if (busy || !lastFailedJobId) return;
      const failedId = lastFailedJobId;
      clearJobUi();
      setStatus("queued", "running");
      appendLog(`Retrying from last completed step…`);
      fetch(`/api/jobs/${encodeURIComponent(failedId)}/retry`, { method: "POST" })
        .then(async (res) => {
          const data = await res.json().catch(() => ({}));
          if (!res.ok) {
            setBusy(false);
            setStatus("failed", "failed");
            appendLog(data.detail || `Retry failed (${res.status})`);
            showRetry(failedId);
            return;
          }
          if (data.resume_from) {
            appendLog(`Resuming from: ${data.resume_from}`);
          }
          watchJob(data.job_id);
        })
        .catch((err) => {
          setBusy(false);
          setStatus("failed", "failed");
          appendLog(String(err));
          showRetry(failedId);
        });
    });
  }

  createForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (busy) return;
    const fd = new FormData(createForm);
    startJob("/api/create", {
      name: String(fd.get("name") || "").trim() || "Lagos",
      duration: Number(fd.get("duration") || 90),
      style: String(fd.get("style") || "cinematic"),
      model: String(fd.get("model") || "").trim() || undefined,
      skip_analyze: fd.get("skip_analyze") === "on",
      force: fd.get("force") === "on",
    }).catch((err) => {
      setBusy(false);
      setStatus("failed", "failed");
      appendLog(String(err));
    });
  });

  reviseForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (busy) return;
    const fd = new FormData(reviseForm);
    startJob("/api/revise", {
      edl: String(fd.get("edl") || ""),
      notes: String(fd.get("notes") || "").trim(),
      model: String(fd.get("model") || "").trim() || undefined,
    }).catch((err) => {
      setBusy(false);
      setStatus("failed", "failed");
      appendLog(String(err));
    });
  });

  folderInput.addEventListener("change", () => {
    if (folderInput.files && folderInput.files.length) {
      filesInput.value = "";
    }
    updatePendingFiles();
  });
  filesInput.addEventListener("change", () => {
    if (filesInput.files && filesInput.files.length) {
      folderInput.value = "";
    }
    updatePendingFiles();
  });

  uploadForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!pendingFiles.length || busy) return;

    const fd = new FormData();
    for (const file of pendingFiles) {
      fd.append("files", file, file.name);
    }
    const replace = uploadForm.querySelector('input[name="replace"]').checked;
    uploadBtn.disabled = true;
    uploadStatus.textContent = `Uploading ${pendingFiles.length} file${pendingFiles.length === 1 ? "" : "s"}…`;

    try {
      const res = await fetch(`/api/media/upload?replace=${replace ? "true" : "false"}`, {
        method: "POST",
        body: fd,
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        uploadStatus.textContent = data.detail || `Upload failed (${res.status})`;
        uploadBtn.disabled = false;
        return;
      }
      renderMedia(data);
      folderInput.value = "";
      filesInput.value = "";
      pendingFiles = [];
      uploadBtn.disabled = true;
      uploadStatus.textContent = `Saved ${data.saved}, skipped ${data.skipped}, rejected ${data.rejected}`;
    } catch (err) {
      uploadStatus.textContent = String(err);
      uploadBtn.disabled = false;
    }
  });

  mediaGrid.addEventListener("click", (event) => {
    const deleteBtn = event.target.closest(".media-delete");
    if (deleteBtn) {
      event.preventDefault();
      const rel = deleteBtn.dataset.rel || "";
      const name = deleteBtn.dataset.name || rel;
      if (!rel) return;
      if (!window.confirm(`Delete ${name} from the library?\n\nThis removes the original and related frames/analysis.`)) {
        return;
      }
      deleteBtn.disabled = true;
      fetch(`/api/media/${rel.split("/").map(encodeURIComponent).join("/")}`, { method: "DELETE" })
        .then(async (res) => {
          const data = await res.json().catch(() => ({}));
          if (!res.ok) {
            throw new Error(data.detail || `Delete failed (${res.status})`);
          }
          renderMedia(data);
          if (uploadStatus) {
            uploadStatus.textContent = `Deleted ${data.deleted}`;
          }
        })
        .catch((err) => {
          deleteBtn.disabled = false;
          if (uploadStatus) uploadStatus.textContent = String(err);
        });
      return;
    }

    const openBtn = event.target.closest(".media-open");
    if (!openBtn) return;
    const card = openBtn.closest(".media-card");
    if (!card) return;
    openPreview(card);
  });

  refreshBtn.addEventListener("click", () => {
    refreshEdits().catch(console.error);
  });

  if (clearOutputsBtn) {
    clearOutputsBtn.addEventListener("click", async () => {
      if (busy) return;
      if (!window.confirm("Delete all EDL and FCPXML output files?")) return;
      clearOutputsBtn.disabled = true;
      setLibraryStatus("Clearing outputs…");
      try {
        const res = await fetch("/api/outputs", { method: "DELETE" });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || `Clear failed (${res.status})`);
        await refreshEdits();
        setLibraryStatus(`Cleared ${data.count || 0} file${data.count === 1 ? "" : "s"}`);
      } catch (err) {
        setLibraryStatus(String(err));
        clearOutputsBtn.disabled = false;
      }
    });
  }

  editList.addEventListener("click", async (event) => {
    const btn = event.target.closest(".edit-delete");
    if (!btn || busy) return;
    const id = btn.dataset.id || "";
    const title = btn.dataset.title || id;
    if (!id) return;
    if (!window.confirm(`Delete output “${title}”?`)) return;
    btn.disabled = true;
    setLibraryStatus(`Deleting ${id}…`);
    try {
      const res = await fetch(`/api/edits/${encodeURIComponent(id)}`, { method: "DELETE" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `Delete failed (${res.status})`);
      await refreshEdits();
      setLibraryStatus(`Deleted ${id}`);
    } catch (err) {
      btn.disabled = false;
      setLibraryStatus(String(err));
    }
  });

  refreshMediaBtn.addEventListener("click", () => {
    refreshMedia().catch(console.error);
  });

  const promptAreas = {
    editor: document.getElementById("prompt-editor-text"),
    vision: document.getElementById("prompt-vision-text"),
    revise: document.getElementById("prompt-revise-text"),
  };
  const settingsForm = document.getElementById("settings-form");
  const stylesForm = document.getElementById("styles-form");
  const settingsStatus = document.getElementById("settings-status");
  const stylesStatus = document.getElementById("styles-status");
  const openaiKeyInput = document.getElementById("openai-api-key");
  const openaiKeyReveal = document.getElementById("openai-api-key-reveal");
  const openaiKeyStatus = document.getElementById("openai-api-key-status");
  let cachedSettings = null;
  let cachedOpenAiKey = "";

  function setOpenAiKeyStatus(text) {
    if (openaiKeyStatus) openaiKeyStatus.textContent = text || "";
  }

  function applyOpenAiKey(key) {
    cachedOpenAiKey = key || "";
    if (openaiKeyInput) openaiKeyInput.value = cachedOpenAiKey;
    if (cachedOpenAiKey) {
      setOpenAiKeyStatus("Key is saved on this Mac.");
    } else {
      setOpenAiKeyStatus("No key saved yet — paste one to enable Create and Revise.");
    }
  }

  if (openaiKeyReveal && openaiKeyInput) {
    openaiKeyReveal.addEventListener("change", () => {
      openaiKeyInput.type = openaiKeyReveal.checked ? "text" : "password";
    });
  }

  function setPromptStatus(id, text) {
    const el = document.querySelector(`[data-status-for="${id}"]`);
    if (el) el.textContent = text;
  }

  async function loadPrompt(id) {
    const area = promptAreas[id];
    if (!area) return;
    const res = await fetch(`/api/prompts/${id}`);
    const data = await res.json();
    if (!res.ok) {
      setPromptStatus(id, data.detail || "Failed to load");
      return;
    }
    area.value = data.content || "";
    setPromptStatus(id, "");
  }

  async function loadAllPrompts() {
    await Promise.all(["editor", "vision", "revise"].map((id) => loadPrompt(id)));
  }

  async function loadSettingsCache() {
    const res = await fetch("/api/settings");
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Failed to load settings");
    cachedSettings = data.settings;
    applyOpenAiKey(data.openai_api_key || "");
    return cachedSettings;
  }

  document.querySelectorAll(".config-tab:not(.nested)").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".config-tab:not(.nested)").forEach((el) => el.classList.remove("active"));
      document.querySelectorAll(".config-pane").forEach((el) => el.classList.remove("active"));
      tab.classList.add("active");
      const pane = document.getElementById(tab.dataset.pane);
      if (pane) {
        pane.classList.remove("hidden");
        pane.hidden = false;
        pane.classList.add("active");
      }
      const saveBar = document.getElementById("settings-save-bar");
      if (saveBar) {
        const inForm = ["pane-frame", "pane-project", "pane-api", "pane-model", "pane-pacing", "pane-advanced"].includes(tab.dataset.pane);
        saveBar.classList.toggle("hidden", !inForm);
      }
    });
  });

  document.querySelectorAll(".config-tab.nested").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".config-tab.nested").forEach((el) => el.classList.remove("active"));
      document.querySelectorAll(".voice-pane").forEach((el) => {
        el.classList.remove("active");
        el.hidden = true;
      });
      tab.classList.add("active");
      const pane = document.getElementById(`prompt-${tab.dataset.voice}`);
      if (pane) {
        pane.hidden = false;
        pane.classList.add("active");
      }
    });
  });

  function matchAspectId(width, height) {
    const w = Number(width);
    const h = Number(height);
    const hit = ASPECT_PRESETS.find((p) => p.w === w && p.h === h);
    return hit ? hit.id : "custom";
  }

  function updateFrameChip(width, height) {
    const label = document.getElementById("frame-chip-label");
    const detail = document.getElementById("frame-chip-detail");
    const preview = document.getElementById("frame-preview");
    const id = matchAspectId(width, height);
    const preset = ASPECT_PRESETS.find((p) => p.id === id);
    if (label) label.textContent = preset ? preset.label : "Custom frame";
    if (detail) detail.textContent = `${width}×${height}`;
    if (preview) {
      preview.style.setProperty("--preview-ar", `${Number(width)} / ${Number(height)}`);
      preview.dataset.ratio = id;
    }
  }

  function selectAspectCard(aspectId) {
    document.querySelectorAll(".aspect-card").forEach((card) => {
      card.setAttribute("aria-pressed", card.dataset.aspect === aspectId ? "true" : "false");
    });
    const customRow = document.getElementById("custom-size-row");
    if (customRow) customRow.classList.toggle("is-hidden", aspectId !== "custom");
  }

  function syncAspectFromInputs() {
    const widthEl = document.getElementById("sequence-width");
    const heightEl = document.getElementById("sequence-height");
    if (!widthEl || !heightEl) return;
    const id = matchAspectId(widthEl.value, heightEl.value);
    selectAspectCard(id);
    updateFrameChip(widthEl.value, heightEl.value);
  }

  document.querySelectorAll(".aspect-card").forEach((card) => {
    card.addEventListener("click", () => {
      const aspect = card.dataset.aspect;
      const widthEl = document.getElementById("sequence-width");
      const heightEl = document.getElementById("sequence-height");
      if (aspect !== "custom" && widthEl && heightEl) {
        widthEl.value = card.dataset.w;
        heightEl.value = card.dataset.h;
      }
      selectAspectCard(aspect);
      if (widthEl && heightEl) updateFrameChip(widthEl.value, heightEl.value);
    });
  });

  ["sequence-width", "sequence-height"].forEach((id) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.addEventListener("input", syncAspectFromInputs);
  });

  function showView(viewId) {
    document.querySelectorAll(".app-nav .nav-btn").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.view === viewId);
    });
    document.querySelectorAll(".view").forEach((view) => {
      const on = view.id === `view-${viewId}`;
      view.classList.toggle("active", on);
      view.hidden = !on;
    });
  }

  document.querySelectorAll(".app-nav .nav-btn").forEach((btn) => {
    btn.addEventListener("click", () => showView(btn.dataset.view));
  });

  document.querySelectorAll("[data-goto]").forEach((btn) => {
    btn.addEventListener("click", () => {
      showView(btn.dataset.goto);
      if (btn.dataset.goto === "settings") {
        const frameTab = document.querySelector('.config-tab[data-pane="pane-frame"]');
        if (frameTab) frameTab.click();
      }
    });
  });

  document.querySelectorAll(".save-prompt").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.prompt;
      const area = promptAreas[id];
      if (!area) return;
      setPromptStatus(id, "Saving…");
      const res = await fetch(`/api/prompts/${id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: area.value }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setPromptStatus(id, data.detail || `Save failed (${res.status})`);
        return;
      }
      setPromptStatus(id, "Saved");
    });
  });

  function collectSettingsFromForms() {
    if (!cachedSettings) throw new Error("Settings not loaded yet");
    const fd = new FormData(settingsForm);
    const next = {
      ...cachedSettings,
      model: String(fd.get("model") || "").trim(),
      default_name: String(fd.get("default_name") || "").trim(),
      default_duration: Number(fd.get("default_duration")),
      default_style: String(fd.get("default_style") || ""),
      strong_score: Number(fd.get("strong_score")),
      max_video_source_duration: Number(fd.get("max_video_source_duration")),
      default_still_duration: Number(fd.get("default_still_duration")),
      vision_batch_size: Number(fd.get("vision_batch_size")),
      request_pause_sec: Number(fd.get("request_pause_sec")),
      max_retries: Number(fd.get("max_retries")),
      spatial_conform: String(fd.get("spatial_conform") || "fill_vertical_fit_wide"),
      sequence_width: Number(fd.get("sequence_width")),
      sequence_height: Number(fd.get("sequence_height")),
      score_weights: { ...cachedSettings.score_weights },
      style_briefs: { ...cachedSettings.style_briefs },
    };
    for (const key of Object.keys(next.score_weights)) {
      next.score_weights[key] = Number(fd.get(`weight_${key}`));
    }
    if (stylesForm) {
      const sfd = new FormData(stylesForm);
      for (const key of Object.keys(next.style_briefs)) {
        next.style_briefs[key] = String(sfd.get(`style_${key}`) || "");
      }
    }
    return next;
  }

  function applySettingsToCreateForm(settings) {
    const nameInput = createForm.querySelector('input[name="name"]');
    const durationInput = createForm.querySelector('input[name="duration"]');
    const modelSelects = [
      document.getElementById("create-model-select"),
      document.getElementById("settings-model-select"),
      document.getElementById("revise-model-select"),
    ];
    const styleSelect = document.getElementById("create-style-select");
    const defaultStyle = document.getElementById("default-style-select");
    const widthEl = document.getElementById("sequence-width");
    const heightEl = document.getElementById("sequence-height");
    if (nameInput) nameInput.value = settings.default_name;
    if (durationInput) durationInput.value = settings.default_duration;
    for (const sel of modelSelects) {
      if (!sel || !settings.model) continue;
      if ([...sel.options].some((o) => o.value === settings.model)) {
        sel.value = settings.model;
        updateModelMeta(sel);
      }
    }
    if (styleSelect && settings.default_style) {
      styleSelect.value = settings.default_style;
    }
    if (defaultStyle && settings.default_style) {
      defaultStyle.value = settings.default_style;
    }
    if (widthEl && settings.sequence_width) widthEl.value = settings.sequence_width;
    if (heightEl && settings.sequence_height) heightEl.value = settings.sequence_height;
    if (settings.sequence_width && settings.sequence_height) {
      updateFrameChip(settings.sequence_width, settings.sequence_height);
      selectAspectCard(matchAspectId(settings.sequence_width, settings.sequence_height));
    }
  }

  function formatMoney(value) {
    if (value === "" || value === null || value === undefined) return "—";
    const num = Number(value);
    if (!Number.isFinite(num)) return "—";
    if (num < 0.01) return `~$${num.toFixed(3)}`;
    return `~$${num.toFixed(2)}`;
  }

  function updateModelMeta(selectEl) {
    if (!selectEl) return;
    const metaId =
      selectEl.id === "settings-model-select"
        ? "settings-model-meta"
        : selectEl.id === "revise-model-select"
          ? "revise-model-meta"
          : "create-model-meta";
    const meta = document.getElementById(metaId);
    if (!meta) return;
    const opt = selectEl.selectedOptions[0];
    if (!opt) {
      meta.innerHTML = "";
      return;
    }
    const task = opt.dataset.task || "?";
    const vision = opt.dataset.vision || "?";
    const edit = opt.dataset.edit || "?";
    const tier = opt.dataset.tier || "?";
    const create = formatMoney(opt.dataset.create);
    const revise = formatMoney(opt.dataset.revise);
    const input = opt.dataset.in ? `$${Number(opt.dataset.in).toFixed(2)}/M in` : "—";
    const output = opt.dataset.out ? `$${Number(opt.dataset.out).toFixed(2)}/M out` : "—";
    const blurb = opt.dataset.blurb || "";
    meta.innerHTML = `
      <strong>${escapeHtml(opt.value)}</strong>
      <div class="meta-row">
        <span>Task ${escapeHtml(task)}/10</span>
        <span>Vision ${escapeHtml(vision)}/10</span>
        <span>Edit ${escapeHtml(edit)}/10</span>
        <span>Cost ${escapeHtml(tier)}</span>
      </div>
      <div class="meta-row">
        <span>Est. create ${escapeHtml(create)}</span>
        <span>Est. revise ${escapeHtml(revise)}</span>
        <span>${escapeHtml(input)}</span>
        <span>${escapeHtml(output)}</span>
      </div>
      <div class="blurb">${escapeHtml(blurb)}</div>
    `;
  }

  document.querySelectorAll(".model-select").forEach((sel) => {
    sel.addEventListener("change", () => {
      // Keep create / settings / revise selects aligned when one changes.
      document.querySelectorAll(".model-select").forEach((other) => {
        if (other !== sel && [...other.options].some((o) => o.value === sel.value)) {
          other.value = sel.value;
        }
        updateModelMeta(other);
      });
    });
    updateModelMeta(sel);
  });

  settingsForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    settingsStatus.textContent = "Saving…";
    try {
      const payload = collectSettingsFromForms();
      const keyValue = openaiKeyInput ? String(openaiKeyInput.value || "").trim() : cachedOpenAiKey;
      const res = await fetch("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          settings: payload,
          openai_api_key: keyValue,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        settingsStatus.textContent = data.detail || `Save failed (${res.status})`;
        return;
      }
      cachedSettings = data.settings;
      applyOpenAiKey(data.openai_api_key || keyValue);
      applySettingsToCreateForm(cachedSettings);
      settingsStatus.textContent = "Settings saved";
    } catch (err) {
      settingsStatus.textContent = String(err);
    }
  });

  stylesForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    stylesStatus.textContent = "Saving…";
    try {
      const payload = collectSettingsFromForms();
      const res = await fetch("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ settings: payload }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        stylesStatus.textContent = data.detail || `Save failed (${res.status})`;
        return;
      }
      cachedSettings = data.settings;
      stylesStatus.textContent = "Styles saved";
    } catch (err) {
      stylesStatus.textContent = String(err);
    }
  });

  loadAllPrompts().catch(console.error);
  loadSettingsCache()
    .then((settings) => {
      applySettingsToCreateForm(settings);
      syncAspectFromInputs();
    })
    .catch(console.error);
  syncAspectFromInputs();
})();
