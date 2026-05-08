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
2. Create a new Web Service on Render and connect your GitHub repository.
3. Add environment variables:
   - `GROQ_API_KEY` — your Groq API key
   - `TESSERACT_CMD` — optional (on Render this is set to `/usr/bin/tesseract` via `render.yaml`)

4. **Optional: Use Render PostgreSQL**
   - Create a PostgreSQL database on Render
   - Copy the database URL from Render
   - Add `DATABASE_URL` environment variable to your Web Service
   - Your business card data will persist across restarts

Render will install dependencies from `requirements.txt` and run:
```bash
gunicorn app:app --bind 0.0.0.0:$PORT
```

> The included `Procfile`, `runtime.txt`, and `.render.yaml` provide Render with the correct start command and Python runtime.

## Notes

- Ensure Tesseract OCR is installed on Windows for local OCR.
  - Preferred: set `TESSERACT_CMD` to the full exe path, e.g. `C:\Program Files\Tesseract-OCR\tesseract.exe`
  - Alternatively: add the folder containing `tesseract.exe` to your PATH (the app will auto-detect it)
- If Tesseract is missing/misconfigured, `POST /scan` returns `400` with actionable setup steps.
