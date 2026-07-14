const state = {
  reviewId: "",
  eventSource: null,
  reviews: [],
  categoryNames: {},
  systemMetrics: null,
  systemMetricsTimer: null,
};

const DEFAULT_UPLOAD_CHUNK_SIZE = 512 * 1024;
const UPLOAD_CHUNK_CONCURRENCY = 4;
const FILE_UPLOAD_CONCURRENCY = 2;
const $ = (id) => document.getElementById(id);

const severityLabels = {
  low: "低",
  medium: "中",
  high: "高",
  critical: "不予过审",
};

const decisionLabels = {
  pass: "通过",
  warn: "预警",
  reject: "不予过审",
  manual_review: "人工复核",
};

const statusLabels = {
  pending: "等待中",
  processing: "审核中",
  completed: "已完成",
  cancelled: "已取消",
  failed: "失败",
};

const phaseLabels = {
  pending: "等待调度",
  ingest: "准备视频",
  preprocess: "抽帧预处理",
  scan: "模型审核",
  judge: "生成裁决",
  done: "完成",
  cancelled: "已取消",
  error: "失败",
};

const eventLabels = {
  status: "状态",
  queued: "已入队",
  segment_start: "分段开始",
  finding: "命中风险",
  segment_complete: "分段完成",
  complete: "审核完成",
  error: "错误",
  stream: "事件流",
  created: "已创建",
};

function labelFromMap(map, value) {
  return map[value] || value || "-";
}

function formatBytes(value) {
  const size = Number(value || 0);
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  if (size < 1024 * 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`;
  return `${(size / 1024 / 1024 / 1024).toFixed(1)} GB`;
}

function formatDuration(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  const seconds = Math.max(0, Math.round(Number(value)));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = seconds % 60;
  if (hours) return `${hours}小时${minutes}分`;
  if (minutes) return `${minutes}分${rest}秒`;
  return `${rest}秒`;
}

function formatMinutes(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return `${Number(value).toFixed(1)} 分钟`;
}

function formatPercent(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return `${Number(value).toFixed(1)}%`;
}

function formatDateTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function datetimeLocalToIso(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toISOString();
}

function findingCategoryLabel(category) {
  return state.categoryNames[category] || category || "未知分类";
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function formatAdvice(value) {
  if (!value) return "";
  if (typeof value === "string") return value;
  if (typeof value === "object") {
    return Object.entries(value)
      .filter(([, item]) => item)
      .map(([key, item]) => `${key}: ${item}`)
      .join("；");
  }
  return String(value);
}

function logEvent(type, data) {
  const line = document.createElement("div");
  const text = typeof data === "string" ? data : JSON.stringify(data);
  line.textContent = `[${new Date().toLocaleTimeString()}] ${labelFromMap(eventLabels, type)}: ${text}`;
  $("eventLog").prepend(line);
}

async function request(path, options = {}) {
  const res = await fetch(path, options);
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} ${text}`);
  }
  return res.json();
}

function requestForm(path, form, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", path);
    xhr.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable && typeof onProgress === "function") {
        onProgress(event.loaded, event.total);
      }
    });
    xhr.addEventListener("load", () => {
      const text = xhr.responseText || "";
      if (xhr.status < 200 || xhr.status >= 300) {
        reject(new Error(`${xhr.status} ${text}`));
        return;
      }
      try {
        resolve(JSON.parse(text));
      } catch (err) {
        reject(new Error(`接口返回不是 JSON：${text || err.message}`));
      }
    });
    xhr.addEventListener("error", () => reject(new Error("上传失败，请检查网络或服务状态")));
    xhr.addEventListener("abort", () => reject(new Error("上传已取消")));
    xhr.send(form);
  });
}

function setReviewId(reviewId) {
  state.reviewId = reviewId;
  $("reviewIdInput").value = reviewId;
}

function setFormMessage(message, type = "info") {
  const node = $("formMessage");
  node.textContent = message || "";
  node.className = `form-message ${type}`;
}

function setSubmitBusy(isBusy, label = "创建并开始审核") {
  const button = $("submitBtn");
  button.disabled = isBusy;
  button.textContent = label;
}

function compactErrorMessage(error) {
  const raw = error?.message || String(error || "未知错误");
  try {
    const match = raw.match(/\{.*\}$/s);
    if (match) {
      const parsed = JSON.parse(match[0]);
      if (parsed.detail) {
        return Array.isArray(parsed.detail) ? parsed.detail.map((item) => item.msg || item).join("；") : String(parsed.detail);
      }
      if (parsed.message) return String(parsed.message);
    }
  } catch (_) {
    // Fall through to the raw message.
  }
  return raw;
}

