import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict
import re
from time import monotonic
from difflib import SequenceMatcher

import cv2
import numpy as np
import pytesseract
import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from flask_sqlalchemy import SQLAlchemy
from werkzeug.exceptions import HTTPException

load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024

# Database configuration
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///knowledge_base.db")
# Handle Render's postgres:// URLs (should be postgresql://)
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)


@app.errorhandler(HTTPException)
def handle_http_exception(exc: HTTPException):
    """
    Ensure API endpoints return JSON instead of default HTML error pages.

    This prevents front-end JSON parsing errors when Werkzeug generates an HTML response
    (e.g., InternalServerError) outside route-level try/except blocks.
    """
    api_paths = {"/scan", "/confirm", "/cards", "/health"}
    wants_json = (
        request.path in api_paths
        or request.path.startswith("/api/")
        or "application/json" in (request.headers.get("Accept") or "")
    )
    if not wants_json:
        return exc
    return jsonify({"error": exc.description}), exc.code


@app.errorhandler(Exception)
def handle_unhandled_exception(exc: Exception):
    api_paths = {"/scan", "/confirm", "/cards", "/health"}
    wants_json = (
        request.path in api_paths
        or request.path.startswith("/api/")
        or "application/json" in (request.headers.get("Accept") or "")
    )
    if not wants_json:
        return ("Internal Server Error", 500)

    # Avoid leaking stack traces; include only a short detail string.
    detail = str(exc)
    if len(detail) > 300:
        detail = detail[:300] + "..."
    return jsonify({"error": "Internal server error.", "detail": detail}), 500

BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
JSON_KB_PATH = INSTANCE_DIR / "knowledge_base.json"

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_TIMEOUT_SEC = int(os.getenv("GROQ_TIMEOUT_SEC", "15") or "15")
TESSERACT_CMD_ENV = os.getenv("TESSERACT_CMD", "").strip()
TESSERACT_LANG = os.getenv("TESSERACT_LANG", "eng").strip() or "eng"
TESSERACT_TIMEOUT_SEC = int(os.getenv("TESSERACT_TIMEOUT_SEC", "6") or "6")
OCR_MAX_VARIANTS = int(os.getenv("OCR_MAX_VARIANTS", "4") or "4")
OCR_TIME_BUDGET_SEC = int(os.getenv("OCR_TIME_BUDGET_SEC", "10") or "10")
OCR_ENABLE_HOMOMORPHIC_DFT = (os.getenv("OCR_ENABLE_HOMOMORPHIC_DFT", "1") or "1").strip().lower() in {"1", "true", "yes", "on"}
OCR_ENABLE_ORIENTATION_VARIANTS = (os.getenv("OCR_ENABLE_ORIENTATION_VARIANTS", "1") or "1").strip().lower() in {"1", "true", "yes", "on"}
APP_VERSION = os.getenv("RENDER_GIT_COMMIT") or os.getenv("GIT_COMMIT") or os.getenv("COMMIT_SHA") or ""


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


def _looks_like_url(value: str) -> bool:
    return bool(re.search(r"(https?://|www\.|[A-Za-z0-9-]+\.[A-Za-z]{2,})", value))


