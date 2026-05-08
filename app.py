import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

import cv2
import numpy as np
import pytesseract
import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from flask_sqlalchemy import SQLAlchemy

load_dotenv()

app = Flask(__name__)

# Database configuration
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///knowledge_base.db")
# Handle Render's postgres:// URLs (should be postgresql://)
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

BASE_DIR = Path(__file__).resolve().parent

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
TESSERACT_CMD_ENV = os.getenv("TESSERACT_CMD", "").strip()


class OCRDependencyError(RuntimeError):
    pass


def _is_windows() -> bool:
    return os.name == "nt"


def resolve_tesseract_cmd() -> str | None:
    """
    Resolve the best tesseract executable path.

    Order:
    1) TESSERACT_CMD env var (exe path or install folder)
    2) PATH via shutil.which("tesseract")
    3) Common Windows install paths
    """
    candidates: list[str] = []

    if TESSERACT_CMD_ENV:
        env_path = Path(TESSERACT_CMD_ENV)
        if env_path.is_dir():
            exe_name = "tesseract.exe" if _is_windows() else "tesseract"
            candidates.append(str(env_path / exe_name))
        else:
            candidates.append(str(env_path))

    which = shutil.which("tesseract")
    if which:
        candidates.append(which)

    if _is_windows():
        candidates.extend(
            [
                r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
            ]
        )

    for candidate in candidates:
        try:
            if Path(candidate).exists():
                return candidate
        except OSError:
            # Defensive: some paths can raise on Windows if malformed.
            continue

    return None


RESOLVED_TESSERACT_CMD = resolve_tesseract_cmd()
if RESOLVED_TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = RESOLVED_TESSERACT_CMD

TARGET_FIELDS = [
    "name",
    "number",
    "address",
    "website",
    "company_name",
    "designation",
]


# Database model
class BusinessCard(db.Model):
    __tablename__ = "business_cards"
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255))
    number = db.Column(db.String(255))
    address = db.Column(db.Text)
    website = db.Column(db.String(255))
    company_name = db.Column(db.String(255))
    designation = db.Column(db.String(255))
    confirmed_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "number": self.number,
            "address": self.address,
            "website": self.website,
            "company_name": self.company_name,
            "designation": self.designation,
            "confirmed_at": self.confirmed_at.isoformat() + "Z" if self.confirmed_at else None,
        }



