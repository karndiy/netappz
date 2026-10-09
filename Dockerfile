# bullseye base: Docker 19.03 seccomp blocks clone3 used by newer glibc (bookworm+)
FROM python:3.12-slim-bullseye

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FILES_ROOT=/data/shared \
    NOTES_DATABASE=/data/instance/notes.sqlite3 \
    PORT=5000

WORKDIR /app

# LibreOffice powers Word to PDF. Carlito/Caladea match Word's Calibri/Cambria
# metrics, and TLWG adds Thai fonts so Thai documents render correctly.
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
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY app.py converters.py pdf_editor.py chat.py telegram_bot.py ./
COPY templates ./templates
COPY static ./static

RUN useradd --create-home appuser \
    && mkdir -p /data/shared /data/instance \
    && chown -R appuser:appuser /data
USER appuser

VOLUME ["/data"]
EXPOSE 5000

# One worker keeps the auto-generated SECRET_KEY consistent and SQLite simple;
# threads handle concurrent requests. Long timeout allows large uploads/ZIPs.
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT} --workers 1 --threads 8 --timeout 600 app:app"]
