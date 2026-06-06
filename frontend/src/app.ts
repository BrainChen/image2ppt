type JobStatus = "queued" | "running" | "done" | "failed";

interface JobArtifact {
  key: "raw_image_preprocess" | "layout_boxes" | "ppt_preview";
  label: string;
  ready: boolean;
  url: string | null;
  filename: string | null;
  updated_at: number | null;
}

interface JobResponse {
  id: string;
  status: JobStatus;
  error: string | null;
  artifacts: JobArtifact[];
  download_url: string | null;
  ready_count: number;
}

const form = document.querySelector<HTMLFormElement>("#upload-form");
const fileInput = document.querySelector<HTMLInputElement>("#file-input");
const accessTokenInput = document.querySelector<HTMLInputElement>("#access-token-input");
const preprocessInput = document.querySelector<HTMLInputElement>("#preprocess-input");
const skipOcrInput = document.querySelector<HTMLInputElement>("#skip-ocr-input");
const mockLayoutInput = document.querySelector<HTMLInputElement>("#mock-layout-input");
const reasoningInput = document.querySelector<HTMLSelectElement>("#reasoning-input");
const submitButton = document.querySelector<HTMLButtonElement>("#submit-button");
const dropZone = document.querySelector<HTMLElement>("#drop-zone");
const fileTitle = document.querySelector<HTMLElement>("#file-title");
const fileMeta = document.querySelector<HTMLElement>("#file-meta");
const sourcePreview = document.querySelector<HTMLImageElement>("#source-preview");
const statusText = document.querySelector<HTMLElement>("#status-text");
const statusDot = document.querySelector<HTMLElement>("#status-dot");
const downloadLink = document.querySelector<HTMLAnchorElement>("#download-link");
const healthLink = document.querySelector<HTMLAnchorElement>("#health-link");
const eventLog = document.querySelector<HTMLOListElement>("#event-log");

let activeJobId: string | null = null;
let pollTimer: number | null = null;
let sourceObjectUrl: string | null = null;
const seenArtifacts = new Set<string>();

if (!form || !fileInput || !accessTokenInput || !preprocessInput || !skipOcrInput || !mockLayoutInput || !reasoningInput) {
  throw new Error("Frontend markup is incomplete.");
}

const savedAccessToken = localStorage.getItem("img2ppt_access_token");
if (savedAccessToken) {
  accessTokenInput.value = savedAccessToken;
}
updateHealthLink();

accessTokenInput.addEventListener("input", updateHealthLink);

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = fileInput.files?.[0];
  if (!file) {
    addLog("请先选择一张 raw-image 图片。");
    return;
  }
  const accessToken = accessTokenInput.value.trim();
  if (!accessToken) {
    addLog("请先输入访问令牌。");
    accessTokenInput.focus();
    return;
  }
  localStorage.setItem("img2ppt_access_token", accessToken);

  resetJobUi();
  renderSourcePreview(file);
  setBusy(true);
  setStatus("queued", "上传中，准备启动任务…");

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
      body: formData,
    });
    if (!response.ok) {
      throw new Error(await response.text());
    }

    const job = (await response.json()) as JobResponse;
    activeJobId = job.id;
    addLog(`任务已创建：${job.id}`);
    renderJob(job);
    schedulePoll(800);
  } catch (error) {
    setBusy(false);
    setStatus("failed", `提交失败：${toMessage(error)}`);
    addLog(`提交失败：${toMessage(error)}`);
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

function schedulePoll(delay: number): void {
  if (pollTimer) {
    window.clearTimeout(pollTimer);
  }
  pollTimer = window.setTimeout(() => {
    void pollJob();
  }, delay);
}

async function pollJob(): Promise<void> {
  if (!activeJobId) {
    return;
  }
  try {
    const response = await fetch(withAccessToken(`/api/jobs/${activeJobId}`));
    if (!response.ok) {
      throw new Error(await response.text());
    }
    const job = (await response.json()) as JobResponse;
    renderJob(job);

    if (job.status === "queued" || job.status === "running") {
      schedulePoll(1800);
    } else {
      setBusy(false);
    }
  } catch (error) {
    addLog(`轮询失败：${toMessage(error)}`);
    schedulePoll(3000);
  }
}

function renderJob(job: JobResponse): void {
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
    addLog(`任务失败：${job.error || "未知错误"}`);
  }
}

function renderArtifact(artifact: JobArtifact): void {
  if (!artifact.ready || !artifact.url) {
    return;
  }

  const card = document.querySelector<HTMLElement>(`[data-stage="${artifact.key}"]`);
  const image = card?.querySelector<HTMLImageElement>("img");
  if (!card || !image) {
    return;
  }

  const cacheKey = `${artifact.key}:${artifact.updated_at || ""}`;
  if (!seenArtifacts.has(cacheKey)) {
    seenArtifacts.add(cacheKey);
    addLog(`${artifact.label} 已生成。`);
  }

  image.src = withAccessToken(artifact.url, artifact.updated_at || Date.now());
  card.classList.add("is-ready");
}

function renderSourcePreview(file: File): void {
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

function resetJobUi(): void {
  seenArtifacts.clear();
  if (pollTimer) {
    window.clearTimeout(pollTimer);
    pollTimer = null;
  }
  activeJobId = null;
  for (const key of ["raw_image_preprocess", "layout_boxes", "ppt_preview"]) {
    const card = document.querySelector<HTMLElement>(`[data-stage="${key}"]`);
    const image = card?.querySelector<HTMLImageElement>("img");
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

function updateSelectedFile(file: File): void {
  if (fileTitle) {
    fileTitle.textContent = file.name;
  }
  if (fileMeta) {
    fileMeta.textContent = `${formatBytes(file.size)} · ${file.type || "image"}`;
  }
}

function setBusy(isBusy: boolean): void {
  if (submitButton) {
    submitButton.disabled = isBusy;
    submitButton.textContent = isBusy ? "处理中…" : "开始处理";
  }
}

function setStatus(status: JobStatus, message: string): void {
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

function statusLabel(job: JobResponse): string {
  if (job.status === "queued") {
    return "任务排队中…";
  }
  if (job.status === "running") {
    return `处理中，已获得 ${job.ready_count}/3 个展示产物…`;
  }
  if (job.status === "done") {
    return "处理完成，可以下载 PPTX。";
  }
  return `处理失败：${job.error || "未知错误"}`;
}

function withAccessToken(path: string, cacheBust?: number): string {
  const url = new URL(path, window.location.origin);
  url.searchParams.set("access_token", accessTokenInput?.value.trim() || "");
  if (cacheBust) {
    url.searchParams.set("v", String(cacheBust));
  }
  return `${url.pathname}${url.search}`;
}

function updateHealthLink(): void {
  if (healthLink) {
    healthLink.href = withAccessToken("/health");
  }
}

function addLog(message: string): void {
  if (!eventLog) {
    return;
  }
  const item = document.createElement("li");
  item.textContent = `${new Date().toLocaleTimeString()} · ${message}`;
  eventLog.prepend(item);
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KB`;
  }
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function toMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
