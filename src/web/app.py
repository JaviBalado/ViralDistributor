"""
ViralDistributor — Web Dashboard
Multi-account, multi-platform video scheduler with OAuth web flow.
Protected by HTTP Basic Auth via environment variables.
"""
import html as html_lib
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
        if acc.channel_thumbnail_url:
            thumb = f'<img src="{html_lib.escape(acc.channel_thumbnail_url)}" width="36" height="36" style="border-radius:50%;flex-shrink:0;" />'
        else:
            thumb = '<div style="width:36px;height:36px;border-radius:50%;background:#222;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:.7rem;color:#555;">?</div>'
        rows += f"""
        <tr>
          <td>{acc.id}</td>
          <td>
            <div style="display:flex;align-items:center;gap:.6rem;">
              {thumb}
              <div>
                <div>{platform_badge} {html_lib.escape(acc.name)}</div>
                {'<div style="font-size:.72rem;color:#555;">'+html_lib.escape(acc.channel_id)+'</div>' if acc.channel_id else ''}
              </div>
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
        auth_url, code_verifier = publisher.get_auth_url(redirect_uri=OAUTH_REDIRECT_URI, state=state)
        _oauth_states[state]["code_verifier"] = code_verifier
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
        credentials_json = publisher.exchange_code(
            code=code,
            redirect_uri=OAUTH_REDIRECT_URI,
            code_verifier=state_data.get("code_verifier"),
        )
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

    # Fetch channel info (thumbnail, channel ID)
    try:
        channel_publisher = YouTubePublisher(credentials_json=credentials_json)
        channel_info = channel_publisher.get_channel_info()
        if channel_info:
            account.channel_id = channel_info["channel_id"]
            account.channel_thumbnail_url = channel_info["thumbnail_url"]
            db.commit()
            logger.info(f"Channel info saved for account #{account.id}: {channel_info['channel_id']}")
    except Exception as e:
        logger.warning(f"Could not fetch channel info for account #{account.id}: {e}")

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
        f'<option value="{a.id}">[{a.platform.upper()}] {html_lib.escape(a.name)}</option>' for a in accounts
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

    ext = Path(file.filename).suffix
    unique_name = f"{uuid.uuid4().hex}{ext}"
    dest = UPLOAD_DIR / unique_name
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
async def posts_page(user: AuthDep, db: Session = Depends(get_db), channel: int = Query(None)):
    all_accounts = db.query(Account).order_by(Account.name).all()

    query = db.query(ScheduledPost).order_by(ScheduledPost.scheduled_at.desc())
    if channel:
        query = query.filter(ScheduledPost.account_id == channel)
    posts = query.limit(200).all()

    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)

    # Channel filter dropdown
    channel_opts = '<option value="">Todos los canales</option>'
    for a in all_accounts:
        sel = 'selected' if channel == a.id else ''
        channel_opts += f'<option value="{a.id}" {sel}>{html_lib.escape(a.name)}</option>'

    # Bulk move account options
    bulk_account_opts = '<option value="">Mover a canal...</option>'
    for a in all_accounts:
        bulk_account_opts += f'<option value="{a.id}">{html_lib.escape(a.name)}</option>'

    # Accounts JSON for modal dropdown
    import json as _json
    accounts_json = _json.dumps([
        {"id": a.id, "name": a.name, "platform": a.platform}
        for a in all_accounts
    ])

    rows = ""
    for p in posts:
        status_badge = _status_badge(p.status)
        scheduled_madrid = _to_madrid(p.scheduled_at).strftime("%d/%m/%Y %H:%M")
        account_name = p.account.name if p.account else "—"
        account_thumb = p.account.channel_thumbnail_url if p.account else None

        if account_thumb:
            account_cell = f'<div style="display:flex;align-items:center;gap:.4rem;"><img src="{html_lib.escape(account_thumb)}" width="22" height="22" style="border-radius:50%;flex-shrink:0;" /><span>{html_lib.escape(account_name)}</span></div>'
        else:
            account_cell = html_lib.escape(account_name)

        if p.status == "pending":
            extra_cell = f'<span style="color:#fbbf24;">{_countdown(p.scheduled_at, now_utc)}</span>'
        elif p.status == "published" and p.video_url:
            extra_cell = f'<a href="{p.video_url}" target="_blank" class="link">Ver vídeo</a>'
        elif p.status == "failed" and p.error_message:
            short = p.error_message[:60] + ("…" if len(p.error_message) > 60 else "")
            extra_cell = f'<span style="color:#f87171;" title="{html_lib.escape(p.error_message)}">{html_lib.escape(short)}</span>'
        else:
            extra_cell = "—"

        checkbox_cell = ""
        if p.status == "pending":
            checkbox_cell = f'<input type="checkbox" class="post-checkbox" data-post-id="{p.id}" onclick="event.stopPropagation();updateBulkControls();" style="width:16px;height:16px;cursor:pointer;" />'

        if p.status == "failed":
            actions = f"""
            <div style="display:flex;flex-direction:column;gap:.3rem;">
              <form method="post" action="/posts/{p.id}/retry">
                <button class="btn" style="padding:.3rem .7rem;font-size:.78rem;width:100%;">Reintentar ahora</button>
              </form>
              <form method="post" action="/posts/{p.id}/reschedule" style="display:flex;gap:.3rem;align-items:center;">
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
        else:
            actions = f"""
            <form method="post" action="/posts/{p.id}/delete" onsubmit="return confirm('¿Eliminar post?')">
              <button class="btn-danger">Eliminar</button>
            </form>"""

        rows += f"""
        <tr data-post-id="{p.id}" style="cursor:pointer;" title="Click para ver/editar detalles">
          <td style="width:32px;" onclick="event.stopPropagation();">{checkbox_cell}</td>
          <td>{p.id}</td>
          <td>{account_cell}</td>
          <td style="max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{html_lib.escape(p.title)}</td>
          <td style="white-space:nowrap;">{scheduled_madrid}</td>
          <td>{status_badge}</td>
          <td style="font-size:.82rem;max-width:200px;">{extra_cell}</td>
          <td style="min-width:160px;" onclick="event.stopPropagation();">{actions}</td>
        </tr>"""

    if not rows:
        rows = '<tr><td colspan="8" style="text-align:center;color:#666;">No hay posts todavía.</td></tr>'

    content = f"""
    <div class="page-header">
      <h2>Posts programados</h2>
      <div style="display:flex;gap:.75rem;align-items:center;">
        <span style="color:#555;font-size:.8rem;">Horario: Madrid (Europe/Madrid)</span>
        <form method="get" action="/posts" style="display:flex;gap:.4rem;align-items:center;">
          <select name="channel" onchange="this.form.submit()" style="padding:.3rem .6rem;font-size:.82rem;background:#0f0f0f;border:1px solid #333;border-radius:5px;color:#e0e0e0;">
            {channel_opts}
          </select>
          {"<button type='submit' class='btn' style='padding:.3rem .6rem;font-size:.78rem;'>Filtrar</button>" if not channel else '<a href="/posts" style="font-size:.78rem;color:#888;padding:.3rem .5rem;border:1px solid #333;border-radius:5px;">✕ Todos</a>'}
        </form>
      </div>
    </div>

    <div id="bulkBar" style="display:none;background:#1a1a1a;border:1px solid #333;border-radius:8px;padding:.6rem 1rem;margin-bottom:1rem;display:none;align-items:center;gap:.75rem;flex-wrap:wrap;">
      <span id="bulkCount" style="color:#888;font-size:.83rem;">0 seleccionados</span>
      <select id="bulkTarget" style="padding:.3rem .6rem;font-size:.82rem;background:#0f0f0f;border:1px solid #333;border-radius:5px;color:#e0e0e0;">
        {bulk_account_opts}
      </select>
      <button class="btn" style="padding:.35rem .8rem;font-size:.8rem;" onclick="doBulkMove()">Mover canal</button>
      <button class="btn-danger" style="padding:.35rem .8rem;font-size:.78rem;" onclick="clearSelection()">Cancelar</button>
    </div>

    <div style="overflow-x:auto;">
      <table>
        <thead>
          <tr>
            <th style="width:32px;"><input type="checkbox" id="selectAll" onchange="toggleAll(this)" style="width:16px;height:16px;cursor:pointer;" /></th>
            <th>#</th><th>Cuenta</th><th>Título</th><th>Programado</th><th>Estado</th><th>Resultado / Tiempo restante</th><th>Acciones</th>
          </tr>
        </thead>
        <tbody id="postsTable">{rows}</tbody>
      </table>
    </div>

    <!-- Detail / Edit Modal -->
    <div id="postModal" onclick="if(event.target===this)closeModal()" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.82);z-index:999;overflow-y:auto;padding:1.5rem;">
      <div style="max-width:640px;margin:0 auto;background:#141414;border:1px solid #333;border-radius:12px;padding:1.5rem;position:relative;">
        <button onclick="closeModal()" style="position:absolute;top:.75rem;right:.75rem;background:none;border:none;color:#666;font-size:1.2rem;cursor:pointer;line-height:1;">✕</button>

        <h3 style="font-size:1.1rem;color:#f0f0f0;margin-bottom:1.25rem;" id="modalTitle">Detalles del post</h3>

        <!-- Thumbnail -->
        <div id="modalThumb" style="margin-bottom:1rem;display:none;">
          <img id="modalThumbImg" src="" alt="thumbnail" style="width:100%;max-height:180px;object-fit:cover;border-radius:8px;border:1px solid #222;" />
        </div>

        <!-- Status info -->
        <div id="modalStatusRow" style="margin-bottom:1rem;display:flex;gap:.75rem;align-items:center;flex-wrap:wrap;">
          <span id="modalStatusBadge"></span>
          <a id="modalVideoLink" href="#" target="_blank" class="link" style="display:none;font-size:.82rem;">Ver en YouTube</a>
          <span id="modalErrorMsg" style="color:#f87171;font-size:.8rem;display:none;"></span>
        </div>

        <div style="display:grid;gap:.1rem;">
          <label>Título</label>
          <input type="text" id="modalTitleInput" maxlength="100" style="font-size:.9rem;" />

          <label style="margin-top:.6rem;">Canal / Cuenta</label>
          <select id="modalAccount" style="font-size:.88rem;"></select>

          <label style="margin-top:.6rem;" id="modalDateLabel">Fecha programada (Madrid)</label>
          <input type="datetime-local" id="modalDate" style="font-size:.88rem;" />

          <label style="margin-top:.6rem;">Descripción</label>
          <textarea id="modalDesc" style="min-height:90px;font-size:.85rem;"></textarea>

          <label style="margin-top:.6rem;">Tags (separados por coma)</label>
          <input type="text" id="modalTags" style="font-size:.88rem;" />
        </div>

        <div id="modalSaveResult" style="display:none;margin-top:.75rem;padding:.6rem;border-radius:6px;font-size:.83rem;"></div>

        <div style="display:flex;gap:.6rem;margin-top:1.1rem;">
          <button class="btn" id="modalSaveBtn" onclick="savePost()" style="flex:1;">Guardar cambios</button>
          <button onclick="closeModal()" style="padding:.5rem 1rem;background:#1a1a1a;border:1px solid #333;border-radius:6px;color:#888;cursor:pointer;font-size:.88rem;">Cancelar</button>
        </div>
        <p id="modalSaveNote" style="margin-top:.5rem;font-size:.73rem;color:#555;"></p>
      </div>
    </div>

    <input type="hidden" id="currentPostId" value="" />

    <script>
    const ACCOUNTS = {accounts_json};
    let currentPostId = null;

    // ── Row click → open modal ──────────────────────────────────
    document.querySelectorAll('tr[data-post-id]').forEach(row => {{
      row.addEventListener('click', e => {{
        if (e.target.closest('button,form,input,a,select')) return;
        openModal(row.dataset.postId);
      }});
    }});

    async function openModal(postId) {{
      currentPostId = postId;
      const modal = document.getElementById('postModal');
      modal.style.display = 'block';
      document.getElementById('modalSaveResult').style.display = 'none';
      document.getElementById('modalTitle').textContent = 'Cargando...';

      try {{
        const res = await fetch('/api/posts/' + postId);
        if (!res.ok) throw new Error('Error al cargar post');
        const d = await res.json();

        document.getElementById('modalTitle').textContent = 'Post #' + d.id;

        // Thumbnail (YouTube)
        const vid = extractVideoId(d.video_url);
        const thumbDiv = document.getElementById('modalThumb');
        if (vid) {{
          document.getElementById('modalThumbImg').src = 'https://img.youtube.com/vi/' + vid + '/mqdefault.jpg';
          thumbDiv.style.display = 'block';
        }} else {{
          thumbDiv.style.display = 'none';
        }}

        // Status
        const statusBadges = {{
          pending: '<span class="badge badge-pending">Pending</span>',
          published: '<span class="badge badge-published">Published</span>',
          failed: '<span class="badge badge-failed">Failed</span>'
        }};
        document.getElementById('modalStatusBadge').innerHTML = statusBadges[d.status] || d.status;

        const videoLink = document.getElementById('modalVideoLink');
        if (d.video_url) {{
          videoLink.href = d.video_url; videoLink.style.display = 'inline';
        }} else {{ videoLink.style.display = 'none'; }}

        const errEl = document.getElementById('modalErrorMsg');
        if (d.error_message) {{
          errEl.textContent = d.error_message; errEl.style.display = 'inline';
        }} else {{ errEl.style.display = 'none'; }}

        // Fields
        document.getElementById('modalTitleInput').value = d.title || '';
        document.getElementById('modalDesc').value = d.description || '';
        document.getElementById('modalTags').value = d.tags || '';
        document.getElementById('modalDate').value = d.scheduled_at || '';

        // Account dropdown
        const accSel = document.getElementById('modalAccount');
        accSel.innerHTML = '';
        ACCOUNTS.forEach(a => {{
          const opt = document.createElement('option');
          opt.value = a.id;
          opt.textContent = '[' + a.platform.toUpperCase() + '] ' + a.name;
          if (a.id === d.account_id) opt.selected = true;
          accSel.appendChild(opt);
        }});

        // Read-only state
        const isPending = d.status === 'pending' || d.status === 'failed';
        document.getElementById('modalDate').disabled = !isPending;
        document.getElementById('modalDateLabel').style.color = isPending ? '#888' : '#444';
        accSel.disabled = !isPending;

        // Save note
        const note = document.getElementById('modalSaveNote');
        if (d.status === 'published') {{
          note.textContent = 'El post está publicado — guardar intentará actualizar el título, descripción y tags en YouTube.';
        }} else {{
          note.textContent = '';
        }}

        document.getElementById('modalSaveBtn').textContent = d.status === 'published' ? 'Guardar y actualizar en YouTube' : 'Guardar cambios';

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
      const resultEl = document.getElementById('modalSaveResult');
      btn.disabled = true; btn.textContent = 'Guardando...';
      resultEl.style.display = 'none';

      const payload = {{
        title: document.getElementById('modalTitleInput').value,
        description: document.getElementById('modalDesc').value,
        tags: document.getElementById('modalTags').value,
        scheduled_at: document.getElementById('modalDate').value,
        account_id: parseInt(document.getElementById('modalAccount').value),
      }};

      try {{
        const res = await fetch('/api/posts/' + currentPostId + '/update', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify(payload),
        }});
        const data = await res.json();
        resultEl.style.display = 'block';
        if (res.ok) {{
          if (data.updated_on_youtube) {{
            resultEl.style.cssText = 'display:block;background:#0d2b1a;border:1px solid #1a7a3a;color:#4ade80;padding:.6rem;border-radius:6px;font-size:.83rem;';
            resultEl.textContent = '✓ Guardado y actualizado en YouTube.';
          }} else if (data.youtube_error) {{
            resultEl.style.cssText = 'display:block;background:#1a1500;border:1px solid #4a3800;color:#fbbf24;padding:.6rem;border-radius:6px;font-size:.83rem;';
            resultEl.textContent = '✓ Guardado localmente. YouTube: ' + data.youtube_error;
          }} else {{
            resultEl.style.cssText = 'display:block;background:#0d2b1a;border:1px solid #1a7a3a;color:#4ade80;padding:.6rem;border-radius:6px;font-size:.83rem;';
            resultEl.textContent = '✓ Cambios guardados.';
          }}
          // Update the row title in the table
          const row = document.querySelector('tr[data-post-id="' + currentPostId + '"]');
          if (row) {{
            const titleTd = row.cells[3];
            if (titleTd) titleTd.textContent = payload.title;
          }}
          setTimeout(() => closeModal(), 1500);
        }} else {{
          resultEl.style.cssText = 'display:block;background:#2b0d0d;border:1px solid #7a1a1a;color:#f87171;padding:.6rem;border-radius:6px;font-size:.83rem;';
          resultEl.textContent = 'Error: ' + (data.detail || JSON.stringify(data));
        }}
      }} catch(err) {{
        resultEl.style.cssText = 'display:block;background:#2b0d0d;border:1px solid #7a1a1a;color:#f87171;padding:.6rem;border-radius:6px;font-size:.83rem;';
        resultEl.textContent = 'Network error: ' + err.message;
        resultEl.style.display = 'block';
      }} finally {{
        btn.disabled = false;
        btn.textContent = 'Guardar cambios';
      }}
    }}

    // ── Bulk select ──────────────────────────────────────────────
    function toggleAll(cb) {{
      document.querySelectorAll('.post-checkbox').forEach(c => c.checked = cb.checked);
      updateBulkControls();
    }}

    function updateBulkControls() {{
      const checked = document.querySelectorAll('.post-checkbox:checked');
      const bar = document.getElementById('bulkBar');
      document.getElementById('bulkCount').textContent = checked.length + ' seleccionado' + (checked.length !== 1 ? 's' : '');
      bar.style.display = checked.length > 0 ? 'flex' : 'none';
    }}

    function clearSelection() {{
      document.querySelectorAll('.post-checkbox').forEach(c => c.checked = false);
      document.getElementById('selectAll').checked = false;
      updateBulkControls();
    }}

    async function doBulkMove() {{
      const targetId = parseInt(document.getElementById('bulkTarget').value);
      if (!targetId) {{ alert('Selecciona un canal de destino'); return; }}
      const ids = Array.from(document.querySelectorAll('.post-checkbox:checked')).map(c => parseInt(c.dataset.postId));
      if (!ids.length) return;
      const targetName = document.getElementById('bulkTarget').selectedOptions[0].textContent;
      if (!confirm('¿Mover ' + ids.length + ' post(s) al canal "' + targetName + '"?')) return;

      try {{
        const res = await fetch('/api/posts/bulk-move', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{post_ids: ids, target_account_id: targetId}}),
        }});
        const data = await res.json();
        if (res.ok) {{
          alert('✓ ' + data.moved + ' post(s) movidos a "' + targetName + '"');
          location.reload();
        }} else {{
          alert('Error: ' + (data.detail || JSON.stringify(data)));
        }}
      }} catch(e) {{
        alert('Network error: ' + e.message);
      }}
    }}

    function extractVideoId(url) {{
      if (!url) return null;
      let m = url.match(/youtube\\.com\\/shorts\\/([^?&]+)/);
      if (m) return m[1];
      m = url.match(/[?&]v=([^&]+)/);
      if (m) return m[1];
      m = url.match(/youtu\\.be\\/([^?&]+)/);
      if (m) return m[1];
      return null;
    }}

    // Close modal with Escape key
    document.addEventListener('keydown', e => {{ if (e.key === 'Escape') closeModal(); }});
    </script>
    """
    return HTMLResponse(_layout("Posts", content))