def preprocess_image(image_bytes: bytes) -> np.ndarray:
    np_array = np.frombuffer(image_bytes, np.uint8)
    image = cv2.imdecode(np_array, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("Invalid image data.")

    max_width = 1000
    height, width = image.shape[:2]
    if width > max_width:
        scale = max_width / float(width)
        image = cv2.resize(image, (int(width * scale), int(height * scale)), interpolation=cv2.INTER_AREA)

    blurred = cv2.GaussianBlur(image, (5, 5), 0)
    gray = cv2.cvtColor(blurred, cv2.COLOR_BGR2GRAY)
    return gray


def extract_text_from_image(image: np.ndarray) -> str:
    try:
        text = pytesseract.image_to_string(image, config="--oem 3 --psm 6")
    except getattr(pytesseract.pytesseract, "TesseractNotFoundError", OSError) as exc:
        resolved = RESOLVED_TESSERACT_CMD
        env_value = TESSERACT_CMD_ENV

        msg_lines = [
            "Tesseract is not installed or it's not in your PATH. See README file for more information.",
            f"TESSERACT_CMD env: {env_value or '(not set)'}",
            f"Resolved tesseract_cmd: {resolved or '(not found)'}",
        ]

        if _is_windows():
            msg_lines.extend(
                [
                    "Fix (Windows):",
                    "1) Install Tesseract OCR (UB Mannheim build is commonly used).",
                    r"2) Set TESSERACT_CMD to the full exe path, e.g. C:\Program Files\Tesseract-OCR\tesseract.exe",
                    r"   (or set TESSERACT_CMD to the install folder C:\Program Files\Tesseract-OCR)",
                    "3) Alternatively, add the folder containing tesseract.exe to your PATH.",
                ]
            )
        else:
            msg_lines.extend(
                [
                    "Fix (Linux/macOS): install tesseract and ensure it's on PATH, or set TESSERACT_CMD to the executable path.",
                ]
            )

        raise OCRDependencyError("\n".join(msg_lines)) from exc
    except OSError as exc:
        # Covers permission/exec issues like WinError 5.
        if "WinError 5" in str(exc):
            raise OCRDependencyError(
                "Tesseract path is not executable.\n"
                f"TESSERACT_CMD env: {TESSERACT_CMD_ENV or '(not set)'}\n"
                f"Resolved tesseract_cmd: {RESOLVED_TESSERACT_CMD or '(not found)'}\n"
                r"Set TESSERACT_CMD to the full exe path, e.g. C:\Program Files\Tesseract-OCR\tesseract.exe"
            ) from exc
        raise
    return text.strip()


def normalize_response(data: Dict[str, Any]) -> Dict[str, Any]:
    normalized = {}
    for field in TARGET_FIELDS:
        value = data.get(field)
        if value in ("", "N/A", "n/a", "null", "None"):
            value = None
        normalized[field] = value
    return normalized


def get_structured_data_with_groq(raw_text: str) -> Dict[str, Any]:
    if not GROQ_API_KEY:
        raise RuntimeError("Missing GROQ_API_KEY in .env file.")

    prompt = (
        "You are an information extraction assistant for business cards.\n"
        "Extract data and return ONLY valid JSON with keys:\n"
        "name, number, address, website, company_name, designation.\n"
        "Rules:\n"
        "1) Use null for missing values.\n"
        "2) If multiple phone numbers exist, combine into one string separated by comma.\n"
        "3) Do not add extra keys.\n"
        "4) Return JSON only, no markdown.\n\n"
        f"Business card OCR text:\n{raw_text}"
    )

    payload = {
        "model": GROQ_MODEL,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": "You extract business card details to strict JSON."},
            {"role": "user", "content": prompt},
        ],
    }

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    response = requests.post(GROQ_URL, headers=headers, json=payload, timeout=45)
    response.raise_for_status()
    model_text = response.json()["choices"][0]["message"]["content"].strip()

    if model_text.startswith("```"):
        model_text = model_text.replace("```json", "").replace("```", "").strip()

    parsed = json.loads(model_text)
    return normalize_response(parsed)


def append_to_knowledge_base(entry: Dict[str, Any]) -> BusinessCard:
    """Save a business card entry to the database."""
    confirmed = normalize_response(entry)
    
    card = BusinessCard(
        name=confirmed.get("name"),
        number=confirmed.get("number"),
        address=confirmed.get("address"),
        website=confirmed.get("website"),
        company_name=confirmed.get("company_name"),
        designation=confirmed.get("designation"),
    )
    
    db.session.add(card)
    db.session.commit()
    return card


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/scan")
def scan_card():
    image_file = request.files.get("image")
    if not image_file:
        return jsonify({"error": "No image uploaded."}), 400

    try:
        processed = preprocess_image(image_file.read())
        raw_text = extract_text_from_image(processed)
        structured = get_structured_data_with_groq(raw_text)
    except OCRDependencyError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    return jsonify({"raw_text": raw_text, "data": structured})


@app.post("/confirm")
def confirm_data():
    payload = request.get_json(silent=True) or {}
    data = payload.get("data", {})
    
    try:
        card = append_to_knowledge_base(data)
        return jsonify({"message": "Saved to knowledge base.", "data": card.to_dict()}), 200
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.get("/cards")
def get_all_cards():
    """Retrieve all saved business cards."""
    try:
        cards = BusinessCard.query.order_by(BusinessCard.confirmed_at.desc()).all()
        return jsonify({"cards": [card.to_dict() for card in cards]}), 200
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.get("/health")
def health():
    return jsonify(
        {
            "status": "ok",
            "tesseract_available": bool(RESOLVED_TESSERACT_CMD),
            "tesseract_cmd": RESOLVED_TESSERACT_CMD,
            "tesseract_env": TESSERACT_CMD_ENV,
        }
    )


@app.before_request
def init_db():
    """Initialize database tables on first request."""
    if not hasattr(init_db, "done"):
        with app.app_context():
            db.create_all()
        init_db.done = True


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(host="0.0.0.0", port=5000, debug=True)