function renderJob(job) {
  $("jobStatus").textContent = labelFromMap(statusLabels, job.status);
  $("jobPhase").textContent = labelFromMap(phaseLabels, job.phase);
  const message = job.error ? `${job.message || "审核失败"}：${job.error}` : (job.message || "");
  $("jobMessage").textContent = message;
  const percentage = job.progress?.percentage ?? (job.status === "completed" ? 100 : 0);
  $("progressBar").style.width = `${Math.max(0, Math.min(100, percentage))}%`;
}

function severityClass(value) {
  return `severity ${value || "low"}`;
}

function renderBatchList(reviews) {
  state.reviews = reviews || [];
  const list = $("batchList");
  list.innerHTML = "";
  if (!state.reviews.length) return;
  state.reviews.forEach((review, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `batch-item${review.review_id === state.reviewId ? " active" : ""}`;
    const title = review.display_name || `任务 ${index + 1}`;
    button.innerHTML = `<span>${escapeHtml(title)}</span><strong>${escapeHtml(review.review_id)}</strong>`;
    button.addEventListener("click", async () => {
      setReviewId(review.review_id);
      renderBatchList(state.reviews);
      await loadJob().catch((err) => alert(err.message));
      await loadReport().catch(() => {});
    });
    list.appendChild(button);
  });
}

function renderReport(report) {
  $("decisionText").textContent = labelFromMap(decisionLabels, report.decision);
  $("riskScore").textContent = report.risk_score ?? "-";
  $("findingCount").textContent = report.findings?.length ?? 0;
  renderNarrative(report);
  const list = $("findings");
  list.innerHTML = "";
  const segments = report.segments || [];
  if (!segments.length && (!report.findings || report.findings.length === 0)) {
    list.innerHTML = '<div class="empty">没有发现明确风险</div>';
    return;
  }
  if (segments.length) {
    segments.forEach((segment) => {
      const group = document.createElement("div");
      group.className = "segment-group";
      const findings = segment.findings || [];
      const segmentLabel = segment.segment_index === 0 ? "字幕通道" : `第 ${segment.segment_index} 段`;
      group.innerHTML = `
        <div class="segment-head">
          <div>
            <strong>${segmentLabel}</strong>
            <span>${segment.start_time} - ${segment.end_time}</span>
          </div>
          <span class="badge">风险分 ${segment.risk_score ?? 0}</span>
        </div>
        ${segment.summary ? `<p class="segment-summary">${segment.summary}</p>` : ""}
      `;
      if (!findings.length) {
        const empty = document.createElement("div");
        empty.className = "empty compact";
        empty.textContent = "该分段未发现明确风险";
        group.appendChild(empty);
      }
      findings.forEach((finding) => group.appendChild(renderFindingCard(finding, segment)));
      list.appendChild(group);
    });
    return;
  }
  report.findings.forEach((finding) => {
    list.appendChild(renderFindingCard(finding));
  });
}

function renderNarrative(report) {
  const node = $("narrative");
  const hasNarrative = report.main_plot || report.plot_structure?.length || report.value_correction_advice;
  if (!hasNarrative) {
    node.innerHTML = "";
    return;
  }
  const context = report.story_context || {};
  const genres = Array.isArray(context.genres) ? context.genres.join("、") : (context.genres || "");
  const signals = Array.isArray(context.signals) ? context.signals.join("；") : (context.signals || "");
  const advice = report.value_correction_advice || {};
  const phases = report.plot_structure || [];
  node.innerHTML = `
    <section class="narrative-section">
      <h3>整片主线剧情</h3>
      <p>${escapeHtml(report.main_plot || "暂无主线剧情摘要")}</p>
      <div class="context-line">
        <span>背景：${escapeHtml(context.background || "未知")}</span>
        <span>题材：${escapeHtml(genres || "未识别")}</span>
        ${signals ? `<span>依据：${escapeHtml(signals)}</span>` : ""}
      </div>
    </section>
    <section class="narrative-section">
      <h3>开头-中间-结尾价值观判断</h3>
      <div class="phase-grid">
        ${phases.map((phase) => `
          <div class="phase-card">
            <strong>${escapeHtml(phase.phase_name || phase.phase || "-")}</strong>
            <small>${escapeHtml(phase.time_range || "")}</small>
            <p><b>剧情：</b>${escapeHtml(phase.plot_summary || "-")}</p>
            <p><b>判断：</b>${escapeHtml(phase.value_judgement || "-")}</p>
            ${phase.risk_points?.length ? `<p><b>风险点：</b>${escapeHtml(phase.risk_points.join("；"))}</p>` : ""}
            <p><b>回正：</b>${escapeHtml(phase.correction_advice || "-")}</p>
          </div>
        `).join("")}
      </div>
    </section>
    <section class="narrative-section">
      <h3>回正建议</h3>
      <div class="advice-grid">
        <p><strong>开头：</strong>${escapeHtml(advice.opening || "-")}</p>
        <p><strong>主线：</strong>${escapeHtml(advice.main || "-")}</p>
        <p><strong>结尾：</strong>${escapeHtml(advice.ending || "-")}</p>
        <p><strong>整体：</strong>${escapeHtml(advice.overall || "-")}</p>
      </div>
    </section>
  `;
}

