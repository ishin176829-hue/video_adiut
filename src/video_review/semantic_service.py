from __future__ import annotations

import json
import os
import subprocess
import sys
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Literal

import aiofiles
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .config import settings
from .utils import new_id, safe_filename


SemanticJobStatus = Literal["pending", "processing", "completed", "failed"]
SUPPORTED_VIDEO_SUFFIXES = {".mp4", ".mov", ".avi", ".mkv", ".webm"}


class SemanticVideoItem(BaseModel):
    video_id: str
    title: str
    local_path: str
    status: SemanticJobStatus = "pending"
    output_path: str | None = None
    raw_output_dir: str | None = None
    frames_sent: int = 0
    segments: int = 0
    error: str | None = None


class SemanticJob(BaseModel):
    job_id: str
    status: SemanticJobStatus = "pending"
    model: str = "gemini-3.1-flash-lite"
    fps: int = 1
    segment_seconds: int = 240
    videos: list[SemanticVideoItem] = Field(default_factory=list)
    progress: dict = Field(default_factory=dict)
    error: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat())


class CreateSemanticJobRequest(BaseModel):
    local_paths: list[str] = Field(min_length=1)
    titles: list[str] = Field(default_factory=list)
    model: str = "gemini-3.1-flash-lite"
    fps: int = Field(default=1, ge=1, le=1)
    segment_seconds: int = Field(default=240, ge=30, le=600)


class SemanticJobResultItem(BaseModel):
    video_id: str
    title: str
    output_path: str
    payload: dict


class SemanticJobResult(BaseModel):
    job_id: str
    status: SemanticJobStatus
    results: list[SemanticJobResultItem]


app = FastAPI(title="SN2S Semantic Video Extraction", version="0.1.0")


def semantic_root() -> Path:
    path = settings.data_dir / "semantic_service"
    path.mkdir(parents=True, exist_ok=True)
    return path


def semantic_jobs_dir() -> Path:
    path = semantic_root() / "jobs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def semantic_upload_dir() -> Path:
    path = semantic_root() / "uploads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def semantic_results_dir() -> Path:
    path = semantic_root() / "results"
    path.mkdir(parents=True, exist_ok=True)
    return path


def job_path(job_id: str) -> Path:
    return semantic_jobs_dir() / f"{job_id}.json"


def save_semantic_job(job: SemanticJob) -> None:
    job.updated_at = datetime.now().isoformat()
    job_path(job.job_id).write_text(job.model_dump_json(indent=2), encoding="utf-8")


def get_semantic_job(job_id: str) -> SemanticJob:
    path = job_path(job_id)
    if not path.exists():
        raise HTTPException(404, "语义抽取任务不存在")
    return SemanticJob.model_validate_json(path.read_text(encoding="utf-8"))


def validate_video_path(path: Path) -> None:
    if path.suffix.lower() not in SUPPORTED_VIDEO_SUFFIXES:
        raise HTTPException(400, f"不支持的视频格式：{path.name}")
    if not path.exists():
        raise HTTPException(400, f"视频不存在：{path}")


def make_semantic_job(request: CreateSemanticJobRequest) -> SemanticJob:
    job_id = new_id("semantic")
    videos = []
    for index, raw_path in enumerate(request.local_paths):
        path = Path(raw_path).expanduser().resolve()
        validate_video_path(path)
        title = request.titles[index] if index < len(request.titles) and request.titles[index] else path.stem
        videos.append(
            SemanticVideoItem(
                video_id=new_id("video"),
                title=title,
                local_path=str(path),
            )
        )
    job = SemanticJob(
        job_id=job_id,
        model=request.model,
        fps=request.fps,
        segment_seconds=request.segment_seconds,
        videos=videos,
        progress={"completed": 0, "total": len(videos)},
    )
    save_semantic_job(job)
    return job


def update_progress(job: SemanticJob) -> None:
    completed = sum(1 for video in job.videos if video.status == "completed")
    failed = sum(1 for video in job.videos if video.status == "failed")
    job.progress = {"completed": completed, "failed": failed, "total": len(job.videos)}
    if failed:
        job.status = "failed" if completed == 0 else "completed"
    elif completed == len(job.videos):
        job.status = "completed"
    else:
        job.status = "processing"


def extraction_script() -> Path:
    return Path(__file__).resolve().parents[2] / "scripts" / "semantic_video_extraction.py"


