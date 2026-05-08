# BizScanner

A Flask-based business card scanner that uses OCR and a Groq model to extract structured contact information from uploaded images.

## Features

- Upload or capture a business card image
- OCR text extraction with Tesseract
- Structured data extraction via Groq API
- Editable confirmation form before saving
- Knowledge base storage in `knowledge_base.json`

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

4. Edit `.env` and set your Groq API key and optional Tesseract path.

## Environment Variables

- `GROQ_API_KEY` — required Groq API key
- `GROQ_MODEL` — optional model name (default: `llama-3.1-8b-instant`)
- `TESSERACT_CMD` — optional path to `tesseract.exe` or install folder

## Run

```powershell
python app.py
```

Open `http://127.0.0.1:5000` in your browser.

## Render Deployment

1. Push this repository to GitHub.
2. Create a new Web Service on Render and connect your GitHub repository.
3. Use the default Python environment.
4. Render will install dependencies from `requirements.txt` and run:

```bash
gunicorn app:app --bind 0.0.0.0:$PORT
```

> The included `Procfile`, `runtime.txt`, and `.render.yaml` provide Render with the correct start command and Python runtime.

## Notes

- Ensure Tesseract OCR is installed on Windows if using local OCR.
- The knowledge base persists confirmed entries in `knowledge_base.json`.