function renderFindingCard(finding, segment = null) {
    const node = document.createElement("div");
    node.className = "finding";
    node.innerHTML = `
      <div class="finding-head">
        <strong>${escapeHtml(finding.sub_category || findingCategoryLabel(finding.category))}</strong>
        <span class="${severityClass(finding.severity)}">${labelFromMap(severityLabels, finding.severity)}</span>
      </div>
      <p><strong>大类：</strong>${escapeHtml(findingCategoryLabel(finding.category))}${finding.risk_level ? ` / ${escapeHtml(finding.risk_level)}` : ""}${finding.rule_tag ? ` / ${escapeHtml(finding.rule_tag)}` : ""}</p>
      ${segment ? `<p><strong>所属分段：</strong>${segment.segment_index === 0 ? "字幕通道" : `第 ${segment.segment_index} 段`}（${segment.start_time} - ${segment.end_time}）</p>` : ""}
      <p><strong>时间：</strong>${finding.start_time} - ${finding.end_time}</p>
      ${finding.original_text ? `<p><strong>命中文字：</strong>${escapeHtml(finding.original_text)}</p>` : ""}
      <p><strong>证据：</strong>${escapeHtml(finding.evidence || "-")}</p>
      <p><strong>原因：</strong>${escapeHtml(finding.reason || "-")}</p>
      ${finding.context_note ? `<p><strong>语境：</strong>${escapeHtml(finding.context_note)}</p>` : ""}
      ${finding.plot_impact ? `<p><strong>主线影响：</strong>${escapeHtml(finding.plot_impact)}</p>` : ""}
      <p><strong>建议：</strong>${escapeHtml(finding.suggested_action || "-")}</p>
      ${formatAdvice(finding.value_correction_advice) ? `<p><strong>回正：</strong>${escapeHtml(formatAdvice(finding.value_correction_advice))}</p>` : ""}
    `;
    return node;
}

async function loadHealth() {
  try {
    await request("/health");
    $("healthDot").className = "dot ok";
    $("healthText").textContent = "服务正常";
  } catch (err) {
    $("healthDot").className = "dot err";
    $("healthText").textContent = "服务异常";
  }
}

async function loadPolicy() {
  const policy = await request("/video/reviews/policies/current");
  $("policyVersion").textContent = policy.version;
  const list = $("rulesList");
  list.innerHTML = "";
  policy.categories.forEach((rule) => {
    state.categoryNames[rule.id] = rule.name;
    const risks = rule.risk_levels
      ? Object.entries(rule.risk_levels).map(([key, value]) => `${key}: ${value}`).join("；")
      : "";
    const node = document.createElement("div");
    node.className = "rule-card";
    node.innerHTML = `
      <div class="rule-head">
        <strong>${rule.name}</strong>
        <span class="${severityClass(rule.severity)}">${labelFromMap(severityLabels, rule.severity)}</span>
      </div>
      <p>${rule.rule}</p>
      ${risks ? `<p><strong>风险等级：</strong>${risks}</p>` : ""}
      <p><strong>处理：</strong>${rule.default_action}</p>
      <p><strong>关键词：</strong>${rule.keywords.slice(0, 10).join("、")}</p>
    `;
    list.appendChild(node);
  });
}

async function loadJob() {
  const reviewId = $("reviewIdInput").value.trim();
  if (!reviewId) return;
  const job = await request(`/video/reviews/${reviewId}`);
  setReviewId(reviewId);
  renderJob(job);
}

async function loadReport() {
  const reviewId = $("reviewIdInput").value.trim();
  if (!reviewId) return;
  const report = await request(`/video/reviews/${reviewId}/report`);
  renderReport(report);
}

