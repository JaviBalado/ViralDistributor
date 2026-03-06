# ViralDistributor

Automated video publishing pipeline for social media platforms.

**Phase 1:** YouTube Shorts (active)
**Phase 2:** Instagram Reels (planned)
**Phase 3:** TikTok (planned)

The project exposes a **web dashboard** (FastAPI) accessible from any browser, protected by credentials set as environment variables. It can be self-hosted on [Coolify](https://coolify.io/) or any Docker-compatible server.

---

## Architecture

```
src/
├── main.py                  # CLI entry point
├── config.py                # Env/config loader
├── models/
│   └── video.py             # VideoPost & PublishResult dataclasses
├── platforms/
│   ├── base.py              # Abstract BasePlatformPublisher
│   ├── youtube.py           # Phase 1 — YouTube uploader
│   ├── instagram.py         # Phase 2 stub
│   └── tiktok.py            # Phase 3 stub
├── utils/
│   └── logger.py            # Centralized logging
└── web/
    └── app.py               # FastAPI dashboard (browser UI + REST API)
```

Adding a new platform = create one file in `src/platforms/` that subclasses `BasePlatformPublisher` and implements `authenticate()`, `publish()`, and `is_authenticated()`.

---

## Stack

| Layer | Technology |
|---|---|
| Language | Python 3.12 |
| Web framework | FastAPI + Uvicorn |
| Auth (web UI) | HTTP Basic Auth (env vars) |
| YouTube API | google-api-python-client |
| Auth (YouTube) | OAuth 2.0 (google-auth-oauthlib) |
| Deployment | Docker + Coolify |

---

## Local Setup

### 1. Clone the repository

```bash
git clone https://github.com/YOUR_USERNAME/ViralDistributor.git
cd ViralDistributor
```

### 2. Create and activate a virtual environment

```bash
python -m venv .venv
source .venv/bin/activate        # macOS / Linux
.venv\Scripts\activate           # Windows
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment variables

```bash
cp .env.example .env
# Edit .env with your values
```

### 5. Add your YouTube credentials

See the section below for how to obtain `client_secrets.json`, then place it at:
```
auth/client_secrets.json
```

### 6. Run locally

```bash
uvicorn src.web.app:app --reload --port 8000
```

Open [http://localhost:8000](http://localhost:8000) and log in with the credentials from your `.env`.

---

## How to Get the YouTube API Key (detailed)

YouTube videos are uploaded via the **YouTube Data API v3**. This requires **OAuth 2.0** credentials (not a simple API key), because uploading acts on behalf of a real Google account.

### Step 1 — Create a Google Cloud project

1. Go to [https://console.cloud.google.com/](https://console.cloud.google.com/)
2. Click the project dropdown at the top → **New Project**
3. Give it a name (e.g. `ViralDistributor`) and click **Create**
4. Make sure the new project is selected in the dropdown

### Step 2 — Enable the YouTube Data API v3

1. In the left sidebar go to **APIs & Services → Library**
2. Search for **YouTube Data API v3**
3. Click on it and press **Enable**

### Step 3 — Configure the OAuth consent screen

1. Go to **APIs & Services → OAuth consent screen**
2. Select **External** (allows any Google account to authorize) → **Create**
3. Fill in the required fields:
   - **App name**: ViralDistributor (or any name)
   - **User support email**: your email
   - **Developer contact**: your email
4. Click **Save and Continue**
5. On the **Scopes** step, click **Add or Remove Scopes**
   - Search for `youtube.upload` and check it
   - Click **Update** then **Save and Continue**
6. On **Test users**, add your own Google account email (the channel owner)
7. Click **Save and Continue** → **Back to Dashboard**

> **Important:** While the app is in "Testing" mode, only the Google accounts you add as Test Users can authorize it. This is fine for personal use.

### Step 4 — Create OAuth 2.0 credentials

1. Go to **APIs & Services → Credentials**
2. Click **+ Create Credentials → OAuth client ID**
3. Select **Desktop app** as the Application type
4. Give it a name (e.g. `ViralDistributor Desktop`) → **Create**
5. A dialog will show your Client ID and Secret. Click **Download JSON**
6. Rename the downloaded file to `client_secrets.json`
7. Place it at `auth/client_secrets.json` in this project

### Step 5 — First-time authentication (generate token)

Run the app once locally:

```bash
uvicorn src.web.app:app --port 8000
```

On the first upload attempt, the app will open a browser window asking you to sign in with your Google account and grant permission. After authorizing, a token file is saved at `auth/tokens/youtube_token.json`.

> This token is automatically refreshed and does not expire unless you revoke it from your Google account settings.

**On a server (Coolify/Docker):** Since there is no browser on the server, you must generate the token locally first and then upload the `auth/tokens/youtube_token.json` file to your server (via Coolify's Persistent Storage or a volume mount).

---

## Deployment on Coolify

### 1. Push this repo to GitHub (public or private)

```bash
git remote add origin https://github.com/YOUR_USERNAME/ViralDistributor.git
git push -u origin main
```

### 2. Create a new resource in Coolify

1. Open your Coolify dashboard
2. Click **New Resource → Public/Private Repository**
3. Paste your GitHub repo URL
4. Coolify detects the `Dockerfile` automatically

### 3. Set environment variables in Coolify

In your Coolify service, go to **Environment Variables** and add:

```
DASHBOARD_USERNAME=admin
DASHBOARD_PASSWORD=your_strong_password_here
YOUTUBE_CLIENT_SECRETS_PATH=auth/client_secrets.json
YOUTUBE_TOKEN_PATH=auth/tokens/youtube_token.json
YOUTUBE_DEFAULT_PRIVACY=private
LOG_LEVEL=INFO
```

### 4. Mount persistent storage for auth tokens

In Coolify, add a **Persistent Storage** volume:
- Container path: `/app/auth`
- This ensures your `client_secrets.json` and OAuth token survive container restarts/redeploys

Upload your files to the volume via SSH or Coolify's file manager.

### 5. Deploy

Click **Deploy**. Once running, access the dashboard at your Coolify-assigned domain. Log in with the credentials you set above.

---

## CLI Usage (optional)

```bash
# Upload a YouTube Short
python -m src.main youtube \
  --file videos/my_short.mp4 \
  --title "My Awesome Short" \
  --description "Check this out!" \
  --tags "#Shorts,topic,keyword" \
  --privacy private

# Upload a long video
python -m src.main youtube \
  --file videos/long_video.mp4 \
  --title "My Long Video" \
  --long \
  --privacy public
```

---

## Security notes

- Dashboard credentials (`DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD`) are validated with `secrets.compare_digest` to prevent timing attacks
- Never commit `.env`, `client_secrets.json`, or `auth/tokens/` — all are in `.gitignore`
- The OAuth token gives upload-only access (`youtube.upload` scope)

---

## Roadmap

- [x] Phase 1: YouTube Shorts upload via web dashboard
- [ ] Phase 2: Instagram Reels (Instagram Graph API)
- [ ] Phase 3: TikTok (Content Posting API)
- [ ] Scheduled publishing (cron / background tasks)
- [ ] Upload history and status tracking