def _clean_website(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip().strip(".,;:")
    if not v:
        return None
    # Fix common OCR confusions
    v = v.replace(" ", "").replace("|", "").replace("'", "")
    v = v.replace("htrp://", "http://").replace("htlp://", "http://").replace("httр://", "http://")
    if not _looks_like_url(v):
        return None
    return v


def _clean_phone(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip()
    if not v:
        return None
    # Keep + and digits, plus comma separators if already present.
    compact = re.sub(r"[^0-9+,]", "", v)
    # Require at least 8 digits total across all numbers.
    digits = re.sub(r"[^0-9]", "", compact)
    if len(digits) < 8:
        return None
    return compact


def _best_similarity(needle: str, haystack: str) -> float:
    needle = re.sub(r"\s+", " ", (needle or "").strip().lower())
    haystack = (haystack or "").strip().lower()
    if not needle or not haystack:
        return 0.0
    scores = []
    for line in haystack.splitlines():
        cleaned_line = re.sub(r"\s+", " ", line.strip())
        if cleaned_line:
            scores.append(SequenceMatcher(None, needle, cleaned_line).ratio())
    return max(scores, default=SequenceMatcher(None, needle, re.sub(r"\s+", " ", haystack)).ratio())


def _extract_json_object(text: str) -> dict[str, Any] | None:
    if not text:
        return None

    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.replace("```json", "").replace("```", "").strip()

    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None

    snippet = cleaned[start : end + 1]
    try:
        parsed = json.loads(snippet)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        return None
    return None


def _heuristic_structured_data(raw_text: str) -> Dict[str, Any]:
    lines = [line.strip(" ,;:|") for line in (raw_text or "").splitlines()]
    lines = [line for line in lines if line]

    phone_pattern = re.compile(r"(\+?\d[\d\s().-]{6,}\d)")
    website_pattern = re.compile(r"(?i)\b(?:https?://)?(?:www\.)?[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?:/[^\s]*)?")
    address_keywords = ("street", "st.", "st ", "road", "rd.", "rd ", "avenue", "ave", "blvd", "lane", "ln", "drive", "dr", "city", "state", "zip")
    title_keywords = ("manager", "director", "founder", "co-founder", "owner", "engineer", "lead", "head", "president", "ceo", "cto", "cmo", "sales", "marketing", "consultant")
    company_keywords = ("inc", "llc", "ltd", "corp", "corporation", "company", "co.", "solutions", "studio", "technologies", "systems", "group", "services", "labs")

    phone_match = phone_pattern.search(raw_text or "")
    website_match = website_pattern.search(raw_text or "")

    name = None
    company_name = None
    designation = None
    address_lines: list[str] = []

    for idx, line in enumerate(lines):
        lower = line.lower()
        if not name and len(line.split()) <= 4 and any(ch.isalpha() for ch in line) and not phone_pattern.search(line) and not website_pattern.search(line):
            if idx == 0 or idx == 1:
                name = line
                continue

        if not company_name and any(keyword in lower for keyword in company_keywords):
            company_name = line
            continue

        if not designation and any(keyword in lower for keyword in title_keywords):
            designation = line
            continue

        looks_like_phone = bool(phone_pattern.search(line))
        looks_like_website = bool(website_pattern.search(line))
        if looks_like_phone or looks_like_website:
            continue
        if any(keyword in lower for keyword in address_keywords) or (re.search(r"\d", line) and len(line) > 12):
            if len(line) > 10:
                address_lines.append(line)

    if not company_name and len(lines) > 1:
        candidate = next((line for line in lines if line.isupper() and len(line) > 2), None)
        if candidate:
            company_name = candidate

    address = ", ".join(address_lines[:3]) if address_lines else None
    website = website_match.group(0) if website_match else None
    phone = phone_match.group(0) if phone_match else None

    return normalize_response(
        {
            "name": name,
            "number": phone,
            "address": address,
            "website": website,
            "company_name": company_name,
            "designation": designation,
        }
    )


def validate_and_flag(data: Dict[str, Any], raw_text: str) -> tuple[Dict[str, Any], dict[str, Any]]:
    """
    Post-process structured data to reduce obvious mistakes and return review hints.

    - Nulls out clearly invalid phone/website values
    - Flags low-confidence fields for UI highlighting
    """
    normalized = normalize_response(data)
    warnings: list[str] = []
    low_confidence: list[str] = []

    website = _clean_website(normalized.get("website"))
    if normalized.get("website") and not website:
        warnings.append("Website looked unreliable; please review.")
        low_confidence.append("website")
    normalized["website"] = website

    number = _clean_phone(normalized.get("number"))
    if normalized.get("number") and not number:
        warnings.append("Phone number looked unreliable; please review.")
        low_confidence.append("number")
    normalized["number"] = number

    # If the model returned a name/company that doesn't appear in OCR at all, flag it.
    ocr_text = raw_text or ""
    for key in ("name", "company_name", "designation"):
        val = normalized.get(key)
        if not val:
            continue
        token = str(val).strip().lower()
        if token and _best_similarity(token, ocr_text) < 0.68:
            low_confidence.append(key)

    # Deduplicate while preserving order.
    seen = set()
    low_confidence = [x for x in low_confidence if not (x in seen or seen.add(x))]

    meta = {
        "warnings": warnings,
        "low_confidence_fields": low_confidence,
    }
    return normalized, meta


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


def _homomorphic_filter_dft(gray: np.ndarray) -> np.ndarray:
    """
    Homomorphic filtering using DFT (frequency domain) to reduce uneven illumination
    and boost detail. Works well for glossy cards / gradients.
    """
    if gray.ndim != 2:
        return gray

    img = gray.astype(np.float32)
    img = np.clip(img, 1.0, 255.0)

    log_img = np.log(img)
    dft = cv2.dft(log_img, flags=cv2.DFT_COMPLEX_OUTPUT)
    dft_shift = np.fft.fftshift(dft, axes=(0, 1))

    rows, cols = gray.shape[:2]
    cy, cx = rows // 2, cols // 2
    y = np.arange(rows, dtype=np.float32) - cy
    x = np.arange(cols, dtype=np.float32) - cx
    xx, yy = np.meshgrid(x, y)
    d2 = (xx * xx) + (yy * yy)

    # High-pass emphasis: attenuate low frequencies (illumination) and mildly boost high frequencies (detail).
    d0 = max(min(rows, cols) / 10.0, 20.0)
    gamma_l = 0.6
    gamma_h = 1.4
    c = 1.0
    h = (gamma_h - gamma_l) * (1.0 - np.exp(-c * (d2 / (d0 * d0)))) + gamma_l

    filtered = dft_shift * h[:, :, None]
    ishift = np.fft.ifftshift(filtered, axes=(0, 1))
    img_back = cv2.idft(ishift, flags=cv2.DFT_REAL_OUTPUT | cv2.DFT_SCALE)

    exp_img = np.exp(img_back)
    exp_img = np.clip(exp_img, 0, None)
    out = cv2.normalize(exp_img, None, 0, 255, cv2.NORM_MINMAX)
    return out.astype(np.uint8)


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


def _orientation_variants(gray: np.ndarray) -> list[np.ndarray]:
    """
    Generate cheap orientation variants to recover OCR when the camera app saves mirrored or rotated images.
    Keeps the set intentionally small to protect latency.
    """
    variants = [gray]

    # Portrait photos of cards often need a 90° rotation.
    h, w = gray.shape[:2]
    if h > int(w * 1.15):
        variants.append(cv2.rotate(gray, cv2.ROTATE_90_CLOCKWISE))

    # Mirrored selfies / front-cam captures.
    variants.append(cv2.flip(gray, 1))  # horizontal

    # Upside-down capture.
    variants.append(cv2.rotate(gray, cv2.ROTATE_180))

    # Dedup quickly (shape + small prefix bytes).
    unique: list[np.ndarray] = []
    seen: set[int] = set()
    for v in variants:
        key = (v.shape[0] << 16) ^ v.shape[1] ^ hash(v.tobytes()[:4096])
        if key in seen:
            continue
        seen.add(key)
        unique.append(v)
    return unique


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

    base_gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    gray_inputs = [base_gray]
    if OCR_ENABLE_ORIENTATION_VARIANTS:
        gray_inputs = _orientation_variants(base_gray)

    variants: list[np.ndarray] = []
    for gray in gray_inputs:
        # Illumination normalization: remove slow-varying background (glare/gradients).
        background = cv2.GaussianBlur(gray, (0, 0), sigmaX=15)
        norm = cv2.divide(gray, background, scale=255)

        # Local contrast (small fonts, low lighting).
        clahe = cv2.createCLAHE(clipLimit=2.7, tileGridSize=(8, 8))
        norm = clahe.apply(norm)

        # Frequency-domain illumination correction + detail emphasis (DFT).
        if OCR_ENABLE_HOMOMORPHIC_DFT:
            norm = _homomorphic_filter_dft(norm)

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
        variants.extend(
            [
                gray_deskew,
                cv2.morphologyEx(otsu_deskew, cv2.MORPH_OPEN, open_kernel, iterations=1),
                cv2.morphologyEx(adapt_g_deskew, cv2.MORPH_OPEN, open_kernel, iterations=1),
                cv2.morphologyEx(adapt_m, cv2.MORPH_OPEN, open_kernel, iterations=1),
                cv2.morphologyEx(otsu_deskew, cv2.MORPH_CLOSE, close_kernel, iterations=1),
            ]
        )

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


def _is_tesseract_timeout_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "timed out" in message or "timeout" in message


def extract_text_from_image(image: np.ndarray | list[np.ndarray]) -> str:
    try:
        candidates = image if isinstance(image, list) else [image]
        psm_values = [6, 11]  # block of text, sparse text

        best_text = ""
        best_score = -1
        start = monotonic()
        for candidate in candidates:
            for psm in psm_values:
                if monotonic() - start > OCR_TIME_BUDGET_SEC:
                    break
                config = (
                    f"--oem 3 --psm {psm} -l {TESSERACT_LANG} "
                    "-c preserve_interword_spaces=1 "
                    "-c tessedit_char_blacklist=|"
                )
                try:
                    extracted = pytesseract.image_to_string(
                        candidate, config=config, timeout=TESSERACT_TIMEOUT_SEC
                    ).strip()
                except RuntimeError as exc:
                    # pytesseract raises RuntimeError on subprocess timeouts; try other variants.
                    if _is_tesseract_timeout_error(exc):
                        continue
                    raise
                score = _score_ocr_text(extracted)
                if score > best_score:
                    best_score = score
                    best_text = extracted
                    # Early exit once we have a strong result.
                    if best_score > 1200 and len(best_text) > 220:
                        break
            else:
                continue
            break

        # If the main sweep timed out or produced little text, do a last quick fallback
        # on the best-looking candidate to avoid a hard scan failure.
        if best_score < 120 and candidates:
            fallback_candidate = candidates[0]
            fallback_configs = [
                f"--oem 3 --psm 6 -l {TESSERACT_LANG} -c preserve_interword_spaces=1",
                f"--oem 3 --psm 11 -l {TESSERACT_LANG} -c preserve_interword_spaces=1",
            ]
            for config in fallback_configs:
                try:
                    extracted = pytesseract.image_to_string(
                        fallback_candidate, config=config, timeout=max(6, TESSERACT_TIMEOUT_SEC // 2)
                    ).strip()
                except RuntimeError as exc:
                    if _is_tesseract_timeout_error(exc):
                        continue
                    raise
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
        return _heuristic_structured_data(raw_text)

    system_prompt = (
        "You are BizScannerExtract, a strict business-card information extractor.\n"
        "Output MUST be a single JSON object (no markdown, no extra text) with EXACTLY these keys:\n"
        "name, number, address, website, company_name, designation.\n"
        "Rules:\n"
        "- Use null when unknown or not present in the text. Never guess.\n"
        "- Only extract what is supported by the OCR text.\n"
        "- If multiple phone numbers exist: join into one string separated by comma.\n"
        "- Website: output a domain/URL without trailing punctuation/spaces; fix obvious OCR typos like 'htrp'->'http'.\n"
        "- Phone: keep + and digits; remove obvious junk; keep readable separators.\n"
        "- Address: keep as a single line string (commas allowed).\n"
        "- Do not invent companies/titles; if unclear, set null.\n"
    )

    user_prompt = (
        "Extract the fields from this OCR text. Remember: JSON only, exactly the required keys.\n\n"
        f"OCR TEXT:\n{raw_text}"
    )

    payload = {
        "model": GROQ_MODEL,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
    }

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(GROQ_URL, headers=headers, json=payload, timeout=GROQ_TIMEOUT_SEC)
        response.raise_for_status()
        response_payload = response.json()
        message = response_payload.get("choices", [{}])[0].get("message", {})
        model_text = (message.get("content") or "").strip()
        parsed = _extract_json_object(model_text)
        if not parsed and isinstance(message.get("parsed"), dict):
            parsed = message["parsed"]
        if not parsed:
            parsed = response_payload if isinstance(response_payload, dict) else None
        if not isinstance(parsed, dict):
            raise RuntimeError("Groq response did not include valid JSON.")
        return normalize_response(parsed)
    except Exception:
        # Best-effort fallback: keep the scan usable even when the model misbehaves.
        fallback = _heuristic_structured_data(raw_text)
        if any(value is not None for value in fallback.values()):
            return fallback
        raise


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
        variants = _generate_ocr_variants(image_bytes)[: max(OCR_MAX_VARIANTS, 1)]
        raw_text = extract_text_from_image(variants)
        structured_raw = get_structured_data_with_groq(raw_text)
        structured, meta = validate_and_flag(structured_raw, raw_text=raw_text)
        meta["source"] = "groq" if GROQ_API_KEY else "heuristic"
    except OCRDependencyError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    return jsonify({"raw_text": raw_text, "data": structured, "meta": meta})


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
            "version": APP_VERSION or None,
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

    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "0").lower() in {"1", "true", "yes"}
    app.run(host="0.0.0.0", port=port, debug=debug)