function connectStream() {
  const reviewId = $("reviewIdInput").value.trim();
  if (!reviewId) return;
  if (state.eventSource) state.eventSource.close();
  state.eventSource = new EventSource(`/video/reviews/${reviewId}/stream`);
  ["status", "queued", "segment_start", "finding", "segment_complete", "complete", "error"].forEach((type) => {
    state.eventSource.addEventListener(type, async (event) => {
      const data = JSON.parse(event.data);
      logEvent(type, data);
      if (type === "finding") {
        await loadJob().catch(() => {});
      }
      if (type === "complete") {
        state.eventSource.close();
        await loadJob().catch(() => {});
        await loadReport().catch(() => {});
      }
      if (type === "error") {
        state.eventSource.close();
        await loadJob().catch(() => {});
      }
    });
  });
  logEvent("stream", `connected ${reviewId}`);
}

function parseVideoUrls(value) {
  const seen = new Set();
  const urls = [];
  String(value || "")
    .split(/\n|,|\s+/)
    .map((item) => item.trim())
    .filter(Boolean)
    .forEach((url) => {
      if (!seen.has(url)) {
        seen.add(url);
        urls.push(url);
      }
    });
  return urls;
}

function renderSelectedFiles() {
  const files = Array.from($("localFile").files || []);
  const list = $("selectedFiles");
  list.innerHTML = "";
  if (!files.length) {
    list.classList.remove("visible");
    return;
  }
  list.classList.add("visible");
  const totalSize = files.reduce((sum, file) => sum + file.size, 0);
  const maxVisible = 8;
  list.innerHTML = `
    <div class="selected-files-head">
      <strong>已选择 ${files.length} 个视频</strong>
      <span>合计 ${formatBytes(totalSize)}</span>
    </div>
    <div class="selected-files-list">
      ${files.slice(0, maxVisible).map((file, index) => `
        <div class="selected-file">
          <span>${index + 1}. ${escapeHtml(file.name)}</span>
          <small>${formatBytes(file.size)}</small>
        </div>
      `).join("")}
      ${files.length > maxVisible ? `<div class="selected-file more">还有 ${files.length - maxVisible} 个文件未展开</div>` : ""}
    </div>
  `;
}

function decorateCreatedReviews(response, files, videoUrls) {
  const reviews = response.reviews || [response];
  return reviews.map((review, index) => ({
    ...review,
    display_name: files[index]?.name || videoUrls[index] || `任务 ${index + 1}`,
  }));
}

function makeUploadTracker(files) {
  const loadedByFile = Array.from({ length: files.length }, () => 0);
  const totalSize = files.reduce((sum, file) => sum + file.size, 0) || 1;
  return {
    update(fileIndex, loaded, message) {
      loadedByFile[fileIndex] = Math.max(loadedByFile[fileIndex] || 0, loaded);
      const totalLoaded = loadedByFile.reduce((sum, item) => sum + item, 0);
      const percent = Math.min(100, Math.round((totalLoaded / totalSize) * 100));
      const fileConcurrency = Math.min(FILE_UPLOAD_CONCURRENCY, files.length);
      setFormMessage(`${message}，视频并发 ${fileConcurrency}，总进度 ${percent}%`, "info");
    },
  };
}

function makeClientSessionId() {
  const random = window.crypto?.randomUUID ? window.crypto.randomUUID().replaceAll("-", "").slice(0, 16) : `${Date.now()}${Math.random()}`.replace(/\D/g, "").slice(0, 16);
  return `session_${random || Date.now()}`;
}

async function initChunkUpload(file) {
  return request("/video/uploads/init", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ filename: file.name, size: file.size }),
  });
}

async function initOssUpload(file) {
  return request("/video/oss/uploads/init", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ filename: file.name, size: file.size, content_type: file.type || "video/mp4" }),
  });
}

async function completeOssUpload(init, file, payload, etag) {
  return request("/video/oss/uploads/complete", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      ...payload,
      upload_id: init.upload_id,
      filename: file.name,
      size: file.size,
      etag: etag || null,
    }),
  });
}

function makeOssClient(init) {
  if (!window.OSS) {
    throw new Error("OSS 浏览器 SDK 未加载");
  }
  return new window.OSS({
    region: init.region,
    endpoint: init.endpoint,
    bucket: init.bucket,
    secure: true,
    authorizationV4: true,
    accessKeyId: init.credentials.access_key_id,
    accessKeySecret: init.credentials.access_key_secret,
    stsToken: init.credentials.security_token,
  });
}

function extractOssEtag(result) {
  const raw = result?.res?.headers?.etag || result?.res?.headers?.ETag || "";
  return String(raw || "").replaceAll('"', "");
}

