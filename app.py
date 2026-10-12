import hmac
import io
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import shutil
import tempfile
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from flask import (
    Flask,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_session import Session
from werkzeug.utils import secure_filename

import chat
import converters
import pdf_editor
import pdf_word


# Settings that can be changed on the Settings page. A saved value overrides the
# server's environment (.env); clearing it falls back to the environment again.
SETTING_GROUPS = [
    {
        "title": "Chat AI",
        "description": "Used by the Chat page and the Telegram bot.",
        "fields": [
            {
                "name": "CHAT_PROVIDER", "label": "AI provider", "default": chat.DEFAULT_PROVIDER,
                "options": [
                    ("anthropic", "Claude (Anthropic API)"),
                    ("local", "Local AI (OpenAI-compatible server)"),
                ],
                "help": "Claude uses the Anthropic settings below; Local AI uses the Local AI settings.",
            },
            {
                "name": "ANTHROPIC_API_KEY", "label": "Anthropic API key", "secret": True,
                "help": "Create one at platform.claude.com/settings/keys. Usage is billed to this key.",
            },
            {
                "name": "CHAT_MODEL", "label": "Model", "default": chat.DEFAULT_MODEL,
                "choices": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"],
                "help": "Pick a model or type another model ID.",
            },
            {
                "name": "CHAT_EFFORT", "label": "Effort", "default": chat.DEFAULT_EFFORT,
                "options": [("low", "Low: fastest"), ("medium", "Medium: balanced"), ("high", "High: most thorough")],
                "help": "How hard Claude thinks before replying. Claude only.",
            },
            {
                "name": "CHAT_INSTRUCTIONS", "label": "Extra instructions", "multiline": True,
                "help": "Optional. For example: Always reply in Thai.",
            },
        ],
    },
    {
        "title": "Local AI",
        "description": "An OpenAI-compatible server on your network or company, such as Open WebUI, vLLM, LM Studio, or Ollama.",
        "fields": [
            {
                "name": "LOCAL_AI_URL", "label": "API base URL", "default": chat.DEFAULT_LOCAL_URL,
                "help": "The address that ends before /chat/completions. Open WebUI: https://<server>/api/v1",
            },
            {
                "name": "LOCAL_AI_API_KEY", "label": "API key", "secret": True,
                "help": "Open WebUI: Settings → Account → API keys. Leave empty if the server needs no key.",
            },
            {
                "name": "LOCAL_AI_MODEL", "label": "Model", "default": chat.DEFAULT_LOCAL_MODEL,
                "choices": [chat.DEFAULT_LOCAL_MODEL],
                "help": "The model ID as the server lists it.",
            },
        ],
    },
    {
        "title": "Telegram bot",
        "description": "Chat with the AI from your phone. Changes apply within about a minute.",
        "fields": [
            {
                "name": "TELEGRAM_BOT_TOKEN", "label": "Bot token", "secret": True,
                "help": "From @BotFather in Telegram. Leave empty to turn the bot off.",
            },
            {
                "name": "TELEGRAM_ALLOWED_USERS", "label": "Allowed user IDs",
                "help": "Comma-separated Telegram user IDs. Message the bot once and it replies with your ID.",
            },
        ],
    },
]
SETTING_FIELDS = {field["name"]: field for group in SETTING_GROUPS for field in group["fields"]}
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@\[\]-]{0,99}")
LOCAL_URL = re.compile(r"https?://[^\s/]+(/\S*)?")
TELEGRAM_TOKEN = re.compile(r"\d+:[A-Za-z0-9_-]{30,}")
TELEGRAM_USERS = re.compile(r"-?\d+(,-?\d+)*")