@app.post("/posts/{post_id}/retry")
async def retry_post(post_id: int, user: AuthDep, db: Session = Depends(get_db)):
    post = db.query(ScheduledPost).filter(ScheduledPost.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found.")
    from datetime import timedelta
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    post.status = "pending"
    post.error_message = None
    post.scheduled_at = now_utc + timedelta(minutes=1)
    db.commit()
    logger.info(f"Post #{post_id} reset to pending, scheduled for 1 min from now.")
    return RedirectResponse(url="/posts", status_code=302)


@app.post("/posts/{post_id}/reschedule")
async def reschedule_post(
    post_id: int,
    user: AuthDep,
    scheduled_at: str = Form(...),
    db: Session = Depends(get_db),
):
    post = db.query(ScheduledPost).filter(ScheduledPost.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found.")
    try:
        new_dt = (
            datetime.fromisoformat(scheduled_at)
            .replace(tzinfo=MADRID_TZ)
            .astimezone(timezone.utc)
            .replace(tzinfo=None)
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format.")
    post.scheduled_at = new_dt
    post.status = "pending"
    post.error_message = None
    db.commit()
    logger.info(f"Post #{post_id} rescheduled to {new_dt} UTC.")
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
    accounts = db.query(Account).order_by(Account.name).all()
    return [
        {
            "id": a.id, "name": a.name, "platform": a.platform,
            "channel_id": a.channel_id,
            "channel_thumbnail_url": a.channel_thumbnail_url,
            "created_at": a.created_at.isoformat(),
        }
        for a in accounts
    ]


@app.get("/api/posts/{post_id}")
async def api_get_post(post_id: int, user: AuthDep, db: Session = Depends(get_db)):
    post = db.query(ScheduledPost).filter(ScheduledPost.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found.")
    return {
        "id": post.id,
        "account_id": post.account_id,
        "account_name": post.account.name if post.account else None,
        "title": post.title,
        "description": post.description,
        "tags": post.tags,
        "scheduled_at": _to_madrid(post.scheduled_at).strftime("%Y-%m-%dT%H:%M"),
        "status": post.status,
        "video_url": post.video_url,
        "error_message": post.error_message,
        "file_path": post.file_path,
    }


class PostUpdatePayload(BaseModel):
    title: str | None = None
    description: str | None = None
    tags: str | None = None
    scheduled_at: str | None = None
    account_id: int | None = None


@app.post("/api/posts/{post_id}/update")
async def update_post(post_id: int, payload: PostUpdatePayload, user: AuthDep, db: Session = Depends(get_db)):
    post = db.query(ScheduledPost).filter(ScheduledPost.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found.")

    if payload.title is not None:
        post.title = payload.title
    if payload.description is not None:
        post.description = payload.description
    if payload.tags is not None:
        post.tags = payload.tags
    if payload.account_id is not None and post.status in ("pending", "failed"):
        account = db.query(Account).filter(Account.id == payload.account_id).first()
        if not account:
            raise HTTPException(status_code=404, detail="Target account not found.")
        post.account_id = payload.account_id
    if payload.scheduled_at and post.status in ("pending", "failed"):
        try:
            new_dt = (
                datetime.fromisoformat(payload.scheduled_at)
                .replace(tzinfo=MADRID_TZ)
                .astimezone(timezone.utc)
                .replace(tzinfo=None)
            )
            post.scheduled_at = new_dt
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format.")

    db.commit()
    logger.info(f"Post #{post_id} updated locally.")

    # If published, try to update on YouTube
    updated_on_youtube = False
    youtube_error = None
    if post.status == "published" and post.video_url and post.account:
        video_id = _extract_video_id(post.video_url)
        if video_id:
            try:
                publisher = YouTubePublisher(credentials_json=post.account.credentials_json)
                tags = [t.strip() for t in post.tags.split(",") if t.strip()] if post.tags else []
                success, error = publisher.update_video(video_id, post.title, post.description or "", tags)
                if success:
                    updated_on_youtube = True
                    post.account.credentials_json = publisher.get_updated_credentials_json()
                    db.commit()
                    logger.info(f"Post #{post_id} updated on YouTube: {video_id}")
                else:
                    youtube_error = error
                    logger.warning(f"Post #{post_id} YouTube update failed: {error}")
            except Exception as e:
                youtube_error = str(e)
                logger.error(f"Post #{post_id} YouTube update exception: {e}")

    return JSONResponse({"success": True, "updated_on_youtube": updated_on_youtube, "youtube_error": youtube_error})


class BulkMovePayload(BaseModel):
    post_ids: list[int]
    target_account_id: int


@app.post("/api/posts/bulk-move")
async def bulk_move_posts(payload: BulkMovePayload, user: AuthDep, db: Session = Depends(get_db)):
    account = db.query(Account).filter(Account.id == payload.target_account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Target account not found.")

    moved = 0
    for post_id in payload.post_ids:
        post = db.query(ScheduledPost).filter(
            ScheduledPost.id == post_id,
            ScheduledPost.status == "pending",
        ).first()
        if post:
            post.account_id = payload.target_account_id
            moved += 1
    db.commit()
    logger.info(f"Bulk move: {moved} posts moved to account #{payload.target_account_id}")
    return JSONResponse({"success": True, "moved": moved})


# ------------------------------------------------------------------
# Queue import (from TikTok Studio / external JSON)
# ------------------------------------------------------------------

class QueuePostItem(BaseModel):
    file_path: str
    title: str
    description: str = ""
    tags: str = ""
    publish_date: str = ""


class QueueImportPayload(BaseModel):
    account_id: int
    posts: list[QueuePostItem]


@app.get("/queue/import-ui", response_class=HTMLResponse)
async def import_queue_page(user: AuthDep, db: Session = Depends(get_db)):
    accounts = db.query(Account).order_by(Account.name).all()
    account_options = "".join(
        f'<option value="{a.id}">[{a.platform.upper()}] {html_lib.escape(a.name)}</option>' for a in accounts
    ) or '<option value="">— No accounts connected —</option>'

    content = f"""
    <div class="page-header"><h2>Import Queue — TikTok Studio JSON</h2></div>
    <div class="card" style="max-width:680px;">
      <p style="color:#888;font-size:.85rem;margin-bottom:1rem;">
        Pega aquí el JSON generado por TikTok Studio (botón "Exportar a Viral Distributor")
        o envíalo directamente via <code>POST /api/queue/import</code>.
      </p>

      <label>Cuenta de destino</label>
      <select id="accountId">{account_options}</select>

      <label>JSON de cola</label>
      <textarea id="queueJson" style="min-height:220px;font-family:monospace;font-size:.8rem;"
        placeholder='{{"account_id": null, "posts": [{{"file_path": "/ruta/video.mp4", "title": "Mi Short", "description": "#shorts", "tags": "shorts,viral", "publish_date": "2026-03-15T18:00:00"}}]}}'></textarea>

      <button class="btn" style="margin-top:1rem;width:100%;" onclick="importQueue()">Importar cola</button>
      <div id="result" style="display:none;margin-top:1rem;padding:.75rem;border-radius:6px;"></div>
    </div>
    <div class="card" style="max-width:680px;margin-top:1.5rem;">
      <p style="color:#888;font-size:.8rem;font-weight:600;margin-bottom:.5rem;">Formato JSON esperado:</p>
      <pre style="background:#0f0f0f;padding:1rem;border-radius:6px;color:#fbbf24;font-size:.75rem;overflow-x:auto;">{{"account_id": null,
  "posts": [
    {{
      "file_path": "/ruta/absoluta/video.mp4",
      "title": "Título del short #shorts",
      "description": "Descripción del vídeo",
      "tags": "shorts,viral,tema",
      "publish_date": "2026-03-15T18:00:00"
    }}
  ]}}</pre>
      <p style="color:#555;font-size:.75rem;margin-top:.5rem;">
        <strong style="color:#888;">publish_date</strong>: hora de Madrid (Europe/Madrid). Si se omite, se publica en 1 minuto.<br>
        <strong style="color:#888;">account_id</strong>: se sobreescribe con la cuenta seleccionada arriba.
      </p>
    </div>
    <script>
    async function importQueue() {{
      const r = document.getElementById('result');
      r.style.display='none';
      let raw;
      try {{ raw = JSON.parse(document.getElementById('queueJson').value); }}
      catch(e) {{ r.style.cssText='display:block;background:#2b0d0d;border:1px solid #7a1a1a;color:#f87171;padding:.75rem;border-radius:6px;'; r.textContent='JSON inválido: '+e.message; return; }}
      raw.account_id = parseInt(document.getElementById('accountId').value);
      try {{
        const res = await fetch('/api/queue/import', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify(raw)}});
        const data = await res.json();
        r.style.display='block';
        if (res.ok) {{
          r.style.cssText='display:block;background:#0d2b1a;border:1px solid #1a7a3a;color:#4ade80;padding:.75rem;border-radius:6px;';
          r.textContent = data.message + ' IDs: ' + (data.post_ids||[]).join(', ');
        }} else {{
          r.style.cssText='display:block;background:#2b0d0d;border:1px solid #7a1a1a;color:#f87171;padding:.75rem;border-radius:6px;';
          r.textContent='Error: '+(data.detail||JSON.stringify(data));
        }}
      }} catch(err) {{
        r.style.cssText='display:block;background:#2b0d0d;border:1px solid #7a1a1a;color:#f87171;padding:.75rem;border-radius:6px;';
        r.textContent='Network error: '+err.message;
      }}
    }}
    </script>
    """
    return HTMLResponse(_layout("Import Queue", content))


@app.post("/api/queue/import")
async def import_queue(
    payload: QueueImportPayload,
    user: AuthDep,
    db: Session = Depends(get_db),
):
    account = db.query(Account).filter(Account.id == payload.account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail=f"Account #{payload.account_id} not found.")

    from datetime import timedelta
    created_ids = []
    errors = []

    for item in payload.posts:
        if not Path(item.file_path).is_file():
            errors.append({"file_path": item.file_path, "error": "File not found"})
            continue

        if item.publish_date:
            try:
                scheduled_dt = (
                    datetime.fromisoformat(item.publish_date)
                    .replace(tzinfo=MADRID_TZ)
                    .astimezone(timezone.utc)
                    .replace(tzinfo=None)
                )
            except ValueError:
                errors.append({"file_path": item.file_path, "error": f"Invalid publish_date: {item.publish_date}"})
                continue
        else:
            scheduled_dt = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=1)

        post = ScheduledPost(
            account_id=payload.account_id,
            title=item.title,
            description=item.description,
            tags=item.tags,
            file_path=str(Path(item.file_path).absolute()),
            scheduled_at=scheduled_dt,
        )
        db.add(post)
        db.flush()
        created_ids.append(post.id)

    db.commit()
    logger.info(f"Queue import: {len(created_ids)} posts created, {len(errors)} errors. Account #{payload.account_id}")

    return JSONResponse({
        "success": True,
        "created": len(created_ids),
        "post_ids": created_ids,
        "errors": errors,
        "message": f"{len(created_ids)} vídeos programados, {len(errors)} errores.",
    })


@app.get("/debug", response_class=HTMLResponse)
async def debug(user: AuthDep):
    client_id = os.getenv("YOUTUBE_CLIENT_ID", "")
    client_secret = os.getenv("YOUTUBE_CLIENT_SECRET", "")
    secrets_path = os.getenv("YOUTUBE_CLIENT_SECRETS_PATH", "auth/client_secrets.json")

    checks = []

    if client_id and client_secret:
        checks.append(("YOUTUBE_CLIENT_ID", "ok", client_id[:20] + "..."))
        checks.append(("YOUTUBE_CLIENT_SECRET", "ok", "Set (hidden)"))
    else:
        checks.append(("YOUTUBE_CLIENT_ID", "error" if not client_id else "ok",
                       "NOT SET" if not client_id else client_id[:20] + "..."))
        checks.append(("YOUTUBE_CLIENT_SECRET", "error" if not client_secret else "ok",
                       "NOT SET" if not client_secret else "Set (hidden)"))
        if os.path.exists(secrets_path):
            checks.append(("client_secrets.json file", "ok", f"Found at {secrets_path}"))
        else:
            checks.append(("client_secrets.json file", "error", f"Not found at {secrets_path}"))

    redirect_uri = os.getenv("OAUTH_REDIRECT_URI", "NOT SET")
    checks.append(("OAUTH_REDIRECT_URI", "ok" if redirect_uri != "NOT SET" else "error", redirect_uri))

    db_url = os.getenv("DATABASE_URL", "sqlite:///./data/viraldistributor.db (default)")
    checks.append(("DATABASE_URL", "ok", db_url))

    checks.append(("data/ directory", "ok" if os.path.isdir("data") else "error",
                   "exists" if os.path.isdir("data") else "MISSING — DB cannot be created"))

    rows = ""
    for name, st, detail in checks:
        color = {"ok": "#4ade80", "warn": "#fbbf24", "error": "#f87171"}[st]
        icon = {"ok": "OK", "warn": "WARN", "error": "ERR"}[st]
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

def _to_madrid(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc).astimezone(MADRID_TZ)


def _countdown(scheduled_at: datetime, now_utc: datetime) -> str:
    diff = scheduled_at - now_utc
    total = int(diff.total_seconds())
    if total <= 0:
        return "Publicando..."
    days = total // 86400
    hours = (total % 86400) // 3600
    minutes = (total % 3600) // 60
    parts = []
    if days:
        parts.append(f"{days} día{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} hora{'s' if hours != 1 else ''}")
    if minutes and not days:
        parts.append(f"{minutes} min")
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
    .page-header {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 1.5rem; flex-wrap: wrap; gap: .75rem; }}
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

    /* Modal form fields */
    #postModal label {{ margin-top: 0; }}
    #postModal input[type=text], #postModal input[type=datetime-local], #postModal textarea, #postModal select {{
      background: #0f0f0f; border: 1px solid #333; border-radius: 6px; color: #e0e0e0;
    }}
    #postModal input:disabled, #postModal select:disabled {{ opacity: .4; cursor: not-allowed; }}
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
    <a href="/queue/import-ui" {"class='active'" if title == "Import Queue" else ""}>Import Queue (JSON)</a>
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