async function uploadChunk(uploadId, file, chunkIndex, start, end, onProgress) {
  const form = new FormData();
  const chunk = file.slice(start, end);
  form.append("chunk_index", String(chunkIndex));
  form.append("chunk", chunk, `${file.name}.part${chunkIndex}`);
  return requestForm(`/video/uploads/${uploadId}/chunk`, form, onProgress);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function uploadChunkWithRetry(uploadId, file, chunkIndex, start, end, onProgress) {
  let attempt = 0;
  while (true) {
    try {
      return await uploadChunk(uploadId, file, chunkIndex, start, end, onProgress);
    } catch (err) {
      attempt += 1;
      if (attempt > 2) throw err;
      await sleep(500 * attempt);
    }
  }
}

async function completeChunkUpload(uploadId, file, payload) {
  return request(`/video/uploads/${uploadId}/complete`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      ...payload,
      filename: file.name,
      chunk_count: Math.max(1, Math.ceil(file.size / payload.chunk_size)),
    }),
  });
}

async function uploadFileInChunks(file, index, files, payload, tracker) {
  const init = await initChunkUpload(file);
  const chunkSize = Math.min(init.chunk_size || DEFAULT_UPLOAD_CHUNK_SIZE, DEFAULT_UPLOAD_CHUNK_SIZE);
  const chunkCount = Math.max(1, Math.ceil(file.size / chunkSize));
  const fileLabel = files.length > 1 ? `第 ${index + 1}/${files.length} 个：${file.name}` : file.name;
  const chunkLoaded = Array.from({ length: chunkCount }, () => 0);
  let nextChunkIndex = 0;
  let completedChunks = 0;

  function updateProgress(chunkIndex, loaded) {
    const start = chunkIndex * chunkSize;
    const end = Math.min(file.size, start + chunkSize);
    chunkLoaded[chunkIndex] = Math.min(end - start, loaded);
    const uploadedInFile = chunkLoaded.reduce((sum, value) => sum + value, 0);
    tracker.update(
      index,
      uploadedInFile,
      `正在上传 ${fileLabel}，分片 ${completedChunks}/${chunkCount}，分片并发 ${Math.min(UPLOAD_CHUNK_CONCURRENCY, chunkCount)}`,
    );
  }

  async function worker() {
    while (nextChunkIndex < chunkCount) {
      const chunkIndex = nextChunkIndex;
      nextChunkIndex += 1;
      const start = chunkIndex * chunkSize;
      const end = Math.min(file.size, start + chunkSize);
      await uploadChunkWithRetry(init.upload_id, file, chunkIndex, start, end, (loaded) => {
        updateProgress(chunkIndex, loaded);
      });
      completedChunks += 1;
      updateProgress(chunkIndex, end - start);
    }
  }

  const workerCount = Math.min(UPLOAD_CHUNK_CONCURRENCY, chunkCount);
  await Promise.all(Array.from({ length: workerCount }, () => worker()));

  setFormMessage(`正在创建审核任务：${fileLabel}`, "info");
  return completeChunkUpload(init.upload_id, file, {
    ...payload,
    chunk_size: chunkSize,
    video_title: files.length > 1 && payload.video_title_prefix
      ? `${payload.video_title_prefix}-${String(index + 1).padStart(2, "0")}`
      : (payload.video_title || file.name),
  });
}

async function uploadFilesInChunks(files, payload) {
  const reviews = Array.from({ length: files.length });
  const tracker = makeUploadTracker(files);
  let nextFileIndex = 0;

  async function worker() {
    while (nextFileIndex < files.length) {
      const fileIndex = nextFileIndex;
      nextFileIndex += 1;
      reviews[fileIndex] = await uploadFileInChunks(files[fileIndex], fileIndex, files, payload, tracker);
    }
  }

  const workerCount = Math.min(FILE_UPLOAD_CONCURRENCY, files.length);
  await Promise.all(Array.from({ length: workerCount }, () => worker()));
  return reviews;
}

async function uploadFileToOss(file, index, files, payload, tracker) {
  const fileLabel = files.length > 1 ? `第 ${index + 1}/${files.length} 个：${file.name}` : file.name;
  setFormMessage(`正在申请 OSS 直传凭证：${fileLabel}`, "info");
  const init = await initOssUpload(file);
  const client = makeOssClient(init);
  tracker.update(index, 0, `正在直传 OSS：${fileLabel}`);
  const result = await client.multipartUpload(init.object_key, file, {
    parallel: UPLOAD_CHUNK_CONCURRENCY,
    partSize: Math.max(DEFAULT_UPLOAD_CHUNK_SIZE, 512 * 1024),
    progress(percent) {
      tracker.update(index, Math.round(file.size * percent), `正在直传 OSS：${fileLabel}`);
    },
  });
  tracker.update(index, file.size, `OSS 上传完成：${fileLabel}`);
  setFormMessage(`正在校验 OSS 文件并创建审核任务：${fileLabel}`, "info");
  return completeOssUpload(
    init,
    file,
    {
      ...payload,
      video_title: files.length > 1 && payload.video_title_prefix
        ? `${payload.video_title_prefix}-${String(index + 1).padStart(2, "0")}`
        : (payload.video_title || file.name),
    },
    extractOssEtag(result),
  );
}

