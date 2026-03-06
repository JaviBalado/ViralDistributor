"""
ViralDistributor — Web Dashboard
Multi-account, multi-platform video scheduler with OAuth web flow.
Protected by HTTP Basic Auth via environment variables.
"""
import os
import secrets
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.orm import Session

from src.db.database import get_db, init_db
from src.db.models import Account, ScheduledPost
from src.platforms.youtube import YouTubePublisher
from src.services.scheduler import start_scheduler, stop_scheduler
from src.utils.logger import get_logger

load_dotenv()
logger = get_logger(__name__)

app = FastAPI(title="ViralDistributor", version="2.0.0", docs_url="/api/docs")
security = HTTPBasic()

UPLOAD_DIR = Path("videos/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# In-memory OAuth state store: { state_token: { "account_name": str, "platform": str } }
_oauth_states: dict[str, dict] = {}

OAUTH_REDIRECT_URI = os.getenv("OAUTH_REDIRECT_URI", "http://localhost:8000/auth/youtube/callback")


# ------------------------------------------------------------------
# Startup / shutdown
# ------------------------------------------------------------------

@app.on_event("startup")
def on_startup():
    init_db()
    start_scheduler()


@app.on_event("shutdown")
def on_shutdown():
    stop_scheduler()


# ------------------------------------------------------------------
# Auth
# ------------------------------------------------------------------

def verify_credentials(credentials: Annotated[HTTPBasicCredentials, Depends(security)]) -> str:
    correct_username = os.getenv("DASHBOARD_USERNAME", "")
    correct_password = os.getenv("DASHBOARD_PASSWORD", "")

    if not correct_username or not correct_password:
        raise HTTPException(status_code=500, detail="DASHBOARD_USERNAME and DASHBOARD_PASSWORD not configured.")

    ok = secrets.compare_digest(credentials.username, correct_username) and \
         secrets.compare_digest(credentials.password, correct_password)

    if not ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


AuthDep = Annotated[str, Depends(verify_credentials)]


# ------------------------------------------------------------------
# Pages
# ------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def root(user: AuthDep):
    return RedirectResponse(url="/accounts")


@app.get("/accounts", response_class=HTMLResponse)
async def accounts_page(user: AuthDep, db: Session = Depends(get_db)):
    accounts = db.query(Account).order_by(Account.created_at.desc()).all()
    rows = ""
    for acc in accounts:
        platform_badge = _platform_badge(acc.platform)
        rows += f"""
        <tr>
          <td>{acc.id}</td>
          <td>{platform_badge} {acc.name}</td>
          <td>{acc.created_at.strftime('%Y-%m-%d %H:%M')}</td>
          <td>
            <form method="post" action="/accounts/{acc.id}/delete" onsubmit="return confirm('Delete account?')">
              <button class="btn-danger">Delete</button>
            </form>
          </td>
        </tr>"""

    if not rows:
        rows = '<tr><td colspan="4" style="text-align:center;color:#666;">No accounts yet. Connect one below.</td></tr>'

    content = f"""
    <div class="page-header">
      <h2>Accounts</h2>
      <a href="/accounts/connect/youtube" class="btn">+ Connect YouTube Account</a>
    </div>
    <table>
      <thead><tr><th>#</th><th>Account</th><th>Connected</th><th>Actions</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
    <div class="card" style="margin-top:1.5rem;">
      <p style="color:#888;font-size:.85rem;">
        <strong style="color:#ccc;">Instagram & TikTok</strong> — coming in Phase 2 & 3.
      </p>
    </div>
    """
    return HTMLResponse(_layout("Accounts", content))


@app.get("/accounts/connect/youtube", response_class=HTMLResponse)
async def connect_youtube_form(user: AuthDep):
    content = """
    <div class="page-header"><h2>Connect YouTube Account</h2></div>
    <div class="card" style="max-width:480px;">
      <form method="post" action="/accounts/connect/youtube">
        <label>Account name (your label, e.g. "Gaming Channel")</label>
        <input type="text" name="account_name" placeholder="My Channel" required maxlength="100" />
        <button type="submit" class="btn" style="margin-top:1rem;width:100%;">
          Authorize with Google
        </button>
      </form>
      <p style="margin-top:1rem;color:#666;font-size:.8rem;">
        You will be redirected to Google to grant upload permissions.
      </p>
    </div>
    """
    return HTMLResponse(_layout("Connect YouTube", content))


@app.post("/accounts/connect/youtube")
async def connect_youtube_start(user: AuthDep, account_name: str = Form(...)):
    state = secrets.token_urlsafe(32)
    _oauth_states[state] = {"account_name": account_name, "platform": "youtube"}
    try:
        publisher = YouTubePublisher()
        auth_url = publisher.get_auth_url(redirect_uri=OAUTH_REDIRECT_URI, state=state)
    except (FileNotFoundError, ValueError) as e:
        return HTMLResponse(_layout("Error", f"""
        <div class="page-header"><h2>Configuration Error</h2></div>
        <div class="card">
          <p style="color:#f87171;margin-bottom:1rem;"><strong>YouTube credentials not configured correctly:</strong></p>
          <pre style="background:#0f0f0f;padding:1rem;border-radius:6px;color:#fbbf24;font-size:.8rem;white-space:pre-wrap;">{e}</pre>
          <p style="margin-top:1rem;color:#888;font-size:.85rem;">
            Make sure <code>YOUTUBE_CLIENT_SECRETS_JSON</code> is set in your Coolify environment variables.
          </p>
          <a href="/accounts" class="btn" style="display:inline-block;margin-top:1rem;">Back to Accounts</a>
        </div>"""), status_code=500)
    except Exception as e:
        logger.error(f"OAuth start failed: {e}")
        return HTMLResponse(_layout("Error", f"""
        <div class="page-header"><h2>Error</h2></div>
        <div class="card">
          <pre style="background:#0f0f0f;padding:1rem;border-radius:6px;color:#f87171;font-size:.8rem;white-space:pre-wrap;">{e}</pre>
          <a href="/accounts" class="btn" style="display:inline-block;margin-top:1rem;">Back</a>
        </div>"""), status_code=500)
    return RedirectResponse(url=auth_url, status_code=302)


@app.get("/auth/youtube/callback")
async def youtube_callback(request: Request, db: Session = Depends(get_db)):
    """OAuth callback — Google redirects here. Does NOT require Basic Auth."""
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    error = request.query_params.get("error")

    if error:
        return HTMLResponse(_layout("Error", f'<div class="card"><p style="color:#f87171;">Authorization denied: {error}</p><a href="/accounts" class="btn">Back</a></div>'))

    if not code or state not in _oauth_states:
        raise HTTPException(status_code=400, detail="Invalid OAuth state.")

    state_data = _oauth_states.pop(state)
    publisher = YouTubePublisher()

    try:
        credentials_json = publisher.exchange_code(code=code, redirect_uri=OAUTH_REDIRECT_URI)
    except Exception as e:
        logger.error(f"OAuth exchange failed: {e}")
        raise HTTPException(status_code=500, detail=f"Token exchange failed: {e}")

    account = Account(
        name=state_data["account_name"],
        platform=state_data["platform"],
        credentials_json=credentials_json,
    )
    db.add(account)
    db.commit()
    logger.info(f"New account connected: {account.name} ({account.platform})")

    return RedirectResponse(url="/accounts", status_code=302)


@app.post("/accounts/{account_id}/delete")
async def delete_account(account_id: int, user: AuthDep, db: Session = Depends(get_db)):
    account = db.query(Account).filter(Account.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found.")
    db.delete(account)
    db.commit()
    return RedirectResponse(url="/accounts", status_code=302)


@app.get("/upload", response_class=HTMLResponse)
async def upload_page(user: AuthDep, db: Session = Depends(get_db)):
    accounts = db.query(Account).order_by(Account.name).all()

    if not accounts:
        content = """
        <div class="page-header"><h2>Schedule Upload</h2></div>
        <div class="card">
          <p style="color:#888;">No accounts connected yet. <a href="/accounts/connect/youtube" class="link">Connect a YouTube account</a> first.</p>
        </div>"""
        return HTMLResponse(_layout("Upload", content))

    account_options = "".join(
        f'<option value="{a.id}">[{a.platform.upper()}] {a.name}</option>' for a in accounts
    )

    now_local = datetime.utcnow().strftime("%Y-%m-%dT%H:%M")

    content = f"""
    <div class="page-header"><h2>Schedule Upload</h2></div>
    <div class="card" style="max-width:560px;">
      <form id="uploadForm" enctype="multipart/form-data">

        <label>Account</label>
        <select name="account_id" required>{account_options}</select>

        <label>Video file</label>
        <input type="file" name="file" accept="video/*" required />

        <label>Title</label>
        <input type="text" name="title" placeholder="My Awesome Short" required maxlength="100" />

        <label>Description</label>
        <textarea name="description" placeholder="Description..."></textarea>

        <label>Tags (comma separated)</label>
        <input type="text" name="tags" placeholder="#Shorts, topic, keyword" />

        <label>Publish date &amp; time (your local time)</label>
        <input type="datetime-local" name="scheduled_at" value="{now_local}" required />

        <button type="submit" class="btn" style="margin-top:1rem;width:100%;">Schedule</button>
        <div id="result" style="display:none;margin-top:1rem;padding:.75rem;border-radius:6px;"></div>
      </form>
    </div>
    <script>
      document.getElementById('uploadForm').addEventListener('submit', async (e) => {{
        e.preventDefault();
        const r = document.getElementById('result');
        r.style.display='none';
        const btn = e.target.querySelector('button');
        btn.textContent='Uploading...'; btn.disabled=true;
        const fd = new FormData(e.target);
        try {{
          const res = await fetch('/upload', {{method:'POST', body:fd}});
          const data = await res.json();
          r.style.display='block';
          if (res.ok) {{
            r.style.cssText='display:block;background:#0d2b1a;border:1px solid #1a7a3a;color:#4ade80;padding:.75rem;border-radius:6px;';
            r.textContent='Scheduled! ' + data.message;
          }} else {{
            r.style.cssText='display:block;background:#2b0d0d;border:1px solid #7a1a1a;color:#f87171;padding:.75rem;border-radius:6px;';
            r.textContent='Error: ' + (data.detail || 'Unknown error');
          }}
        }} catch(err) {{
          r.style.cssText='display:block;background:#2b0d0d;border:1px solid #7a1a1a;color:#f87171;padding:.75rem;border-radius:6px;';
          r.textContent='Network error: '+err.message;
        }} finally {{ btn.textContent='Schedule'; btn.disabled=false; }}
      }});
    </script>
    """
    return HTMLResponse(_layout("Upload", content))


@app.post("/upload")
async def schedule_upload(
    user: AuthDep,
    file: Annotated[UploadFile, File()],
    account_id: int = Form(...),
    title: str = Form(...),
    description: str = Form(""),
    tags: str = Form(""),
    scheduled_at: str = Form(...),
    db: Session = Depends(get_db),
):
    account = db.query(Account).filter(Account.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found.")

    # Save video file with unique name to avoid collisions
    ext = Path(file.filename).suffix
    unique_name = f"{uuid.uuid4().hex}{ext}"
    dest = UPLOAD_DIR / unique_name
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)

    # Parse scheduled_at (browser sends local time in ISO format without timezone)
    try:
        scheduled_dt = datetime.fromisoformat(scheduled_at)
    except ValueError:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Invalid date format.")

    post = ScheduledPost(
        account_id=account_id,
        title=title,
        description=description,
        tags=tags,
        file_path=str(dest.absolute()),
        scheduled_at=scheduled_dt,
    )
    db.add(post)
    db.commit()
    logger.info(f"Post scheduled: '{title}' for {scheduled_dt} on account #{account_id}")

    return JSONResponse({"success": True, "message": f"Video scheduled for {scheduled_dt.strftime('%Y-%m-%d %H:%M')}."})


@app.get("/posts", response_class=HTMLResponse)
async def posts_page(user: AuthDep, db: Session = Depends(get_db)):
    posts = (
        db.query(ScheduledPost)
        .order_by(ScheduledPost.scheduled_at.desc())
        .limit(100)
        .all()
    )

    rows = ""
    for p in posts:
        status_badge = _status_badge(p.status)
        url_cell = f'<a href="{p.video_url}" target="_blank" class="link">View</a>' if p.video_url else (p.error_message or "—")
        rows += f"""
        <tr>
          <td>{p.id}</td>
          <td>{p.account.name if p.account else '—'}</td>
          <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{p.title}</td>
          <td>{p.scheduled_at.strftime('%Y-%m-%d %H:%M')}</td>
          <td>{status_badge}</td>
          <td style="font-size:.8rem;color:#888;">{url_cell}</td>
          <td>
            <form method="post" action="/posts/{p.id}/delete" onsubmit="return confirm('Delete post?')">
              <button class="btn-danger">Delete</button>
            </form>
          </td>
        </tr>"""

    if not rows:
        rows = '<tr><td colspan="7" style="text-align:center;color:#666;">No posts yet.</td></tr>'

    content = f"""
    <div class="page-header"><h2>Scheduled Posts</h2></div>
    <div style="overflow-x:auto;">
      <table>
        <thead><tr><th>#</th><th>Account</th><th>Title</th><th>Scheduled</th><th>Status</th><th>Result</th><th>Actions</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>"""
    return HTMLResponse(_layout("Posts", content))


@app.post("/posts/{post_id}/delete")
async def delete_post(post_id: int, user: AuthDep, db: Session = Depends(get_db)):
    post = db.query(ScheduledPost).filter(ScheduledPost.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found.")
    # Delete the video file if it still exists and post is not published
    if post.status != "published":
        Path(post.file_path).unlink(missing_ok=True)
    db.delete(post)
    db.commit()
    return RedirectResponse(url="/posts", status_code=302)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/debug", response_class=HTMLResponse)
async def debug(user: AuthDep):
    """Shows configuration status to help diagnose issues."""
    client_id = os.getenv("YOUTUBE_CLIENT_ID", "")
    client_secret = os.getenv("YOUTUBE_CLIENT_SECRET", "")
    secrets_path = os.getenv("YOUTUBE_CLIENT_SECRETS_PATH", "auth/client_secrets.json")

    checks = []

    # Check YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET
    if client_id and client_secret:
        checks.append(("YOUTUBE_CLIENT_ID", "ok", client_id[:20] + "..."))
        checks.append(("YOUTUBE_CLIENT_SECRET", "ok", "Set (hidden)"))
    else:
        checks.append(("YOUTUBE_CLIENT_ID", "error" if not client_id else "ok",
                       "NOT SET" if not client_id else client_id[:20] + "..."))
        checks.append(("YOUTUBE_CLIENT_SECRET", "error" if not client_secret else "ok",
                       "NOT SET" if not client_secret else "Set (hidden)"))
        # Check file fallback
        if os.path.exists(secrets_path):
            checks.append(("client_secrets.json file", "ok", f"Found at {secrets_path}"))
        else:
            checks.append(("client_secrets.json file", "error", f"Not found at {secrets_path}"))

    # Check OAUTH_REDIRECT_URI
    redirect_uri = os.getenv("OAUTH_REDIRECT_URI", "NOT SET")
    checks.append(("OAUTH_REDIRECT_URI", "ok" if redirect_uri != "NOT SET" else "error", redirect_uri))

    # Check DATABASE_URL
    db_url = os.getenv("DATABASE_URL", "sqlite:///./data/viraldistributor.db (default)")
    checks.append(("DATABASE_URL", "ok", db_url))

    # Check data dir
    checks.append(("data/ directory", "ok" if os.path.isdir("data") else "error",
                   "exists" if os.path.isdir("data") else "MISSING — DB cannot be created"))

    rows = ""
    for name, status, detail in checks:
        color = {"ok": "#4ade80", "warn": "#fbbf24", "error": "#f87171"}[status]
        icon = {"ok": "OK", "warn": "WARN", "error": "ERR"}[status]
        rows += f"<tr><td>{name}</td><td><span class='badge' style='background:#1a1a1a;color:{color};'>{icon}</span></td><td style='color:#aaa;font-size:.8rem;'>{detail}</td></tr>"

    content = f"""
    <div class="page-header"><h2>Debug — Configuration Check</h2></div>
    <table><thead><tr><th>Check</th><th>Status</th><th>Detail</th></tr></thead>
    <tbody>{rows}</tbody></table>
    <p style="margin-top:1rem;color:#555;font-size:.78rem;">Remove or restrict this endpoint in production once configured.</p>
    """
    return HTMLResponse(_layout("Debug", content))


# ------------------------------------------------------------------
# HTML helpers
# ------------------------------------------------------------------

def _layout(title: str, content: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>{title} — ViralDistributor</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: system-ui, sans-serif; background: #0a0a0a; color: #e0e0e0; min-height: 100vh; display: flex; flex-direction: column; }}
    a {{ color: inherit; text-decoration: none; }}
    a.link {{ color: #60a5fa; text-decoration: underline; }}

    /* Sidebar */
    .layout {{ display: flex; min-height: 100vh; }}
    nav {{ width: 220px; background: #111; border-right: 1px solid #222; padding: 1.5rem 0; flex-shrink: 0; display: flex; flex-direction: column; }}
    .nav-brand {{ padding: 0 1.25rem 1.5rem; border-bottom: 1px solid #222; margin-bottom: 1rem; }}
    .nav-brand h1 {{ font-size: 1.1rem; color: #ff4444; font-weight: 700; }}
    .nav-brand p {{ font-size: .7rem; color: #555; margin-top: .2rem; }}
    nav a {{ display: block; padding: .6rem 1.25rem; color: #888; font-size: .88rem; border-left: 3px solid transparent; transition: all .15s; }}
    nav a:hover, nav a.active {{ color: #e0e0e0; background: #1a1a1a; border-left-color: #ff4444; }}
    .nav-section {{ font-size: .7rem; color: #444; text-transform: uppercase; letter-spacing: .08em; padding: .75rem 1.25rem .25rem; margin-top: .5rem; }}

    /* Main */
    main {{ flex: 1; padding: 2rem; overflow-y: auto; }}
    .page-header {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 1.5rem; }}
    .page-header h2 {{ font-size: 1.25rem; color: #f0f0f0; }}

    /* Cards */
    .card {{ background: #141414; border: 1px solid #222; border-radius: 8px; padding: 1.25rem; }}

    /* Forms */
    label {{ display: block; font-size: .8rem; color: #888; margin-top: .9rem; margin-bottom: .3rem; }}
    input[type=text], input[type=file], input[type=datetime-local], textarea, select {{
      width: 100%; padding: .5rem .75rem; background: #0f0f0f; border: 1px solid #333;
      border-radius: 6px; color: #e0e0e0; font-size: .88rem; outline: none;
    }}
    input:focus, textarea:focus, select:focus {{ border-color: #555; }}
    textarea {{ resize: vertical; min-height: 80px; }}

    /* Buttons */
    .btn {{ display: inline-block; padding: .5rem 1.1rem; background: #ff4444; color: #fff;
      border: none; border-radius: 6px; font-size: .88rem; cursor: pointer; font-weight: 600; }}
    .btn:hover {{ background: #cc3333; }}
    .btn-danger {{ padding: .3rem .7rem; background: transparent; color: #888; border: 1px solid #333;
      border-radius: 5px; font-size: .78rem; cursor: pointer; }}
    .btn-danger:hover {{ color: #f87171; border-color: #7a1a1a; }}

    /* Table */
    table {{ width: 100%; border-collapse: collapse; background: #141414; border: 1px solid #222; border-radius: 8px; overflow: hidden; }}
    th {{ background: #1a1a1a; color: #888; font-size: .78rem; font-weight: 600; text-transform: uppercase;
      letter-spacing: .05em; padding: .75rem 1rem; text-align: left; }}
    td {{ padding: .7rem 1rem; border-top: 1px solid #1e1e1e; font-size: .85rem; vertical-align: middle; }}
    tr:hover td {{ background: #161616; }}

    /* Badges */
    .badge {{ display: inline-block; padding: .2rem .55rem; border-radius: 4px; font-size: .72rem; font-weight: 600; }}
    .badge-yt {{ background: #3b0000; color: #ff6b6b; }}
    .badge-ig {{ background: #1a0a2e; color: #c084fc; }}
    .badge-tt {{ background: #001a1a; color: #67e8f9; }}
    .badge-pending {{ background: #1a1500; color: #fbbf24; }}
    .badge-published {{ background: #0d2b1a; color: #4ade80; }}
    .badge-failed {{ background: #2b0d0d; color: #f87171; }}
  </style>
</head>
<body>
<div class="layout">
  <nav>
    <div class="nav-brand">
      <h1>ViralDistributor</h1>
      <p>v2.0</p>
    </div>
    <div class="nav-section">Manage</div>
    <a href="/accounts" {"class='active'" if title in ("Accounts","Connect YouTube") else ""}>Accounts</a>
    <a href="/upload" {"class='active'" if title == "Upload" else ""}>Schedule Upload</a>
    <a href="/posts" {"class='active'" if title == "Posts" else ""}>Scheduled Posts</a>
    <div class="nav-section" style="margin-top:auto;">Platforms</div>
    <a style="opacity:.4;pointer-events:none;">Instagram (Phase 2)</a>
    <a style="opacity:.4;pointer-events:none;">TikTok (Phase 3)</a>
  </nav>
  <main>{content}</main>
</div>
</body>
</html>"""


def _platform_badge(platform: str) -> str:
    badges = {
        "youtube": '<span class="badge badge-yt">YouTube</span>',
        "instagram": '<span class="badge badge-ig">Instagram</span>',
        "tiktok": '<span class="badge badge-tt">TikTok</span>',
    }
    return badges.get(platform, platform)


def _status_badge(status: str) -> str:
    badges = {
        "pending": '<span class="badge badge-pending">Pending</span>',
        "published": '<span class="badge badge-published">Published</span>',
        "failed": '<span class="badge badge-failed">Failed</span>',
    }
    return badges.get(status, status)
