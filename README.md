# BizScanner

A Flask-based business card scanner that uses OCR + OpenCV preprocessing and an optional Groq model to extract structured contact information from uploaded images.

## Features

- Upload or capture a business card image
- OCR text extraction with Tesseract
- OpenCV preprocessing (CLAHE, denoise, deskew, DFT homomorphic filter, rotate/flip variants)
- Structured data extraction via Groq API (with heuristic fallback)
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

4. Edit `.env` and set your values. If `GROQ_API_KEY` is empty, the app will still work using heuristic extraction.

## Database

- **Local development:** Uses SQLite (`knowledge_base.db`)
- **Render deployment:** Uses PostgreSQL (data persists across restarts)

To use PostgreSQL locally, set in `.env`:

```
DATABASE_URL=postgresql://user:password@localhost/bizscanner
```

## Environment Variables

- `GROQ_API_KEY` — optional Groq API key (if unset, the app uses heuristic extraction)
- `GROQ_MODEL` — optional model name (default: `llama-3.1-8b-instant`)
- `GROQ_TIMEOUT_SEC` — Groq request timeout (default: `15`)
- `TESSERACT_CMD` — optional path to `tesseract.exe` or install folder
- `TESSERACT_LANG` — Tesseract language (default: `eng`)
- `TESSERACT_TIMEOUT_SEC` — per-call OCR timeout (default: `6`)
- `OCR_MAX_VARIANTS` — max preprocessing variants to try (default: `4`)
- `OCR_TIME_BUDGET_SEC` — overall OCR time budget per scan (default: `10`)
- `OCR_ENABLE_HOMOMORPHIC_DFT` — enable DFT-based illumination correction (default: `1`)
- `OCR_ENABLE_ORIENTATION_VARIANTS` — include rotate/flip variants (default: `1`)
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
   - `GROQ_API_KEY` (optional)
   - `GROQ_MODEL` (optional)
4. Create a Render PostgreSQL database named `bizscanner` (the Blueprint wires `DATABASE_URL` automatically).

> This project deploys on Render as a Docker service. The Docker image installs Tesseract and sets `TESSERACT_CMD=/usr/bin/tesseract` automatically.

## Prompt template

- The reusable Groq “skill” prompt lives at `prompts/groq_business_card_extractor.md`.

## Notes

- Ensure Tesseract OCR is installed on Windows for local OCR.
  - Preferred: set `TESSERACT_CMD` to the full exe path, e.g. `C:\Program Files\Tesseract-OCR\tesseract.exe`
  - Alternatively: add the folder containing `tesseract.exe` to your PATH (the app will auto-detect it)
- If Tesseract is missing/misconfigured, `POST /scan` returns `400` with actionable setup steps.