async function uploadFilesToOss(files, payload) {
  const reviews = Array.from({ length: files.length });
  const tracker = makeUploadTracker(files);
  let nextFileIndex = 0;

  async function worker() {
    while (nextFileIndex < files.length) {
      const fileIndex = nextFileIndex;
      nextFileIndex += 1;
      reviews[fileIndex] = await uploadFileToOss(files[fileIndex], fileIndex, files, payload, tracker);
    }
  }

  const workerCount = Math.min(FILE_UPLOAD_CONCURRENCY, files.length);
  await Promise.all(Array.from({ length: workerCount }, () => worker()));
  return reviews;
}

async function uploadFilesForReview(files, payload) {
  if (!window.OSS) {
    setFormMessage("OSS 直传 SDK 未加载，降级为服务端分片上传。", "info");
    return uploadFilesInChunks(files, payload);
  }
  try {
    return await uploadFilesToOss(files, payload);
  } catch (err) {
    const message = compactErrorMessage(err);
    if (/404|未配置 ALIYUN_OSS_BUCKET|OSS STS 临时凭证签发配置不完整|未配置阿里云签名凭证|未配置 ALIYUN_STS_ROLE_ARN|OSS 浏览器 SDK 未加载/.test(message)) {
      setFormMessage(`OSS 直传不可用，降级为服务端分片上传：${message}`, "info");
      return uploadFilesInChunks(files, payload);
    }
    throw err;
  }
}

function renderAdminStats(data) {
  $("adminTotal").textContent = data.total ?? 0;
  $("adminProcessing").textContent = data.processing ?? 0;
  $("adminCompleted").textContent = data.completed ?? 0;
  $("adminFailed").textContent = data.failed ?? 0;
  $("adminVideoMinutes").textContent = formatMinutes(data.total_video_minutes || 0);
  $("adminAvgTotal").textContent = formatDuration(data.avg_total_seconds);

  const rows = $("adminRows");
  rows.innerHTML = "";
  if (!data.reviews?.length) {
    rows.innerHTML = '<tr><td colspan="9">暂无任务数据</td></tr>';
    return;
  }
  data.reviews.forEach((item) => {
    const tr = document.createElement("tr");
    const source = item.source_url || item.local_path || "";
    tr.innerHTML = `
      <td>
        <div class="admin-video-title" title="${escapeHtml(item.video_title || source || item.review_id)}">${escapeHtml(item.video_title || source || item.review_id)}</div>
        <span class="admin-subtext">${escapeHtml(item.review_id)}</span>
      </td>
      <td>${labelFromMap(statusLabels, item.status)}<span class="admin-subtext">${labelFromMap(phaseLabels, item.phase)}</span></td>
      <td>${formatMinutes(item.duration_minutes)}</td>
      <td>${formatDuration(item.upload_seconds)}</td>
      <td>${formatDuration(item.queue_seconds)}</td>
      <td>${formatDuration(item.review_seconds)}</td>
      <td>${formatDuration(item.total_seconds)}</td>
      <td>${labelFromMap(decisionLabels, item.decision)}<span class="admin-subtext">${item.risk_score === null || item.risk_score === undefined ? "" : `风险分 ${item.risk_score}`}</span></td>
      <td>${formatDateTime(item.created_at)}</td>
    `;
    rows.appendChild(tr);
  });
}

async function loadAdminStats() {
  const limit = Math.max(10, Math.min(500, Number($("adminLimit").value || 100)));
  const status = $("adminStatusFilter").value;
  const createdFrom = datetimeLocalToIso($("adminCreatedFrom").value);
  const createdTo = datetimeLocalToIso($("adminCreatedTo").value);
  const params = new URLSearchParams({ limit: String(limit) });
  if (status) params.set("status", status);
  if (createdFrom) params.set("created_from", createdFrom);
  if (createdTo) params.set("created_to", createdTo);
  const data = await request(`/video/admin/stats?${params.toString()}`);
  renderAdminStats(data);
}

