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
INSTANCE_DIR = BASE_DIR / "instance"
JSON_KB_PATH = INSTANCE_DIR / "knowledge_base.json"

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
TESSERACT_CMD_ENV = os.getenv("TESSERACT_CMD", "").strip()
TESSERACT_LANG = os.getenv("TESSERACT_LANG", "eng").strip() or "eng"


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

    if not _is_windows():
        # If PATH is minimal/misconfigured, still try common absolute locations.
        candidates.extend(
            [
                "/usr/bin/tesseract",
                "/usr/local/bin/tesseract",
                "/bin/tesseract",
            ]
        )

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
    """
    Preprocess business card images for OCR.

    Returns a single image optimized for text recognition.
    """
    np_array = np.frombuffer(image_bytes, np.uint8)
    image = cv2.imdecode(np_array, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("Invalid image data.")

    max_width = 1400
    height, width = image.shape[:2]
    if width > max_width:
        scale = max_width / float(width)
        image = cv2.resize(image, (int(width * scale), int(height * scale)), interpolation=cv2.INTER_AREA)

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    # Boost local contrast for small fonts / low lighting.
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    gray = clahe.apply(gray)

    # Denoise while preserving edges.
    denoised = cv2.bilateralFilter(gray, d=7, sigmaColor=55, sigmaSpace=55)

    # Adaptive threshold to handle uneven lighting and glossy cards.
    thresh = cv2.adaptiveThreshold(
        denoised,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        31,
        8,
    )

    # Reduce pepper noise / small artifacts.
    kernel = np.ones((2, 2), np.uint8)
    cleaned = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel, iterations=1)

    return cleaned


def _rotate_bound(gray_or_binary: np.ndarray, angle_deg: float) -> np.ndarray:
    (h, w) = gray_or_binary.shape[:2]
    center = (w / 2.0, h / 2.0)
    m = cv2.getRotationMatrix2D(center, angle_deg, 1.0)
    cos = abs(m[0, 0])
    sin = abs(m[0, 1])
    new_w = int((h * sin) + (w * cos))
    new_h = int((h * cos) + (w * sin))
    m[0, 2] += (new_w / 2.0) - center[0]
    m[1, 2] += (new_h / 2.0) - center[1]
    return cv2.warpAffine(
        gray_or_binary,
        m,
        (new_w, new_h),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE,
    )


def _estimate_skew_angle(binary: np.ndarray) -> float:
    """
    Estimate skew angle from a binarized image (text as white on black or black on white).
    Returns an angle in degrees for deskewing (rotate by -angle).
    """
    if binary.ndim != 2:
        return 0.0

    # Ensure "ink" is white for contour analysis.
    work = binary
    if np.mean(work) > 127:
        work = 255 - work

    coords = cv2.findNonZero(work)
    if coords is None or len(coords) < 200:
        return 0.0

    rect = cv2.minAreaRect(coords)
    angle = rect[-1]
    # OpenCV returns angle in [-90, 0). Normalize to a small rotation.
    if angle < -45:
        angle = 90 + angle
    # Ignore extreme rotations; business cards usually only mildly skewed.
    if abs(angle) > 25:
        return 0.0
    return float(angle)