def verify_telegram_token(token):
    """Return the bot's @username, or raise ValueError when Telegram rejects the token."""
    from telegram_bot import Telegram

    try:
        return Telegram(token).call("getMe", http_timeout=10).get("username", "")
    except Exception as error:
        raise ValueError(f"Telegram did not accept this bot token ({error}).") from error


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("FILES_SECRET_KEY") or secrets.token_hex(32),
        FILES_ROOT=os.environ.get("FILES_ROOT", str(Path(__file__).parent / "shared")),
        NOTES_DATABASE=os.environ.get(
            "NOTES_DATABASE", str(Path(app.instance_path) / "notes.sqlite3")
        ),
        DATABASE_URL=os.environ.get("DATABASE_URL"),
        REDIS_URL=os.environ.get("REDIS_URL"),
        MAX_CONTENT_LENGTH=5 * 1024 * 1024 * 1024,
    )
    if test_config:
        app.config.update(test_config)

    root = Path(app.config["FILES_ROOT"]).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    app.config["FILES_ROOT"] = root
    database_url = app.config["DATABASE_URL"]
    if database_url:
        import psycopg
        from psycopg.rows import dict_row

        @contextmanager
        def notes_connection():
            with psycopg.connect(database_url, row_factory=dict_row) as connection:
                yield connection

        def sql(query):
            return query.replace("?", "%s")

        returning_id = " RETURNING id"
        id_column = "id SERIAL PRIMARY KEY"
        timestamp_type = "TIMESTAMP"
        timestamp_select = "to_char({0}, 'YYYY-MM-DD HH24:MI:SS') AS {0}"
    else:
        notes_database = Path(app.config["NOTES_DATABASE"]).expanduser().resolve()
        notes_database.parent.mkdir(parents=True, exist_ok=True)

        @contextmanager
        def notes_connection():
            connection = sqlite3.connect(notes_database)
            connection.row_factory = sqlite3.Row
            try:
                with connection:
                    yield connection
            finally:
                connection.close()

        def sql(query):
            return query

        returning_id = ""
        id_column = "id INTEGER PRIMARY KEY AUTOINCREMENT"
        timestamp_type = "TEXT"
        timestamp_select = "{0}"

    with notes_connection() as connection:
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS notes (
                {id_column},
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

    notes_query = (
        "SELECT id, title, content, "
        f"{timestamp_select.format('created_at')}, {timestamp_select.format('updated_at')} "
        "FROM notes ORDER BY notes.updated_at DESC, id DESC"
    )

    # Redis keeps sessions server-side and caches the notes list when configured.
    cache = None
    if app.config["REDIS_URL"]:
        import redis

        cache = redis.Redis.from_url(app.config["REDIS_URL"])
        app.config.update(SESSION_TYPE="redis", SESSION_REDIS=cache)
        Session(app)

    notes_cache_key = "notes:list"

    def load_notes():
        if cache is not None:
            cached = cache.get(notes_cache_key)
            if cached is not None:
                return json.loads(cached)
        with notes_connection() as connection:
            rows = [dict(row) for row in connection.execute(notes_query).fetchall()]
        if cache is not None:
            cache.set(notes_cache_key, json.dumps(rows), ex=300)
        return rows

    def invalidate_notes():
        if cache is not None:
            cache.delete(notes_cache_key)

    with notes_connection() as connection:
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS chat_conversations (
                {id_column},
                title TEXT NOT NULL,
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS chat_messages (
                {id_column},
                conversation_id INTEGER NOT NULL REFERENCES chat_conversations (id),
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                source TEXT NOT NULL,
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

    class ChatStore:
        """Conversations shared by the web chat page and the Telegram bot."""

        conversation_columns = (
            f"id, title, {timestamp_select.format('created_at')}, {timestamp_select.format('updated_at')}"
        )

        def conversations(self):
            with notes_connection() as connection:
                rows = connection.execute(
                    f"SELECT {self.conversation_columns} FROM chat_conversations "
                    "ORDER BY chat_conversations.updated_at DESC, id DESC"
                ).fetchall()
            return [dict(row) for row in rows]

        def conversation(self, conversation_id):
            with notes_connection() as connection:
                row = connection.execute(
                    sql(f"SELECT {self.conversation_columns} FROM chat_conversations WHERE id = ?"),
                    (conversation_id,),
                ).fetchone()
            return dict(row) if row else None

        def latest_id(self):
            conversations = self.conversations()
            return conversations[0]["id"] if conversations else None

        def create(self, title="New chat"):
            with notes_connection() as connection:
                cursor = connection.execute(
                    sql("INSERT INTO chat_conversations (title) VALUES (?)") + returning_id,
                    (title,),
                )
                return cursor.fetchone()["id"] if returning_id else cursor.lastrowid

        def messages(self, conversation_id, after_id=0):
            with notes_connection() as connection:
                rows = connection.execute(
                    sql(
                        "SELECT id, role, content, source, "
                        f"{timestamp_select.format('created_at')} FROM chat_messages "
                        "WHERE conversation_id = ? AND id > ? ORDER BY id"
                    ),
                    (conversation_id, after_id),
                ).fetchall()
            return [dict(row) for row in rows]

        def add(self, conversation_id, role, content, source):
            with notes_connection() as connection:
                cursor = connection.execute(
                    sql(
                        "INSERT INTO chat_messages (conversation_id, role, content, source) "
                        "VALUES (?, ?, ?, ?)"
                    )
                    + returning_id,
                    (conversation_id, role, content, source),
                )
                message_id = cursor.fetchone()["id"] if returning_id else cursor.lastrowid
                connection.execute(
                    sql("UPDATE chat_conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = ?"),
                    (conversation_id,),
                )
                if role == "user":
                    # The first message names the conversation, like most chat apps.
                    connection.execute(
                        sql(
                            "UPDATE chat_conversations SET title = ? "
                            "WHERE id = ? AND title = 'New chat'"
                        ),
                        (" ".join(content.split())[:60] or "New chat", conversation_id),
                    )
            return message_id

        def delete(self, conversation_id):
            with notes_connection() as connection:
                connection.execute(
                    sql("DELETE FROM chat_messages WHERE conversation_id = ?"), (conversation_id,)
                )
                cursor = connection.execute(
                    sql("DELETE FROM chat_conversations WHERE id = ?"), (conversation_id,)
                )
            return cursor.rowcount > 0

    chat_store = ChatStore()
    app.extensions["chat"] = chat_store

    with notes_connection() as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS app_settings (name TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )

    class SettingsStore:
        """Values saved on the Settings page, shared with the Telegram bot through the database."""

        def all(self):
            with notes_connection() as connection:
                rows = connection.execute("SELECT name, value FROM app_settings").fetchall()
            return {row["name"]: row["value"] for row in rows}

        def get(self, name):
            with notes_connection() as connection:
                row = connection.execute(
                    sql("SELECT value FROM app_settings WHERE name = ?"), (name,)
                ).fetchone()
            return row["value"] if row else None

        def save(self, changes):
            """Apply {name: value}; a value of None removes the saved setting."""
            with notes_connection() as connection:
                for name, value in changes.items():
                    if value is None:
                        connection.execute(sql("DELETE FROM app_settings WHERE name = ?"), (name,))
                    else:
                        connection.execute(
                            sql(
                                "INSERT INTO app_settings (name, value) VALUES (?, ?) "
                                "ON CONFLICT (name) DO UPDATE SET value = excluded.value"
                            ),
                            (name, value),
                        )

    settings_store = SettingsStore()
    app.extensions["settings"] = settings_store
    chat.use_settings(settings_store.get)

    def parse_note_time(value):
        # Both backends store UTC as "YYYY-MM-DD HH:MM:SS".
        return datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)

    @app.template_filter("note_iso")
    def note_iso(value):
        return parse_note_time(value).isoformat().replace("+00:00", "Z")

    @app.template_filter("note_short_date")
    def note_short_date(value):
        moment = parse_note_time(value)
        if moment.date() == datetime.now(timezone.utc).date():
            return moment.strftime("%H:%M")
        return moment.strftime("%Y-%m-%d")

    @app.template_filter("note_long_date")
    def note_long_date(value):
        moment = parse_note_time(value)
        return f"{moment.day} {moment.strftime('%B %Y')} at {moment.strftime('%H:%M')} UTC"

    def relative_path(value):
        value = value or ""
        if "\x00" in value or value.startswith(("/", "\\")):
            abort(400, "Invalid path.")
        value = value.replace("\\", "/")
        parts = value.split("/")
        if any(part in (".", "..") for part in parts):
            abort(400, "Invalid path.")
        if parts and len(parts[0]) >= 2 and parts[0][1] == ":":
            abort(400, "Invalid path.")
        return Path(*[part for part in parts if part])

    def resolve_path(value):
        relative = relative_path(value)
        target = (root / relative).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            abort(400, "Invalid path.")

        current = root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                abort(400, "Symbolic links are not supported.")
        return target

    def normalized_relative(value):
        path = relative_path(value)
        return path.as_posix() if path.parts else ""

    def csrf_token():
        if "_csrf_token" not in session:
            session["_csrf_token"] = secrets.token_urlsafe(32)
        return session["_csrf_token"]

    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.before_request
    def validate_csrf():
        if request.method == "POST":
            # Forms send the token as a field; the PDF editor's JSON requests send a header.
            supplied = request.form.get("_csrf_token") or request.headers.get("X-CSRF-Token", "")
            if not hmac.compare_digest(supplied, session.get("_csrf_token", "")):
                abort(400, "Invalid form token. Reload the page and try again.")

    @app.get("/")
    def index():
        relative = relative_path(request.args.get("path", ""))
        directory = resolve_path(relative.as_posix() if relative.parts else "")
        if not directory.is_dir():
            abort(404)

        entries = []
        for item in sorted(directory.iterdir(), key=lambda entry: (entry.is_file(), entry.name.lower())):
            if item.is_symlink():
                continue
            item_relative = item.relative_to(root).as_posix()
            entries.append(
                {
                    "name": item.name,
                    "path": item_relative,
                    "is_dir": item.is_dir(),
                    "size": item.stat().st_size if item.is_file() else None,
                }
            )
        current = relative.as_posix() if relative.parts else ""
        parent = PurePosixPath(current).parent.as_posix() if current else None
        if parent == ".":
            parent = ""
        return render_template(
            "index.html",
            entries=entries,
            current=current,
            parent=parent,
        )

    @app.get("/updates")
    def updates():
        entries = [
            {
                "version": "1.18.0",
                "date": "2026-10-12",
                "changes": [
                    "Chat and the Telegram bot can now use a local AI instead of Claude: pick the AI provider on the Settings page.",
                    "Local AI works with any OpenAI-compatible server (Open WebUI, vLLM, LM Studio, Ollama). It starts set to https://ai.esm-solution.com/api/v1 with the qwen3/qwen3.6-35b model.",
                    "Set the server address, API key, and model on the Settings page; changes apply right away.",
                    "Replies stream in as they are written, and the model's hidden thinking (<think>) is left out of the reply.",
                ],
            },
            {
                "version": "1.17.0",
                "date": "2026-10-09",
                "changes": [
                    "PDF to Word now keeps Thai text at the right size and in bold in Word (Thai uses Word's complex-script settings, which were missing).",
                    "Thai fonts get the names Word knows, such as TH Sarabun New, and Thai lines no longer clip their tone marks.",
                    "Scanned PDFs are read with Thai + English text recognition (OCR) and rebuilt as editable paragraphs, headings, and table rows.",
                    "English inside Thai sentences is read correctly, and broken Thai from older PDFs (split sara am, shifted tone marks) is repaired.",
                    "Choose whether scanned pages are recognized automatically, always, or never; the result says what was done.",
                    "Word to PDF lays out documents that use TH Sarabun, Angsana, Cordia, or Tahoma with matching Thai fonts.",
                ],
            },
            {
                "version": "1.16.0",
                "date": "2026-10-09",
                "changes": [
                    "Added Edit PDF, an Acrobat-style editor opened from the Convert page.",
                    "Click any line of text in a PDF to change it; add text, pictures, and a drawn signature; draw, add shapes, highlight, and comment.",
                    "Whiteout covers content, and Redact permanently removes text, pictures, and drawings under the box.",
                    "Rotate, reorder, duplicate, delete, and insert blank pages; add a watermark and page numbers.",
                    "Undo and redo, zoom, drag-and-drop pictures onto a page, and automatic saving so a reload keeps your edits for 12 hours.",
                    "Download the edited PDF or save a copy straight into Files. Works on phones and tablets too.",
                ],
            },
            {
                "version": "1.15.0",
                "date": "2026-10-09",
                "changes": [
                    "Added Merge PDFs to the Convert page: combine up to 50 PDFs into one, with a bookmark for each file.",
                    "Drag the chosen files into a different order, or use the arrow buttons on phones and tablets; remove files one by one.",
                    "Dropping or choosing more files now adds them to the list instead of replacing it.",
                    "Dropping a file outside a drop area no longer opens it in the browser and leaves the app.",
                ],
            },
            {
                "version": "1.14.1",
                "date": "2026-10-09",
                "changes": [
                    "Moved the note Save button to the top toolbar, between the full screen and delete buttons.",
                    "The button is always in reach, even in long notes and with the phone keyboard open.",
                ],
            },
            {
                "version": "1.14.0",
                "date": "2026-10-09",
                "changes": [
                    "The app can now be opened at http://netappz.local from phones, tablets, and computers on the same network.",
                    "The app also listens on port 80, so the address no longer needs :5000. The old address still works.",
                    "The netappz.local name follows the server's IP address if it changes.",
                ],
            },
            {
                "version": "1.13.0",
                "date": "2026-10-08",
                "changes": [
                    "Added a Settings page for the Anthropic API key, chat model, effort, extra instructions, and the Telegram bot.",
                    "Saved settings apply right away, without editing .env or restarting; secrets are never shown in full.",
                    "Fixed the Telegram bot stopping as soon as it started checking for messages.",
                ],
            },
            {
                "version": "1.12.0",
                "date": "2026-10-08",
                "changes": [
                    "Added a Chat page for talking with Claude, with replies that appear as they are written.",
                    "Added an optional Telegram bot that shares the same conversations, so you can chat from anywhere.",
                    "Only Telegram accounts you allow can use the bot; /new starts a fresh conversation.",
                ],
            },
            {
                "version": "1.11.0",
                "date": "2026-10-08",
                "changes": [
                    "Added a Convert page with Word to PDF, PDF to Word, Image to PDF, and PDF to Image tools.",
                    "Image to PDF combines several images into one PDF and supports iPhone HEIC photos.",
                    "PDF to Image saves PNG or JPG pages, as a ZIP when the PDF has more than one page.",
                ],
            },
            {
                "version": "1.10.0",
                "date": "2026-10-08",
                "changes": [
                    "Notes now fill the whole screen on phones, tablets, laptops, and large displays.",
                    "Added a sidebar button to hide the note list and a full-screen button where the browser supports it.",
                    "Notes follow the device's light or dark appearance and stay clear of notches and home bars.",
                    "Larger tap targets on touch screens and a compact layout for phones in landscape.",
                ],
            },
            {
                "version": "1.9.0",
                "date": "2026-10-08",
                "changes": [
                    "Redesigned Notes to look like macOS Notes, with a note list beside a full-height editor.",
                    "Notes save automatically while you type; Command-S or Ctrl-S also saves.",
                    "Added note search, a compose button for new notes, and a phone layout with a back button.",
                ],
            },
            {
                "version": "1.8.0",
                "date": "2026-10-08",
                "changes": [
                    "Added a search box to the update log to filter by version, date, or keyword.",
                ],
            },
            {
                "version": "1.7.0",
                "date": "2026-10-08",
                "changes": [
                    "Notes are now stored in PostgreSQL when DATABASE_URL is set; SQLite is still used otherwise.",
                    "Sessions are kept in Redis when REDIS_URL is set, so they survive app restarts.",
                    "The notes list is cached in Redis and refreshed whenever a note changes.",
                    "Docker Compose now starts PostgreSQL and Redis alongside the app.",
                ],
            },
            {
                "version": "1.6.0",
                "date": "2026-10-08",
                "changes": [
                    "Added Docker support with a Dockerfile and Docker Compose setup.",
                    "The app now runs on the gunicorn production server inside Docker.",
                    "Shared files and notes are kept in mounted folders between container restarts.",
                ],
            },
            {
                "version": "1.5.0",
                "date": "2026-10-07",
                "changes": [
                    "Added a basic notes page with saved, editable notes.",
                ],
            },
            {
                "version": "1.4.0",
                "date": "2026-10-07",
                "changes": [
                    "Added a notes page with persistent create, edit, and delete actions.",
                ],
            },
            {
                "version": "1.3.0",
                "date": "2026-10-07",
                "changes": [
                    "Improved the layout and touch controls for phones, tablets, and desktop screens.",
                ],
            },
            {
                "version": "1.2.0",
                "date": "2026-10-07",
                "changes": [
                    "Added rename actions for files and folders.",
                    "Added confirmed deletion for files and folders.",
                ],
            },
            {
                "version": "1.1.0",
                "date": "2026-10-07",
                "changes": [
                    "Removed sign-in so devices on the network can open the file manager directly.",
                ],
            },
            {
                "version": "1.0.0",
                "date": "2026-10-07",
                "changes": [
                    "Added LAN file browsing, folder creation, and file or folder uploads.",
                    "Added file downloads and ZIP downloads for folders.",
                ],
            },
        ]
        query = request.args.get("q", "").strip()[:100]
        if query:
            needle = query.lower()
            matches = []
            for entry in entries:
                if needle in entry["version"].lower() or needle in entry["date"]:
                    matches.append(entry)
                    continue
                changes = [change for change in entry["changes"] if needle in change.lower()]
                if changes:
                    matches.append({**entry, "changes": changes})
            entries = matches
        return render_template("updates.html", entries=entries, query=query)

    @app.get("/notes")
    def notes():
        all_notes = load_notes()
        query = request.args.get("q", "").strip()[:100]
        shown = all_notes
        if query:
            needle = query.lower()
            shown = [
                note
                for note in all_notes
                if needle in note["title"].lower() or needle in note["content"].lower()
            ]

        note_id = request.args.get("id", type=int)
        creating = request.args.get("new") == "1" or not all_notes
        selected = None
        if note_id is not None and not creating:
            selected = next((note for note in all_notes if note["id"] == note_id), None)
            if selected is None:
                abort(404)
        elif not creating and shown:
            selected = shown[0]

        return render_template(
            "notes.html",
            notes=shown,
            total=len(all_notes),
            selected=selected,
            creating=creating,
            # On phones the editor replaces the list only when a note was explicitly opened.
            explicit=note_id is not None or request.args.get("new") == "1",
            query=query,
        )

    @app.post("/notes/create")
    def create_note():
        title = " ".join(request.form.get("title", "").splitlines()).strip()
        content = request.form.get("content", "").strip()
        if not title or not content:
            flash("Enter both a title and note text.", "error")
        elif len(title) > 120 or len(content) > 10000:
            flash("Titles must be at most 120 characters and notes at most 10,000.", "error")
        else:
            with notes_connection() as connection:
                cursor = connection.execute(
                    sql("INSERT INTO notes (title, content) VALUES (?, ?)") + returning_id,
                    (title, content),
                )
                note_id = cursor.fetchone()["id"] if returning_id else cursor.lastrowid
            invalidate_notes()
            flash("Note saved.", "success")
            return redirect(url_for("notes", id=note_id))
        return redirect(url_for("notes", new=1))

    @app.post("/notes/<int:note_id>/edit")
    def edit_note(note_id):
        title = " ".join(request.form.get("title", "").splitlines()).strip()
        content = request.form.get("content", "").strip()
        if not title or not content:
            flash("Enter both a title and note text.", "error")
        elif len(title) > 120 or len(content) > 10000:
            flash("Titles must be at most 120 characters and notes at most 10,000.", "error")
        else:
            with notes_connection() as connection:
                cursor = connection.execute(
                    sql(
                        """
                        UPDATE notes
                        SET title = ?, content = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """
                    ),
                    (title, content, note_id),
                )
            if cursor.rowcount == 0:
                abort(404)
            invalidate_notes()
            flash("Note updated.", "success")
        return redirect(url_for("notes", id=note_id))

    @app.post("/notes/<int:note_id>/delete")
    def delete_note(note_id):
        with notes_connection() as connection:
            cursor = connection.execute(sql("DELETE FROM notes WHERE id = ?"), (note_id,))
        if cursor.rowcount == 0:
            abort(404)
        invalidate_notes()
        flash("Note deleted.", "success")
        return redirect(url_for("notes"))

    @app.post("/folder")
    def create_folder():
        current = normalized_relative(request.form.get("path", ""))
        name = request.form.get("name", "").strip()
        if not name or name in (".", "..") or "/" in name or "\\" in name:
            flash("Enter a valid folder name.", "error")
            return redirect(url_for("index", path=current))
        parent = resolve_path(current)
        if not parent.is_dir():
            abort(404)
        folder = resolve_path((Path(current) / name).as_posix())
        try:
            folder.mkdir()
        except FileExistsError:
            flash("A file or folder with that name already exists.", "error")
        else:
            flash("Folder created.", "success")
        return redirect(url_for("index", path=current))

    @app.post("/rename")
    def rename_item():
        source_relative = normalized_relative(request.form.get("path", ""))
        source = resolve_path(source_relative)
        if not source_relative or not source.exists():
            abort(404)
        parent_relative = (
            source.parent.relative_to(root).as_posix()
            if source.parent != root
            else ""
        )

        new_name = request.form.get("name", "").strip()
        if (
            not new_name
            or new_name in (".", "..")
            or "/" in new_name
            or "\\" in new_name
            or "\x00" in new_name
        ):
            flash("Enter a valid file or folder name.", "error")
            return redirect(url_for("index", path=parent_relative))

        destination = resolve_path(
            (Path(source_relative).parent / new_name).as_posix()
        )
        if destination.exists():
            flash("A file or folder with that name already exists.", "error")
            return redirect(url_for("index", path=parent_relative))

        try:
            source.rename(destination)
        except OSError as error:
            flash(f"Could not rename item: {error}", "error")
        else:
            flash("Item renamed.", "success")
        return redirect(url_for("index", path=parent_relative))

    @app.post("/delete")
    def delete_item():
        relative = normalized_relative(request.form.get("path", ""))
        target = resolve_path(relative)
        if not relative or not target.exists():
            abort(404)

        parent = target.parent.relative_to(root).as_posix()
        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        except OSError as error:
            flash(f"Could not delete item: {error}", "error")
        else:
            flash("Item deleted.", "success")
        return redirect(url_for("index", path=parent))

    @app.post("/upload")
    def upload():
        current = normalized_relative(request.form.get("path", ""))
        destination = resolve_path(current)
        if not destination.is_dir():
            abort(404)

        uploads = request.files.getlist("files") + request.files.getlist("folder_files")
        saved = 0
        for upload_file in uploads:
            original = upload_file.filename or ""
            if not original:
                continue
            original = original.replace("\\", "/")
            relative = relative_path(original)
            if not relative.parts:
                continue
            safe_name = secure_filename(relative.name)
            if not safe_name:
                flash(f"Skipped an invalid filename: {relative.name}", "error")
                continue

            relative = relative.with_name(safe_name)
            target = resolve_path((Path(current) / relative).as_posix())
            target.parent.mkdir(parents=True, exist_ok=True)
            target = resolve_path((Path(current) / relative).as_posix())
            if target.is_dir():
                flash(f"Skipped a folder named {safe_name}.", "error")
                continue
            upload_file.save(target)
            saved += 1

        if saved:
            flash(f"Uploaded {saved} file(s).", "success")
        elif not uploads:
            flash("Choose at least one file to upload.", "error")
        return redirect(url_for("index", path=current))

    @app.get("/download/<path:relative>")
    def download(relative):
        target = resolve_path(relative)
        if target.is_file():
            return send_file(target, as_attachment=True, download_name=target.name)
        if not target.is_dir():
            abort(404)

        archive_file = tempfile.TemporaryFile()
        with zipfile.ZipFile(archive_file, "w", zipfile.ZIP_DEFLATED) as archive:
            for item in target.rglob("*"):
                if item.is_symlink():
                    continue
                if item.is_file():
                    archive.write(item, item.relative_to(target.parent))
        archive_file.seek(0)
        return send_file(
            archive_file,
            as_attachment=True,
            download_name=f"{target.name}.zip",
            mimetype="application/zip",
        )

    convert_tools = {
        "word2pdf": converters.WORD_EXTENSIONS,
        "pdf2word": converters.PDF_EXTENSIONS,
        "image2pdf": converters.IMAGE_EXTENSIONS,
        "pdf2image": converters.PDF_EXTENSIONS,
        "pdfmerge": converters.PDF_EXTENSIONS,
    }
    multi_file_tools = {"image2pdf", "pdfmerge"}
    max_convert_bytes = 200 * 1024 * 1024

    def download_stem(filename):
        # Keep non-English names (e.g. Thai) for the download, minus path and unsafe characters.
        name = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
        stem = "".join(
            char for char in Path(name).stem if char.isprintable() and char not in '<>:"/\\|?*'
        ).strip(" .")
        return stem[:100] or "converted"

    @app.get("/chat")
    def chat_page():
        conversations = chat_store.conversations()
        conversation_id = request.args.get("id", type=int)
        starting_new = request.args.get("new") == "1"
        selected = None
        if conversation_id is not None:
            selected = chat_store.conversation(conversation_id)
            if selected is None:
                abort(404)
        elif conversations and not starting_new:
            selected = conversations[0]
        return render_template(
            "chat.html",
            conversations=conversations,
            selected=selected,
            messages=chat_store.messages(selected["id"]) if selected else [],
            configured=chat.is_configured(),
            provider=chat.provider(),
            assistant=chat.assistant_name(),
            telegram_enabled=bool(chat.setting("TELEGRAM_BOT_TOKEN")),
            # On phones the conversation replaces the list only when one was explicitly opened.
            explicit=conversation_id is not None or starting_new,
        )

    @app.post("/chat/send")
    def chat_send():
        text = request.form.get("message", "").strip()
        if not text:
            return jsonify(error="Type a message first."), 400
        if len(text) > 20000:
            return jsonify(error="Messages can be at most 20,000 characters."), 400
        conversation_id = request.form.get("conversation_id", type=int)
        if conversation_id is None or chat_store.conversation(conversation_id) is None:
            conversation_id = chat_store.create()
        user_message_id = chat_store.add(conversation_id, "user", text, "web")
        history = chat_store.messages(conversation_id)

        def events():
            def line(payload):
                return json.dumps(payload, ensure_ascii=False) + "\n"

            yield line({"type": "start", "conversation_id": conversation_id, "user_message_id": user_message_id})
            replies = chat.stream_reply(history)
            try:
                while True:
                    yield line(next(replies))
            except StopIteration as done:
                reply = done.value
            except chat.ChatError as error:
                yield line({"type": "error", "error": str(error)})
                return
            except Exception:
                app.logger.exception("Chat reply failed")
                yield line({"type": "error", "error": "Something went wrong while the AI was replying."})
                return
            message_id = chat_store.add(conversation_id, "assistant", reply, "web") if reply else None
            yield line({"type": "done", "message_id": message_id})

        return Response(
            events(),
            mimetype="application/x-ndjson",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/chat/<int:conversation_id>/messages")
    def chat_messages(conversation_id):
        if chat_store.conversation(conversation_id) is None:
            abort(404)
        after_id = request.args.get("after", default=0, type=int)
        return jsonify(messages=chat_store.messages(conversation_id, after_id))

    @app.post("/chat/<int:conversation_id>/delete")
    def chat_delete(conversation_id):
        if not chat_store.delete(conversation_id):
            abort(404)
        flash("Chat deleted.", "success")
        return redirect(url_for("chat_page"))

    def settings_view(values=None, errors=None):
        saved = settings_store.all()
        groups = []
        for group in SETTING_GROUPS:
            fields = []
            for field in group["fields"]:
                name = field["name"]
                environment = os.environ.get(name, "").strip()
                if name in saved:
                    source, current = "saved", saved[name]
                elif environment:
                    source, current = "environment", environment
                else:
                    source, current = "default", field.get("default", "")
                item = {
                    **field,
                    "source": source,
                    "is_set": bool(current),
                    "has_env": bool(environment),
                    "error": (errors or {}).get(name),
                }
                if field.get("secret"):
                    # Never send secrets back to the browser; show only the last characters.
                    item["hint"] = f"…{current[-4:]}" if len(current) >= 12 else ""
                else:
                    item["value"] = (values or {}).get(name, current)
                fields.append(item)
            groups.append({**group, "fields": fields})
        return groups

    def read_settings_form():
        """Return ({name: value or None}, {name: error}) from the posted settings form."""
        saved = settings_store.all()
        changes, errors = {}, {}
        for name, field in SETTING_FIELDS.items():
            if field.get("secret"):
                if request.form.get(f"clear_{name}"):
                    changes[name] = None
                    continue
                value = request.form.get(name, "").strip()
                if not value:
                    continue  # Blank keeps the current secret.
            else:
                value = request.form.get(name, "").strip().replace("\r\n", "\n")
                if name == "TELEGRAM_ALLOWED_USERS":
                    value = ",".join(part for part in re.split(r"[\s,]+", value) if part)
                fallback = os.environ.get(name, "").strip() or field.get("default", "")
                if value == fallback:
                    # Matches what the server would use anyway, so don't pin it.
                    changes[name] = None
                    continue
                if not value:
                    changes[name] = "" if fallback else None
                    continue

            if name in ("CHAT_MODEL", "LOCAL_AI_MODEL") and not MODEL_ID.fullmatch(value):
                errors[name] = "Enter a model ID such as claude-opus-5-5."
            elif name == "CHAT_EFFORT" and value not in {key for key, _ in field["options"]}:
                errors[name] = "Choose low, medium, or high."
            elif name == "CHAT_PROVIDER" and value not in {key for key, _ in field["options"]}:
                errors[name] = "Choose Claude or Local AI."
            elif name == "LOCAL_AI_URL" and not LOCAL_URL.fullmatch(value):
                errors[name] = "Enter an address that starts with http:// or https://."
            elif name == "LOCAL_AI_API_KEY" and any(c.isspace() for c in value):
                errors[name] = "API keys can't contain spaces."
            elif name == "CHAT_INSTRUCTIONS" and len(value) > 4000:
                errors[name] = "Keep instructions under 4,000 characters."
            elif name == "TELEGRAM_ALLOWED_USERS" and not TELEGRAM_USERS.fullmatch(value):
                errors[name] = "Use numeric Telegram user IDs separated by commas."
            elif name == "TELEGRAM_BOT_TOKEN" and value != saved.get(name):
                if not TELEGRAM_TOKEN.fullmatch(value):
                    errors[name] = "This doesn't look like a bot token from @BotFather."
                else:
                    try:
                        verify_telegram_token(value)
                    except ValueError as error:
                        errors[name] = str(error)
            elif name == "ANTHROPIC_API_KEY" and (len(value) < 20 or any(c.isspace() for c in value)):
                errors[name] = "This doesn't look like an Anthropic API key."
            changes[name] = value
        return changes, errors

    def server_info():
        # Changing these needs a restart, so they are shown but not editable.
        return [
            ("Shared files folder", str(root)),
            ("Notes and chat database", "PostgreSQL" if database_url else "SQLite"),
            ("Session and cache store", "Redis" if cache is not None else "Signed cookies, no cache"),
            ("Fixed session key (FILES_SECRET_KEY)", "Set" if os.environ.get("FILES_SECRET_KEY") else "Not set"),
            ("Largest upload", "5 GB"),
        ]

    @app.get("/settings")
    def settings_page():
        return render_template("settings.html", groups=settings_view(), server=server_info())

    @app.post("/settings")
    def save_settings():
        changes, errors = read_settings_form()
        if errors:
            flash("Nothing was saved. Fix the highlighted settings and try again.", "error")
            values = {name: request.form.get(name, "") for name, field in SETTING_FIELDS.items() if not field.get("secret")}
            return render_template("settings.html", groups=settings_view(values, errors), server=server_info()), 400
        settings_store.save(changes)
        flash("Settings saved.", "success")
        return redirect(url_for("settings_page"))

    @app.get("/convert")
    def convert_page():
        return render_template("convert.html")

    @app.post("/convert/<tool>")
    def convert(tool):
        if tool not in convert_tools:
            abort(404)
        wants_json = request.headers.get("X-Requested-With") == "fetch"

        def fail(message, status=400):
            if wants_json:
                return jsonify(error=message), status
            flash(message, "error")
            return redirect(url_for("convert_page", _anchor=tool))

        if (request.content_length or 0) > max_convert_bytes:
            return fail("Files for conversion must be 200 MB or smaller in total.", 413)
        uploads = [upload for upload in request.files.getlist("files") if upload.filename]
        if not uploads:
            return fail("Choose a file to convert.")
        if tool not in multi_file_tools and len(uploads) > 1:
            return fail("Choose one file at a time for this conversion.")
        for upload in uploads:
            if Path(upload.filename).suffix.lower() not in convert_tools[tool]:
                return fail(f"“{download_stem(upload.filename)}” is not a supported file type here.")

        stem = download_stem(uploads[0].filename)
        note = ""
        with tempfile.TemporaryDirectory(prefix="convert-") as temp_dir:
            workdir = Path(temp_dir)
            sources = []
            for index, upload in enumerate(uploads):
                source = workdir / f"input-{index:03d}{Path(upload.filename).suffix.lower()}"
                upload.save(source)
                sources.append(source)
            try:
                if tool == "word2pdf":
                    result = converters.word_to_pdf(sources[0], workdir)
                    download_name = f"{stem}.pdf"
                elif tool == "pdf2word":
                    ocr = request.form.get("ocr", "auto")
                    result, note = pdf_word.pdf_to_word(sources[0], workdir, ocr if ocr in ("auto", "on", "off") else "auto")
                    download_name = f"{stem}.docx"
                elif tool == "image2pdf":
                    page_size = request.form.get("page_size", "a4")
                    if page_size not in ("a4", "letter", "original"):
                        page_size = "a4"
                    result = converters.images_to_pdf(sources, workdir, page_size)
                    download_name = f"{stem}.pdf"
                elif tool == "pdfmerge":
                    names = [download_stem(upload.filename) for upload in uploads]
                    result = converters.merge_pdfs(sources, workdir, names)
                    download_name = f"{stem}-merged.pdf"
                else:
                    image_format = "jpg" if request.form.get("image_format") == "jpg" else "png"
                    dpi = {"96": 96, "150": 150, "300": 300}.get(request.form.get("dpi"), 150)
                    result = converters.pdf_to_images(
                        sources[0], workdir, image_format, dpi, name=stem
                    )
                    download_name = result.name
            except converters.ConversionError as error:
                return fail(str(error))

            output = tempfile.TemporaryFile()
            with result.open("rb") as handle:
                shutil.copyfileobj(handle, output)
            output.seek(0)

        response = send_file(
            output,
            as_attachment=True,
            download_name=download_name,
            mimetype=mimetypes.guess_type(download_name)[0] or "application/octet-stream",
        )
        if note:
            # Headers are Latin-1 only, so the note travels URL-encoded.
            response.headers["X-Convert-Note"] = quote(note)
        return response

    def editor_workspace(doc_id):
        folder = pdf_editor.workspace(doc_id)
        if folder is None:
            abort(404)
        return folder

    @app.get("/convert/edit")
    def pdf_editor_start():
        return render_template("pdf_editor.html", doc_id=None, info=None)

    @app.post("/convert/edit/open")
    def pdf_editor_open():
        wants_json = request.headers.get("X-Requested-With") == "fetch"

        def fail(message, status=400):
            if wants_json:
                return jsonify(error=message), status
            flash(message, "error")
            return redirect(url_for("pdf_editor_start"))

        if (request.content_length or 0) > max_convert_bytes:
            return fail("PDFs to edit must be 200 MB or smaller.", 413)
        upload = request.files.get("files")
        if not upload or not upload.filename:
            return fail("Choose a PDF to edit.")
        if Path(upload.filename).suffix.lower() != ".pdf":
            return fail(f"“{download_stem(upload.filename)}” is not a PDF.")
        try:
            doc_id, _info = pdf_editor.create_workspace(upload, download_stem(upload.filename))
        except converters.ConversionError as error:
            return fail(str(error))
        url = url_for("pdf_editor_page", doc_id=doc_id)
        return jsonify(url=url) if wants_json else redirect(url)

    @app.get("/convert/edit/<doc_id>")
    def pdf_editor_page(doc_id):
        folder = pdf_editor.workspace(doc_id)
        if folder is None:
            flash("That PDF is no longer open. Open it again to keep editing.", "error")
            return redirect(url_for("pdf_editor_start"))
        return render_template("pdf_editor.html", doc_id=doc_id, info=pdf_editor.load_info(folder))

    @app.get("/convert/edit/<doc_id>/page/<int:number>.png")
    def pdf_editor_page_image(doc_id, number):
        image = pdf_editor.render_page(editor_workspace(doc_id), number, request.args.get("w", type=int))
        if image is None:
            abort(404)
        response = Response(image, mimetype="image/png")
        response.headers["Cache-Control"] = "private, max-age=3600"
        return response

    @app.get("/convert/edit/<doc_id>/page/<int:number>/text")
    def pdf_editor_page_text(doc_id, number):
        lines = pdf_editor.page_text(editor_workspace(doc_id), number)
        if lines is None:
            abort(404)
        return jsonify(lines=lines)

    @app.post("/convert/edit/<doc_id>/state")
    def pdf_editor_state(doc_id):
        try:
            pdf_editor.save_state(editor_workspace(doc_id), request.get_data())
        except (converters.ConversionError, ValueError) as error:
            return jsonify(error=str(error) or "Could not keep these edits."), 400
        return jsonify(ok=True)

    @app.post("/convert/edit/<doc_id>/save")
    def pdf_editor_save(doc_id):
        folder = editor_workspace(doc_id)
        payload = request.get_json(silent=True) or {}
        name = download_stem(payload.get("name") or json.loads((folder / "meta.json").read_text())["name"])
        try:
            data = pdf_editor.build_pdf(folder, payload.get("state"))
        except converters.ConversionError as error:
            return jsonify(error=str(error)), 400
        if payload.get("target") == "files":
            # Save a copy into the shared files, next to everything else, without overwriting.
            target = root / f"{name}-edited.pdf"
            counter = 2
            while target.exists():
                target = root / f"{name}-edited ({counter}).pdf"
                counter += 1
            target.write_bytes(data)
            return jsonify(saved=target.name, url=url_for("index"))
        return send_file(
            io.BytesIO(data),
            as_attachment=True,
            download_name=f"{name}-edited.pdf",
            mimetype="application/pdf",
        )

    @app.errorhandler(413)
    def file_too_large(_error):
        return "Upload exceeds the 5 GB limit.", 413

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)