function drawLineChart(ctx, points, key, color, bounds) {
  if (!points.length) return;
  const { left, top, width, height } = bounds;
  ctx.beginPath();
  points.forEach((point, index) => {
    const x = left + (points.length === 1 ? width : (index / (points.length - 1)) * width);
    const y = top + height - (Math.max(0, Math.min(100, Number(point[key] || 0))) / 100) * height;
    if (index === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.strokeStyle = color;
  ctx.lineWidth = 2.5;
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  ctx.stroke();
}

function drawSystemChart(data) {
  const canvas = $("systemChart");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const width = Math.max(520, Math.round(rect.width || canvas.clientWidth || 520));
  const height = 260;
  canvas.width = width * dpr;
  canvas.height = height * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);

  const points = data?.points || [];
  const pad = { left: 42, right: 14, top: 16, bottom: 32 };
  const chart = {
    left: pad.left,
    top: pad.top,
    width: width - pad.left - pad.right,
    height: height - pad.top - pad.bottom,
  };

  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, width, height);
  ctx.strokeStyle = "#e5e5e0";
  ctx.lineWidth = 1;
  ctx.font = "12px Inter, system-ui, sans-serif";
  ctx.fillStyle = "#6b7280";

  [0, 25, 50, 75, 100].forEach((tick) => {
    const y = chart.top + chart.height - (tick / 100) * chart.height;
    ctx.beginPath();
    ctx.moveTo(chart.left, y);
    ctx.lineTo(chart.left + chart.width, y);
    ctx.stroke();
    ctx.fillText(`${tick}%`, 4, y + 4);
  });

  const thresholdY = chart.top + chart.height - 0.8 * chart.height;
  ctx.save();
  ctx.setLineDash([8, 8]);
  ctx.strokeStyle = "#f4a0b7";
  ctx.beginPath();
  ctx.moveTo(chart.left, thresholdY);
  ctx.lineTo(chart.left + chart.width, thresholdY);
  ctx.stroke();
  ctx.restore();

  if (!points.length) {
    ctx.fillStyle = "#6b7280";
    ctx.textAlign = "center";
    ctx.fillText("暂无监控数据", width / 2, height / 2);
    ctx.textAlign = "left";
    return;
  }

  drawLineChart(ctx, points, "memory_percent", "#2563eb", chart);
  drawLineChart(ctx, points, "cpu_percent", "#16a34a", chart);

  const first = new Date(points[0].timestamp);
  const last = new Date(points[points.length - 1].timestamp);
  ctx.fillStyle = "#6b7280";
  ctx.textAlign = "left";
  ctx.fillText(first.toLocaleTimeString(), chart.left, height - 8);
  ctx.textAlign = "right";
  ctx.fillText(last.toLocaleTimeString(), chart.left + chart.width, height - 8);
  ctx.textAlign = "left";
}

function renderSystemMetrics(data) {
  state.systemMetrics = data;
  const latest = data.latest || {};
  $("systemCpu").textContent = formatPercent(latest.cpu_percent);
  $("systemMemory").textContent = formatPercent(latest.memory_percent);
  $("systemMemoryUsed").textContent = `${formatBytes(latest.memory_used_bytes)} / ${formatBytes(latest.memory_total_bytes)}`;
  $("systemLoad").textContent = latest.load_1m === null || latest.load_1m === undefined ? "-" : String(latest.load_1m);
  $("systemHost").textContent = `${data.hostname || "-"} / ${data.cpu_count || 0} 核 / ${data.points?.length || 0} 个采样点`;
  drawSystemChart(data);
}

async function loadSystemMetrics() {
  const windowSeconds = Math.max(60, Math.min(3600, Number($("systemWindow").value || 1800)));
  const data = await request(`/video/admin/system-metrics?window_seconds=${windowSeconds}`);
  renderSystemMetrics(data);
}

async function submitReview(event) {
  event.preventDefault();
  setFormMessage("");
  const files = Array.from($("localFile").files || []);
  const videoUrls = parseVideoUrls($("videoUrls").value);
  const startSeconds = $("startSeconds").value === "" ? null : Number($("startSeconds").value);
  const endSeconds = $("endSeconds").value === "" ? null : Number($("endSeconds").value);
  if (startSeconds !== null && endSeconds !== null && endSeconds <= startSeconds) {
    alert("审核结束秒数必须大于开始秒数");
    return;
  }
  if (!videoUrls.length && files.length === 0) {
    alert("请填写视频链接或选择本地视频文件");
    return;
  }
  if (files.length > 50) {
    alert("单次最多上传 50 个视频文件");
    return;
  }
  if (videoUrls.length > 50) {
    alert("单次最多提交 50 个视频链接");
    return;
  }
  setSubmitBusy(true, files.length ? "正在上传..." : "正在创建...");
  try {
    let response;
    if (files.length > 0) {
      const sessionId = makeClientSessionId();
      const uploadPayload = {
        session_id: sessionId,
        video_title: $("videoTitle").value.trim() || null,
        video_title_prefix: $("videoTitle").value.trim() || null,
        fps: Number($("fps").value || 1),
        segment_seconds: Number($("segmentSeconds").value || 180),
        start_seconds: startSeconds,
        end_seconds: endSeconds,
      };
      const reviews = await uploadFilesForReview(files, uploadPayload);
      response = files.length > 1 ? { success: true, count: reviews.length, reviews } : reviews[0];
    } else {
      const commonPayload = {
        fps: Number($("fps").value || 1),
        segment_seconds: Number($("segmentSeconds").value || 180),
        start_seconds: startSeconds,
        end_seconds: endSeconds,
      };
      setFormMessage(`正在创建 ${videoUrls.length} 个链接审核任务...`, "info");
      if (videoUrls.length > 1) {
        response = await request("/video/reviews/bulk", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            ...commonPayload,
            video_urls: videoUrls,
            video_title_prefix: $("videoTitle").value.trim() || null,
          }),
        });
      } else {
        response = await request("/video/reviews", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            ...commonPayload,
            video_url: videoUrls[0],
            video_title: $("videoTitle").value.trim() || null,
          }),
        });
      }
    }
    const reviews = decorateCreatedReviews(response, files, videoUrls);
    setReviewId(reviews[0].review_id);
    renderBatchList(reviews);
    $("eventLog").innerHTML = "";
    logEvent("created", response);
    setFormMessage(`已创建 ${reviews.length} 个审核任务，可在右侧任务列表切换查看。`, "success");
    connectStream();
    await loadJob();
  } catch (err) {
    const message = compactErrorMessage(err);
    setFormMessage(`创建失败：${message}`, "error");
    logEvent("error", { message });
  } finally {
    setSubmitBusy(false);
  }
}

