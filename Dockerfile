# bullseye base: Docker 19.03 seccomp blocks clone3 used by newer glibc (bookworm+)
FROM python:3.12-slim-bullseye

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FILES_ROOT=/data/shared \
    NOTES_DATABASE=/data/instance/notes.sqlite3 \
    FEED_MEDIA=/data/feed \
    PORT=5000

WORKDIR /app

# LibreOffice powers Word to PDF. Carlito/Caladea match Word's Calibri/Cambria
# metrics, and TLWG adds Thai fonts so Thai documents render correctly.
# Tesseract reads scanned PDFs (Thai + English) for PDF to Word.
# Debian 11 reached end of life, so packages come from the frozen archive.
RUN printf '%s\n' \
        'deb http://archive.debian.org/debian bullseye main' \
        'deb http://archive.debian.org/debian bullseye-updates main' \
        'deb http://archive.debian.org/debian-security bullseye-security main' \
        > /etc/apt/sources.list \
    && rm -f /etc/apt/sources.list.d/* \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends \
        libreoffice-writer-nogui \
        fonts-dejavu-core fonts-liberation2 fonts-crosextra-carlito fonts-crosextra-caladea fonts-thai-tlwg \
        tesseract-ocr tesseract-ocr-tha tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

# Tesseract's "best" Thai and English models read scans far more accurately than the
# small ones Debian ships. Pinned to the 4.1.0 release and checked by SHA-256.
RUN mkdir -p /usr/share/tesseract-ocr/best \
    && cp -r /usr/share/tesseract-ocr/4.00/tessdata/configs /usr/share/tesseract-ocr/best/ \
    && python -c "import hashlib, urllib.request; \
url = 'https://raw.githubusercontent.com/tesseract-ocr/tessdata_best/4.1.0/{}.traineddata'; \
want = {'tha': 'ee8adab6dc69eb8df3d3c8307ae8295471b7bd7d86a06d9267aa8f479b064eac', \
        'eng': '8280aed0782fe27257a68ea10fe7ef324ca0f8d85bd2fd145d1c2b560bcb66ba'}; \
[(lambda data: (hashlib.sha256(data).hexdigest() == sha or exit(f'{lang}: bad checksum')) \
    and open(f'/usr/share/tesseract-ocr/best/{lang}.traineddata', 'wb').write(data)) \
  (urllib.request.urlopen(url.format(lang), timeout=120).read()) for lang, sha in want.items()]"

# Word documents name Windows Thai fonts; map them to the matching TLWG fonts
# (Laksaman is TH Sarabun) so LibreOffice lays Thai text out the same way.
COPY fonts-thai-aliases.conf /etc/fonts/conf.d/60-thai-aliases.conf

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY app.py converters.py pdf_editor.py pdf_word.py chat.py telegram_bot.py ./
COPY templates ./templates
COPY static ./static

RUN useradd --create-home appuser \
    && mkdir -p /data/shared /data/instance /data/feed \
    && chown -R appuser:appuser /data
USER appuser

VOLUME ["/data"]
EXPOSE 5000

# One worker keeps the auto-generated SECRET_KEY consistent and SQLite simple;
# threads handle concurrent requests. Long timeout allows large uploads/ZIPs.
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT} --workers 1 --threads 8 --timeout 600 app:app"]
