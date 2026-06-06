// frontend/src/app.ts
var form = document.querySelector("#upload-form");
var fileInput = document.querySelector("#file-input");
var accessTokenInput = document.querySelector("#access-token-input");
var preprocessInput = document.querySelector("#preprocess-input");
var skipOcrInput = document.querySelector("#skip-ocr-input");
var mockLayoutInput = document.querySelector("#mock-layout-input");
var reasoningInput = document.querySelector("#reasoning-input");
var submitButton = document.querySelector("#submit-button");
var dropZone = document.querySelector("#drop-zone");
var fileTitle = document.querySelector("#file-title");
var fileMeta = document.querySelector("#file-meta");
var sourcePreview = document.querySelector("#source-preview");
var statusText = document.querySelector("#status-text");
var statusDot = document.querySelector("#status-dot");
var downloadLink = document.querySelector("#download-link");
var healthLink = document.querySelector("#health-link");
var eventLog = document.querySelector("#event-log");
var activeJobId = null;
var pollTimer = null;
var sourceObjectUrl = null;
var seenArtifacts = /* @__PURE__ */ new Set();
if (!form || !fileInput || !accessTokenInput || !preprocessInput || !skipOcrInput || !mockLayoutInput || !reasoningInput) {
  throw new Error("Frontend markup is incomplete.");
}
var savedAccessToken = localStorage.getItem("img2ppt_access_token");
if (savedAccessToken) {
  accessTokenInput.value = savedAccessToken;
}
updateHealthLink();
accessTokenInput.addEventListener("input", updateHealthLink);
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = fileInput.files?.[0];
  if (!file) {
    addLog("\u8BF7\u5148\u9009\u62E9\u4E00\u5F20 raw-image \u56FE\u7247\u3002");
    return;
  }
  const accessToken = accessTokenInput.value.trim();
  if (!accessToken) {
    addLog("\u8BF7\u5148\u8F93\u5165\u8BBF\u95EE\u4EE4\u724C\u3002");
    accessTokenInput.focus();
    return;
  }
  localStorage.setItem("img2ppt_access_token", accessToken);
  resetJobUi();
  renderSourcePreview(file);
  setBusy(true);
  setStatus("queued", "\u4E0A\u4F20\u4E2D\uFF0C\u51C6\u5907\u542F\u52A8\u4EFB\u52A1\u2026");
  try {
    const formData = new FormData();
    formData.append("file", file);
    formData.append("access_token", accessToken);
    formData.append("preprocess_raw_image", String(preprocessInput.checked));
    formData.append("skip_ocr", String(skipOcrInput.checked));
    formData.append("mock_layout", String(mockLayoutInput.checked));
    if (reasoningInput.value) {
      formData.append("use_reasoning", reasoningInput.value);
    }
    const response = await fetch("/api/jobs", {
      method: "POST",
      body: formData
    });
    if (!response.ok) {
      throw new Error(await response.text());
    }
    const job = await response.json();
    activeJobId = job.id;
    addLog(`\u4EFB\u52A1\u5DF2\u521B\u5EFA\uFF1A${job.id}`);
    renderJob(job);
    schedulePoll(800);
  } catch (error) {
    setBusy(false);
    setStatus("failed", `\u63D0\u4EA4\u5931\u8D25\uFF1A${toMessage(error)}`);
    addLog(`\u63D0\u4EA4\u5931\u8D25\uFF1A${toMessage(error)}`);
  }
});
fileInput.addEventListener("change", () => {
  const file = fileInput.files?.[0];
  if (!file) {
    return;
  }
  updateSelectedFile(file);
  renderSourcePreview(file);
});
for (const eventName of ["dragenter", "dragover"]) {
  dropZone?.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropZone.classList.add("is-dragging");
  });
}
for (const eventName of ["dragleave", "drop"]) {
  dropZone?.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropZone.classList.remove("is-dragging");
  });
}
dropZone?.addEventListener("drop", (event) => {
  const file = event.dataTransfer?.files?.[0];
  if (!file) {
    return;
  }
  const dataTransfer = new DataTransfer();
  dataTransfer.items.add(file);
  fileInput.files = dataTransfer.files;
  updateSelectedFile(file);
  renderSourcePreview(file);
});
function schedulePoll(delay) {
  if (pollTimer) {
    window.clearTimeout(pollTimer);
  }
  pollTimer = window.setTimeout(() => {
    void pollJob();
  }, delay);
}
async function pollJob() {
  if (!activeJobId) {
    return;
  }
  try {
    const response = await fetch(withAccessToken(`/api/jobs/${activeJobId}`));
    if (!response.ok) {
      throw new Error(await response.text());
    }
    const job = await response.json();
    renderJob(job);
    if (job.status === "queued" || job.status === "running") {
      schedulePoll(1800);
    } else {
      setBusy(false);
    }
  } catch (error) {
    addLog(`\u8F6E\u8BE2\u5931\u8D25\uFF1A${toMessage(error)}`);
    schedulePoll(3e3);
  }
}
function renderJob(job) {
  const statusMessage = statusLabel(job);
  setStatus(job.status, statusMessage);
  for (const artifact of job.artifacts) {
    renderArtifact(artifact);
  }
  if (job.download_url) {
    downloadLink?.classList.remove("is-disabled");
    downloadLink?.removeAttribute("aria-disabled");
    if (downloadLink) {
      downloadLink.href = withAccessToken(job.download_url);
    }
  }
  if (job.status === "failed") {
    addLog(`\u4EFB\u52A1\u5931\u8D25\uFF1A${job.error || "\u672A\u77E5\u9519\u8BEF"}`);
  }
}
function renderArtifact(artifact) {
  if (!artifact.ready || !artifact.url) {
    return;
  }
  const card = document.querySelector(`[data-stage="${artifact.key}"]`);
  const image = card?.querySelector("img");
  if (!card || !image) {
    return;
  }
  const cacheKey = `${artifact.key}:${artifact.updated_at || ""}`;
  if (!seenArtifacts.has(cacheKey)) {
    seenArtifacts.add(cacheKey);
    addLog(`${artifact.label} \u5DF2\u751F\u6210\u3002`);
  }
  image.src = withAccessToken(artifact.url, artifact.updated_at || Date.now());
  card.classList.add("is-ready");
}
function renderSourcePreview(file) {
  if (!sourcePreview) {
    return;
  }
  if (sourceObjectUrl) {
    URL.revokeObjectURL(sourceObjectUrl);
  }
  sourceObjectUrl = URL.createObjectURL(file);
  sourcePreview.src = sourceObjectUrl;
  sourcePreview.closest(".stage-card")?.classList.add("is-ready");
}
function resetJobUi() {
  seenArtifacts.clear();
  if (pollTimer) {
    window.clearTimeout(pollTimer);
    pollTimer = null;
  }
  activeJobId = null;
  for (const key of ["raw_image_preprocess", "layout_boxes", "ppt_preview"]) {
    const card = document.querySelector(`[data-stage="${key}"]`);
    const image = card?.querySelector("img");
    card?.classList.remove("is-ready");
    if (image) {
      image.removeAttribute("src");
    }
  }
  downloadLink?.classList.add("is-disabled");
  downloadLink?.setAttribute("aria-disabled", "true");
  downloadLink?.removeAttribute("href");
  if (eventLog) {
    eventLog.innerHTML = "";
  }
}
function updateSelectedFile(file) {
  if (fileTitle) {
    fileTitle.textContent = file.name;
  }
  if (fileMeta) {
    fileMeta.textContent = `${formatBytes(file.size)} \xB7 ${file.type || "image"}`;
  }
}
function setBusy(isBusy) {
  if (submitButton) {
    submitButton.disabled = isBusy;
    submitButton.textContent = isBusy ? "\u5904\u7406\u4E2D\u2026" : "\u5F00\u59CB\u5904\u7406";
  }
}
function setStatus(status, message) {
  if (statusText) {
    statusText.textContent = message;
  }
  statusDot?.classList.remove("is-running", "is-done", "is-failed");
  if (status === "queued" || status === "running") {
    statusDot?.classList.add("is-running");
  } else if (status === "done") {
    statusDot?.classList.add("is-done");
  } else if (status === "failed") {
    statusDot?.classList.add("is-failed");
  }
}
function statusLabel(job) {
  if (job.status === "queued") {
    return "\u4EFB\u52A1\u6392\u961F\u4E2D\u2026";
  }
  if (job.status === "running") {
    return `\u5904\u7406\u4E2D\uFF0C\u5DF2\u83B7\u5F97 ${job.ready_count}/3 \u4E2A\u5C55\u793A\u4EA7\u7269\u2026`;
  }
  if (job.status === "done") {
    return "\u5904\u7406\u5B8C\u6210\uFF0C\u53EF\u4EE5\u4E0B\u8F7D PPTX\u3002";
  }
  return `\u5904\u7406\u5931\u8D25\uFF1A${job.error || "\u672A\u77E5\u9519\u8BEF"}`;
}
function withAccessToken(path, cacheBust) {
  const url = new URL(path, window.location.origin);
  url.searchParams.set("access_token", accessTokenInput?.value.trim() || "");
  if (cacheBust) {
    url.searchParams.set("v", String(cacheBust));
  }
  return `${url.pathname}${url.search}`;
}
function updateHealthLink() {
  if (healthLink) {
    healthLink.href = withAccessToken("/health");
  }
}
function addLog(message) {
  if (!eventLog) {
    return;
  }
  const item = document.createElement("li");
  item.textContent = `${(/* @__PURE__ */ new Date()).toLocaleTimeString()} \xB7 ${message}`;
  eventLog.prepend(item);
}
function formatBytes(bytes) {
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KB`;
  }
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
function toMessage(error) {
  return error instanceof Error ? error.message : String(error);
}
//# sourceMappingURL=app.js.map
