# AUTO-DOLA

A clean-room Python + Docker rewrite of the extracted automation flow: bulk Dola/Seedance video generation, image generation, TTS, persistent jobs, artifact downloads, and a professional React dashboard.

## Quick Start

```powershell
copy .env.example .env
docker compose up --build
```

Open `http://localhost:3000`.

The backend API runs on `http://localhost:8000`.

## Development

```powershell
make dev
make test
make lint
```

Backend code lives in `backend/app`. Frontend code lives in `frontend/src`.

## Security Notes

AUTO-DOLA expects user-provided credentials/cookies/API keys through settings or `.env`. It does not ship vendor credentials and does not implement license bypass behavior.
