# AUTO-DOLA

A clean-room Python + Docker rewrite of the extracted automation flow: bulk Dola/Seedance video generation, image generation, TTS, persistent jobs, artifact downloads, and a professional React dashboard.

## Quick Start

```powershell
copy .env.example .env
docker compose up --build
```

Open `http://localhost:3000`.

The backend API runs on `http://localhost:8000`.

Generated videos are written directly to your host Downloads folder through Docker. By default, `.env.example` points `AUTO_DOLA_DOWNLOADS_DIR` at:

```powershell
C:\Users\Muhammad Huzaifa\Downloads\AUTO-DOLA
```

Docker mounts that folder into the app as `/data/downloads`, so new MP4 outputs appear in Downloads without using a browser folder picker.

## Prompt Generator Gemini API

Open `Prompt Generator` in the app and fill:

- `Gemini API key`
- `Gemini API host`, for example `localhost:8045` or `https://generativelanguage.googleapis.com/v1beta`
- `Gemini model`, default `gemini-2.5-flash`

Click `Save API Configuration`, then generate prompts. If you enter `localhost:8045`, AUTO-DOLA automatically calls it as `http://localhost:8045`. These settings are stored in the app settings table using the existing encryption layer. You can also set `GEMINI_API_KEY`, `GEMINI_BASE_URL`, and `GEMINI_MODEL` in `.env`.

## Dola Video Sessions

AUTO-DOLA builds a fresh Dola web session for every video item. It fetches public Dola cookies such as `ttwid` automatically, then optionally merges user-owned auth cookies if you provide them locally.

For Docker, place a cookie file at `secrets/dola_auth_cookies` before starting the stack. For local backend runs, you can also use `auth_cookies.txt` in the repo root or `backend/auth_cookies.txt`. The file may contain either `name=value` lines or one raw browser cookie header string. These files are ignored by Git.

Check session readiness without exposing cookie values:

```powershell
Invoke-RestMethod http://localhost:8000/api/system/dola-session
```

## Development

```powershell
make dev
make test
make lint
```

Backend code lives in `backend/app`. Frontend code lives in `frontend/src`.

## Security Notes

AUTO-DOLA expects user-provided credentials/cookies/API keys through settings or `.env`. It does not ship vendor credentials and does not implement license bypass behavior.
