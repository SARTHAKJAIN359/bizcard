# BizScanner

A Flask-based business card scanner that uses OCR and a Groq model to extract structured contact information from uploaded images.

## Features

- Upload or capture a business card image
- OCR text extraction with Tesseract
- Structured data extraction via Groq API
- Editable confirmation form before saving
- Knowledge base storage in a database (SQLite locally, PostgreSQL on Render)

## Setup

1. Create and activate a Python virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

2. Install dependencies:

```powershell
python -m pip install -r requirements.txt
```

3. Copy environment variables:

```powershell
copy .env.example .env
```

4. Edit `.env` and set your Groq API key. Database defaults to local SQLite.

## Database

- **Local development:** Uses SQLite (`knowledge_base.db`)
- **Render deployment:** Uses PostgreSQL (data persists across restarts)

To use PostgreSQL locally, set in `.env`:
```
DATABASE_URL=postgresql://user:password@localhost/bizscanner
```

## Environment Variables

- `GROQ_API_KEY` — required Groq API key
- `GROQ_MODEL` — optional model name (default: `llama-3.1-8b-instant`)
- `TESSERACT_CMD` — optional path to `tesseract.exe` or install folder
- `DATABASE_URL` — optional database URL (defaults to local SQLite)

## Run

```powershell
python app.py
```

Open `http://127.0.0.1:5000` in your browser.

## Render Deployment

1. Push this repository to GitHub.
2. In Render, create a **Blueprint** from this repo (uses `render.yaml`).
3. Add environment variables in Render:
   - `GROQ_API_KEY` — required Groq API key
   - `GROQ_MODEL` — optional (defaults to `llama-3.1-8b-instant`)
4. Create a Render PostgreSQL database named `bizscanner` (the Blueprint wires `DATABASE_URL` automatically).

> This project deploys on Render as a Docker service. The Docker image installs Tesseract and sets `TESSERACT_CMD=/usr/bin/tesseract` automatically.

## Replit Deployment

This repository now includes Replit configuration for the free tier.

1. Open the project on Replit and ensure the `.replit` and `replit.nix` files are present.
2. In Replit secrets, set:
   - `GROQ_API_KEY`
   - optionally `GROQ_MODEL`
   - optionally `TESSERACT_CMD` if you want to override the default path
3. Replit will install Python dependencies and Tesseract via Nix.
4. Run the repl. The app listens on `0.0.0.0:$PORT` automatically.

> Use SQLite locally by leaving `DATABASE_URL` empty.

## Notes

- Ensure Tesseract OCR is installed on Windows for local OCR.
  - Preferred: set `TESSERACT_CMD` to the full exe path, e.g. `C:\Program Files\Tesseract-OCR\tesseract.exe`
  - Alternatively: add the folder containing `tesseract.exe` to your PATH (the app will auto-detect it)
- If Tesseract is missing/misconfigured, `POST /scan` returns `400` with actionable setup steps.