def _generate_ocr_variants(image_bytes: bytes) -> list[np.ndarray]:
    """
    Create multiple preprocessing variants to handle:
    - different font sizes
    - low contrast / colored backgrounds
    - uneven lighting / glare
    - thin strokes
    - mild skew
    """
    np_array = np.frombuffer(image_bytes, np.uint8)
    image = cv2.imdecode(np_array, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("Invalid image data.")

    max_width = 2000
    h, w = image.shape[:2]
    if w > max_width:
        scale = max_width / float(w)
        image = cv2.resize(image, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    # Illumination normalization: remove slow-varying background (glare/gradients).
    background = cv2.GaussianBlur(gray, (0, 0), sigmaX=15)
    norm = cv2.divide(gray, background, scale=255)

    # Local contrast.
    clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
    norm = clahe.apply(norm)

    # Denoise for small fonts.
    denoised = cv2.fastNlMeansDenoising(norm, h=18, templateWindowSize=7, searchWindowSize=21)

    # Unsharp mask for crisper edges (helps thin fonts).
    blur = cv2.GaussianBlur(denoised, (0, 0), sigmaX=1.2)
    sharp = cv2.addWeighted(denoised, 1.65, blur, -0.65, 0)

    # Threshold variants.
    _, otsu = cv2.threshold(sharp, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    adapt_g = cv2.adaptiveThreshold(sharp, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 9)
    adapt_m = cv2.adaptiveThreshold(sharp, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY, 31, 9)

    # Deskew based on a stable binary mask.
    skew_angle = _estimate_skew_angle(otsu)
    if skew_angle != 0.0:
        gray_deskew = _rotate_bound(sharp, skew_angle)
        _, otsu_deskew = cv2.threshold(gray_deskew, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        adapt_g_deskew = cv2.adaptiveThreshold(
            gray_deskew, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 9
        )
    else:
        gray_deskew = sharp
        otsu_deskew = otsu
        adapt_g_deskew = adapt_g

    # Morphology to reduce speckle and improve character connectivity.
    open_kernel = np.ones((2, 2), np.uint8)
    close_kernel = np.ones((2, 2), np.uint8)
    variants = [
        gray_deskew,
        cv2.morphologyEx(otsu_deskew, cv2.MORPH_OPEN, open_kernel, iterations=1),
        cv2.morphologyEx(adapt_g_deskew, cv2.MORPH_OPEN, open_kernel, iterations=1),
        cv2.morphologyEx(adapt_m, cv2.MORPH_OPEN, open_kernel, iterations=1),
        cv2.morphologyEx(otsu_deskew, cv2.MORPH_CLOSE, close_kernel, iterations=1),
    ]

    # Inverted variants help when the card has dark background and light text.
    variants.extend([255 - v for v in variants if v.ndim == 2])

    # Deduplicate by basic hash to avoid repeated OCR work.
    unique: list[np.ndarray] = []
    seen: set[int] = set()
    for v in variants:
        key = hash(v.tobytes()[:4096]) ^ (v.shape[0] << 16) ^ v.shape[1]
        if key in seen:
            continue
        seen.add(key)
        unique.append(v)

    return unique


def _score_ocr_text(text: str) -> int:
    # Prefer more useful characters; penalize high junk ratio.
    if not text:
        return 0
    stripped = text.strip()
    if not stripped:
        return 0

    alnum = sum(ch.isalnum() for ch in stripped)
    spaces = sum(ch.isspace() for ch in stripped)
    useful = sum(ch in "@+-.(),:/&_#" for ch in stripped)
    junk = sum((not ch.isprintable()) or (ch in "�") for ch in stripped)
    # Reward length but keep it bounded.
    length = min(len(stripped), 600)
    return (alnum * 3) + useful + (spaces // 2) + length - (junk * 10)


def extract_text_from_image(image: np.ndarray | list[np.ndarray]) -> str:
    try:
        candidates = image if isinstance(image, list) else [image]
        psm_values = [6, 11]  # block of text, sparse text

        best_text = ""
        best_score = -1
        for candidate in candidates:
            for psm in psm_values:
                config = (
                    f"--oem 3 --psm {psm} -l {TESSERACT_LANG} "
                    "-c preserve_interword_spaces=1 "
                    "-c tessedit_char_blacklist=|"
                )
                extracted = pytesseract.image_to_string(candidate, config=config).strip()
                score = _score_ocr_text(extracted)
                if score > best_score:
                    best_score = score
                    best_text = extracted

        text = best_text
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
        "3) Normalize website to a plain domain/URL without trailing punctuation.\n"
        "4) Normalize phone numbers to readable format; keep country code if present.\n"
        "5) Do not add extra keys.\n"
        "6) Return JSON only, no markdown.\n\n"
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


def append_to_json_kb(entry: Dict[str, Any], confirmed_at: datetime) -> None:
    """
    Best-effort append to a JSON knowledge base file.

    Stored under ./instance so it doesn't get committed to git and matches the typical Flask instance pattern.
    """
    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)

    record = {
        "confirmed_at": confirmed_at.isoformat() + "Z",
        "data": normalize_response(entry),
    }

    items: list[dict[str, Any]]
    if JSON_KB_PATH.exists():
        try:
            loaded = json.loads(JSON_KB_PATH.read_text(encoding="utf-8"))
            items = loaded if isinstance(loaded, list) else []
        except Exception:
            items = []
    else:
        items = []

    items.append(record)
    tmp_path = JSON_KB_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, JSON_KB_PATH)


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/scan")
def scan_card():
    image_file = request.files.get("image")
    if not image_file:
        return jsonify({"error": "No image uploaded."}), 400

    try:
        image_bytes = image_file.read()
        variants = _generate_ocr_variants(image_bytes)
        raw_text = extract_text_from_image(variants)
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
        json_saved = True
        json_error = None
        try:
            append_to_json_kb(data, confirmed_at=card.confirmed_at or datetime.utcnow())
        except Exception as exc:
            json_saved = False
            json_error = str(exc)

        return (
            jsonify(
                {
                    "message": "Saved to knowledge base.",
                    "data": card.to_dict(),
                    "json_saved": json_saved,
                    "json_error": json_error,
                }
            ),
            200,
        )
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