function bindNav() {
  document.querySelectorAll(".nav-link").forEach((link) => {
    link.addEventListener("click", () => {
      document.querySelectorAll(".nav-link").forEach((item) => item.classList.remove("active"));
      link.classList.add("active");
    });
  });
}

document.addEventListener("DOMContentLoaded", async () => {
  if (window.location.pathname === "/admin" && !window.location.hash) {
    window.location.hash = "#admin";
  }
  bindNav();
  $("reviewForm").addEventListener("submit", submitReview);
  $("localFile").addEventListener("change", renderSelectedFiles);
  $("loadJobBtn").addEventListener("click", () => loadJob().catch((err) => alert(err.message)));
  $("streamBtn").addEventListener("click", connectStream);
  $("loadReportBtn").addEventListener("click", () => loadReport().catch((err) => alert(err.message)));
  $("refreshPolicyBtn").addEventListener("click", () => loadPolicy().catch((err) => alert(err.message)));
  $("refreshAdminBtn").addEventListener("click", () => loadAdminStats().catch((err) => alert(err.message)));
  $("adminStatusFilter").addEventListener("change", () => loadAdminStats().catch((err) => alert(err.message)));
  $("adminLimit").addEventListener("change", () => loadAdminStats().catch((err) => alert(err.message)));
  $("adminCreatedFrom").addEventListener("change", () => loadAdminStats().catch((err) => alert(err.message)));
  $("adminCreatedTo").addEventListener("change", () => loadAdminStats().catch((err) => alert(err.message)));
  $("clearAdminTimeBtn").addEventListener("click", () => {
    $("adminCreatedFrom").value = "";
    $("adminCreatedTo").value = "";
    loadAdminStats().catch((err) => alert(err.message));
  });
  $("refreshSystemBtn").addEventListener("click", () => loadSystemMetrics().catch((err) => alert(err.message)));
  $("systemWindow").addEventListener("change", () => loadSystemMetrics().catch((err) => alert(err.message)));
  window.addEventListener("resize", () => {
    if (state.systemMetrics) drawSystemChart(state.systemMetrics);
  });
  $("focusCreateBtn").addEventListener("click", () => $("videoUrls").focus());
  await loadHealth();
  await loadPolicy();
  await loadSystemMetrics().catch(() => {});
  await loadAdminStats().catch(() => {});
  state.systemMetricsTimer = window.setInterval(() => {
    loadSystemMetrics().catch(() => {});
  }, 10000);
});
