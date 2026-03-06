"""
ViralDistributor — Web Dashboard
Accessible from browser, protected by HTTP Basic Auth via env vars.
Designed to run on Coolify (or any Docker-compatible host).
"""
import os
import secrets
import shutil
from pathlib import Path
from typing import Annotated

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from src.models.video import Platform, PrivacyStatus, VideoPost
from src.platforms.youtube import YouTubePublisher
from src.utils.logger import get_logger

load_dotenv()
logger = get_logger(__name__)

app = FastAPI(title="ViralDistributor", version="1.0.0", docs_url="/api/docs")
security = HTTPBasic()

UPLOAD_DIR = Path("videos/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


# ------------------------------------------------------------------
# Auth dependency
# ------------------------------------------------------------------

def verify_credentials(credentials: Annotated[HTTPBasicCredentials, Depends(security)]):
    correct_username = os.getenv("DASHBOARD_USERNAME", "")
    correct_password = os.getenv("DASHBOARD_PASSWORD", "")

    if not correct_username or not correct_password:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server credentials not configured. Set DASHBOARD_USERNAME and DASHBOARD_PASSWORD.",
        )

    username_ok = secrets.compare_digest(credentials.username.encode(), correct_username.encode())
    password_ok = secrets.compare_digest(credentials.password.encode(), correct_password.encode())

    if not (username_ok and password_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


AuthDep = Annotated[str, Depends(verify_credentials)]


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard(user: AuthDep):
    return HTMLResponse(content=_render_dashboard(), status_code=200)


@app.post("/upload/youtube")
async def upload_to_youtube(
    user: AuthDep,
    file: Annotated[UploadFile, File(description="Video file to upload")],
    title: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
    tags: Annotated[str, Form(description="Comma-separated tags")] = "",
    privacy: Annotated[PrivacyStatus, Form()] = PrivacyStatus.PRIVATE,
    is_short: Annotated[bool, Form()] = True,
):
    # Save uploaded file temporarily
    dest = UPLOAD_DIR / file.filename
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)

    tag_list = [t.strip() for t in tags.split(",") if t.strip()]

    video = VideoPost(
        file_path=str(dest),
        title=title,
        description=description,
        tags=tag_list,
        privacy=privacy,
        is_short=is_short,
    )

    publisher = YouTubePublisher()
    result = publisher.publish(video)

    # Clean up temp file after upload
    dest.unlink(missing_ok=True)

    if result.success:
        return JSONResponse({"success": True, "video_id": result.video_id, "url": result.video_url})
    else:
        raise HTTPException(status_code=500, detail=result.error_message)


@app.get("/health")
async def health():
    return {"status": "ok"}


# ------------------------------------------------------------------
# Simple HTML dashboard
# ------------------------------------------------------------------

def _render_dashboard() -> str:
    return """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>ViralDistributor</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui, sans-serif; background: #0f0f0f; color: #f0f0f0; min-height: 100vh; }
    header { background: #1a1a1a; padding: 1.5rem 2rem; border-bottom: 1px solid #333; }
    header h1 { font-size: 1.5rem; color: #ff4444; }
    header p { color: #888; font-size: 0.9rem; margin-top: 0.25rem; }
    main { max-width: 640px; margin: 2rem auto; padding: 0 1rem; }
    .card { background: #1a1a1a; border: 1px solid #333; border-radius: 8px; padding: 1.5rem; margin-bottom: 1.5rem; }
    .card h2 { font-size: 1rem; margin-bottom: 1rem; color: #ccc; }
    label { display: block; font-size: 0.85rem; color: #aaa; margin-bottom: 0.25rem; margin-top: 0.75rem; }
    input, textarea, select { width: 100%; padding: 0.5rem 0.75rem; background: #111; border: 1px solid #444;
      border-radius: 6px; color: #f0f0f0; font-size: 0.9rem; }
    textarea { resize: vertical; min-height: 80px; }
    .row { display: flex; gap: 1rem; }
    .row > div { flex: 1; }
    button { margin-top: 1rem; width: 100%; padding: 0.75rem; background: #ff4444; color: white;
      border: none; border-radius: 6px; font-size: 1rem; cursor: pointer; font-weight: 600; }
    button:hover { background: #cc3333; }
    #result { margin-top: 1rem; padding: 0.75rem; border-radius: 6px; display: none; }
    #result.success { background: #0d2b1a; border: 1px solid #1a7a3a; color: #4ade80; }
    #result.error { background: #2b0d0d; border: 1px solid #7a1a1a; color: #f87171; }
    .badge { display: inline-block; padding: 0.2rem 0.5rem; border-radius: 4px; font-size: 0.75rem;
      background: #222; border: 1px solid #444; color: #888; }
  </style>
</head>
<body>
  <header>
    <h1>ViralDistributor</h1>
    <p>Upload and publish your videos to social platforms</p>
  </header>
  <main>
    <div class="card">
      <h2>YouTube Shorts / Videos <span class="badge">Phase 1</span></h2>
      <form id="ytForm">
        <label>Video file</label>
        <input type="file" id="file" accept="video/*" required />

        <label>Title</label>
        <input type="text" id="title" placeholder="My Awesome Short" required maxlength="100" />

        <label>Description</label>
        <textarea id="desc" placeholder="Description..."></textarea>

        <label>Tags (comma separated)</label>
        <input type="text" id="tags" placeholder="#Shorts, topic, keyword" />

        <div class="row">
          <div>
            <label>Privacy</label>
            <select id="privacy">
              <option value="private">Private</option>
              <option value="unlisted">Unlisted</option>
              <option value="public">Public</option>
            </select>
          </div>
          <div>
            <label>Type</label>
            <select id="isShort">
              <option value="true">Short (&lt;= 60s)</option>
              <option value="false">Long video</option>
            </select>
          </div>
        </div>

        <button type="submit">Upload to YouTube</button>
        <div id="result"></div>
      </form>
    </div>

    <div class="card" style="opacity:0.5;">
      <h2>Instagram Reels <span class="badge">Phase 2 — Coming soon</span></h2>
    </div>
    <div class="card" style="opacity:0.5;">
      <h2>TikTok <span class="badge">Phase 3 — Coming soon</span></h2>
    </div>
  </main>

  <script>
    document.getElementById('ytForm').addEventListener('submit', async (e) => {
      e.preventDefault();
      const resultEl = document.getElementById('result');
      resultEl.style.display = 'none';

      const formData = new FormData();
      formData.append('file', document.getElementById('file').files[0]);
      formData.append('title', document.getElementById('title').value);
      formData.append('description', document.getElementById('desc').value);
      formData.append('tags', document.getElementById('tags').value);
      formData.append('privacy', document.getElementById('privacy').value);
      formData.append('is_short', document.getElementById('isShort').value);

      const btn = e.target.querySelector('button');
      btn.textContent = 'Uploading...';
      btn.disabled = true;

      try {
        const res = await fetch('/upload/youtube', { method: 'POST', body: formData });
        const data = await res.json();
        resultEl.style.display = 'block';
        if (res.ok) {
          resultEl.className = 'success';
          resultEl.innerHTML = `Upload successful! <a href="${data.url}" target="_blank" style="color:#4ade80">${data.url}</a>`;
        } else {
          resultEl.className = 'error';
          resultEl.textContent = 'Error: ' + (data.detail || 'Unknown error');
        }
      } catch (err) {
        resultEl.style.display = 'block';
        resultEl.className = 'error';
        resultEl.textContent = 'Network error: ' + err.message;
      } finally {
        btn.textContent = 'Upload to YouTube';
        btn.disabled = false;
      }
    });
  </script>
</body>
</html>
"""
