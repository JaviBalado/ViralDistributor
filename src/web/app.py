"""
ViralDistributor — Web Dashboard
Multi-account, multi-platform video scheduler with OAuth web flow.
Protected by HTTP Basic Auth via environment variables.
"""
import html as html_lib
import json as _json
import os
import secrets
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse, parse_qs
from zoneinfo import ZoneInfo

MADRID_TZ = ZoneInfo("Europe/Madrid")

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.db.database import get_db, init_db
from src.db.models import Account, ScheduledPost
from src.platforms.youtube   import YouTubePublisher
from src.platforms.tiktok    import TikTokPublisher
from src.platforms.instagram import InstagramPublisher
from src.services.scheduler import start_scheduler, stop_scheduler
from src.utils.logger import get_logger

load_dotenv()
logger = get_logger(__name__)

app = FastAPI(title="ViralDistributor", version="2.0.0", docs_url="/api/docs")
security = HTTPBasic()

UPLOAD_DIR = Path("videos/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# In-memory OAuth state store: { state_token: { "account_name": str, "platform": str, ... } }
_oauth_states: dict[str, dict] = {}

# Redirect URIs per platform — can be overridden via env vars
_BASE = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
YOUTUBE_REDIRECT_URI   = os.getenv("OAUTH_REDIRECT_URI",           f"{_BASE}/auth/youtube/callback")
TIKTOK_REDIRECT_URI    = os.getenv("TIKTOK_REDIRECT_URI",           f"{_BASE}/auth/tiktok/callback")
INSTAGRAM_REDIRECT_URI = os.getenv("INSTAGRAM_REDIRECT_URI",        f"{_BASE}/auth/instagram/callback")


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

    ok = (secrets.compare_digest(credentials.username, correct_username) and
          secrets.compare_digest(credentials.password, correct_password))

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
        if acc.channel_thumbnail_url:
            thumb = (f'<img src="{html_lib.escape(acc.channel_thumbnail_url)}" '
                     f'width="36" height="36" style="border-radius:50%;flex-shrink:0;" />')
        else:
            thumb = ('<div style="width:36px;height:36px;border-radius:50%;background:#222;'
                     'flex-shrink:0;display:flex;align-items:center;justify-content:center;'
                     'font-size:.7rem;color:#555;">?</div>')
        sub = (f'<div style="font-size:.72rem;color:#555;">{html_lib.escape(acc.channel_id)}</div>'
               if acc.channel_id else "")
        rows += f"""
        <tr>
          <td>{acc.id}</td>
          <td>
            <div style="display:flex;align-items:center;gap:.6rem;">
              {thumb}
              <div>{platform_badge} {html_lib.escape(acc.name)}{sub}</div>
            </div>
          </td>
          <td>{acc.created_at.strftime('%Y-%m-%d %H:%M')}</td>
          <td>
            <form method="post" action="/accounts/{acc.id}/delete" onsubmit="return confirm('Delete account?')">
              <button class="btn-danger">Delete</button>
            </form>
          </td>
        </tr>"""

    if not rows:
        rows = '<tr><td colspan="4" style="text-align:center;color:#666;">No accounts yet.</td></tr>'

    content = f"""
    <div class="page-header">
      <h2>Accounts</h2>
      <div style="display:flex;gap:.5rem;flex-wrap:wrap;">
        <a href="/accounts/connect/youtube"   class="btn">+ YouTube</a>
        <a href="/accounts/connect/tiktok"    class="btn" style="background:#111;border:1px solid #67e8f9;color:#67e8f9;">+ TikTok</a>
        <a href="/accounts/connect/instagram" class="btn" style="background:#111;border:1px solid #c084fc;color:#c084fc;">+ Instagram</a>
      </div>
    </div>
    <table>
      <thead><tr><th>#</th><th>Account</th><th>Connected</th><th>Actions</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
    <div class="card" style="margin-top:1.5rem;">
      <p style="color:#666;font-size:.82rem;">
        <strong style="color:#888;">TikTok</strong> — requires <em>video.upload</em> scope (inbox) or
        <em>video.publish</em> (direct post, needs TikTok app review).<br/>
        <strong style="color:#888;">Instagram</strong> — requires a Business/Creator account linked to a Facebook Page.
        Token valid ~60 days; reconnect when it expires.
      </p>
    </div>
    """
    return HTMLResponse(_layout("Accounts", content))


# ── YouTube OAuth ────────────────────────────────────────────────

@app.get("/accounts/connect/youtube", response_class=HTMLResponse)
async def connect_youtube_form(user: AuthDep):
    content = """
    <div class="page-header"><h2>Connect YouTube Account</h2></div>
    <div class="card" style="max-width:480px;">
      <form method="post" action="/accounts/connect/youtube">
        <label>Account name (your label, e.g. "Gaming Channel")</label>
        <input type="text" name="account_name" placeholder="My Channel" required maxlength="100" />
        <button type="submit" class="btn" style="margin-top:1rem;width:100%;">Authorize with Google</button>
      </form>
    </div>"""
    return HTMLResponse(_layout("Connect YouTube", content))


@app.post("/accounts/connect/youtube")
async def connect_youtube_start(user: AuthDep, account_name: str = Form(...)):
    state = secrets.token_urlsafe(32)
    _oauth_states[state] = {"account_name": account_name, "platform": "youtube"}
    try:
        publisher = YouTubePublisher()
        auth_url, code_verifier = publisher.get_auth_url(redirect_uri=YOUTUBE_REDIRECT_URI, state=state)
        _oauth_states[state]["code_verifier"] = code_verifier
    except Exception as e:
        logger.error(f"YouTube OAuth start failed: {e}")
        return HTMLResponse(_layout("Error", _error_card(str(e))), status_code=500)
    return RedirectResponse(url=auth_url, status_code=302)


@app.get("/auth/youtube/callback")
async def youtube_callback(request: Request, db: Session = Depends(get_db)):
    """OAuth callback — no Basic Auth (Google redirects here)."""
    code  = request.query_params.get("code")
    state = request.query_params.get("state")
    error = request.query_params.get("error")
    if error:
        return HTMLResponse(_layout("Error", _error_card(f"Authorization denied: {error}")))
    if not code or state not in _oauth_states:
        raise HTTPException(status_code=400, detail="Invalid OAuth state.")

    state_data = _oauth_states.pop(state)
    publisher  = YouTubePublisher()
    try:
        credentials_json = publisher.exchange_code(
            code=code, redirect_uri=YOUTUBE_REDIRECT_URI,
            code_verifier=state_data.get("code_verifier"),
        )
    except Exception as e:
        logger.error(f"YouTube OAuth exchange failed: {e}")
        raise HTTPException(status_code=500, detail=f"Token exchange failed: {e}")

    account = Account(name=state_data["account_name"], platform="youtube",
                      credentials_json=credentials_json)
    db.add(account)
    db.commit()

    try:
        p    = YouTubePublisher(credentials_json=credentials_json)
        info = p.get_channel_info()
        if info:
            account.channel_id            = info["channel_id"]
            account.channel_thumbnail_url = info["thumbnail_url"]
            db.commit()
    except Exception as e:
        logger.warning(f"Could not fetch YouTube channel info: {e}")

    logger.info(f"YouTube account connected: {account.name} (#{account.id})")
    return RedirectResponse(url="/accounts", status_code=302)


# ── TikTok OAuth ─────────────────────────────────────────────────

@app.get("/accounts/connect/tiktok", response_class=HTMLResponse)
async def connect_tiktok_form(user: AuthDep):
    has_keys = bool(os.getenv("TIKTOK_CLIENT_KEY") and os.getenv("TIKTOK_CLIENT_SECRET"))
    warn = "" if has_keys else """
    <div class="card" style="border-color:#4a3800;background:#1a1500;margin-bottom:1rem;">
      <p style="color:#fbbf24;font-size:.83rem;">
        ⚠ <strong>TIKTOK_CLIENT_KEY</strong> y <strong>TIKTOK_CLIENT_SECRET</strong> no están configurados.
        Añádelos al <code>.env</code> antes de continuar.
      </p>
    </div>"""
    content = f"""
    <div class="page-header"><h2>Connect TikTok Account</h2></div>
    {warn}
    <div class="card" style="max-width:480px;">
      <form method="post" action="/accounts/connect/tiktok">
        <label>Account name (your label)</label>
        <input type="text" name="account_name" placeholder="My TikTok" required maxlength="100" />
        <button type="submit" class="btn" style="margin-top:1rem;width:100%;background:#111;border:1px solid #67e8f9;color:#67e8f9;">
          Authorize with TikTok
        </button>
      </form>
      <p style="margin-top:.75rem;color:#555;font-size:.77rem;">
        Si tu app solo tiene el scope <em>video.upload</em> (sin revisión de TikTok), los vídeos
        irán a tu bandeja de entrada de TikTok y deberás publicarlos manualmente desde la app.
        Con <em>video.publish</em> (requiere revisión) se publican directamente.
      </p>
    </div>"""
    return HTMLResponse(_layout("Connect TikTok", content))


@app.post("/accounts/connect/tiktok")
async def connect_tiktok_start(user: AuthDep, account_name: str = Form(...)):
    state = secrets.token_urlsafe(32)
    _oauth_states[state] = {"account_name": account_name, "platform": "tiktok"}
    try:
        publisher = TikTokPublisher()
        auth_url, code_verifier = publisher.get_auth_url(redirect_uri=TIKTOK_REDIRECT_URI, state=state)
        _oauth_states[state]["code_verifier"] = code_verifier
    except Exception as e:
        logger.error(f"TikTok OAuth start failed: {e}")
        return HTMLResponse(_layout("Error", _error_card(str(e))), status_code=500)
    return RedirectResponse(url=auth_url, status_code=302)


@app.get("/auth/tiktok/callback")
async def tiktok_callback(request: Request, db: Session = Depends(get_db)):
    """TikTok OAuth callback — no Basic Auth."""
    code  = request.query_params.get("code")
    state = request.query_params.get("state")
    error = request.query_params.get("error")
    if error:
        return HTMLResponse(_layout("Error", _error_card(f"TikTok authorization denied: {error}")))
    if not code or state not in _oauth_states:
        raise HTTPException(status_code=400, detail="Invalid OAuth state.")

    state_data    = _oauth_states.pop(state)
    code_verifier = state_data.get("code_verifier", "")
    publisher     = TikTokPublisher()
    try:
        credentials_json = publisher.exchange_code(
            code=code, redirect_uri=TIKTOK_REDIRECT_URI, code_verifier=code_verifier,
        )
    except Exception as e:
        logger.error(f"TikTok OAuth exchange failed: {e}")
        raise HTTPException(status_code=500, detail=f"TikTok token exchange failed: {e}")

    account = Account(name=state_data["account_name"], platform="tiktok",
                      credentials_json=credentials_json)
    db.add(account)
    db.commit()

    try:
        p    = TikTokPublisher(credentials_json=credentials_json)
        info = p.get_user_info()
        if info:
            account.channel_id            = info["channel_id"]
            account.channel_thumbnail_url = info["thumbnail_url"]
            db.commit()
    except Exception as e:
        logger.warning(f"Could not fetch TikTok user info: {e}")

    logger.info(f"TikTok account connected: {account.name} (#{account.id})")
    return RedirectResponse(url="/accounts", status_code=302)


# ── Instagram OAuth ──────────────────────────────────────────────

@app.get("/accounts/connect/instagram", response_class=HTMLResponse)
async def connect_instagram_form(user: AuthDep):
    has_keys = bool(os.getenv("FACEBOOK_APP_ID") and os.getenv("FACEBOOK_APP_SECRET"))
    warn = "" if has_keys else """
    <div class="card" style="border-color:#4a3800;background:#1a1500;margin-bottom:1rem;">
      <p style="color:#fbbf24;font-size:.83rem;">
        ⚠ <strong>FACEBOOK_APP_ID</strong> y <strong>FACEBOOK_APP_SECRET</strong> no están configurados.
        Añádelos al <code>.env</code> antes de continuar.
      </p>
    </div>"""
    content = f"""
    <div class="page-header"><h2>Connect Instagram Account</h2></div>
    {warn}
    <div class="card" style="max-width:480px;">
      <form method="post" action="/accounts/connect/instagram">
        <label>Account name (your label)</label>
        <input type="text" name="account_name" placeholder="My Instagram" required maxlength="100" />
        <button type="submit" class="btn" style="margin-top:1rem;width:100%;background:#111;border:1px solid #c084fc;color:#c084fc;">
          Authorize with Facebook / Instagram
        </button>
      </form>
      <p style="margin-top:.75rem;color:#555;font-size:.77rem;">
        Necesitas una cuenta de Instagram de tipo <strong>Business o Creator</strong> vinculada a una
        Facebook Page. El token dura ~60 días; reconecta cuando expire.
      </p>
    </div>"""
    return HTMLResponse(_layout("Connect Instagram", content))


@app.post("/accounts/connect/instagram")
async def connect_instagram_start(user: AuthDep, account_name: str = Form(...)):
    state = secrets.token_urlsafe(32)
    _oauth_states[state] = {"account_name": account_name, "platform": "instagram"}
    try:
        publisher = InstagramPublisher()
        auth_url, _ = publisher.get_auth_url(redirect_uri=INSTAGRAM_REDIRECT_URI, state=state)
    except Exception as e:
        logger.error(f"Instagram OAuth start failed: {e}")
        return HTMLResponse(_layout("Error", _error_card(str(e))), status_code=500)
    return RedirectResponse(url=auth_url, status_code=302)


@app.get("/auth/instagram/callback")
async def instagram_callback(request: Request, db: Session = Depends(get_db)):
    """Instagram/Facebook OAuth callback — no Basic Auth."""
    code  = request.query_params.get("code")
    state = request.query_params.get("state")
    error = request.query_params.get("error")
    if error:
        desc = request.query_params.get("error_description", error)
        return HTMLResponse(_layout("Error", _error_card(f"Instagram authorization denied: {desc}")))
    if not code or state not in _oauth_states:
        raise HTTPException(status_code=400, detail="Invalid OAuth state.")

    state_data = _oauth_states.pop(state)
    publisher  = InstagramPublisher()
    try:
        credentials_json = publisher.exchange_code(code=code, redirect_uri=INSTAGRAM_REDIRECT_URI)
    except Exception as e:
        logger.error(f"Instagram OAuth exchange failed: {e}")
        raise HTTPException(status_code=500, detail=f"Instagram token exchange failed: {e}")

    account = Account(name=state_data["account_name"], platform="instagram",
                      credentials_json=credentials_json)
    db.add(account)
    db.commit()

    try:
        p    = InstagramPublisher(credentials_json=credentials_json)
        info = p.get_user_info()
        if info:
            account.channel_id            = info["channel_id"]
            account.channel_thumbnail_url = info["thumbnail_url"]
            db.commit()
    except Exception as e:
        logger.warning(f"Could not fetch Instagram user info: {e}")

    logger.info(f"Instagram account connected: {account.name} (#{account.id})")
    return RedirectResponse(url="/accounts", status_code=302)


# ── Account delete ───────────────────────────────────────────────

@app.post("/accounts/{account_id}/delete")
async def delete_account(account_id: int, user: AuthDep, db: Session = Depends(get_db)):
    account = db.query(Account).filter(Account.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found.")
    db.delete(account)
    db.commit()
    return RedirectResponse(url="/accounts", status_code=302)


# ------------------------------------------------------------------
# Upload / Schedule
# ------------------------------------------------------------------

@app.get("/upload", response_class=HTMLResponse)
async def upload_page(user: AuthDep, db: Session = Depends(get_db)):
    accounts = db.query(Account).order_by(Account.name).all()

    if not accounts:
        content = """
        <div class="page-header"><h2>Schedule Upload</h2></div>
        <div class="card">
          <p style="color:#888;">No accounts connected yet.
          <a href="/accounts" class="link">Connect an account</a> first.</p>
        </div>"""
        return HTMLResponse(_layout("Upload", content))

    account_options = "".join(
        f'<option value="{a.id}">[{a.platform.upper()}] {html_lib.escape(a.name)}</option>'
        for a in accounts
    )
    now_local = datetime.now(MADRID_TZ).strftime("%Y-%m-%dT%H:%M")

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
        <label>Publish date &amp; time (Madrid time)</label>
        <input type="datetime-local" name="scheduled_at" value="{now_local}" required />
        <button type="submit" class="btn" style="margin-top:1rem;width:100%;">Schedule</button>
        <div id="result" style="display:none;margin-top:1rem;padding:.75rem;border-radius:6px;"></div>
      </form>
    </div>
    <script>
      document.getElementById('uploadForm').addEventListener('submit', async (e) => {{
        e.preventDefault();
        const r   = document.getElementById('result');
        const btn = e.target.querySelector('button[type=submit]');
        r.style.display = 'none'; btn.textContent = 'Uploading...'; btn.disabled = true;
        try {{
          const res  = await fetch('/upload', {{method:'POST', body: new FormData(e.target)}});
          const data = await res.json();
          r.style.display = 'block';
          if (res.ok) {{
            r.style.cssText = 'display:block;background:#0d2b1a;border:1px solid #1a7a3a;color:#4ade80;padding:.75rem;border-radius:6px;';
            r.textContent   = '✓ Scheduled! ' + data.message;
          }} else {{
            r.style.cssText = 'display:block;background:#2b0d0d;border:1px solid #7a1a1a;color:#f87171;padding:.75rem;border-radius:6px;';
            r.textContent   = 'Error: ' + (data.detail || 'Unknown error');
          }}
        }} catch(err) {{
          r.style.cssText = 'display:block;background:#2b0d0d;border:1px solid #7a1a1a;color:#f87171;padding:.75rem;border-radius:6px;';
          r.textContent   = 'Network error: ' + err.message;
        }} finally {{ btn.textContent = 'Schedule'; btn.disabled = false; }}
      }});
    </script>"""
    return HTMLResponse(_layout("Upload", content))


@app.post("/upload")
async def schedule_upload(
    user: AuthDep,
    file: Annotated[UploadFile, File()],
    account_id: int   = Form(...),
    title: str        = Form(...),
    description: str  = Form(""),
    tags: str         = Form(""),
    scheduled_at: str = Form(...),
    db: Session       = Depends(get_db),
):
    account = db.query(Account).filter(Account.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found.")

    ext         = Path(file.filename).suffix
    unique_name = f"{uuid.uuid4().hex}{ext}"
    dest        = UPLOAD_DIR / unique_name
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        scheduled_dt = (
            datetime.fromisoformat(scheduled_at)
            .replace(tzinfo=MADRID_TZ)
            .astimezone(timezone.utc)
            .replace(tzinfo=None)
        )
    except ValueError:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Invalid date format.")

    post = ScheduledPost(account_id=account_id, title=title, description=description,
                         tags=tags, file_path=str(dest.absolute()), scheduled_at=scheduled_dt)
    db.add(post)
    db.commit()
    logger.info(f"Post scheduled: '{title}' for {scheduled_dt} on account #{account_id}")
    return JSONResponse({"success": True,
                         "message": f"Video scheduled for {scheduled_dt.strftime('%Y-%m-%d %H:%M')}."})


# ------------------------------------------------------------------
# Scheduled Posts
# ------------------------------------------------------------------

@app.get("/posts", response_class=HTMLResponse)
async def posts_page(user: AuthDep, db: Session = Depends(get_db), channel: int = Query(None)):
    all_accounts = db.query(Account).order_by(Account.name).all()

    query = db.query(ScheduledPost).order_by(ScheduledPost.scheduled_at.desc())
    if channel:
        query = query.filter(ScheduledPost.account_id == channel)
    posts = query.limit(200).all()

    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)

    # Channel filter options
    channel_opts = '<option value="">Todos los canales</option>'
    for a in all_accounts:
        sel = "selected" if channel == a.id else ""
        channel_opts += f'<option value="{a.id}" {sel}>{html_lib.escape(a.name)}</option>'

    # Bulk move options
    bulk_account_opts = '<option value="">Mover a canal...</option>'
    for a in all_accounts:
        bulk_account_opts += f'<option value="{a.id}">[{a.platform.upper()}] {html_lib.escape(a.name)}</option>'

    # For the edit modal account dropdown
    accounts_json = _json.dumps([
        {"id": a.id, "name": a.name, "platform": a.platform}
        for a in all_accounts
    ])

    # For the cross-post modal — accounts grouped by platform (excl. youtube since we're cross-posting TO other platforms)
    accounts_by_platform = {}
    for a in all_accounts:
        accounts_by_platform.setdefault(a.platform, []).append(
            {"id": a.id, "name": a.name,
             "thumbnail": a.channel_thumbnail_url or ""}
        )
    accounts_by_platform_json = _json.dumps(accounts_by_platform)

    rows = ""
    for p in posts:
        status_badge   = _status_badge(p.status)
        sched_madrid   = _to_madrid(p.scheduled_at).strftime("%d/%m/%Y %H:%M")
        account_name   = p.account.name if p.account else "—"
        account_thumb  = p.account.channel_thumbnail_url if p.account else None

        if account_thumb:
            account_cell = (f'<div style="display:flex;align-items:center;gap:.4rem;">'
                            f'<img src="{html_lib.escape(account_thumb)}" width="22" height="22" '
                            f'style="border-radius:50%;flex-shrink:0;" />'
                            f'<span>{html_lib.escape(account_name)}</span></div>')
        else:
            account_cell = html_lib.escape(account_name)

        if p.status == "pending":
            extra_cell = f'<span style="color:#fbbf24;">{_countdown(p.scheduled_at, now_utc)}</span>'
        elif p.status == "published" and p.video_url:
            extra_cell = f'<a href="{p.video_url}" target="_blank" class="link">Ver vídeo ↗</a>'
        elif p.status == "failed" and p.error_message:
            short = p.error_message[:60] + ("…" if len(p.error_message) > 60 else "")
            extra_cell = f'<span style="color:#f87171;" title="{html_lib.escape(p.error_message)}">{html_lib.escape(short)}</span>'
        else:
            extra_cell = "—"

        checkbox_cell = ""
        if p.status == "pending":
            checkbox_cell = (f'<input type="checkbox" class="post-checkbox" data-post-id="{p.id}" '
                             f'onclick="event.stopPropagation();updateBulkControls();" '
                             f'style="width:16px;height:16px;cursor:pointer;" />')

        # Actions column
        if p.status == "failed":
            actions = f"""
            <div style="display:flex;flex-direction:column;gap:.3rem;">
              <form method="post" action="/posts/{p.id}/retry">
                <button class="btn" style="padding:.3rem .7rem;font-size:.78rem;width:100%;">Reintentar ahora</button>
              </form>
              <form method="post" action="/posts/{p.id}/reschedule" style="display:flex;gap:.3rem;">
                <input type="datetime-local" name="scheduled_at" value="{_to_madrid(p.scheduled_at).strftime('%Y-%m-%dT%H:%M')}"
                  style="flex:1;padding:.25rem .4rem;font-size:.74rem;background:#0f0f0f;border:1px solid #444;border-radius:4px;color:#e0e0e0;"/>
                <button type="submit" class="btn" style="padding:.25rem .55rem;font-size:.74rem;white-space:nowrap;">Reprogramar</button>
              </form>
              <form method="post" action="/posts/{p.id}/delete" onsubmit="return confirm('¿Eliminar post?')">
                <button class="btn-danger" style="width:100%;">Eliminar</button>
              </form>
            </div>"""
        elif p.status == "pending":
            actions = f"""
            <div style="display:flex;flex-direction:column;gap:.3rem;">
              <button class="btn" disabled style="padding:.3rem .7rem;font-size:.78rem;width:100%;opacity:.35;cursor:not-allowed;">En cola...</button>
              <form method="post" action="/posts/{p.id}/delete" onsubmit="return confirm('¿Eliminar post?')">
                <button class="btn-danger" style="width:100%;">Eliminar</button>
              </form>
            </div>"""
        else:  # published
            actions = f"""
            <div style="display:flex;flex-direction:column;gap:.3rem;">
              <button onclick="openCrosspost({p.id}, {_json.dumps(p.title)})"
                class="btn" style="padding:.3rem .7rem;font-size:.78rem;width:100%;background:#111;border:1px solid #555;">
                ↗ Cross-post
              </button>
              <form method="post" action="/posts/{p.id}/delete" onsubmit="return confirm('¿Eliminar post?')">
                <button class="btn-danger" style="width:100%;">Eliminar</button>
              </form>
            </div>"""

        rows += f"""
        <tr data-post-id="{p.id}" style="cursor:pointer;" title="Click para ver/editar detalles">
          <td style="width:32px;" onclick="event.stopPropagation();">{checkbox_cell}</td>
          <td>{p.id}</td>
          <td>{account_cell}</td>
          <td style="max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{html_lib.escape(p.title)}</td>
          <td style="white-space:nowrap;">{sched_madrid}</td>
          <td>{status_badge}</td>
          <td style="font-size:.82rem;max-width:200px;">{extra_cell}</td>
          <td style="min-width:180px;" onclick="event.stopPropagation();">{actions}</td>
        </tr>"""

    if not rows:
        rows = '<tr><td colspan="8" style="text-align:center;color:#666;">No hay posts todavía.</td></tr>'

    content = f"""
    <div class="page-header">
      <h2>Posts programados</h2>
      <div style="display:flex;gap:.75rem;align-items:center;flex-wrap:wrap;">
        <span style="color:#555;font-size:.8rem;">Horario: Madrid</span>
        <form method="get" action="/posts" style="display:flex;gap:.4rem;align-items:center;">
          <select name="channel" onchange="this.form.submit()"
            style="padding:.3rem .6rem;font-size:.82rem;background:#0f0f0f;border:1px solid #333;border-radius:5px;color:#e0e0e0;">
            {channel_opts}
          </select>
          {"<button type='submit' class='btn' style='padding:.3rem .6rem;font-size:.78rem;'>Filtrar</button>"
           if not channel
           else '<a href="/posts" style="font-size:.78rem;color:#888;padding:.3rem .5rem;border:1px solid #333;border-radius:5px;">✕ Todos</a>'}
        </form>
      </div>
    </div>

    <!-- Bulk bar -->
    <div id="bulkBar" style="display:none;background:#1a1a1a;border:1px solid #333;border-radius:8px;padding:.6rem 1rem;margin-bottom:1rem;align-items:center;gap:.75rem;flex-wrap:wrap;">
      <span id="bulkCount" style="color:#888;font-size:.83rem;">0 seleccionados</span>
      <select id="bulkTarget" style="padding:.3rem .6rem;font-size:.82rem;background:#0f0f0f;border:1px solid #333;border-radius:5px;color:#e0e0e0;">
        {bulk_account_opts}
      </select>
      <button class="btn" style="padding:.35rem .8rem;font-size:.8rem;" onclick="doBulkMove()">Mover canal</button>
      <button class="btn-danger" style="padding:.35rem .8rem;" onclick="clearSelection()">Cancelar</button>
    </div>

    <div style="overflow-x:auto;">
      <table>
        <thead>
          <tr>
            <th style="width:32px;"><input type="checkbox" id="selectAll" onchange="toggleAll(this)" style="width:16px;height:16px;cursor:pointer;"/></th>
            <th>#</th><th>Cuenta</th><th>Título</th><th>Programado</th><th>Estado</th>
            <th>Resultado / Tiempo</th><th>Acciones</th>
          </tr>
        </thead>
        <tbody id="postsTable">{rows}</tbody>
      </table>
    </div>

    <!-- ── Edit / Detail Modal ────────────────────────────────── -->
    <div id="postModal" onclick="if(event.target===this)closeModal()"
      style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.82);z-index:998;overflow-y:auto;padding:1.5rem;">
      <div style="max-width:620px;margin:0 auto;background:#141414;border:1px solid #333;border-radius:12px;padding:1.5rem;position:relative;">
        <button onclick="closeModal()" style="position:absolute;top:.75rem;right:.75rem;background:none;border:none;color:#666;font-size:1.2rem;cursor:pointer;">✕</button>
        <h3 id="modalTitle" style="font-size:1.1rem;color:#f0f0f0;margin-bottom:1.1rem;">Detalles</h3>
        <div id="modalThumb" style="display:none;margin-bottom:.9rem;">
          <img id="modalThumbImg" src="" alt="thumbnail" style="width:100%;max-height:160px;object-fit:cover;border-radius:8px;border:1px solid #222;" />
        </div>
        <div id="modalStatusRow" style="display:flex;gap:.75rem;align-items:center;flex-wrap:wrap;margin-bottom:.9rem;">
          <span id="modalStatusBadge"></span>
          <a id="modalVideoLink" href="#" target="_blank" class="link" style="display:none;font-size:.82rem;">Ver en plataforma ↗</a>
          <span id="modalErrorMsg" style="color:#f87171;font-size:.8rem;display:none;"></span>
        </div>
        <div style="display:grid;gap:.1rem;">
          <label>Título</label>
          <input type="text" id="modalTitleInput" maxlength="100" />
          <label style="margin-top:.6rem;">Canal / Cuenta</label>
          <select id="modalAccount"></select>
          <label style="margin-top:.6rem;" id="modalDateLabel">Fecha programada (Madrid)</label>
          <input type="datetime-local" id="modalDate" />
          <label style="margin-top:.6rem;">Descripción</label>
          <textarea id="modalDesc" style="min-height:80px;"></textarea>
          <label style="margin-top:.6rem;">Tags (separados por coma)</label>
          <input type="text" id="modalTags" />
        </div>
        <div id="modalSaveResult" style="display:none;margin-top:.75rem;padding:.6rem;border-radius:6px;font-size:.83rem;"></div>
        <div style="display:flex;gap:.6rem;margin-top:1rem;">
          <button class="btn" id="modalSaveBtn" onclick="savePost()" style="flex:1;">Guardar cambios</button>
          <button onclick="closeModal()" style="padding:.5rem 1rem;background:#1a1a1a;border:1px solid #333;border-radius:6px;color:#888;cursor:pointer;font-size:.88rem;">Cancelar</button>
        </div>
        <p id="modalSaveNote" style="margin-top:.5rem;font-size:.73rem;color:#555;"></p>
      </div>
    </div>

    <!-- ── Cross-post Modal ───────────────────────────────────── -->
    <div id="cpModal" onclick="if(event.target===this)closeCrosspost()"
      style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.82);z-index:999;overflow-y:auto;padding:1.5rem;">
      <div style="max-width:560px;margin:0 auto;background:#141414;border:1px solid #333;border-radius:12px;padding:1.5rem;position:relative;">
        <button onclick="closeCrosspost()" style="position:absolute;top:.75rem;right:.75rem;background:none;border:none;color:#666;font-size:1.2rem;cursor:pointer;">✕</button>
        <h3 style="font-size:1.1rem;color:#f0f0f0;margin-bottom:.3rem;">↗ Cross-post</h3>
        <p id="cpTitle" style="color:#666;font-size:.82rem;margin-bottom:1.1rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;"></p>

        <!-- TikTok card -->
        <div style="background:#0a0a0a;border:1px solid #1a2a2a;border-radius:8px;padding:1rem;margin-bottom:.75rem;">
          <div style="display:flex;align-items:center;gap:.5rem;margin-bottom:.6rem;">
            <span class="badge badge-tt">TikTok</span>
          </div>
          <div id="cpTikTokBody">
            <div id="cpTikTokNoAcc" style="color:#555;font-size:.82rem;display:none;">
              Sin cuentas TikTok. <a href="/accounts/connect/tiktok" class="link">Conectar</a>
            </div>
            <select id="cpTikTokAcc" style="width:100%;padding:.35rem .6rem;font-size:.82rem;background:#0f0f0f;border:1px solid #333;border-radius:5px;color:#e0e0e0;margin-bottom:.5rem;"></select>
            <div style="display:flex;gap:.4rem;flex-wrap:wrap;">
              <button class="btn" style="padding:.3rem .7rem;font-size:.8rem;" onclick="doCrosspost('tiktok', null)">Publicar ahora</button>
              <input type="datetime-local" id="cpTikTokDate" style="flex:1;min-width:140px;padding:.3rem .5rem;font-size:.8rem;background:#0f0f0f;border:1px solid #333;border-radius:5px;color:#e0e0e0;" />
              <button class="btn" style="padding:.3rem .7rem;font-size:.8rem;" onclick="doCrosspost('tiktok', document.getElementById('cpTikTokDate').value)">Programar</button>
            </div>
          </div>
        </div>

        <!-- Instagram card -->
        <div style="background:#0a0a0a;border:1px solid #1a0a2e;border-radius:8px;padding:1rem;">
          <div style="display:flex;align-items:center;gap:.5rem;margin-bottom:.6rem;">
            <span class="badge badge-ig">Instagram</span>
            <span style="color:#555;font-size:.75rem;">Reels</span>
          </div>
          <div id="cpIgBody">
            <div id="cpIgNoAcc" style="color:#555;font-size:.82rem;display:none;">
              Sin cuentas Instagram. <a href="/accounts/connect/instagram" class="link">Conectar</a>
            </div>
            <select id="cpIgAcc" style="width:100%;padding:.35rem .6rem;font-size:.82rem;background:#0f0f0f;border:1px solid #333;border-radius:5px;color:#e0e0e0;margin-bottom:.5rem;"></select>
            <div style="display:flex;gap:.4rem;flex-wrap:wrap;">
              <button class="btn" style="padding:.3rem .7rem;font-size:.8rem;" onclick="doCrosspost('instagram', null)">Publicar ahora</button>
              <input type="datetime-local" id="cpIgDate" style="flex:1;min-width:140px;padding:.3rem .5rem;font-size:.8rem;background:#0f0f0f;border:1px solid #333;border-radius:5px;color:#e0e0e0;" />
              <button class="btn" style="padding:.3rem .7rem;font-size:.8rem;" onclick="doCrosspost('instagram', document.getElementById('cpIgDate').value)">Programar</button>
            </div>
          </div>
        </div>

        <div id="cpResult" style="display:none;margin-top:.75rem;padding:.6rem;border-radius:6px;font-size:.83rem;"></div>
      </div>
    </div>

    <script>
    const ACCOUNTS      = {accounts_json};
    const ACCTS_BY_PLAT = {accounts_by_platform_json};
    let currentPostId   = null;
    let cpPostId        = null;

    // ── Row click → edit modal ───────────────────────────────
    document.querySelectorAll('tr[data-post-id]').forEach(row => {{
      row.addEventListener('click', e => {{
        if (e.target.closest('button,form,input,a,select')) return;
        openModal(row.dataset.postId);
      }});
    }});

    async function openModal(postId) {{
      currentPostId = postId;
      document.getElementById('postModal').style.display = 'block';
      document.getElementById('modalSaveResult').style.display = 'none';
      document.getElementById('modalTitle').textContent = 'Cargando…';
      try {{
        const res = await fetch('/api/posts/' + postId);
        if (!res.ok) throw new Error('Error al cargar post');
        const d = await res.json();
        document.getElementById('modalTitle').textContent = 'Post #' + d.id;

        // Thumbnail
        const vid = extractVideoId(d.video_url);
        const tDiv = document.getElementById('modalThumb');
        if (vid && d.video_url && d.video_url.includes('youtube')) {{
          document.getElementById('modalThumbImg').src = 'https://img.youtube.com/vi/' + vid + '/mqdefault.jpg';
          tDiv.style.display = 'block';
        }} else {{ tDiv.style.display = 'none'; }}

        // Status
        const badges = {{
          pending:   '<span class="badge badge-pending">Pending</span>',
          published: '<span class="badge badge-published">Published</span>',
          failed:    '<span class="badge badge-failed">Failed</span>',
        }};
        document.getElementById('modalStatusBadge').innerHTML = badges[d.status] || d.status;
        const vl = document.getElementById('modalVideoLink');
        if (d.video_url) {{ vl.href = d.video_url; vl.style.display = 'inline'; }}
        else {{ vl.style.display = 'none'; }}
        const em = document.getElementById('modalErrorMsg');
        if (d.error_message) {{ em.textContent = d.error_message; em.style.display = 'inline'; }}
        else {{ em.style.display = 'none'; }}

        // Fields
        document.getElementById('modalTitleInput').value = d.title || '';
        document.getElementById('modalDesc').value       = d.description || '';
        document.getElementById('modalTags').value       = d.tags || '';
        document.getElementById('modalDate').value       = d.scheduled_at || '';

        // Account dropdown
        const sel = document.getElementById('modalAccount');
        sel.innerHTML = '';
        ACCOUNTS.forEach(a => {{
          const o = document.createElement('option');
          o.value = a.id;
          o.textContent = '[' + a.platform.toUpperCase() + '] ' + a.name;
          if (a.id === d.account_id) o.selected = true;
          sel.appendChild(o);
        }});

        const isPending = d.status === 'pending' || d.status === 'failed';
        document.getElementById('modalDate').disabled    = !isPending;
        document.getElementById('modalDateLabel').style.color = isPending ? '#888' : '#444';
        sel.disabled = !isPending;

        const note = document.getElementById('modalSaveNote');
        note.textContent = d.status === 'published'
          ? 'Post publicado — guardar intentará actualizar título, descripción y tags en la plataforma.'
          : '';
        document.getElementById('modalSaveBtn').textContent = d.status === 'published'
          ? 'Guardar y actualizar en plataforma'
          : 'Guardar cambios';
      }} catch(e) {{
        document.getElementById('modalTitle').textContent = 'Error: ' + e.message;
      }}
    }}

    function closeModal() {{
      document.getElementById('postModal').style.display = 'none';
      currentPostId = null;
    }}

    async function savePost() {{
      if (!currentPostId) return;
      const btn = document.getElementById('modalSaveBtn');
      const res = document.getElementById('modalSaveResult');
      btn.disabled = true; btn.textContent = 'Guardando…'; res.style.display = 'none';
      const payload = {{
        title:        document.getElementById('modalTitleInput').value,
        description:  document.getElementById('modalDesc').value,
        tags:         document.getElementById('modalTags').value,
        scheduled_at: document.getElementById('modalDate').value,
        account_id:   parseInt(document.getElementById('modalAccount').value),
      }};
      try {{
        const r    = await fetch('/api/posts/' + currentPostId + '/update', {{
          method: 'POST', headers: {{'Content-Type':'application/json'}}, body: JSON.stringify(payload),
        }});
        const data = await r.json();
        res.style.display = 'block';
        if (r.ok) {{
          if (data.updated_on_youtube || data.updated_on_platform) {{
            res.style.cssText = 'display:block;background:#0d2b1a;border:1px solid #1a7a3a;color:#4ade80;padding:.6rem;border-radius:6px;font-size:.83rem;';
            res.textContent   = '✓ Guardado y actualizado en la plataforma.';
          }} else if (data.youtube_error || data.platform_error) {{
            res.style.cssText = 'display:block;background:#1a1500;border:1px solid #4a3800;color:#fbbf24;padding:.6rem;border-radius:6px;font-size:.83rem;';
            res.textContent   = '✓ Guardado localmente. Plataforma: ' + (data.youtube_error || data.platform_error);
          }} else {{
            res.style.cssText = 'display:block;background:#0d2b1a;border:1px solid #1a7a3a;color:#4ade80;padding:.6rem;border-radius:6px;font-size:.83rem;';
            res.textContent   = '✓ Cambios guardados.';
          }}
          const row = document.querySelector('tr[data-post-id="' + currentPostId + '"]');
          if (row && row.cells[3]) row.cells[3].textContent = payload.title;
          setTimeout(() => closeModal(), 1400);
        }} else {{
          res.style.cssText = 'display:block;background:#2b0d0d;border:1px solid #7a1a1a;color:#f87171;padding:.6rem;border-radius:6px;font-size:.83rem;';
          res.textContent   = 'Error: ' + (data.detail || JSON.stringify(data));
        }}
      }} catch(err) {{
        res.style.cssText = 'display:block;background:#2b0d0d;border:1px solid #7a1a1a;color:#f87171;padding:.6rem;border-radius:6px;font-size:.83rem;';
        res.textContent   = 'Network error: ' + err.message;
        res.style.display = 'block';
      }} finally {{ btn.disabled = false; btn.textContent = 'Guardar cambios'; }}
    }}

    // ── Bulk select ───────────────────────────────────────────
    function toggleAll(cb) {{
      document.querySelectorAll('.post-checkbox').forEach(c => c.checked = cb.checked);
      updateBulkControls();
    }}
    function updateBulkControls() {{
      const checked = document.querySelectorAll('.post-checkbox:checked');
      document.getElementById('bulkCount').textContent =
        checked.length + ' seleccionado' + (checked.length !== 1 ? 's' : '');
      document.getElementById('bulkBar').style.display = checked.length > 0 ? 'flex' : 'none';
    }}
    function clearSelection() {{
      document.querySelectorAll('.post-checkbox').forEach(c => c.checked = false);
      document.getElementById('selectAll').checked = false;
      updateBulkControls();
    }}
    async function doBulkMove() {{
      const targetId = parseInt(document.getElementById('bulkTarget').value);
      if (!targetId) {{ alert('Selecciona un canal de destino'); return; }}
      const ids      = Array.from(document.querySelectorAll('.post-checkbox:checked'))
                           .map(c => parseInt(c.dataset.postId));
      if (!ids.length) return;
      const name = document.getElementById('bulkTarget').selectedOptions[0].textContent;
      if (!confirm('¿Mover ' + ids.length + ' post(s) al canal "' + name + '"?')) return;
      try {{
        const res  = await fetch('/api/posts/bulk-move', {{
          method: 'POST', headers: {{'Content-Type':'application/json'}},
          body: JSON.stringify({{post_ids: ids, target_account_id: targetId}}),
        }});
        const data = await res.json();
        if (res.ok) {{ alert('✓ ' + data.moved + ' post(s) movidos.'); location.reload(); }}
        else        {{ alert('Error: ' + (data.detail || JSON.stringify(data))); }}
      }} catch(e) {{ alert('Network error: ' + e.message); }}
    }}

    // ── Cross-post modal ──────────────────────────────────────
    function openCrosspost(postId, title) {{
      cpPostId = postId;
      document.getElementById('cpTitle').textContent = title;
      document.getElementById('cpResult').style.display = 'none';

      // Set default datetime to now + 5 min
      const dt = new Date(Date.now() + 5 * 60000);
      const pad = n => String(n).padStart(2, '0');
      const dtStr = dt.getFullYear() + '-' + pad(dt.getMonth()+1) + '-' + pad(dt.getDate()) +
                    'T' + pad(dt.getHours()) + ':' + pad(dt.getMinutes());
      document.getElementById('cpTikTokDate').value = dtStr;
      document.getElementById('cpIgDate').value     = dtStr;

      // Populate TikTok accounts
      const ttAccs = ACCTS_BY_PLAT['tiktok'] || [];
      const ttSel  = document.getElementById('cpTikTokAcc');
      ttSel.innerHTML = '';
      if (ttAccs.length) {{
        ttAccs.forEach(a => {{
          const o = document.createElement('option'); o.value = a.id; o.textContent = a.name; ttSel.appendChild(o);
        }});
        ttSel.style.display = 'block';
        document.getElementById('cpTikTokNoAcc').style.display = 'none';
      }} else {{
        ttSel.style.display = 'none';
        document.getElementById('cpTikTokNoAcc').style.display = 'block';
      }}

      // Populate Instagram accounts
      const igAccs = ACCTS_BY_PLAT['instagram'] || [];
      const igSel  = document.getElementById('cpIgAcc');
      igSel.innerHTML = '';
      if (igAccs.length) {{
        igAccs.forEach(a => {{
          const o = document.createElement('option'); o.value = a.id; o.textContent = a.name; igSel.appendChild(o);
        }});
        igSel.style.display = 'block';
        document.getElementById('cpIgNoAcc').style.display = 'none';
      }} else {{
        igSel.style.display = 'none';
        document.getElementById('cpIgNoAcc').style.display = 'block';
      }}

      document.getElementById('cpModal').style.display = 'block';
    }}

    function closeCrosspost() {{
      document.getElementById('cpModal').style.display = 'none';
      cpPostId = null;
    }}

    async function doCrosspost(platform, scheduledAt) {{
      if (!cpPostId) return;
      const accSel   = document.getElementById(platform === 'tiktok' ? 'cpTikTokAcc' : 'cpIgAcc');
      const accountId = parseInt(accSel.value);
      if (!accountId) {{ alert('No hay cuentas de ' + platform + ' conectadas.'); return; }}
      const res = document.getElementById('cpResult');
      res.style.display = 'none';
      try {{
        const r = await fetch('/api/posts/' + cpPostId + '/crosspost', {{
          method: 'POST', headers: {{'Content-Type':'application/json'}},
          body: JSON.stringify({{platform, account_id: accountId, scheduled_at: scheduledAt || null}}),
        }});
        const data = await r.json();
        res.style.display = 'block';
        if (r.ok) {{
          res.style.cssText = 'display:block;background:#0d2b1a;border:1px solid #1a7a3a;color:#4ade80;padding:.6rem;border-radius:6px;font-size:.83rem;';
          res.textContent   = '✓ ' + data.message + ' (Post #' + data.new_post_id + ')';
          setTimeout(() => closeCrosspost(), 1800);
        }} else {{
          res.style.cssText = 'display:block;background:#2b0d0d;border:1px solid #7a1a1a;color:#f87171;padding:.6rem;border-radius:6px;font-size:.83rem;';
          res.textContent   = 'Error: ' + (data.detail || JSON.stringify(data));
        }}
      }} catch(e) {{
        res.style.cssText = 'display:block;background:#2b0d0d;border:1px solid #7a1a1a;color:#f87171;padding:.6rem;border-radius:6px;font-size:.83rem;';
        res.textContent   = 'Network error: ' + e.message;
        res.style.display = 'block';
      }}
    }}

    function extractVideoId(url) {{
      if (!url) return null;
      let m = url.match(/youtube\\.com\\/shorts\\/([^?&]+)/);
      if (m) return m[1];
      m = url.match(/[?&]v=([^&]+)/);
      if (m) return m[1];
      return null;
    }}

    document.addEventListener('keydown', e => {{
      if (e.key === 'Escape') {{ closeModal(); closeCrosspost(); }}
    }});
    </script>
    """
    return HTMLResponse(_layout("Posts", content))


@app.post("/posts/{post_id}/retry")
async def retry_post(post_id: int, user: AuthDep, db: Session = Depends(get_db)):
    from datetime import timedelta
    post = db.query(ScheduledPost).filter(ScheduledPost.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found.")
    post.status        = "pending"
    post.error_message = None
    post.scheduled_at  = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=1)
    db.commit()
    return RedirectResponse(url="/posts", status_code=302)


@app.post("/posts/{post_id}/reschedule")
async def reschedule_post(post_id: int, user: AuthDep,
                          scheduled_at: str = Form(...), db: Session = Depends(get_db)):
    post = db.query(ScheduledPost).filter(ScheduledPost.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found.")
    try:
        new_dt = (datetime.fromisoformat(scheduled_at)
                  .replace(tzinfo=MADRID_TZ).astimezone(timezone.utc).replace(tzinfo=None))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format.")
    post.scheduled_at  = new_dt
    post.status        = "pending"
    post.error_message = None
    db.commit()
    return RedirectResponse(url="/posts", status_code=302)


@app.post("/posts/{post_id}/delete")
async def delete_post(post_id: int, user: AuthDep, db: Session = Depends(get_db)):
    post = db.query(ScheduledPost).filter(ScheduledPost.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found.")
    if post.status != "published":
        Path(post.file_path).unlink(missing_ok=True)
    db.delete(post)
    db.commit()
    return RedirectResponse(url="/posts", status_code=302)


@app.get("/health")
async def health():
    return {"status": "ok"}


# ------------------------------------------------------------------
# JSON API
# ------------------------------------------------------------------

@app.get("/api/accounts")
async def api_list_accounts(user: AuthDep, db: Session = Depends(get_db)):
    return [
        {"id": a.id, "name": a.name, "platform": a.platform,
         "channel_id": a.channel_id, "channel_thumbnail_url": a.channel_thumbnail_url,
         "created_at": a.created_at.isoformat()}
        for a in db.query(Account).order_by(Account.name).all()
    ]


@app.get("/api/posts/{post_id}")
async def api_get_post(post_id: int, user: AuthDep, db: Session = Depends(get_db)):
    post = db.query(ScheduledPost).filter(ScheduledPost.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found.")
    return {
        "id": post.id, "account_id": post.account_id,
        "account_name": post.account.name if post.account else None,
        "title": post.title, "description": post.description, "tags": post.tags,
        "scheduled_at": _to_madrid(post.scheduled_at).strftime("%Y-%m-%dT%H:%M"),
        "status": post.status, "video_url": post.video_url,
        "error_message": post.error_message, "file_path": post.file_path,
    }


class PostUpdatePayload(BaseModel):
    title: str | None        = None
    description: str | None  = None
    tags: str | None         = None
    scheduled_at: str | None = None
    account_id: int | None   = None


@app.post("/api/posts/{post_id}/update")
async def update_post(post_id: int, payload: PostUpdatePayload,
                      user: AuthDep, db: Session = Depends(get_db)):
    post = db.query(ScheduledPost).filter(ScheduledPost.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found.")

    if payload.title       is not None: post.title       = payload.title
    if payload.description is not None: post.description = payload.description
    if payload.tags        is not None: post.tags        = payload.tags
    if payload.account_id  is not None and post.status in ("pending", "failed"):
        if not db.query(Account).filter(Account.id == payload.account_id).first():
            raise HTTPException(status_code=404, detail="Target account not found.")
        post.account_id = payload.account_id
    if payload.scheduled_at and post.status in ("pending", "failed"):
        try:
            post.scheduled_at = (datetime.fromisoformat(payload.scheduled_at)
                                 .replace(tzinfo=MADRID_TZ).astimezone(timezone.utc).replace(tzinfo=None))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format.")
    db.commit()

    updated_on_youtube = False
    youtube_error      = None
    if post.status == "published" and post.video_url and post.account:
        video_id = _extract_video_id(post.video_url)
        if video_id and post.account.platform == "youtube":
            try:
                pub     = YouTubePublisher(credentials_json=post.account.credentials_json)
                tags    = [t.strip() for t in post.tags.split(",") if t.strip()] if post.tags else []
                ok, err = pub.update_video(video_id, post.title, post.description or "", tags)
                if ok:
                    updated_on_youtube = True
                    post.account.credentials_json = pub.get_updated_credentials_json()
                    db.commit()
                else:
                    youtube_error = err
            except Exception as e:
                youtube_error = str(e)

    return JSONResponse({"success": True,
                         "updated_on_youtube": updated_on_youtube,
                         "youtube_error": youtube_error})


class BulkMovePayload(BaseModel):
    post_ids: list[int]
    target_account_id: int


@app.post("/api/posts/bulk-move")
async def bulk_move_posts(payload: BulkMovePayload, user: AuthDep, db: Session = Depends(get_db)):
    if not db.query(Account).filter(Account.id == payload.target_account_id).first():
        raise HTTPException(status_code=404, detail="Target account not found.")
    moved = 0
    for pid in payload.post_ids:
        post = db.query(ScheduledPost).filter(
            ScheduledPost.id == pid, ScheduledPost.status == "pending").first()
        if post:
            post.account_id = payload.target_account_id
            moved += 1
    db.commit()
    return JSONResponse({"success": True, "moved": moved})


class CrosspostPayload(BaseModel):
    platform: str
    account_id: int
    scheduled_at: str | None = None  # Madrid time ISO; None = now + 1 min


@app.post("/api/posts/{post_id}/crosspost")
async def crosspost_post(post_id: int, payload: CrosspostPayload,
                         user: AuthDep, db: Session = Depends(get_db)):
    from datetime import timedelta
    original = db.query(ScheduledPost).filter(ScheduledPost.id == post_id).first()
    if not original:
        raise HTTPException(status_code=404, detail="Original post not found.")
    if not Path(original.file_path).exists():
        raise HTTPException(status_code=400,
                            detail="El fichero de vídeo ya no existe en disco. No se puede hacer cross-post.")

    target = db.query(Account).filter(
        Account.id == payload.account_id, Account.platform == payload.platform).first()
    if not target:
        raise HTTPException(status_code=404,
                            detail=f"No hay ninguna cuenta de {payload.platform} con ese ID.")

    if payload.scheduled_at:
        try:
            sched_dt = (datetime.fromisoformat(payload.scheduled_at)
                        .replace(tzinfo=MADRID_TZ).astimezone(timezone.utc).replace(tzinfo=None))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format.")
    else:
        sched_dt = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=1)

    new_post = ScheduledPost(
        account_id=payload.account_id,
        title=original.title,
        description=original.description,
        tags=original.tags,
        file_path=original.file_path,
        scheduled_at=sched_dt,
    )
    db.add(new_post)
    db.commit()
    logger.info(f"Cross-post #{new_post.id} created from #{post_id} → {payload.platform} account #{payload.account_id}")
    return JSONResponse({
        "success": True,
        "new_post_id": new_post.id,
        "message": f"Post programado para {payload.platform} en {sched_dt.strftime('%Y-%m-%d %H:%M')} UTC.",
    })


# ------------------------------------------------------------------
# Queue import
# ------------------------------------------------------------------

class QueuePostItem(BaseModel):
    file_path: str
    title: str
    description: str = ""
    tags: str        = ""
    publish_date: str = ""


class QueueImportPayload(BaseModel):
    account_id: int
    posts: list[QueuePostItem]


@app.get("/queue/import-ui", response_class=HTMLResponse)
async def import_queue_page(user: AuthDep, db: Session = Depends(get_db)):
    accounts = db.query(Account).order_by(Account.name).all()
    account_options = "".join(
        f'<option value="{a.id}">[{a.platform.upper()}] {html_lib.escape(a.name)}</option>'
        for a in accounts
    ) or '<option value="">— No accounts connected —</option>'

    content = f"""
    <div class="page-header"><h2>Import Queue — JSON</h2></div>
    <div class="card" style="max-width:680px;">
      <p style="color:#888;font-size:.85rem;margin-bottom:1rem;">
        Importa una lista de vídeos ya generados vía <code>POST /api/queue/import</code>
        o pegando el JSON aquí.
      </p>
      <label>Cuenta de destino</label>
      <select id="accountId">{account_options}</select>
      <label>JSON de cola</label>
      <textarea id="queueJson" style="min-height:200px;font-family:monospace;font-size:.8rem;"
        placeholder='{{"account_id":null,"posts":[{{"file_path":"/ruta/video.mp4","title":"Mi Short","description":"","tags":"shorts,viral","publish_date":"2026-03-15T18:00:00"}}]}}'></textarea>
      <button class="btn" style="margin-top:1rem;width:100%;" onclick="importQueue()">Importar cola</button>
      <div id="result" style="display:none;margin-top:1rem;padding:.75rem;border-radius:6px;"></div>
    </div>
    <script>
    async function importQueue() {{
      const r = document.getElementById('result');
      r.style.display = 'none';
      let raw;
      try {{ raw = JSON.parse(document.getElementById('queueJson').value); }}
      catch(e) {{ r.style.cssText='display:block;background:#2b0d0d;border:1px solid #7a1a1a;color:#f87171;padding:.75rem;border-radius:6px;'; r.textContent='JSON inválido: '+e.message; return; }}
      raw.account_id = parseInt(document.getElementById('accountId').value);
      try {{
        const res  = await fetch('/api/queue/import', {{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(raw)}});
        const data = await res.json();
        r.style.display = 'block';
        if (res.ok) {{
          r.style.cssText = 'display:block;background:#0d2b1a;border:1px solid #1a7a3a;color:#4ade80;padding:.75rem;border-radius:6px;';
          r.textContent   = data.message + ' IDs: ' + (data.post_ids||[]).join(', ');
        }} else {{
          r.style.cssText = 'display:block;background:#2b0d0d;border:1px solid #7a1a1a;color:#f87171;padding:.75rem;border-radius:6px;';
          r.textContent   = 'Error: '+(data.detail||JSON.stringify(data));
        }}
      }} catch(err) {{
        r.style.cssText = 'display:block;background:#2b0d0d;border:1px solid #7a1a1a;color:#f87171;padding:.75rem;border-radius:6px;';
        r.textContent   = 'Network error: '+err.message;
      }}
    }}
    </script>"""
    return HTMLResponse(_layout("Import Queue", content))


@app.post("/api/queue/import")
async def import_queue(payload: QueueImportPayload, user: AuthDep, db: Session = Depends(get_db)):
    from datetime import timedelta
    account = db.query(Account).filter(Account.id == payload.account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail=f"Account #{payload.account_id} not found.")

    created_ids, errors = [], []
    for item in payload.posts:
        if not Path(item.file_path).is_file():
            errors.append({"file_path": item.file_path, "error": "File not found"}); continue
        if item.publish_date:
            try:
                sched_dt = (datetime.fromisoformat(item.publish_date)
                            .replace(tzinfo=MADRID_TZ).astimezone(timezone.utc).replace(tzinfo=None))
            except ValueError:
                errors.append({"file_path": item.file_path, "error": f"Invalid publish_date"}); continue
        else:
            sched_dt = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=1)

        post = ScheduledPost(account_id=payload.account_id, title=item.title,
                             description=item.description, tags=item.tags,
                             file_path=str(Path(item.file_path).absolute()), scheduled_at=sched_dt)
        db.add(post); db.flush(); created_ids.append(post.id)
    db.commit()
    return JSONResponse({"success": True, "created": len(created_ids), "post_ids": created_ids,
                         "errors": errors, "message": f"{len(created_ids)} vídeos programados, {len(errors)} errores."})


@app.get("/debug", response_class=HTMLResponse)
async def debug_page(user: AuthDep):
    checks = []
    for key, label in [("YOUTUBE_CLIENT_ID", "YouTube Client ID"),
                       ("YOUTUBE_CLIENT_SECRET", "YouTube Client Secret"),
                       ("TIKTOK_CLIENT_KEY", "TikTok Client Key"),
                       ("TIKTOK_CLIENT_SECRET", "TikTok Client Secret"),
                       ("FACEBOOK_APP_ID", "Facebook App ID"),
                       ("FACEBOOK_APP_SECRET", "Facebook App Secret"),
                       ("OAUTH_REDIRECT_URI", "YouTube Redirect URI"),
                       ("TIKTOK_REDIRECT_URI", "TikTok Redirect URI"),
                       ("INSTAGRAM_REDIRECT_URI", "Instagram Redirect URI"),
                       ("DATABASE_URL", "Database URL"),
                       ("DASHBOARD_USERNAME", "Dashboard Username"),
                       ("DASHBOARD_PASSWORD", "Dashboard Password")]:
        val = os.getenv(key, "")
        st  = "ok" if val else "warn"
        disp = (val[:25] + "…" if len(val) > 25 else val) if val and "SECRET" not in key and "PASSWORD" not in key else ("Set" if val else "NOT SET")
        checks.append((label, st, disp))

    rows = ""
    for name, st, detail in checks:
        color = {"ok": "#4ade80", "warn": "#fbbf24", "error": "#f87171"}[st]
        icon  = {"ok": "OK", "warn": "—", "error": "ERR"}[st]
        rows += f"<tr><td>{name}</td><td><span class='badge' style='background:#1a1a1a;color:{color};'>{icon}</span></td><td style='color:#aaa;font-size:.8rem;'>{html_lib.escape(detail)}</td></tr>"

    content = f"""
    <div class="page-header"><h2>Debug — Configuration</h2></div>
    <table><thead><tr><th>Variable</th><th>Status</th><th>Value</th></tr></thead>
    <tbody>{rows}</tbody></table>"""
    return HTMLResponse(_layout("Debug", content))


# ------------------------------------------------------------------
# HTML helpers
# ------------------------------------------------------------------

def _to_madrid(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc).astimezone(MADRID_TZ)


def _countdown(scheduled_at: datetime, now_utc: datetime) -> str:
    diff  = scheduled_at - now_utc
    total = int(diff.total_seconds())
    if total <= 0:
        return "Publicando…"
    days    = total // 86400
    hours   = (total % 86400) // 3600
    minutes = (total % 3600) // 60
    parts   = []
    if days:    parts.append(f"{days} día{'s' if days != 1 else ''}")
    if hours:   parts.append(f"{hours} hora{'s' if hours != 1 else ''}")
    if minutes and not days: parts.append(f"{minutes} min")
    return ", ".join(parts) or "Menos de 1 min"


def _extract_video_id(video_url: str) -> str | None:
    if not video_url:
        return None
    if "youtube.com/shorts/" in video_url:
        return video_url.split("youtube.com/shorts/")[-1].split("?")[0]
    if "youtube.com/watch" in video_url:
        params = parse_qs(urlparse(video_url).query)
        return params.get("v", [None])[0]
    if "youtu.be/" in video_url:
        return video_url.split("youtu.be/")[-1].split("?")[0]
    return None


def _error_card(msg: str) -> str:
    return f"""
    <div class="page-header"><h2>Error</h2></div>
    <div class="card">
      <pre style="background:#0f0f0f;padding:1rem;border-radius:6px;color:#f87171;font-size:.8rem;white-space:pre-wrap;">{html_lib.escape(msg)}</pre>
      <a href="/accounts" class="btn" style="display:inline-block;margin-top:1rem;">Back to Accounts</a>
    </div>"""


def _layout(title: str, content: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>{html_lib.escape(title)} — ViralDistributor</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: system-ui, sans-serif; background: #0a0a0a; color: #e0e0e0; min-height: 100vh; }}
    a {{ color: inherit; text-decoration: none; }}
    a.link {{ color: #60a5fa; text-decoration: underline; }}

    .layout {{ display: flex; min-height: 100vh; }}
    nav {{ width: 220px; background: #111; border-right: 1px solid #222; padding: 1.5rem 0; flex-shrink: 0; display: flex; flex-direction: column; }}
    .nav-brand {{ padding: 0 1.25rem 1.5rem; border-bottom: 1px solid #222; margin-bottom: 1rem; }}
    .nav-brand h1 {{ font-size: 1.1rem; color: #ff4444; font-weight: 700; }}
    .nav-brand p  {{ font-size: .7rem; color: #555; margin-top: .2rem; }}
    nav a {{ display: block; padding: .6rem 1.25rem; color: #888; font-size: .88rem; border-left: 3px solid transparent; transition: all .15s; }}
    nav a:hover, nav a.active {{ color: #e0e0e0; background: #1a1a1a; border-left-color: #ff4444; }}
    .nav-section {{ font-size: .7rem; color: #444; text-transform: uppercase; letter-spacing: .08em; padding: .75rem 1.25rem .25rem; margin-top: .5rem; }}

    main {{ flex: 1; padding: 2rem; overflow-y: auto; }}
    .page-header {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 1.5rem; flex-wrap: wrap; gap: .75rem; }}
    .page-header h2 {{ font-size: 1.25rem; color: #f0f0f0; }}

    .card {{ background: #141414; border: 1px solid #222; border-radius: 8px; padding: 1.25rem; }}

    label {{ display: block; font-size: .8rem; color: #888; margin-top: .9rem; margin-bottom: .3rem; }}
    input[type=text], input[type=file], input[type=datetime-local], textarea, select {{
      width: 100%; padding: .5rem .75rem; background: #0f0f0f; border: 1px solid #333;
      border-radius: 6px; color: #e0e0e0; font-size: .88rem; outline: none;
    }}
    input:focus, textarea:focus, select:focus {{ border-color: #555; }}
    textarea {{ resize: vertical; min-height: 80px; }}

    .btn {{ display: inline-block; padding: .5rem 1.1rem; background: #ff4444; color: #fff;
      border: none; border-radius: 6px; font-size: .88rem; cursor: pointer; font-weight: 600; }}
    .btn:hover {{ background: #cc3333; }}
    .btn-danger {{ padding: .3rem .7rem; background: transparent; color: #888; border: 1px solid #333;
      border-radius: 5px; font-size: .78rem; cursor: pointer; }}
    .btn-danger:hover {{ color: #f87171; border-color: #7a1a1a; }}

    table {{ width: 100%; border-collapse: collapse; background: #141414; border: 1px solid #222; border-radius: 8px; overflow: hidden; }}
    th {{ background: #1a1a1a; color: #888; font-size: .78rem; font-weight: 600; text-transform: uppercase; letter-spacing: .05em; padding: .75rem 1rem; text-align: left; }}
    td {{ padding: .7rem 1rem; border-top: 1px solid #1e1e1e; font-size: .85rem; vertical-align: middle; }}
    tr:hover td {{ background: #161616; }}

    .badge {{ display: inline-block; padding: .2rem .55rem; border-radius: 4px; font-size: .72rem; font-weight: 600; }}
    .badge-yt  {{ background: #3b0000; color: #ff6b6b; }}
    .badge-ig  {{ background: #1a0a2e; color: #c084fc; }}
    .badge-tt  {{ background: #001a1a; color: #67e8f9; }}
    .badge-pending   {{ background: #1a1500; color: #fbbf24; }}
    .badge-published {{ background: #0d2b1a; color: #4ade80; }}
    .badge-failed    {{ background: #2b0d0d; color: #f87171; }}

    #postModal input:disabled, #postModal select:disabled {{ opacity: .4; cursor: not-allowed; }}
  </style>
</head>
<body>
<div class="layout">
  <nav>
    <div class="nav-brand"><h1>ViralDistributor</h1><p>v2.0</p></div>
    <div class="nav-section">Manage</div>
    <a href="/accounts"       {"class='active'" if title in ("Accounts","Connect YouTube","Connect TikTok","Connect Instagram") else ""}>Accounts</a>
    <a href="/upload"         {"class='active'" if title == "Upload"       else ""}>Schedule Upload</a>
    <a href="/posts"          {"class='active'" if title == "Posts"        else ""}>Scheduled Posts</a>
    <a href="/queue/import-ui"{"class='active'" if title == "Import Queue" else ""}>Import Queue (JSON)</a>
    <div class="nav-section">Tools</div>
    <a href="/debug"          {"class='active'" if title == "Debug"        else ""}>Debug / Config</a>
  </nav>
  <main>{content}</main>
</div>
</body>
</html>"""


def _platform_badge(platform: str) -> str:
    return {
        "youtube":   '<span class="badge badge-yt">YouTube</span>',
        "instagram": '<span class="badge badge-ig">Instagram</span>',
        "tiktok":    '<span class="badge badge-tt">TikTok</span>',
    }.get(platform, platform)


def _status_badge(status: str) -> str:
    return {
        "pending":   '<span class="badge badge-pending">Pending</span>',
        "published": '<span class="badge badge-published">Published</span>',
        "failed":    '<span class="badge badge-failed">Failed</span>',
    }.get(status, status)