def run_extract_command(job: SemanticJob, video: SemanticVideoItem, result_root: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    src_dir = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = src_dir + os.pathsep + env.get("PYTHONPATH", "")
    command = [
        sys.executable,
        str(extraction_script()),
        "--model",
        job.model,
        "--fps",
        str(job.fps),
        "extract",
        video.local_path,
        "--title",
        video.title,
        "--episode-id",
        Path(video.local_path).stem,
        "--segment-seconds",
        str(job.segment_seconds),
        "--out-dir",
        str(result_root),
    ]
    return subprocess.run(command, capture_output=True, text=True, check=False, env=env)


def find_output_path(result_root: Path, video: SemanticVideoItem) -> Path:
    candidates = sorted((result_root / Path(video.local_path).stem).glob("*.screenplay-semantic.json"))
    if not candidates:
        raise FileNotFoundError(f"未找到语义结果文件：{result_root / Path(video.local_path).stem}")
    return candidates[-1]


async def run_semantic_job(job_id: str) -> None:
    job = get_semantic_job(job_id)
    job.status = "processing"
    save_semantic_job(job)
    result_root = semantic_results_dir() / job_id
    result_root.mkdir(parents=True, exist_ok=True)
    for index, video in enumerate(job.videos):
        job = get_semantic_job(job_id)
        video = job.videos[index]
        video.status = "processing"
        save_semantic_job(job)
        process = await asyncio.to_thread(run_extract_command, job, video, result_root)
        if process.returncode != 0:
            video.status = "failed"
            video.error = (process.stderr or process.stdout or "语义抽取失败")[-4000:]
            update_progress(job)
            save_semantic_job(job)
            continue
        try:
            output_path = find_output_path(result_root, video)
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            video.status = "completed"
            video.output_path = str(output_path)
            video.raw_output_dir = str(output_path.parent / "segments")
            video.frames_sent = int(payload.get("total_frames_sent") or 0)
            video.segments = len(payload.get("segments") or [])
        except Exception as exc:
            video.status = "failed"
            video.error = f"{type(exc).__name__}: {exc}"
        update_progress(job)
        save_semantic_job(job)


async def dispatch_semantic_job(job_id: str) -> None:
    if os.getenv("SEMANTIC_SERVICE_RUN_INLINE", "").lower() in {"1", "true", "yes"}:
        await run_semantic_job(job_id)
        return
    asyncio.create_task(run_semantic_job(job_id))


@app.get("/health")
async def health() -> dict:
    return {"status": "healthy", "service": "sn2s-semantic-video-extraction"}


@app.get("/", response_class=HTMLResponse)
async def root_page() -> HTMLResponse:
    return HTMLResponse(
        """
<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>SN2S Semantic Video Extraction</title>
    <style>
      :root { color-scheme: light; --line: #d8dee8; --muted: #5d6675; --ink: #101828; --blue: #1769e0; --bg: #f6f8fb; }
      * { box-sizing: border-box; }
      body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: var(--ink); background: var(--bg); line-height: 1.45; }
      header { padding: 24px 32px 16px; border-bottom: 1px solid var(--line); background: #fff; }
      h1 { margin: 0 0 6px; font-size: 26px; font-weight: 700; }
      main { max-width: 1180px; margin: 0 auto; padding: 24px 28px 40px; }
      section { margin-bottom: 22px; }
      h2 { margin: 0 0 12px; font-size: 18px; }
      .panel { background: #fff; border: 1px solid var(--line); border-radius: 8px; padding: 16px; }
      .grid { display: grid; grid-template-columns: minmax(260px, 1fr) 160px 220px auto; gap: 12px; align-items: end; }
      label { display: block; font-size: 13px; color: var(--muted); margin-bottom: 5px; }
      input, button { font: inherit; }
      input[type="file"], input[type="number"], input[type="text"] { width: 100%; border: 1px solid var(--line); border-radius: 6px; padding: 9px 10px; background: #fff; }
      button { border: 1px solid #155dbe; background: var(--blue); color: #fff; border-radius: 6px; padding: 10px 14px; cursor: pointer; white-space: nowrap; }
      button.secondary { border-color: var(--line); background: #fff; color: var(--ink); }
      button:disabled { opacity: .55; cursor: not-allowed; }
      table { width: 100%; border-collapse: collapse; background: #fff; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
      th, td { border-bottom: 1px solid var(--line); padding: 10px 12px; text-align: left; font-size: 14px; vertical-align: top; }
      th { background: #eef2f7; color: #364152; font-weight: 600; }
      tr:last-child td { border-bottom: 0; }
      .muted { color: var(--muted); }
      .status { font-weight: 650; }
      .status.completed { color: #0f7a3c; }
      .status.failed { color: #b42318; }
      .status.processing, .status.pending { color: #a15c00; }
      pre { margin: 0; max-height: 420px; overflow: auto; background: #101828; color: #e6edf7; border-radius: 8px; padding: 14px; font-size: 12px; }
      .actions { display: flex; gap: 8px; flex-wrap: wrap; }
      .result-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 10px; }
      @media (max-width: 820px) { header { padding: 20px; } main { padding: 18px; } .grid { grid-template-columns: 1fr; } }
    </style>
  </head>
  <body>
    <header>
      <h1>SN2S Semantic Video Extraction</h1>
      <div class="muted">1fps 批量视频语义抽取 · <a href="/docs">API Docs</a> · <a href="/health">Health</a></div>
    </header>
    <main>
      <section class="panel">
        <h2>上传视频</h2>
        <form id="upload-form" class="grid">
          <div>
            <label for="files">视频文件</label>
            <input id="files" name="files" type="file" accept="video/*" multiple required />
          </div>
          <div>
            <label for="segment-seconds">分段秒数</label>
            <input id="segment-seconds" name="segment_seconds" type="number" min="30" max="600" step="30" value="240" />
          </div>
          <div>
            <label for="model">模型</label>
            <input id="model" name="model" type="text" value="gemini-3.1-flash-lite" />
          </div>
          <button id="submit-upload" type="submit">开始抽取</button>
        </form>
        <div id="message" class="muted" style="margin-top: 10px;"></div>
      </section>

      <section>
        <h2>任务</h2>
        <table id="jobs-table">
          <thead>
            <tr>
              <th>Job ID</th>
              <th>状态</th>
              <th>视频</th>
              <th>进度</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody id="jobs-body">
            <tr><td colspan="5" class="muted">暂无任务</td></tr>
          </tbody>
        </table>
      </section>

      <section id="result-section" style="display:none;">
        <div class="result-head">
          <h2>结果 JSON</h2>
          <button id="download-result" class="secondary" type="button">下载 JSON</button>
        </div>
        <pre id="result-json"></pre>
      </section>
    </main>
    <script>
      const jobs = new Map();
      let activeResult = null;

      const uploadForm = document.getElementById('upload-form');
      const jobsBody = document.getElementById('jobs-body');
      const message = document.getElementById('message');
      const resultSection = document.getElementById('result-section');
      const resultJson = document.getElementById('result-json');
      const downloadResult = document.getElementById('download-result');

      function setMessage(text) {
        message.textContent = text || '';
      }

      function videoSummary(job) {
        return job.videos.map(video => {
          const counts = video.frames_sent ? ` · ${video.frames_sent}帧/${video.segments}段` : '';
          const error = video.error ? ` · ${video.error.slice(0, 100)}` : '';
          return `${video.title} (${video.status}${counts}${error})`;
        }).join('\\n');
      }

      function renderJobs() {
        const list = Array.from(jobs.values()).sort((a, b) => b.created_at.localeCompare(a.created_at));
        if (!list.length) {
          jobsBody.innerHTML = '<tr><td colspan="5" class="muted">暂无任务</td></tr>';
          return;
        }
        jobsBody.innerHTML = '';
        for (const job of list) {
          const tr = document.createElement('tr');
          const completed = job.progress?.completed || 0;
          const failed = job.progress?.failed || 0;
          const total = job.progress?.total || job.videos.length;
          tr.innerHTML = `
            <td><code>${job.job_id}</code></td>
            <td><span class="status ${job.status}">${job.status}</span></td>
            <td style="white-space: pre-line;">${videoSummary(job)}</td>
            <td>${completed}/${total}${failed ? ` · failed ${failed}` : ''}</td>
            <td><div class="actions"></div></td>
          `;
          const actions = tr.querySelector('.actions');
          const refresh = document.createElement('button');
          refresh.type = 'button';
          refresh.className = 'secondary';
          refresh.textContent = '刷新';
          refresh.onclick = () => refreshJob(job.job_id);
          actions.appendChild(refresh);
          const view = document.createElement('button');
          view.type = 'button';
          view.textContent = '查看结果';
          view.disabled = job.status !== 'completed';
          view.onclick = () => loadResult(job.job_id);
          actions.appendChild(view);
          jobsBody.appendChild(tr);
        }
      }

      async function refreshJob(jobId) {
        const response = await fetch(`/semantic/jobs/${jobId}`);
        if (!response.ok) throw new Error(await response.text());
        const job = await response.json();
        jobs.set(job.job_id, job);
        renderJobs();
        return job;
      }

      async function pollJob(jobId) {
        for (;;) {
          const job = await refreshJob(jobId);
          if (job.status === 'completed' || job.status === 'failed') return job;
          await new Promise(resolve => setTimeout(resolve, 2500));
        }
      }

      async function loadResult(jobId) {
        const response = await fetch(`/semantic/jobs/${jobId}/result`);
        if (!response.ok) {
          setMessage(await response.text());
          return;
        }
        activeResult = await response.json();
        resultJson.textContent = JSON.stringify(activeResult, null, 2);
        resultSection.style.display = '';
        setMessage(`已加载 ${jobId} 的结果`);
      }

      uploadForm.addEventListener('submit', async (event) => {
        event.preventDefault();
        const files = document.getElementById('files').files;
        if (!files.length) return;
        const submit = document.getElementById('submit-upload');
        submit.disabled = true;
        setMessage('正在上传...');
        try {
          const formData = new FormData();
          for (const file of files) formData.append('files', file);
          formData.append('segment_seconds', document.getElementById('segment-seconds').value || '240');
          formData.append('model', document.getElementById('model').value || 'gemini-3.1-flash-lite');
          const response = await fetch('/semantic/uploads', { method: 'POST', body: formData });
          if (!response.ok) throw new Error(await response.text());
          const job = await response.json();
          jobs.set(job.job_id, job);
          renderJobs();
          setMessage(`任务已提交：${job.job_id}`);
          pollJob(job.job_id).then(done => {
            setMessage(done.status === 'completed' ? `任务完成：${done.job_id}` : `任务失败：${done.job_id}`);
          }).catch(error => setMessage(error.message));
        } catch (error) {
          setMessage(error.message);
        } finally {
          submit.disabled = false;
        }
      });

      downloadResult.addEventListener('click', () => {
        if (!activeResult) return;
        const blob = new Blob([JSON.stringify(activeResult, null, 2)], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = `${activeResult.job_id}.json`;
        link.click();
        URL.revokeObjectURL(url);
      });
    </script>
  </body>
</html>
        """.strip()
    )


@app.post("/semantic/jobs", response_model=SemanticJob)
async def create_semantic_job(request: CreateSemanticJobRequest) -> SemanticJob:
    job = make_semantic_job(request)
    await dispatch_semantic_job(job.job_id)
    return job


@app.post("/semantic/uploads", response_model=SemanticJob)
async def upload_semantic_job(
    files: list[UploadFile] = File(...),
    model: str = Form("gemini-3.1-flash-lite"),
    fps: int = Form(1),
    segment_seconds: int = Form(240),
) -> SemanticJob:
    if fps != 1:
        raise HTTPException(400, "当前语义抽取服务只支持 1fps")
    upload_dir = semantic_upload_dir() / new_id("upload")
    upload_dir.mkdir(parents=True, exist_ok=False)
    local_paths: list[str] = []
    titles: list[str] = []
    for file in files:
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in SUPPORTED_VIDEO_SUFFIXES:
            raise HTTPException(400, f"不支持的视频格式：{file.filename}")
        filename = safe_filename(file.filename or f"video{suffix}", f"video{suffix}")
        target = upload_dir / filename
        async with aiofiles.open(target, "wb") as handle:
            while chunk := await file.read(1024 * 1024):
                await handle.write(chunk)
        local_paths.append(str(target))
        titles.append(Path(file.filename or filename).stem)
    request = CreateSemanticJobRequest(
        local_paths=local_paths,
        titles=titles,
        model=model,
        fps=fps,
        segment_seconds=segment_seconds,
    )
    job = make_semantic_job(request)
    await dispatch_semantic_job(job.job_id)
    return job


@app.get("/semantic/jobs/{job_id}", response_model=SemanticJob)
async def read_semantic_job(job_id: str) -> SemanticJob:
    return get_semantic_job(job_id)


@app.get("/semantic/jobs/{job_id}/result", response_model=SemanticJobResult)
async def read_semantic_job_result(job_id: str) -> SemanticJobResult:
    job = get_semantic_job(job_id)
    if job.status != "completed":
        raise HTTPException(409, "语义抽取任务尚未完成")
    results = []
    for video in job.videos:
        if not video.output_path:
            continue
        output_path = Path(video.output_path)
        results.append(
            SemanticJobResultItem(
                video_id=video.video_id,
                title=video.title,
                output_path=str(output_path),
                payload=json.loads(output_path.read_text(encoding="utf-8")),
            )
        )
    return SemanticJobResult(job_id=job.job_id, status=job.status, results=results)
