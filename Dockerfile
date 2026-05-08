FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# System deps:
# - tesseract-ocr: OCR engine
# - libgl1 + libglib2.0-0: runtime deps for opencv
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV TESSERACT_CMD=/usr/bin/tesseract

CMD ["sh", "-c", "gunicorn app:app --bind 0.0.0.0:$PORT --timeout 120 --graceful-timeout 30 --keep-alive 5"]
