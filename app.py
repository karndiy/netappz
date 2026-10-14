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
import threading
import time
import zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import quote

import click
from flask import (
    Flask,
    Response,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_session import Session
from markupsafe import Markup, escape
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

import chat
import converters
import pdf_editor
import pdf_word


# Settings that can be changed on the Settings page. A saved value overrides the
# server's environment (.env); clearing it falls back to the environment again.
SETTING_GROUPS = [
    {
        "section": "ai",
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
        "section": "ai",
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
                "name": "LOCAL_AI_MODELS", "label": "Models", "multiline": True, "rows": 3,
                "default": "\n".join(chat.DEFAULT_LOCAL_MODELS),
                "help": "One model ID per line, as the server lists it. Add a line to make another model available.",
            },
            {
                "name": "LOCAL_AI_MODEL", "label": "Model to use", "default": chat.DEFAULT_LOCAL_MODEL,
                "options": [],  # Filled from the Models list.
                "help": "Chat and the Telegram bot reply with this model. Save after adding a model to pick it here.",
            },
        ],
    },
    {
        "key": "telegram",
        "section": "telegram",
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
# The Settings page's categories: personal ones for everyone, the rest for administrators.
SETTINGS_SECTIONS = [
    {"key": "account", "title": "Profile & password", "icon": "👤", "admin": False,
     "description": "Your profile photo, a few words about you, and your password."},
    {"key": "notifications", "title": "Notifications", "icon": "🔔", "admin": False,
     "description": "Choose what you're told about and whether alerts pop up."},
    {"key": "users", "title": "Users & sign-up", "icon": "👥", "admin": True,
     "description": "Add people, approve new accounts, and choose who can sign up."},
    {"key": "ai", "title": "Chat AI", "icon": "🤖", "admin": True,
     "description": "The AI that answers on the Chat page and in Telegram."},
    {"key": "telegram", "title": "Telegram bot", "icon": "✈️", "admin": True,
     "description": "Chat with the AI from your phone through a Telegram bot."},
    {"key": "server", "title": "Server", "icon": "🖥️", "admin": True,
     "description": "Storage, database and version. Changed in the server's files."},
    {"key": "activity", "title": "Activity log", "icon": "📜", "admin": True,
     "description": "What people do in Notes and Chat: notes written, changed and deleted, and chat messages."},
    {"key": "system", "title": "System & backups", "icon": "🩺", "admin": True,
     "description": "Automatic checks of the database, Redis, disk and backups, and the database backups."},
]
SECTION_KEYS = {section["key"]: section for section in SETTINGS_SECTIONS}
# Alerts people can turn off. Sign-up and approval alerts are always sent.
NOTIFY_KINDS = {
    "mention": "Someone @mentions you in a post or comment",
    "comment": "Comments on your posts and on posts you commented on",
    "like": "Likes on your posts",
    "post": "New posts from other people",
    "request": "News about requests on the Update log",
    "mission": "New missions, and news about mission work",
    "wallet": "Points you receive in your wallet",
}
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@\[\]-]{0,99}")
LOCAL_URL = re.compile(r"https?://[^\s/]+(/\S*)?")
USERNAME = re.compile(r"[A-Za-z0-9._@-]{2,40}")
MIN_PASSWORD = 8
TELEGRAM_TOKEN = re.compile(r"\d+:[A-Za-z0-9_-]{30,}")
TELEGRAM_USERS = re.compile(r"-?\d+(,-?\d+)*")


# Patch notes, newest first. Add an entry for every change.
UPDATE_LOG = [
    {
        "version": "1.37.0",
        "date": "2026-10-14",
        "changes": [
            "New Wallet page with points (request #12: a reward wallet for depositing and transferring funds). Everyone sees their balance and a history of every change to it.",
            "Administrators deposit points into anyone's wallet, or take them back, and see everyone's balance. Anyone can send points from their own wallet to another person, who is notified.",
            "Missions can now have a reward: when an administrator approves someone's work, the reward goes straight into their wallet.",
            "Wallet alerts can be turned off under Notifications in Settings like the other kinds.",
        ],
    },
    {
        "version": "1.36.0",
        "date": "2026-10-14",
        "changes": [
            "New Missions page (request #11: a mission system with tasks submitted and approved by the administrator). Administrators set missions with a title and details, and everyone is notified.",
            "Anyone can submit their work for an open mission. Administrators see it under Waiting for approval and approve it or send it back with a note; the person is notified either way and can submit again after a send-back.",
            "People see only their own submissions; administrators see everyone's. Work can be withdrawn until it's approved, and administrators can close, reopen or delete a mission.",
            "Mission alerts can be turned off under Notifications in Settings like the other kinds.",
        ],
    },
    {
        "version": "1.35.0",
        "date": "2026-10-14",
        "changes": [
            "The Convert page now has a Conversion log (request #10: a log for Convert files that users and administrators can delete from). It lists each conversion with the time, the tool, the file names and the result, or why it failed.",
            "Each person sees only their own conversions and can delete single entries or clear their whole log. Administrators see everyone's conversions and can delete any entry or clear the log for everyone.",
            "Only file names are kept, never the files, and entries are removed automatically after a year.",
        ],
    },
    {
        "version": "1.34.0",
        "date": "2026-10-14",
        "changes": [
            "Notes and Chat are now private: each person sees only their own notes and chats (request: separate notes and chat for each user). Notes and chats from before this update, which everyone shared, now belong to the first administrator.",
            "New Settings category for administrators, Activity log: who added, changed or deleted a note (with its title), and who sent a chat message (with the start of the message) or deleted a chat. Filter by person, by Notes or Chat, or search. Entries are kept for a year.",
            "The Notes and Chat pages say that only you can see your notes and chats, and what administrators can see in the log.",
            "Telegram bot chats belong to the first administrator and appear on their Chat page. Deleting an account also deletes that person's notes and chats.",
        ],
    },
    {
        "version": "1.33.0",
        "date": "2026-10-14",
        "changes": [
            "New Settings category, System & backups, for administrators (request: automatic system checks and backups in Settings).",
            "The server checks itself every 15 minutes: the database, Redis, disk space, and whether the latest backup is recent and worked. Administrators are notified when something needs attention and again when it's fixed. Check now runs the checks at once.",
            "The database backups are listed with their age and size, and each can be downloaded. Back up now makes a new one within a minute.",
            "The backup schedule (how often, and how many backups to keep) can be changed on the same page.",
        ],
    },
    {
        "version": "1.32.0",
        "date": "2026-10-14",
        "changes": [
            "Administrators can send a request to Claude from the Update log page with the new Send to Claude button (request: check requests automatically and send them to Claude to process).",
            "The server checks every 5 minutes. Claude makes the change, adds its entry to this log, and checks that everything still works. The app is then updated and the request marked Done with the version that has it, and the person who asked is notified.",
            "Each request shows a note from Claude: what was changed, or why it couldn't be done, in which case it goes back to Waiting.",
            "Only administrators can send requests to Claude. Other people's requests wait until an administrator sends them.",
        ],
    },
    {
        "version": "1.31.0",
        "date": "2026-10-14",
        "changes": [
            "The database is backed up automatically to the backups folder on the server computer, outside Docker (request: database backups the system can use after a restart). A backup is made each time the server starts and then every 6 hours, and the newest 28 are kept (about a week).",
            "If the database is ever lost, for example after Docker is reset, it is filled from the newest backup when it starts again, so accounts, notes, posts, comments and settings come back. Feed photos and videos are stored separately and are not part of these backups.",
            "An administrator can also go back to any saved backup with scripts/db-restore.sh. The current data is saved first.",
            "The app now starts by itself when the server computer is turned on and signs in, so it's available again without anyone opening Docker (request: start Network Files at boot).",
        ],
    },
    {
        "version": "1.30.0",
        "date": "2026-10-12",
        "changes": [
            "Settings is organised into categories, with a menu on the left (request: tidy up the Settings page). Personal: Profile & password, Notifications. Administration (administrators only): Users & sign-up, Chat AI, Telegram bot, Server.",
            "Everyone can now open Settings for their own profile, password and notification choices. The overview shows each category's state at a glance, such as accounts waiting for approval or whether Telegram is connected. On phones the overview is the menu.",
            "Profiles: add a profile photo (cropped to a square, location data removed) and a few words about yourself. Each member has a profile page with their posts in a grid.",
            "Notification choices: turn off the kinds you don't want (new posts, comments, likes, mentions, requests) and the pop-ups.",
            "Users: approve sign-ups at the top, make someone an administrator or remove it, and manage each account from one Manage button.",
            "New Feed design for PC and phones, in the style of Instagram and Facebook (request: new Feed look). Members are on the right on a PC and in a row at the top on phones and tablets.",
            "Photos swipe sideways with dots and arrows. Double-tap a photo to like it, or tap it to view it full size. Likes show who liked the post.",
            "The new-post box grows as you write. You can drag photos onto it or paste them, remove a photo before posting, and on phones jump to it with the round + button.",
            "Comments are sent without reloading the page, with Reply to mention someone. Long posts fold with See more. You can edit your own posts and copy a link to any post. Older posts load as you scroll.",
            "Fixed the Telegram bot sometimes failing to start after an upgrade, when it and the web app tried to update the database at the same moment.",
        ],
    },
    {
        "version": "1.29.0",
        "date": "2026-10-12",
        "changes": [
            "Feed: share posts with text, photos and videos with everyone who uses the app, then like and comment on them (request: a Facebook-style feed).",
            "Photos (JPG, PNG, GIF, WebP and iPhone HEIC) up to 25 MB and videos (MP4, MOV, WebM) up to 500 MB, up to 10 per post, with a progress bar while uploading. Videos can be played and skipped through.",
            "Type @name in a post or comment to mention someone. Links in posts open in a new tab. You can delete your own posts and comments, and comments on your posts.",
            "Notifications: a bell in the top bar shows how many are unread, and new ones pop up while the app is open. You're told about comments, likes, mentions, new posts, new members, and changes to your requests.",
            "The Notifications page lists them with unread ones highlighted. Opening one marks it as read; you can also mark one as read or unread, mark all as read, or remove the read ones.",
            "Sign-up: people can create their own account from the sign-in page (request: a sign-up system). By default an administrator approves each new account on Settings, and is notified when someone signs up. Sign-up can also be open to everyone or turned off.",
        ],
    },
    {
        "version": "1.28.0",
        "date": "2026-10-12",
        "changes": [
            "Sign-in: everyone now signs in with a username and password before using the app (request: a simple user login).",
            "The first visit creates the administrator account. Administrators add people, set new passwords, and remove accounts on the Settings page.",
            "Settings is for administrators only. Everyone can change their own password by clicking their name at the top.",
            "Passwords are stored as salted hashes, and after 8 wrong passwords sign-in pauses for 10 minutes.",
            "Your name and a Log out button are in the top bar. The Telegram bot works as before.",
        ],
    },
    {
        "version": "1.27.0",
        "date": "2026-10-12",
        "changes": [
            "The Update log page now takes requests: ask for a new feature or report a problem, with an optional name.",
            "Every request shows its status (Waiting, In progress, Done), and the list can be filtered by status.",
            "When a request is marked Done, it records the app version that has the change, with a link to its patch notes.",
            "Search on this page finds requests as well as patch notes.",
        ],
    },
    {
        "version": "1.26.1",
        "date": "2026-10-12",
        "changes": [
            "Deleting or renaming a file that is already gone (a double click, Reload, or a change from another device) no longer shows a Not Found page: you go back to the folder with a short note.",
            "The Delete button turns off after the first click, and Back or Reload after a delete no longer sends it again.",
        ],
    },
    {
        "version": "1.26.0",
        "date": "2026-10-12",
        "changes": [
            "PDF to Word keeps charts: bar, line and pie charts drawn in the PDF come out as pictures with their labels, instead of scattered numbers.",
            "Bullet lists stay bullet lists, instead of being squeezed into narrow table columns.",
            "Linked text (such as underlined links) no longer disappears from the Word file.",
            "Tables keep their real column widths.",
            "Scanned Thai: fewer stray spaces inside words (ขั บ → ขับ), tick boxes with broken outlines come back as ☐, and nearby lines are no longer joined by mistake. The sample form now reads 96.6% right.",
        ],
    },
    {
        "version": "1.25.0",
        "date": "2026-10-12",
        "changes": [
            "Much more accurate PDF to Word for scanned Thai documents: a scanned driving licence form went from about 62% to 96% of characters right.",
            "Thai tone marks and upper vowels are no longer lost (เรียน, ชั่วคราว, อื่น ๆ come out whole), and the junk lines they used to leave are gone.",
            "Dotted fill-in lines on forms are no longer read as long strings of numbers; they come back as dotted lines you can type on.",
            "Tick boxes and circles on forms come back as ☐ and ○.",
            "Words standing alone between fill-in lines (such as สัญชาติ) are found, and English inside Thai text stays correct.",
            "Uses Tesseract's most accurate Thai and English models.",
        ],
    },
    {
        "version": "1.24.0",
        "date": "2026-10-12",
        "changes": [
            "Faster and steadier server: if Redis is slow or down, pages no longer hang, and Notes read straight from the database instead.",
            "Redis now has a 64 MB memory limit, writes to disk once instead of twice, and makes room by dropping the oldest expiring data first.",
            "Docker checks the app's health every 30 seconds (database and Redis) and restarts anything that stops answering.",
            "Container logs are capped at 30 MB each, so they can't fill the disk.",
        ],
    },
    {
        "version": "1.23.0",
        "date": "2026-10-12",
        "changes": [
            "Notes can hold links: web addresses (https://… or www.…) are underlined as you type.",
            "Put the cursor on a link and an Open button appears; on a computer, ⌘-click or Ctrl-click opens it right away.",
            "New Insert link button (⌘K): select some text first to turn it into a named link, written as [text](address).",
            "Notes are still saved as plain text, so nothing changes for existing notes.",
        ],
    },
    {
        "version": "1.22.0",
        "date": "2026-10-12",
        "changes": [
            "Code in chat replies can be downloaded as a zip: each code block becomes a file, named the way the AI labels it (for example app.py).",
            "The Chat page shows a Download code button under replies that contain code, and each code block shows its file name.",
            "In Telegram, replies with code also come with the zip file, ready to save.",
            "The AI is asked to label every code file with its name, so the zip keeps folders like templates/index.html.",
        ],
    },
    {
        "version": "1.21.0",
        "date": "2026-10-12",
        "changes": [
            "Settings now shows whether the Telegram bot is connected, and as which bot.",
            "If another program is using the same bot token (for example the Claude Code Telegram plugin), Settings says so and explains how to fix it.",
            "Telegram chats use the AI chosen on the Settings page, including Local AI.",
        ],
    },
    {
        "version": "1.20.0",
        "date": "2026-10-12",
        "changes": [
            "Fixed Chat and the Telegram bot with Local AI: the ai.esm-solution.com firewall (Cloudflare) was blocking the app's requests, which showed up as a rejected API key.",
            "If a firewall blocks the app again, the chat now says so instead of blaming the API key.",
        ],
    },
    {
        "version": "1.19.0",
        "date": "2026-10-12",
        "changes": [
            "Local AI at https://ai.esm-solution.com/api/v1 is now the default AI provider for Chat and the Telegram bot.",
            "Settings has a Models list for the Local AI server: add one model ID per line to make it available.",
            "Pick the model to use from that list; it starts with qwen3/qwen3.6-35b and qwen3/qwen3.8-27b.",
        ],
    },
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
APP_VERSION = UPDATE_LOG[0]["version"]
# Database dumps made by the backup service (scripts/db-backup.sh).
BACKUP_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.dump")
SYSTEM_STATUSES = {"ok": "OK", "warning": "Warning", "problem": "Problem"}
SYSTEM_CHECK_STATE = "_system_checks"  # app_settings row: the last automatic check
# Settings → Activity log: what people do in Notes and Chat.
ACTIVITY_AREAS = {"notes": "Notes", "chat": "Chat"}
ACTIVITY_DETAIL = 300  # characters of a note title or chat message kept in the log
ACTIVITY_KEEP_DAYS = 365
# Convert page: each person's conversion log. Administrators see and can delete everyone's.
CONVERT_TOOL_NAMES = {"word2pdf": "Word to PDF", "pdf2word": "PDF to Word", "image2pdf": "Image to PDF",
                      "pdf2image": "PDF to Image", "pdfmerge": "Merge PDFs"}
CONVERT_LOG_SHOWN = 50
REQUEST_KINDS = {"feature": "New feature or change", "problem": "Something isn't working"}
# "queued" hands a request to Claude Code: scripts/claude-worker.sh picks it up (administrators only).
MISSION_STATUSES = {"pending": "Waiting for approval", "approved": "Approved", "returned": "Sent back"}
MISSION_TEXT_LIMIT = 4000
# Wallet: points people hold. Administrators deposit them, people transfer them, approved missions pay them.
WALLET_KINDS = {"deposit": "Deposit", "withdraw": "Taken away", "transfer_in": "Received",
                "transfer_out": "Sent", "reward": "Mission reward"}
WALLET_MAX = 1_000_000  # the most points in one deposit, transfer or mission reward
WALLET_NOTE = 200
WALLET_SHOWN = 100
REQUEST_STATUSES = {"open": "Waiting", "queued": "Sent to Claude", "in_progress": "In progress", "done": "Done"}
# Photos and videos on the Feed. HEIC photos from iPhones are turned into JPEG.
FEED_IMAGES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif",
               ".webp": "image/webp", ".heic": "image/jpeg", ".heif": "image/jpeg"}
# QuickTime (.mov) from phones is H.264 in an MP4-style box, which browsers play as video/mp4.
FEED_VIDEOS = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/mp4", ".webm": "video/webm"}
FEED_MAX_FILES = 10
FEED_MAX_IMAGE = 25 * 1024 * 1024
FEED_MAX_VIDEO = 500 * 1024 * 1024
FEED_POST_LIMIT = 5000
FEED_COMMENT_LIMIT = 2000
MENTION = re.compile(r"(?<![\w@.])@([A-Za-z0-9._-]{1,39}[A-Za-z0-9_-])")
# Links and @mentions in one pass, so a mention inside a link is left alone.
FEED_TOKENS = re.compile(r"(?P<link>https?://[^\s<]+[^\s<.,;:!?)\]'\"])|(?<![\w@.])@(?P<name>[A-Za-z0-9._-]{1,39}[A-Za-z0-9_-])")
SIGNUP_MODES = {
    "approval": "Anyone can sign up; an administrator approves each new account",
    "open": "Anyone can sign up and start right away",
    "off": "Off: only administrators add people",
}


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
    # Photos and videos posted on the Feed, kept apart from the shared files.
    feed_media_dir = Path(
        app.config.get("FEED_MEDIA") or os.environ.get("FEED_MEDIA")
        or Path(app.config["NOTES_DATABASE"]).expanduser().parent / "feed"
    ).resolve()
    feed_media_dir.mkdir(parents=True, exist_ok=True)
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

    def lock_schema(connection):
        """The web app and the Telegram bot start together; on Postgres only one may change tables at a time."""
        if database_url:
            connection.execute("SELECT pg_advisory_xact_lock(4207301)")

    def table_columns(connection, table):
        if database_url:
            rows = connection.execute(
                "SELECT column_name AS name FROM information_schema.columns WHERE table_name = %s", (table,)
            ).fetchall()
        else:
            rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        return {row["name"] for row in rows}

    def time_as(column, alias="created_at"):
        """A timestamp column read as "YYYY-MM-DD HH:MM:SS" under a plain name, on either database."""
        if database_url:
            return f"to_char({column}, 'YYYY-MM-DD HH24:MI:SS') AS {alias}"
        return f"{column} AS {alias}"

    def placeholders(values):
        return ", ".join("?" for _ in values)

    with notes_connection() as connection:
        lock_schema(connection)
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

        # Private notes (1.34.0): each note belongs to the person who wrote it.
        if "user_id" not in table_columns(connection, "notes"):
            connection.execute("ALTER TABLE notes ADD COLUMN user_id INTEGER")
        connection.execute("CREATE INDEX IF NOT EXISTS notes_user ON notes (user_id)")

    notes_query = (
        "SELECT id, title, content, "
        f"{timestamp_select.format('created_at')}, {timestamp_select.format('updated_at')} "
        "FROM notes WHERE user_id = ? ORDER BY notes.updated_at DESC, id DESC"
    )

    # Redis keeps sessions server-side and caches the notes list when configured.
    cache = None
    if app.config["REDIS_URL"]:
        import redis

        # Short timeouts so a stuck Redis fails fast instead of hanging every request.
        cache = redis.Redis.from_url(
            app.config["REDIS_URL"],
            socket_connect_timeout=3,
            socket_timeout=3,
            socket_keepalive=True,
            health_check_interval=30,
            retry_on_timeout=True,
        )
        app.config.update(SESSION_TYPE="redis", SESSION_REDIS=cache, SESSION_KEY_PREFIX="session:")
        Session(app)
    cache_errors = (redis.RedisError,) if cache is not None else ()

    def notes_cache_key(user_id):
        return f"notes:list:{user_id}"

    def load_notes(user_id):
        """One person's notes, newest first."""
        # The notes cache is optional: on any Redis error, read the database directly.
        if cache is not None:
            try:
                cached = cache.get(notes_cache_key(user_id))
                if cached is not None:
                    return json.loads(cached)
            except cache_errors as error:
                app.logger.warning("Notes cache unavailable: %s", error)
        with notes_connection() as connection:
            rows = [dict(row) for row in connection.execute(sql(notes_query), (user_id,)).fetchall()]
        if cache is not None:
            try:
                cache.set(notes_cache_key(user_id), json.dumps(rows), ex=300)
            except cache_errors:
                pass
        return rows

    def invalidate_notes(user_id):
        if cache is not None:
            try:
                cache.delete(notes_cache_key(user_id))
            except cache_errors as error:
                app.logger.warning("Could not clear the notes cache: %s", error)

    with notes_connection() as connection:
        lock_schema(connection)
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
        # Private chats (1.34.0): each conversation belongs to one person.
        if "user_id" not in table_columns(connection, "chat_conversations"):
            connection.execute("ALTER TABLE chat_conversations ADD COLUMN user_id INTEGER")
        connection.execute("CREATE INDEX IF NOT EXISTS chat_conversations_user ON chat_conversations (user_id)")
        # What people do in Notes and Chat, for administrators (Settings → Activity log).
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS activity_log (
                {id_column},
                user_id INTEGER,
                username TEXT NOT NULL DEFAULT '',
                area TEXT NOT NULL,
                action TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute("CREATE INDEX IF NOT EXISTS activity_log_created ON activity_log (created_at)")
        # Conversions on the Convert page (1.35.0): file names and results only, never the files.
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS convert_log (
                {id_column},
                user_id INTEGER,
                username TEXT NOT NULL DEFAULT '',
                tool TEXT NOT NULL,
                files TEXT NOT NULL DEFAULT '',
                result TEXT NOT NULL DEFAULT '',
                ok INTEGER NOT NULL DEFAULT 1,
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute("CREATE INDEX IF NOT EXISTS convert_log_user ON convert_log (user_id)")

    class ActivityLog:
        """Who did what in Notes and Chat. The username is copied so entries outlive deleted accounts."""

        def add(self, user_id, area, action, detail=""):
            detail = " ".join(detail.split())
            detail = detail if len(detail) <= ACTIVITY_DETAIL else detail[:ACTIVITY_DETAIL - 1] + "…"
            with notes_connection() as connection:
                connection.execute(
                    sql(
                        "INSERT INTO activity_log (user_id, username, area, action, detail) "
                        "VALUES (?, COALESCE((SELECT username FROM users WHERE id = ?), ''), ?, ?, ?)"
                    ),
                    (user_id, user_id, area, action, detail),
                )

        def search(self, username="", area="", query="", before_id=None, limit=100):
            conditions, params = [], []
            if username:
                conditions.append("username = ?")
                params.append(username)
            if area:
                conditions.append("area = ?")
                params.append(area)
            if query:
                conditions.append("(LOWER(detail) LIKE ? OR LOWER(action) LIKE ?)")
                params += [f"%{query.lower()}%"] * 2
            if before_id:
                conditions.append("id < ?")
                params.append(before_id)
            where = f"WHERE {' AND '.join(conditions)} " if conditions else ""
            with notes_connection() as connection:
                rows = connection.execute(
                    sql(
                        f"SELECT id, username, area, action, detail, {timestamp_select.format('created_at')} "
                        f"FROM activity_log {where}ORDER BY id DESC LIMIT ?"
                    ),
                    (*params, limit),
                ).fetchall()
            return [dict(row) for row in rows]

        def usernames(self):
            with notes_connection() as connection:
                rows = connection.execute(
                    "SELECT DISTINCT username FROM activity_log WHERE username <> '' ORDER BY username"
                ).fetchall()
            return [row["username"] for row in rows]

        def count_since(self, hours):
            since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
            with notes_connection() as connection:
                row = connection.execute(
                    sql("SELECT COUNT(*) AS n FROM activity_log WHERE created_at >= ?"), (since,)
                ).fetchone()
            return row["n"]

        def prune(self, days=ACTIVITY_KEEP_DAYS):
            before = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
            with notes_connection() as connection:
                connection.execute(sql("DELETE FROM activity_log WHERE created_at < ?"), (before,))

    activity_log = ActivityLog()
    app.extensions["activity"] = activity_log

    class ConvertLog:
        """Who converted what on the Convert page, and how it went. The files themselves are never kept."""

        def add(self, user_id, tool, files, result, ok=True):
            files = ", ".join(files)
            files = files if len(files) <= ACTIVITY_DETAIL else files[:ACTIVITY_DETAIL - 1] + "…"
            result = " ".join(result.split())[:ACTIVITY_DETAIL]
            with notes_connection() as connection:
                connection.execute(
                    sql(
                        "INSERT INTO convert_log (user_id, username, tool, files, result, ok) "
                        "VALUES (?, COALESCE((SELECT username FROM users WHERE id = ?), ''), ?, ?, ?, ?)"
                    ),
                    (user_id, user_id, tool, files, result, 1 if ok else 0),
                )

        def entries(self, user_id=None, limit=CONVERT_LOG_SHOWN):
            """Newest first: one person's entries, or everyone's when user_id is None."""
            where, params = ("WHERE user_id = ? ", (user_id,)) if user_id is not None else ("", ())
            with notes_connection() as connection:
                rows = connection.execute(
                    sql(
                        f"SELECT id, user_id, username, tool, files, result, ok, "
                        f"{timestamp_select.format('created_at')} FROM convert_log {where}ORDER BY id DESC LIMIT ?"
                    ),
                    (*params, limit),
                ).fetchall()
            return [dict(row) for row in rows]

        def owner(self, entry_id):
            with notes_connection() as connection:
                row = connection.execute(
                    sql("SELECT user_id FROM convert_log WHERE id = ?"), (entry_id,)
                ).fetchone()
            return None if row is None else row["user_id"]

        def delete(self, entry_id):
            with notes_connection() as connection:
                connection.execute(sql("DELETE FROM convert_log WHERE id = ?"), (entry_id,))

        def clear(self, user_id=None):
            with notes_connection() as connection:
                if user_id is None:
                    connection.execute("DELETE FROM convert_log")
                else:
                    connection.execute(sql("DELETE FROM convert_log WHERE user_id = ?"), (user_id,))

        def prune(self, days=ACTIVITY_KEEP_DAYS):
            before = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
            with notes_connection() as connection:
                connection.execute(sql("DELETE FROM convert_log WHERE created_at < ?"), (before,))

    convert_log = ConvertLog()
    app.extensions["convert_log"] = convert_log

    class ChatStore:
        """Each person's conversations, from the web chat page and (for the bot's owner) Telegram."""

        conversation_columns = (
            f"id, title, {timestamp_select.format('created_at')}, {timestamp_select.format('updated_at')}"
        )

        def conversations(self, user_id):
            with notes_connection() as connection:
                rows = connection.execute(
                    sql(
                        f"SELECT {self.conversation_columns} FROM chat_conversations WHERE user_id = ? "
                        "ORDER BY chat_conversations.updated_at DESC, id DESC"
                    ),
                    (user_id,),
                ).fetchall()
            return [dict(row) for row in rows]

        def conversation(self, conversation_id, user_id):
            """The conversation if it belongs to this person, else None."""
            with notes_connection() as connection:
                row = connection.execute(
                    sql(f"SELECT {self.conversation_columns} FROM chat_conversations WHERE id = ? AND user_id = ?"),
                    (conversation_id, user_id),
                ).fetchone()
            return dict(row) if row else None

        def latest_id(self, user_id):
            conversations = self.conversations(user_id)
            return conversations[0]["id"] if conversations else None

        def create(self, user_id, title="New chat"):
            with notes_connection() as connection:
                cursor = connection.execute(
                    sql("INSERT INTO chat_conversations (title, user_id) VALUES (?, ?)") + returning_id,
                    (title, user_id),
                )
                return cursor.fetchone()["id"] if returning_id else cursor.lastrowid

        def telegram_owner(self):
            """The Telegram bot has no sign-in, so its chats belong to the first administrator."""
            with notes_connection() as connection:
                row = connection.execute(
                    "SELECT MIN(id) AS id FROM users WHERE is_admin = 1 AND is_active = 1"
                ).fetchone()
            return row["id"] if row else None

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

        def message(self, message_id, user_id):
            with notes_connection() as connection:
                row = connection.execute(
                    sql(
                        "SELECT chat_messages.id, conversation_id, role, content FROM chat_messages "
                        "JOIN chat_conversations ON chat_conversations.id = chat_messages.conversation_id "
                        "WHERE chat_messages.id = ? AND chat_conversations.user_id = ?"
                    ),
                    (message_id, user_id),
                ).fetchone()
            return dict(row) if row else None

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

        def delete(self, conversation_id, user_id):
            if self.conversation(conversation_id, user_id) is None:
                return False
            with notes_connection() as connection:
                connection.execute(
                    sql("DELETE FROM chat_messages WHERE conversation_id = ?"), (conversation_id,)
                )
                connection.execute(sql("DELETE FROM chat_conversations WHERE id = ?"), (conversation_id,))
            return True

        def delete_user_content(self, user_id):
            with notes_connection() as connection:
                connection.execute(
                    sql(
                        "DELETE FROM chat_messages WHERE conversation_id IN "
                        "(SELECT id FROM chat_conversations WHERE user_id = ?)"
                    ),
                    (user_id,),
                )
                connection.execute(sql("DELETE FROM chat_conversations WHERE user_id = ?"), (user_id,))
                connection.execute(sql("DELETE FROM notes WHERE user_id = ?"), (user_id,))

    chat_store = ChatStore()
    app.extensions["chat"] = chat_store

    with notes_connection() as connection:
        lock_schema(connection)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS app_settings (name TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS users (
                {id_column},
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS user_requests (
                {id_column},
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '',
                requested_by TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'open',
                done_version TEXT NOT NULL DEFAULT '',
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # Claude processing (1.32.0): who sent a request to Claude, and Claude's result.
        existing = table_columns(connection, "user_requests")
        for column in ("sent_by", "note"):
            if column not in existing:
                connection.execute(f"ALTER TABLE user_requests ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")

        # Sign-up (1.29.0): accounts made on the sign-up page can wait for an administrator.
        existing = table_columns(connection, "users")
        # Sign-up (1.29.0) and profiles (1.30.0) added these columns to existing databases.
        for column, definition in (("is_active", "INTEGER NOT NULL DEFAULT 1"), ("avatar", "TEXT NOT NULL DEFAULT ''"),
                                   ("bio", "TEXT NOT NULL DEFAULT ''"), ("prefs", "TEXT NOT NULL DEFAULT '{}'")):
            if column not in existing:
                connection.execute(f"ALTER TABLE users ADD COLUMN {column} {definition}")
        # Notes and chats from before 1.34.0 were shared by everyone; give them to the first administrator.
        owner = connection.execute("SELECT MIN(id) AS id FROM users WHERE is_admin = 1").fetchone()["id"]
        if owner is not None:
            for table in ("notes", "chat_conversations"):
                connection.execute(sql(f"UPDATE {table} SET user_id = ? WHERE user_id IS NULL"), (owner,))
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS feed_posts (
                {id_column},
                user_id INTEGER NOT NULL,
                body TEXT NOT NULL DEFAULT '',
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS feed_media (
                {id_column},
                post_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                stored TEXT NOT NULL,
                mime TEXT NOT NULL,
                position INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS feed_comments (
                {id_column},
                post_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                body TEXT NOT NULL,
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS feed_likes (
                post_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                PRIMARY KEY (post_id, user_id)
            )
            """
        )
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS notifications (
                {id_column},
                user_id INTEGER NOT NULL,
                actor_id INTEGER,
                kind TEXT NOT NULL,
                message TEXT NOT NULL,
                link TEXT NOT NULL DEFAULT '',
                post_id INTEGER,
                is_read INTEGER NOT NULL DEFAULT 0,
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # Missions (1.36.0): tasks administrators set, the work people hand in, and its approval.
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS missions (
                {id_column},
                title TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '',
                created_by INTEGER,
                is_open INTEGER NOT NULL DEFAULT 1,
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS mission_submissions (
                {id_column},
                mission_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                body TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                review_note TEXT NOT NULL DEFAULT '',
                reviewed_by INTEGER,
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # Rewards (1.37.0): approved mission work pays points into the wallet.
        if "reward" not in table_columns(connection, "missions"):
            connection.execute("ALTER TABLE missions ADD COLUMN reward INTEGER NOT NULL DEFAULT 0")
        # Wallet (1.37.0): every change to someone's points is a row; the balance is their sum.
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS wallet_entries (
                {id_column},
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                kind TEXT NOT NULL,
                other_id INTEGER,
                note TEXT NOT NULL DEFAULT '',
                created_by INTEGER,
                created_at {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        for statement in (
            "CREATE INDEX IF NOT EXISTS wallet_entries_user ON wallet_entries (user_id, id)",
            "CREATE INDEX IF NOT EXISTS mission_submissions_mission ON mission_submissions (mission_id, user_id)",
            "CREATE INDEX IF NOT EXISTS notifications_user ON notifications (user_id, is_read, id)",
            "CREATE INDEX IF NOT EXISTS feed_comments_post ON feed_comments (post_id, id)",
            "CREATE INDEX IF NOT EXISTS feed_media_post ON feed_media (post_id, position)",
        ):
            connection.execute(statement)

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

    class RequestStore:
        """Requests from the people who use the app (features, problems) and how far along they are."""

        columns = (
            "id, kind, title, details, requested_by, status, done_version, sent_by, note, "
            f"{timestamp_select.format('created_at')}, {timestamp_select.format('updated_at')}"
        )

        def all(self):
            with notes_connection() as connection:
                rows = connection.execute(
                    f"SELECT {self.columns} FROM user_requests ORDER BY "
                    "CASE status WHEN 'in_progress' THEN 0 WHEN 'queued' THEN 1 WHEN 'open' THEN 2 ELSE 3 END, "
                    "updated_at DESC, id DESC"
                ).fetchall()
            return [dict(row) for row in rows]

        def get(self, request_id):
            with notes_connection() as connection:
                row = connection.execute(
                    sql(f"SELECT {self.columns} FROM user_requests WHERE id = ?"), (request_id,)
                ).fetchone()
            return dict(row) if row else None

        def add(self, kind, title, details, requested_by):
            with notes_connection() as connection:
                cursor = connection.execute(
                    sql("INSERT INTO user_requests (kind, title, details, requested_by) VALUES (?, ?, ?, ?)")
                    + returning_id,
                    (kind, title, details, requested_by),
                )
                return cursor.fetchone()["id"] if returning_id else cursor.lastrowid

        def set_status(self, request_id, status, done_version="", sent_by="", note=""):
            with notes_connection() as connection:
                connection.execute(
                    sql(
                        "UPDATE user_requests SET status = ?, done_version = ?, sent_by = ?, note = ?, "
                        "updated_at = CURRENT_TIMESTAMP WHERE id = ?"
                    ),
                    (status, done_version, sent_by, note, request_id),
                )

        def next_queued(self):
            """The oldest request an administrator sent to Claude, or None."""
            with notes_connection() as connection:
                row = connection.execute(
                    f"SELECT {self.columns} FROM user_requests WHERE status = 'queued' ORDER BY updated_at, id"
                ).fetchone()
            return dict(row) if row else None

        def delete(self, request_id):
            with notes_connection() as connection:
                connection.execute(sql("DELETE FROM user_requests WHERE id = ?"), (request_id,))

    class MissionStore:
        """Missions administrators set, and the work people submit for an administrator to approve."""

        mission_columns = (
            "missions.id, missions.title, missions.details, missions.created_by, missions.is_open, missions.reward, "
            f"users.username AS creator, {time_as('missions.created_at')}"
        )
        submission_columns = (
            "mission_submissions.id, mission_submissions.mission_id, mission_submissions.user_id, "
            "mission_submissions.body, mission_submissions.status, mission_submissions.review_note, "
            "users.username AS author, users.avatar AS author_avatar, reviewers.username AS reviewer, "
            f"{time_as('mission_submissions.created_at')}, {time_as('mission_submissions.updated_at', 'updated_at')}"
        )

        def all(self):
            """Every mission, open ones first and newest first, each with its submissions (oldest first)."""
            with notes_connection() as connection:
                missions = [dict(row) for row in connection.execute(
                    f"SELECT {self.mission_columns} FROM missions LEFT JOIN users ON users.id = missions.created_by "
                    "ORDER BY missions.is_open DESC, missions.id DESC"
                ).fetchall()]
                submissions = connection.execute(
                    f"SELECT {self.submission_columns} FROM mission_submissions "
                    "JOIN users ON users.id = mission_submissions.user_id "
                    "LEFT JOIN users reviewers ON reviewers.id = mission_submissions.reviewed_by "
                    "ORDER BY mission_submissions.id"
                ).fetchall()
            by_id = {mission["id"]: {**mission, "submissions": []} for mission in missions}
            for row in submissions:
                if row["mission_id"] in by_id:
                    by_id[row["mission_id"]]["submissions"].append(dict(row))
            return list(by_id.values())

        def mission(self, mission_id):
            with notes_connection() as connection:
                row = connection.execute(
                    sql(f"SELECT {self.mission_columns} FROM missions LEFT JOIN users ON users.id = missions.created_by "
                        "WHERE missions.id = ?"), (mission_id,)
                ).fetchone()
            return dict(row) if row else None

        def add(self, title, details, user_id, reward=0):
            with notes_connection() as connection:
                cursor = connection.execute(
                    sql("INSERT INTO missions (title, details, created_by, reward) VALUES (?, ?, ?, ?)") + returning_id,
                    (title, details, user_id, reward),
                )
                return cursor.fetchone()["id"] if returning_id else cursor.lastrowid

        def set_open(self, mission_id, is_open):
            with notes_connection() as connection:
                connection.execute(sql("UPDATE missions SET is_open = ? WHERE id = ?"), (1 if is_open else 0, mission_id))

        def delete(self, mission_id):
            with notes_connection() as connection:
                connection.execute(sql("DELETE FROM mission_submissions WHERE mission_id = ?"), (mission_id,))
                connection.execute(sql("DELETE FROM missions WHERE id = ?"), (mission_id,))

        def submission(self, submission_id):
            with notes_connection() as connection:
                row = connection.execute(
                    sql(f"SELECT {self.submission_columns} FROM mission_submissions "
                        "JOIN users ON users.id = mission_submissions.user_id "
                        "LEFT JOIN users reviewers ON reviewers.id = mission_submissions.reviewed_by "
                        "WHERE mission_submissions.id = ?"), (submission_id,)
                ).fetchone()
            return dict(row) if row else None

        def active_submission(self, mission_id, user_id):
            """This person's submission that is waiting or approved, which blocks handing in another."""
            with notes_connection() as connection:
                row = connection.execute(
                    sql("SELECT id, status FROM mission_submissions WHERE mission_id = ? AND user_id = ? "
                        "AND status IN ('pending', 'approved')"), (mission_id, user_id)
                ).fetchone()
            return dict(row) if row else None

        def submit(self, mission_id, user_id, body):
            with notes_connection() as connection:
                cursor = connection.execute(
                    sql("INSERT INTO mission_submissions (mission_id, user_id, body) VALUES (?, ?, ?)") + returning_id,
                    (mission_id, user_id, body),
                )
                return cursor.fetchone()["id"] if returning_id else cursor.lastrowid

        def review(self, submission_id, status, note, reviewer_id):
            with notes_connection() as connection:
                connection.execute(
                    sql("UPDATE mission_submissions SET status = ?, review_note = ?, reviewed_by = ?, "
                        "updated_at = CURRENT_TIMESTAMP WHERE id = ?"),
                    (status, note, reviewer_id, submission_id),
                )

        def delete_submission(self, submission_id):
            with notes_connection() as connection:
                connection.execute(sql("DELETE FROM mission_submissions WHERE id = ?"), (submission_id,))

        def delete_user_content(self, user_id):
            """When an account is deleted, its submitted work goes; its missions stay for everyone else."""
            with notes_connection() as connection:
                connection.execute(sql("DELETE FROM mission_submissions WHERE user_id = ?"), (user_id,))
                connection.execute(sql("UPDATE mission_submissions SET reviewed_by = NULL WHERE reviewed_by = ?"),
                                   (user_id,))
                connection.execute(sql("UPDATE missions SET created_by = NULL WHERE created_by = ?"), (user_id,))

    class WalletStore:
        """Each person's points: a row for every deposit, transfer and reward, so the history explains the balance."""

        columns = (
            "wallet_entries.id, wallet_entries.user_id, wallet_entries.amount, wallet_entries.kind, "
            "wallet_entries.note, owners.username AS owner, others.username AS other, "
            f"{time_as('wallet_entries.created_at')}"
        )

        def lock(self, connection):
            """One change at a time, so two transfers can't both spend the same points."""
            if database_url:
                connection.execute("SELECT pg_advisory_xact_lock(4207302)")
            else:
                connection.execute("BEGIN IMMEDIATE")

        def _balance(self, connection, user_id):
            return connection.execute(
                sql("SELECT COALESCE(SUM(amount), 0) AS total FROM wallet_entries WHERE user_id = ?"), (user_id,)
            ).fetchone()["total"]

        def balance(self, user_id):
            with notes_connection() as connection:
                return self._balance(connection, user_id)

        def balances(self):
            """{user id: points} for everyone who has had points."""
            with notes_connection() as connection:
                rows = connection.execute(
                    "SELECT user_id, SUM(amount) AS total FROM wallet_entries GROUP BY user_id"
                ).fetchall()
            return {row["user_id"]: row["total"] for row in rows}

        def history(self, user_id=None, limit=WALLET_SHOWN):
            """Newest first: one person's entries, or everyone's when user_id is None."""
            where, values = ("WHERE wallet_entries.user_id = ? ", (user_id,)) if user_id is not None else ("", ())
            with notes_connection() as connection:
                rows = connection.execute(
                    sql(f"SELECT {self.columns} FROM wallet_entries "
                        "LEFT JOIN users owners ON owners.id = wallet_entries.user_id "
                        "LEFT JOIN users others ON others.id = wallet_entries.other_id "
                        f"{where}ORDER BY wallet_entries.id DESC LIMIT {int(limit)}"), values
                ).fetchall()
            return [dict(row) for row in rows]

        def _add(self, connection, user_id, amount, kind, note, created_by, other_id=None):
            connection.execute(
                sql("INSERT INTO wallet_entries (user_id, amount, kind, other_id, note, created_by) "
                    "VALUES (?, ?, ?, ?, ?, ?)"),
                (user_id, amount, kind, other_id, note, created_by),
            )

        def deposit(self, user_id, amount, note, admin_id):
            """An administrator adds points (amount > 0) or takes them away (amount < 0, never below zero)."""
            with notes_connection() as connection:
                self.lock(connection)
                if amount < 0 and self._balance(connection, user_id) + amount < 0:
                    raise ValueError("They don't have that many points to take away.")
                self._add(connection, user_id, amount, "deposit" if amount > 0 else "withdraw", note, admin_id)

        def transfer(self, from_id, to_id, amount, note):
            with notes_connection() as connection:
                self.lock(connection)
                if self._balance(connection, from_id) < amount:
                    raise ValueError("You don't have enough points for that transfer.")
                self._add(connection, from_id, -amount, "transfer_out", note, from_id, to_id)
                self._add(connection, to_id, amount, "transfer_in", note, from_id, from_id)

        def reward(self, user_id, amount, note, admin_id):
            with notes_connection() as connection:
                self._add(connection, user_id, amount, "reward", note, admin_id)

        def delete_user_content(self, user_id):
            """When an account is deleted, its points go; the other side of its transfers keeps theirs."""
            with notes_connection() as connection:
                connection.execute(sql("DELETE FROM wallet_entries WHERE user_id = ?"), (user_id,))
                connection.execute(sql("UPDATE wallet_entries SET other_id = NULL WHERE other_id = ?"), (user_id,))
                connection.execute(sql("UPDATE wallet_entries SET created_by = NULL WHERE created_by = ?"), (user_id,))

    class UserStore:
        """Accounts for signing in. Passwords are stored only as salted hashes."""

        columns = (f"id, username, password_hash, is_admin, is_active, avatar, bio, prefs, "
                   f"{timestamp_select.format('created_at')}")

        def count(self):
            with notes_connection() as connection:
                return connection.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]

        def all(self):
            with notes_connection() as connection:
                rows = connection.execute(f"SELECT {self.columns} FROM users ORDER BY LOWER(username)").fetchall()
            return [dict(row) for row in rows]

        def get(self, user_id):
            with notes_connection() as connection:
                row = connection.execute(sql(f"SELECT {self.columns} FROM users WHERE id = ?"), (user_id,)).fetchone()
            return dict(row) if row else None

        def by_name(self, username):
            with notes_connection() as connection:
                row = connection.execute(
                    sql(f"SELECT {self.columns} FROM users WHERE LOWER(username) = LOWER(?)"), (username,)
                ).fetchone()
            return dict(row) if row else None

        def add(self, username, password, is_admin=False, is_active=True):
            with notes_connection() as connection:
                cursor = connection.execute(
                    sql("INSERT INTO users (username, password_hash, is_admin, is_active) VALUES (?, ?, ?, ?)")
                    + returning_id,
                    (username, generate_password_hash(password), 1 if is_admin else 0, 1 if is_active else 0),
                )
                return cursor.fetchone()["id"] if returning_id else cursor.lastrowid

        def activate(self, user_id):
            with notes_connection() as connection:
                connection.execute(sql("UPDATE users SET is_active = 1 WHERE id = ?"), (user_id,))

        def set_profile(self, user_id, bio, avatar):
            with notes_connection() as connection:
                connection.execute(sql("UPDATE users SET bio = ?, avatar = ? WHERE id = ?"), (bio, avatar, user_id))

        def set_prefs(self, user_id, prefs):
            with notes_connection() as connection:
                connection.execute(sql("UPDATE users SET prefs = ? WHERE id = ?"), (json.dumps(prefs), user_id))

        def set_admin(self, user_id, is_admin):
            with notes_connection() as connection:
                connection.execute(sql("UPDATE users SET is_admin = ? WHERE id = ?"), (1 if is_admin else 0, user_id))

        def admin_ids(self):
            return [user["id"] for user in self.all() if user["is_admin"] and user["is_active"]]

        def set_password(self, user_id, password):
            with notes_connection() as connection:
                connection.execute(
                    sql("UPDATE users SET password_hash = ? WHERE id = ?"), (generate_password_hash(password), user_id)
                )

        def delete(self, user_id):
            with notes_connection() as connection:
                connection.execute(sql("DELETE FROM users WHERE id = ?"), (user_id,))

    def user_prefs(user):
        try:
            prefs = json.loads(user.get("prefs") or "{}")
        except ValueError:
            prefs = {}
        return {"mute": [k for k in prefs.get("mute", []) if k in NOTIFY_KINDS], "popups": prefs.get("popups", True)}

    class NotificationStore:
        """Alerts for each person (comments, likes, mentions, new members...), read or not yet read."""

        columns = (
            "notifications.id, notifications.kind, notifications.message, notifications.link, "
            "notifications.post_id, notifications.is_read, users.username AS actor, "
            f"{time_as('notifications.created_at')}"
        )

        def add(self, user_ids, actor_id, kind, message, link="", post_id=None):
            targets = sorted({uid for uid in user_ids if uid and uid != actor_id})
            if not targets:
                return
            if kind in NOTIFY_KINDS:  # Leave out people who turned this kind off.
                with notes_connection() as connection:
                    rows = connection.execute(
                        sql(f"SELECT id, prefs FROM users WHERE id IN ({placeholders(targets)})"), targets
                    ).fetchall()
                muted = {row["id"] for row in rows if kind in user_prefs(dict(row))["mute"]}
                targets = [uid for uid in targets if uid not in muted]
            with notes_connection() as connection:
                for user_id in targets:
                    connection.execute(
                        sql(
                            "INSERT INTO notifications (user_id, actor_id, kind, message, link, post_id) "
                            "VALUES (?, ?, ?, ?, ?, ?)"
                        ),
                        (user_id, actor_id, kind, message[:300], link, post_id),
                    )

        def for_user(self, user_id, unread_only=False, after_id=0, limit=100):
            with notes_connection() as connection:
                rows = connection.execute(
                    sql(
                        f"SELECT {self.columns} FROM notifications "
                        "LEFT JOIN users ON users.id = notifications.actor_id "
                        "WHERE notifications.user_id = ? AND notifications.id > ?"
                        + (" AND notifications.is_read = 0" if unread_only else "")
                        + f" ORDER BY notifications.id DESC LIMIT {int(limit)}"
                    ),
                    (user_id, after_id),
                ).fetchall()
            return [dict(row) for row in rows]

        def get(self, user_id, notification_id):
            with notes_connection() as connection:
                row = connection.execute(
                    sql(
                        f"SELECT {self.columns} FROM notifications "
                        "LEFT JOIN users ON users.id = notifications.actor_id "
                        "WHERE notifications.user_id = ? AND notifications.id = ?"
                    ),
                    (user_id, notification_id),
                ).fetchone()
            return dict(row) if row else None

        def unread_count(self, user_id):
            with notes_connection() as connection:
                return connection.execute(
                    sql("SELECT COUNT(*) AS n FROM notifications WHERE user_id = ? AND is_read = 0"), (user_id,)
                ).fetchone()["n"]

        def latest_id(self, user_id):
            with notes_connection() as connection:
                row = connection.execute(
                    sql("SELECT MAX(id) AS n FROM notifications WHERE user_id = ?"), (user_id,)
                ).fetchone()
            return row["n"] or 0

        def set_read(self, user_id, notification_id, read=True):
            with notes_connection() as connection:
                connection.execute(
                    sql("UPDATE notifications SET is_read = ? WHERE user_id = ? AND id = ?"),
                    (1 if read else 0, user_id, notification_id),
                )

        def read_all(self, user_id):
            with notes_connection() as connection:
                connection.execute(
                    sql("UPDATE notifications SET is_read = 1 WHERE user_id = ? AND is_read = 0"), (user_id,)
                )

        def read_for_post(self, user_id, post_id):
            """Opening a post counts as reading the alerts about it."""
            with notes_connection() as connection:
                connection.execute(
                    sql("UPDATE notifications SET is_read = 1 WHERE user_id = ? AND post_id = ? AND is_read = 0"),
                    (user_id, post_id),
                )

        def clear_read(self, user_id):
            with notes_connection() as connection:
                connection.execute(sql("DELETE FROM notifications WHERE user_id = ? AND is_read = 1"), (user_id,))

    class FeedStore:
        """Posts with text, photos and videos that members share, with likes and comments."""

        post_columns = (
            "feed_posts.id, feed_posts.user_id, feed_posts.body, users.username AS author, users.avatar AS author_avatar, "
            f"{time_as('feed_posts.created_at')}"
        )

        def posts(self, viewer_id, before_id=None, post_id=None, user_id=None, limit=15):
            where, params = [], []
            if user_id is not None:
                where.append("feed_posts.user_id = ?")
                params.append(user_id)
            if post_id is not None:
                where.append("feed_posts.id = ?")
                params.append(post_id)
            if before_id:
                where.append("feed_posts.id < ?")
                params.append(before_id)
            with notes_connection() as connection:
                posts = [
                    dict(row)
                    for row in connection.execute(
                        sql(
                            f"SELECT {self.post_columns} FROM feed_posts "
                            "JOIN users ON users.id = feed_posts.user_id"
                            + (" WHERE " + " AND ".join(where) if where else "")
                            + f" ORDER BY feed_posts.id DESC LIMIT {int(limit)}"
                        ),
                        params,
                    ).fetchall()
                ]
                if not posts:
                    return []
                ids = [post["id"] for post in posts]
                marks = placeholders(ids)
                media = connection.execute(
                    sql(f"SELECT id, post_id, kind, mime FROM feed_media WHERE post_id IN ({marks}) "
                        "ORDER BY position, id"), ids
                ).fetchall()
                likes = connection.execute(
                    sql(f"SELECT feed_likes.post_id, feed_likes.user_id, users.username FROM feed_likes "
                        f"JOIN users ON users.id = feed_likes.user_id WHERE feed_likes.post_id IN ({marks}) "
                        "ORDER BY LOWER(users.username)"), ids
                ).fetchall()
                comment_time = time_as("feed_comments.created_at")
                comments = connection.execute(
                    sql(f"SELECT feed_comments.id, feed_comments.post_id, feed_comments.user_id, feed_comments.body, "
                        f"users.username AS author, users.avatar AS author_avatar, {comment_time} FROM feed_comments "
                        f"JOIN users ON users.id = feed_comments.user_id WHERE feed_comments.post_id IN ({marks}) "
                        "ORDER BY feed_comments.id"), ids
                ).fetchall()
            by_id = {post["id"]: {**post, "media": [], "likers": [], "liked": False, "comments": []} for post in posts}
            for row in media:
                by_id[row["post_id"]]["media"].append(dict(row))
            for row in likes:
                post = by_id[row["post_id"]]
                post["likers"].append(row["username"])
                post["liked"] = post["liked"] or row["user_id"] == viewer_id
            for row in comments:
                by_id[row["post_id"]]["comments"].append(dict(row))
            return [by_id[post_id] for post_id in ids]

        def post(self, post_id):
            with notes_connection() as connection:
                row = connection.execute(
                    sql(f"SELECT {self.post_columns} FROM feed_posts JOIN users ON users.id = feed_posts.user_id "
                        "WHERE feed_posts.id = ?"), (post_id,)
                ).fetchone()
            return dict(row) if row else None

        def add_post(self, user_id, body, media):
            """media: [(kind, stored name, mime)] already saved in the media folder."""
            with notes_connection() as connection:
                cursor = connection.execute(
                    sql("INSERT INTO feed_posts (user_id, body) VALUES (?, ?)") + returning_id, (user_id, body)
                )
                post_id = cursor.fetchone()["id"] if returning_id else cursor.lastrowid
                for position, (kind, stored, mime) in enumerate(media):
                    connection.execute(
                        sql("INSERT INTO feed_media (post_id, kind, stored, mime, position) VALUES (?, ?, ?, ?, ?)"),
                        (post_id, kind, stored, mime, position),
                    )
            return post_id

        def edit_post(self, post_id, body):
            with notes_connection() as connection:
                connection.execute(sql("UPDATE feed_posts SET body = ? WHERE id = ?"), (body, post_id))

        def stats(self, user_id):
            """Posts and likes received, for a profile."""
            with notes_connection() as connection:
                posts = connection.execute(
                    sql("SELECT COUNT(*) AS n FROM feed_posts WHERE user_id = ?"), (user_id,)).fetchone()["n"]
                likes = connection.execute(
                    sql("SELECT COUNT(*) AS n FROM feed_likes JOIN feed_posts ON feed_posts.id = feed_likes.post_id "
                        "WHERE feed_posts.user_id = ?"), (user_id,)).fetchone()["n"]
            return {"posts": posts, "likes": likes}

        def post_counts(self):
            with notes_connection() as connection:
                rows = connection.execute("SELECT user_id, COUNT(*) AS n FROM feed_posts GROUP BY user_id").fetchall()
            return {row["user_id"]: row["n"] for row in rows}

        def media(self, media_id):
            with notes_connection() as connection:
                row = connection.execute(
                    sql("SELECT id, post_id, kind, stored, mime FROM feed_media WHERE id = ?"), (media_id,)
                ).fetchone()
            return dict(row) if row else None

        def delete_post(self, post_id):
            with notes_connection() as connection:
                stored = [row["stored"] for row in connection.execute(
                    sql("SELECT stored FROM feed_media WHERE post_id = ?"), (post_id,)).fetchall()]
                for table in ("feed_media", "feed_comments", "feed_likes", "notifications"):
                    connection.execute(sql(f"DELETE FROM {table} WHERE post_id = ?"), (post_id,))
                connection.execute(sql("DELETE FROM feed_posts WHERE id = ?"), (post_id,))
            for name in stored:
                (feed_media_dir / name).unlink(missing_ok=True)

        def toggle_like(self, post_id, user_id):
            """Return True when the post is now liked by this person."""
            with notes_connection() as connection:
                cursor = connection.execute(
                    sql("DELETE FROM feed_likes WHERE post_id = ? AND user_id = ?"), (post_id, user_id)
                )
                if cursor.rowcount:
                    return False
                connection.execute(sql("INSERT INTO feed_likes (post_id, user_id) VALUES (?, ?)"), (post_id, user_id))
                return True

        def like_count(self, post_id):
            with notes_connection() as connection:
                return connection.execute(
                    sql("SELECT COUNT(*) AS n FROM feed_likes WHERE post_id = ?"), (post_id,)
                ).fetchone()["n"]

        def add_comment(self, post_id, user_id, body):
            with notes_connection() as connection:
                cursor = connection.execute(
                    sql("INSERT INTO feed_comments (post_id, user_id, body) VALUES (?, ?, ?)") + returning_id,
                    (post_id, user_id, body),
                )
                return cursor.fetchone()["id"] if returning_id else cursor.lastrowid

        def comment(self, comment_id):
            with notes_connection() as connection:
                row = connection.execute(
                    sql("SELECT id, post_id, user_id, body FROM feed_comments WHERE id = ?"), (comment_id,)
                ).fetchone()
            return dict(row) if row else None

        def commenter_ids(self, post_id):
            with notes_connection() as connection:
                rows = connection.execute(
                    sql("SELECT DISTINCT user_id FROM feed_comments WHERE post_id = ?"), (post_id,)
                ).fetchall()
            return [row["user_id"] for row in rows]

        def delete_comment(self, comment_id):
            with notes_connection() as connection:
                connection.execute(sql("DELETE FROM feed_comments WHERE id = ?"), (comment_id,))

        def delete_user_content(self, user_id):
            """When an account is deleted, its posts, comments, likes and alerts go too."""
            with notes_connection() as connection:
                post_ids = [row["id"] for row in connection.execute(
                    sql("SELECT id FROM feed_posts WHERE user_id = ?"), (user_id,)).fetchall()]
            for post_id in post_ids:
                self.delete_post(post_id)
            with notes_connection() as connection:
                for table in ("feed_comments", "feed_likes", "notifications"):
                    connection.execute(sql(f"DELETE FROM {table} WHERE user_id = ?"), (user_id,))
                connection.execute(sql("UPDATE notifications SET actor_id = NULL WHERE actor_id = ?"), (user_id,))

    user_store = UserStore()
    app.extensions["users"] = user_store

    notification_store = NotificationStore()
    app.extensions["notifications"] = notification_store

    feed_store = FeedStore()
    app.extensions["feed"] = feed_store

    request_store = RequestStore()
    app.extensions["requests"] = request_store

    mission_store = MissionStore()
    app.extensions["missions"] = mission_store

    wallet_store = WalletStore()
    app.extensions["wallet"] = wallet_store

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

    # ---------------------------------------------------------------- sign-in

    PUBLIC_ENDPOINTS = {"static", "healthz", "login", "setup", "register"}
    failed_logins = {}  # address -> times of recent failed sign-ins
    signups = {}  # address -> times of recent sign-ups

    def signup_mode():
        mode = settings_store.get("SIGNUP_MODE")
        return mode if mode in SIGNUP_MODES else "approval"

    def too_many_failures(address):
        recent = [t for t in failed_logins.get(address, []) if time.time() - t < 600]
        failed_logins[address] = recent
        return len(recent) >= 8

    def safe_next(target):
        # Only paths on this site, never another host ("//evil.example" or "https://…").
        return target if target and target.startswith("/") and not target.startswith("//") else url_for("index")

    def sign_in(user):
        session.clear()
        regenerate = getattr(app.session_interface, "regenerate", None)
        if regenerate:
            try:
                regenerate(session)  # A new session id, so an old one can't be reused.
            except Exception:
                pass
        session["user_id"] = user["id"]
        session.permanent = True

    @app.context_processor
    def account_context():
        user = g.get("user")
        alerts = {}
        if user:
            try:
                alerts = {"unread_alerts": notification_store.unread_count(user["id"]),
                          "latest_alert": notification_store.latest_id(user["id"]),
                          "alert_popups": user_prefs(user)["popups"]}
            except Exception:  # The top bar must never break a page.
                app.logger.exception("Could not count notifications")
        return {"current_user": user, **alerts}

    @app.before_request
    def require_login():
        g.user = None
        user_id = session.get("user_id")
        if user_id is not None:
            g.user = user_store.get(user_id)
            if g.user is None or not g.user["is_active"]:
                g.user = None
                session.pop("user_id", None)  # The account was deleted or is waiting for approval.
        if g.user or request.endpoint in PUBLIC_ENDPOINTS:
            return None
        if user_store.count() == 0:
            return redirect(url_for("setup"))
        # Scripts (chat, PDF editor) get a plain error they can show; pages go to the sign-in form.
        wants_json = request.headers.get("X-CSRF-Token") or request.accept_mimetypes.best == "application/json"
        if wants_json or request.path == "/chat/send":
            return jsonify(error="You've been signed out. Reload the page and sign in again."), 401
        target = request.full_path if request.method == "GET" else (request.referrer or "/")
        return redirect(url_for("login", next=target.rstrip("?")))

    def admin_required():
        if not (g.user and g.user["is_admin"]):
            abort(403, "Only an administrator can open this page.")

    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        """The very first visit creates the administrator account."""
        if user_store.count():
            return redirect(url_for("login"))
        error = None
        username = ""
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            error = check_new_account(username, password, request.form.get("confirm", ""))
            if not error:
                user_id = user_store.add(username, password, is_admin=True)
                sign_in(user_store.get(user_id))
                flash(f"Welcome, {username}! You're the administrator: add other people on the Settings page.", "success")
                return redirect(url_for("index"), code=303)
        return render_template("auth.html", mode="setup", error=error, username=username), (400 if error else 200)

    def check_new_account(username, password, confirm, check_name=True):
        if check_name and not USERNAME.fullmatch(username):
            return "Use 2 to 40 letters, numbers, dots, dashes, underscores or @ for the username."
        if check_name and user_store.by_name(username):
            return "That username is taken."
        if len(password) < MIN_PASSWORD:
            return f"Use at least {MIN_PASSWORD} characters for the password."
        if password != confirm:
            return "The two passwords don't match."
        return None

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if user_store.count() == 0:
            return redirect(url_for("setup"))
        next_page = safe_next(request.values.get("next", ""))
        if g.user:
            return redirect(next_page)
        error = None
        username = ""
        if request.method == "POST":
            address = request.remote_addr or "?"
            username = request.form.get("username", "").strip()
            user = user_store.by_name(username)
            if too_many_failures(address):
                error = "Too many wrong passwords. Wait 10 minutes and try again."
            elif user and check_password_hash(user["password_hash"], request.form.get("password", "")):
                failed_logins.pop(address, None)
                if not user["is_active"]:
                    error = "Your account is waiting for an administrator to approve it. Try again later."
                else:
                    sign_in(user)
                    return redirect(next_page, code=303)
            else:
                failed_logins.setdefault(address, []).append(time.time())
                error = "Wrong username or password."
        return render_template("auth.html", mode="login", error=error, username=username, next=next_page,
                               signup=signup_mode()), (401 if error else 200)

    @app.route("/register", methods=["GET", "POST"])
    def register():
        """The sign-up page: anyone can make an account unless an administrator turned it off."""
        if user_store.count() == 0:
            return redirect(url_for("setup"))
        if g.user:
            return redirect(url_for("index"))
        mode = signup_mode()
        if mode == "off":
            flash("Sign-up is turned off. Ask an administrator to add you.", "error")
            return redirect(url_for("login"), code=303)
        error = None
        username = ""
        if request.method == "POST":
            address = request.remote_addr or "?"
            recent = [t for t in signups.get(address, []) if time.time() - t < 3600]
            signups[address] = recent
            username = request.form.get("username", "").strip()
            if len(recent) >= 5:
                error = "Too many new accounts from this device. Try again in an hour."
            else:
                error = check_new_account(username, request.form.get("password", ""), request.form.get("confirm", ""))
            if not error:
                recent.append(time.time())
                active = mode == "open"
                user_id = user_store.add(username, request.form["password"], is_active=active)
                notification_store.add(
                    user_store.admin_ids(), user_id, "signup",
                    f"{username} signed up." + ("" if active else " Approve or remove the account on Settings."),
                    url_for("settings_section", section="users"),
                )
                if active:
                    sign_in(user_store.get(user_id))
                    flash(f"Welcome, {username}! Your account is ready.", "success")
                    return redirect(url_for("feed"), code=303)
                flash("Thanks! Your account was created. You can sign in once an administrator approves it.", "success")
                return redirect(url_for("login"), code=303)
        return render_template("auth.html", mode="register", error=error, username=username,
                               approval=mode == "approval"), (400 if error else 200)

    @app.post("/logout")
    def logout():
        session.clear()
        flash("You've signed out.", "success")
        return redirect(url_for("login"), code=303)

    @app.route("/account", methods=["GET", "POST"])
    def account():
        error = None
        if request.method == "GET":
            return redirect(url_for("settings_section", section="account"))
        if request.method == "POST":
            if not check_password_hash(g.user["password_hash"], request.form.get("current", "")):
                error = "Your current password is not right."
            else:
                error = check_new_account("", request.form.get("password", ""), request.form.get("confirm", ""),
                                          check_name=False)
            if not error:
                user_store.set_password(g.user["id"], request.form["password"])
                flash("Your password was changed.", "success")
                return redirect(url_for("settings_section", section="account", _anchor="password"), code=303)
        return render_settings("account", 400, password_error=error, **section_context("account"))

    @app.post("/settings/users")
    def add_user():
        admin_required()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        error = check_new_account(username, password, password)
        if error:
            flash(error, "error")
        else:
            user_store.add(username, password, is_admin=bool(request.form.get("is_admin")))
            flash(f"Added {username}. Give them their password; they can change it under their name at the top.",
                  "success")
        return redirect(url_for("settings_section", section="users"), code=303)

    @app.post("/settings/users/<int:user_id>/password")
    def reset_password(user_id):
        admin_required()
        user = user_store.get(user_id)
        password = request.form.get("password", "")
        if user is None:
            flash("That account no longer exists.", "error")
        elif len(password) < MIN_PASSWORD:
            flash(f"Use at least {MIN_PASSWORD} characters for the password.", "error")
        else:
            user_store.set_password(user_id, password)
            flash(f"New password set for {user['username']}.", "success")
        return redirect(url_for("settings_section", section="users"), code=303)

    @app.post("/settings/users/<int:user_id>/approve")
    def approve_user(user_id):
        admin_required()
        user = user_store.get(user_id)
        if user is None:
            flash("That account no longer exists.", "error")
        else:
            user_store.activate(user_id)
            notification_store.add([user_id], g.user["id"], "approved",
                                   "Your account was approved. Welcome!", url_for("feed"))
            flash(f"Approved {user['username']}. They can sign in now.", "success")
        return redirect(url_for("settings_section", section="users"), code=303)

    @app.post("/settings/users/<int:user_id>/admin")
    def toggle_admin(user_id):
        admin_required()
        user = user_store.get(user_id)
        make_admin = request.form.get("is_admin") == "1"
        if user is None:
            flash("That account no longer exists.", "error")
        elif not make_admin and user["id"] == g.user["id"]:
            flash("You can't remove your own administrator rights. Ask another administrator.", "error")
        elif make_admin and not user["is_active"]:
            flash("Approve the account first.", "error")
        else:
            user_store.set_admin(user_id, make_admin)
            flash(f"{user['username']} is {'now an administrator' if make_admin else 'no longer an administrator'}.",
                  "success")
        return redirect(url_for("settings_section", section="users"), code=303)

    @app.post("/settings/signup")
    def save_signup_mode():
        admin_required()
        mode = request.form.get("mode", "")
        if mode not in SIGNUP_MODES:
            flash("Choose who can sign up.", "error")
        else:
            settings_store.save({"SIGNUP_MODE": mode})
            flash("Sign-up setting saved.", "success")
        return redirect(url_for("settings_section", section="users"), code=303)

    @app.post("/settings/users/<int:user_id>/delete")
    def delete_user(user_id):
        admin_required()
        user = user_store.get(user_id)
        admins = [u for u in user_store.all() if u["is_admin"]]
        if user is None:
            flash("That account no longer exists.", "error")
        elif user["id"] == g.user["id"]:
            flash("You can't delete your own account while signed in to it.", "error")
        elif user["is_admin"] and len(admins) <= 1:
            flash("Keep at least one administrator.", "error")
        else:
            feed_store.delete_user_content(user_id)
            chat_store.delete_user_content(user_id)
            mission_store.delete_user_content(user_id)
            wallet_store.delete_user_content(user_id)
            invalidate_notes(user_id)
            remove_avatar(user["avatar"])
            user_store.delete(user_id)
            flash(f"Deleted {user['username']}.", "success")
        return redirect(url_for("settings_section", section="users"), code=303)

    @app.before_request
    def validate_csrf():
        if request.method == "POST":
            # Forms send the token as a field; the PDF editor's JSON requests send a header.
            supplied = request.form.get("_csrf_token") or request.headers.get("X-CSRF-Token", "")
            if not hmac.compare_digest(supplied, session.get("_csrf_token", "")):
                abort(400, "Invalid form token. Reload the page and try again.")

    @app.get("/healthz")
    def healthz():
        """For Docker's health check: the database and Redis must both answer."""
        problems = []
        try:
            with notes_connection() as connection:
                connection.execute("SELECT 1").fetchone()
        except Exception as error:
            problems.append(f"database: {error}")
        if cache is not None:
            try:
                cache.ping()
            except cache_errors as error:
                problems.append(f"redis: {error}")
        if problems:
            return jsonify(status="error", problems=problems), 503
        return jsonify(status="ok")

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
        entries = UPDATE_LOG
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
        requests_all = request_store.all()
        counts = {status: sum(1 for r in requests_all if r["status"] == status) for status in REQUEST_STATUSES}
        shown_status = request.args.get("status", "")
        shown_status = shown_status if shown_status in REQUEST_STATUSES else ""
        shown = [r for r in requests_all if not shown_status or r["status"] == shown_status]
        if query:
            needle = query.lower()
            shown = [r for r in shown if needle in r["title"].lower() or needle in r["details"].lower()
                     or needle in r["requested_by"].lower() or needle == r["done_version"]]
        return render_template(
            "updates.html", entries=entries, query=query, requests=shown, counts=counts,
            total=len(requests_all), shown_status=shown_status, statuses=REQUEST_STATUSES,
            kinds=REQUEST_KINDS, form=session.pop("request_form", None),
        )

    @app.post("/updates/requests")
    def add_request():
        kind = request.form.get("kind", "feature")
        title = " ".join(request.form.get("title", "").split())
        details = request.form.get("details", "").strip().replace("\r\n", "\n")
        requested_by = " ".join(request.form.get("requested_by", "").split())
        error = None
        if kind not in REQUEST_KINDS:
            error = "Choose what kind of request this is."
        elif not title:
            error = "Give the request a short title."
        elif len(title) > 120 or len(details) > 4000 or len(requested_by) > 60:
            error = "Keep the title under 120 characters, details under 4,000, and the name under 60."
        if error:
            flash(error, "error")
            session["request_form"] = {"kind": kind, "title": title, "details": details, "requested_by": requested_by}
            return redirect(url_for("updates", _anchor="requests"), code=303)
        request_store.add(kind, title, details, requested_by)
        notification_store.add(user_store.admin_ids(), g.user["id"], "request",
                               f"{g.user['username']} sent a request: “{title}”", url_for("updates", _anchor="requests"))
        flash("Thanks! Your request was added. Its status shows here as it's worked on.", "success")
        return redirect(url_for("updates", _anchor="requests"), code=303)

    def update_request_status(item, status, actor, note=""):
        """Sets a request's status and tells the person who asked. Done records the app version."""
        request_store.set_status(item["id"], status, APP_VERSION if status == "done" else "",
                                 actor["username"] if status == "queued" else item["sent_by"], note)
        requester = user_store.by_name(item["requested_by"]) if item["requested_by"] else None
        if requester and status != item["status"]:
            notification_store.add([requester["id"]], actor["id"] if actor else None, "request",
                                   f"Your request “{item['title']}” is now {REQUEST_STATUSES[status].lower()}.",
                                   url_for("updates", _anchor="requests"))

    @app.cli.command("claude-next")
    def claude_next():
        """Prints the oldest request sent to Claude as JSON (nothing if there is none).

        Used by scripts/claude-worker.sh. A request whose sender is no longer an
        administrator is put back to Waiting instead.
        """
        while (item := request_store.next_queued()) is not None:
            sender = user_store.by_name(item["sent_by"])
            if sender and sender["is_admin"] and sender["is_active"]:
                print(json.dumps(item, ensure_ascii=False))
                return
            with app.test_request_context():
                update_request_status(item, "open", None, "Not sent: only an administrator can send requests to Claude.")

    @app.cli.command("claude-status")
    @click.argument("request_id", type=int)
    @click.argument("status", type=click.Choice(list(REQUEST_STATUSES)))
    @click.option("--note", default="", help="Shown under the request, such as Claude's summary.")
    def claude_status(request_id, status, note):
        """Sets a request's status from scripts/claude-worker.sh, notifying the requester."""
        item = request_store.get(request_id)
        if item is None:
            raise click.ClickException(f"Request {request_id} does not exist.")
        with app.test_request_context():
            update_request_status(item, status, user_store.by_name(item["sent_by"]), note[:2000])

    @app.post("/updates/requests/<int:request_id>/status")
    def request_status(request_id):
        item = request_store.get(request_id)
        status = request.form.get("status", "")
        if item is None or status not in REQUEST_STATUSES:
            flash("That request no longer exists." if item is None else "Choose a status.", "error")
            return redirect(url_for("updates", _anchor="requests"), code=303)
        # Sending to Claude changes the app's code, so only administrators can do it.
        if status == "queued" and not g.user["is_admin"]:
            flash("Only an administrator can send a request to Claude.", "error")
            return redirect(url_for("updates", _anchor="requests"), code=303)
        update_request_status(item, status, g.user)
        flash(f"“{item['title']}” is now {REQUEST_STATUSES[status].lower()}.", "success")
        return redirect(url_for("updates", status=request.form.get("shown_status") or None, _anchor="requests"), code=303)

    @app.post("/updates/requests/<int:request_id>/delete")
    def delete_request(request_id):
        if request_store.get(request_id) is not None:
            request_store.delete(request_id)
            flash("Request deleted.", "success")
        return redirect(url_for("updates", status=request.form.get("shown_status") or None, _anchor="requests"), code=303)

    # --------------------------------------------------------------- missions

    def mission_text(name, limit=MISSION_TEXT_LIMIT):
        return request.form.get(name, "").strip().replace("\r\n", "\n")[:limit + 1]

    @app.get("/missions")
    def missions():
        user = g.user
        everything = mission_store.all()
        for mission in everything:
            mine = [item for item in mission["submissions"] if item["user_id"] == user["id"]]
            mission["mine"] = mine
            mission["can_submit"] = bool(mission["is_open"]) and not any(
                item["status"] in ("pending", "approved") for item in mine)
            mission["approved"] = sum(1 for item in mission["submissions"] if item["status"] == "approved")
        waiting = [{**item, "mission_title": mission["title"]} for mission in everything
                   for item in mission["submissions"] if item["status"] == "pending"] if user["is_admin"] else []
        return render_template(
            "missions.html", missions=[m for m in everything if m["is_open"]],
            closed=[m for m in everything if not m["is_open"]], waiting=waiting,
            statuses=MISSION_STATUSES, form=session.pop("mission_form", None),
        )

    @app.post("/missions")
    def create_mission():
        admin_required()
        title = " ".join(request.form.get("title", "").split())
        details = mission_text("details")
        reward = wallet_amount(request.form.get("reward", ""), allow_zero=True)
        error = None
        if not title:
            error = "Give the mission a short title."
        elif len(title) > 120 or len(details) > MISSION_TEXT_LIMIT:
            error = "Keep the title under 120 characters and the details under 4,000."
        elif reward is None:
            error = f"The reward is a whole number of points from 0 to {WALLET_MAX:,}."
        if error:
            flash(error, "error")
            session["mission_form"] = {"title": title[:120], "details": details[:MISSION_TEXT_LIMIT],
                                       "reward": request.form.get("reward", "")[:12]}
            return redirect(url_for("missions"), code=303)
        mission_store.add(title, details, g.user["id"], reward)
        everyone = [user["id"] for user in user_store.all() if user["is_active"]]
        notification_store.add(everyone, g.user["id"], "mission",
                               f"{g.user['username']} set a new mission: “{title}”", url_for("missions"))
        flash("Mission added. Everyone can now submit their work for it.", "success")
        return redirect(url_for("missions"), code=303)

    @app.post("/missions/<int:mission_id>/open")
    def mission_open(mission_id):
        admin_required()
        mission = mission_store.mission(mission_id)
        if mission is None:
            flash("That mission no longer exists.", "error")
        else:
            is_open = request.form.get("open") == "1"
            mission_store.set_open(mission_id, is_open)
            flash(f"“{mission['title']}” is {'open again' if is_open else 'closed: no new work can be submitted'}.",
                  "success")
        return redirect(url_for("missions"), code=303)

    @app.post("/missions/<int:mission_id>/delete")
    def mission_delete(mission_id):
        admin_required()
        if mission_store.mission(mission_id) is not None:
            mission_store.delete(mission_id)
            flash("Mission deleted, with the work submitted for it.", "success")
        return redirect(url_for("missions"), code=303)

    @app.post("/missions/<int:mission_id>/submit")
    def mission_submit(mission_id):
        mission = mission_store.mission(mission_id)
        body = mission_text("body")
        error = None
        if mission is None:
            error = "That mission no longer exists."
        elif not mission["is_open"]:
            error = "This mission is closed, so no new work can be submitted."
        elif mission_store.active_submission(mission_id, g.user["id"]):
            error = "You've already submitted work for this mission."
        elif not body:
            error = "Describe the work you did before submitting."
        elif len(body) > MISSION_TEXT_LIMIT:
            error = "Keep your submission under 4,000 characters."
        if error:
            flash(error, "error")
            return redirect(url_for("missions", _anchor=f"mission-{mission_id}"), code=303)
        mission_store.submit(mission_id, g.user["id"], body)
        notification_store.add(user_store.admin_ids(), g.user["id"], "mission",
                               f"{g.user['username']} submitted work for “{mission['title']}”",
                               url_for("missions", _anchor="mission-review"))
        flash("Submitted. An administrator will approve it or send it back with a note.", "success")
        return redirect(url_for("missions", _anchor=f"mission-{mission_id}"), code=303)

    @app.post("/missions/submissions/<int:submission_id>/review")
    def mission_review(submission_id):
        admin_required()
        item = mission_store.submission(submission_id)
        decision = request.form.get("decision", "")
        note = mission_text("note", 1000)[:1000]
        if item is None:
            flash("That submission no longer exists.", "error")
        elif decision not in ("approved", "returned"):
            flash("Choose Approve or Send back.", "error")
        elif decision == "returned" and not note:
            flash("Say what to change when sending work back.", "error")
        elif item["status"] == "approved":
            flash("That work is already approved.", "error")
        else:
            mission = mission_store.mission(item["mission_id"])
            mission_store.review(submission_id, decision, note, g.user["id"])
            what = "approved" if decision == "approved" else "sent back"
            paid = decision == "approved" and mission["reward"] > 0
            if paid:
                wallet_store.reward(item["user_id"], mission["reward"], mission["title"][:WALLET_NOTE], g.user["id"])
            notification_store.add([item["user_id"]], g.user["id"], "mission",
                                   f"{g.user['username']} {what} your work for “{mission['title']}”"
                                   + (f" and {mission['reward']:,} points went to your wallet" if paid else "")
                                   + (f": {note}" if note else ""),
                                   url_for("missions", _anchor=f"mission-{item['mission_id']}"))
            flash(f"{item['author']}'s work {what}"
                  + (f", and {mission['reward']:,} points went to their wallet." if paid else "."), "success")
        return redirect(url_for("missions", _anchor="mission-review"), code=303)

    @app.post("/missions/submissions/<int:submission_id>/delete")
    def mission_submission_delete(submission_id):
        item = mission_store.submission(submission_id)
        if item is None:
            flash("That submission no longer exists.", "error")
        elif not g.user["is_admin"] and (item["user_id"] != g.user["id"] or item["status"] == "approved"):
            abort(403, "You can only withdraw your own work before it's approved.")
        else:
            mission_store.delete_submission(submission_id)
            flash("Submission removed.", "success")
        return redirect(url_for("missions", _anchor=f"mission-{item['mission_id']}" if item else None), code=303)

    # ----------------------------------------------------------------- wallet

    def wallet_amount(value, allow_zero=False):
        """A whole number of points from the form ("1,500" is fine), or None if it isn't one."""
        value = value.strip().replace(",", "")
        if allow_zero and not value:
            return 0
        if not value.isdigit():
            return None
        amount = int(value)
        return amount if (0 if allow_zero else 1) <= amount <= WALLET_MAX else None

    @app.get("/wallet")
    def wallet():
        user = g.user
        people = [person for person in user_store.all() if person["is_active"]]
        balances = wallet_store.balances() if user["is_admin"] else {}
        return render_template(
            "wallet.html", balance=wallet_store.balance(user["id"]),
            history=wallet_store.history(user["id"]),
            everyone=wallet_store.history() if user["is_admin"] else [],
            recipients=[person for person in people if person["id"] != user["id"]],
            people=[{**person, "balance": balances.get(person["id"], 0)} for person in people],
            kinds=WALLET_KINDS, max_amount=WALLET_MAX,
        )

    @app.post("/wallet/transfer")
    def wallet_transfer():
        recipient = user_store.by_name(request.form.get("to", "").strip())
        amount = wallet_amount(request.form.get("amount", ""))
        note = " ".join(request.form.get("note", "").split())[:WALLET_NOTE]
        error = None
        if recipient is None or not recipient["is_active"]:
            error = "Choose who to send points to."
        elif recipient["id"] == g.user["id"]:
            error = "You can't send points to yourself."
        elif amount is None:
            error = f"Send a whole number of points from 1 to {WALLET_MAX:,}."
        else:
            try:
                wallet_store.transfer(g.user["id"], recipient["id"], amount, note)
            except ValueError as exc:
                error = str(exc)
        if error:
            flash(error, "error")
        else:
            notification_store.add([recipient["id"]], g.user["id"], "wallet",
                                   f"{g.user['username']} sent you {amount:,} points" + (f": {note}" if note else ""),
                                   url_for("wallet"))
            flash(f"Sent {amount:,} points to {recipient['username']}.", "success")
        return redirect(url_for("wallet"), code=303)

    @app.post("/wallet/deposit")
    def wallet_deposit():
        admin_required()
        person = user_store.get(request.form.get("user_id", type=int) or 0)
        amount = wallet_amount(request.form.get("amount", ""))
        take = request.form.get("action") == "take"
        note = " ".join(request.form.get("note", "").split())[:WALLET_NOTE]
        error = None
        if person is None:
            error = "Choose whose wallet to change."
        elif amount is None:
            error = f"Enter a whole number of points from 1 to {WALLET_MAX:,}."
        else:
            try:
                wallet_store.deposit(person["id"], -amount if take else amount, note, g.user["id"])
            except ValueError as exc:
                error = str(exc)
        if error:
            flash(error, "error")
        else:
            if not take:
                notification_store.add([person["id"]], g.user["id"], "wallet",
                                       f"{g.user['username']} deposited {amount:,} points in your wallet"
                                       + (f": {note}" if note else ""), url_for("wallet"))
            flash(f"{'Took' if take else 'Deposited'} {amount:,} points {'from' if take else 'for'} "
                  f"{person['username']}.", "success")
        return redirect(url_for("wallet"), code=303)

    # ------------------------------------------------------------------- feed

    @app.template_filter("feed_text")
    def feed_text(value):
        """Post and comment text: escaped, with links and @mentions made clickable."""
        def linked(match):
            if match.group("link"):
                return f'<a href="{match.group("link")}" target="_blank" rel="noopener noreferrer">{match.group("link")}</a>'
            return f'<span class="feed-mention">@{match.group("name")}</span>'

        return Markup(FEED_TOKENS.sub(linked, str(escape(value))))

    def mentioned_ids(text):
        names = {name.lower() for name in MENTION.findall(text)}
        users = (user_store.by_name(name) for name in names)
        return [user["id"] for user in users if user and user["is_active"]]

    def snippet(text, size=60):
        text = " ".join(text.split())
        return text if len(text) <= size else text[: size - 1] + "…"

    def save_feed_upload(upload):
        """Check one photo or video and save it; return (kind, stored name, mime) or raise ValueError."""
        name = download_stem(upload.filename) + Path(upload.filename).suffix.lower()
        extension = Path(upload.filename).suffix.lower()
        if extension in FEED_IMAGES:
            kind, limit = "image", FEED_MAX_IMAGE
        elif extension in FEED_VIDEOS:
            kind, limit = "video", FEED_MAX_VIDEO
        else:
            raise ValueError(f"“{name}” is not a photo or video this page can show (JPG, PNG, GIF, WebP, HEIC, "
                             "MP4, MOV or WebM).")
        temp = tempfile.NamedTemporaryFile(dir=feed_media_dir, prefix=".upload-", delete=False)
        try:
            with temp:
                upload.save(temp)
            path = Path(temp.name)
            size = path.stat().st_size
            if size > limit:
                raise ValueError(f"“{name}” is too big: photos can be up to 25 MB and videos up to 500 MB.")
            with path.open("rb") as handle:
                head = handle.read(16)
            if kind == "image":
                from PIL import Image, UnidentifiedImageError
                if extension in (".heic", ".heif"):
                    from pillow_heif import register_heif_opener
                    register_heif_opener()
                try:
                    with Image.open(path) as image:
                        image.verify()
                    if extension in (".heic", ".heif"):
                        with Image.open(path) as image:
                            converted = path.with_suffix(".jpg")
                            image.convert("RGB").save(converted, "JPEG", quality=88)
                        path.unlink()
                        path = converted
                        extension = ".jpg"
                except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError):
                    raise ValueError(f"“{name}” is not a photo the server can read.")
            else:
                box = head[4:8]
                is_webm = head[:4] == b"\x1aE\xdf\xa3"
                is_mp4 = box == b"ftyp" or (extension == ".mov" and box in (b"moov", b"wide", b"mdat", b"free", b"skip"))
                if not (is_webm if extension == ".webm" else is_mp4):
                    raise ValueError(f"“{name}” is not a video the browser can play.")
            stored = f"{secrets.token_hex(16)}{extension}"
            path.rename(feed_media_dir / stored)
            return kind, stored, (FEED_IMAGES | FEED_VIDEOS)[extension]
        except BaseException:
            Path(temp.name).unlink(missing_ok=True)
            Path(temp.name).with_suffix(".jpg").unlink(missing_ok=True)
            raise

    avatar_dir = feed_media_dir / "avatars"
    avatar_dir.mkdir(exist_ok=True)
    AVATAR_NAME = re.compile(r"[0-9a-f]{32}\.jpg")

    def save_avatar(upload):
        """Crop a profile photo to a 320 px square JPEG (no location data kept); return its file name."""
        from PIL import Image, ImageOps, UnidentifiedImageError
        from pillow_heif import register_heif_opener

        register_heif_opener()
        data = upload.read(FEED_MAX_IMAGE + 1)
        if len(data) > FEED_MAX_IMAGE:
            raise ValueError("Profile photos can be up to 25 MB.")
        try:
            with Image.open(io.BytesIO(data)) as image:
                image = ImageOps.exif_transpose(image).convert("RGB")
                image = ImageOps.fit(image, (320, 320), Image.LANCZOS)
        except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError):
            raise ValueError("That file is not a photo the server can read. Try a JPG or PNG.")
        name = f"{secrets.token_hex(16)}.jpg"
        image.save(avatar_dir / name, "JPEG", quality=88)
        return name

    def remove_avatar(name):
        if name and AVATAR_NAME.fullmatch(name):
            (avatar_dir / name).unlink(missing_ok=True)

    @app.get("/avatars/<name>")
    def avatar_image(name):
        if not AVATAR_NAME.fullmatch(name) or not (avatar_dir / name).is_file():
            abort(404)
        # Each new photo gets a new name, so browsers can keep this one for good.
        response = send_file(avatar_dir / name, mimetype="image/jpeg", max_age=365 * 24 * 3600)
        response.headers["Cache-Control"] = "private, max-age=31536000, immutable"
        return response

    @app.template_filter("hue")
    def name_hue(name):
        """A steady colour for someone's initial when they have no profile photo."""
        return sum(ord(char) * (index + 7) for index, char in enumerate(name or "?")) % 360

    def feed_people():
        counts = feed_store.post_counts()
        people = [{**user, "posts": counts.get(user["id"], 0)} for user in user_store.all() if user["is_active"]]
        return sorted(people, key=lambda person: (person["id"] != g.user["id"], -person["posts"],
                                                  person["username"].lower()))

    def feed_render(posts, **context):
        context.setdefault("single", False)
        context.setdefault("profile", None)
        context.setdefault("before", None)
        context.setdefault("older", None)
        return render_template(
            "feed.html", posts=posts, people=feed_people(), me_stats=feed_store.stats(g.user["id"]),
            max_files=FEED_MAX_FILES, accept=",".join([*FEED_IMAGES, *FEED_VIDEOS]),
            form=session.pop("feed_form", None), **context,
        )

    def feed_back(post_id=None, single=False):
        if single and post_id:
            return redirect(url_for("feed_post", post_id=post_id), code=303)
        return redirect(url_for("feed", _anchor=f"post-{post_id}" if post_id else None), code=303)

    def can_change(owner_id):
        return g.user["id"] == owner_id or g.user["is_admin"]

    @app.get("/feed")
    def feed():
        before = request.args.get("before", type=int)
        posts = feed_store.posts(g.user["id"], before_id=before)
        older = posts[-1]["id"] if len(posts) == 15 else None
        if request.args.get("partial"):  # Infinite scroll asks for just the next posts.
            return render_template("_feed_posts.html", posts=posts, single=False, older=older,
                                   next_url=url_for("feed", before=older, partial=1) if older else "")
        return feed_render(posts, before=before, older=older)

    @app.get("/feed/u/<username>")
    def feed_profile(username):
        person = user_store.by_name(username)
        if person is None or not person["is_active"]:
            flash("There's no member with that name.", "error")
            return redirect(url_for("feed"), code=303)
        before = request.args.get("before", type=int)
        posts = feed_store.posts(g.user["id"], before_id=before, user_id=person["id"], limit=30)
        older = posts[-1]["id"] if len(posts) == 30 else None
        if request.args.get("partial"):
            return render_template("_feed_posts.html", posts=posts, single=False, older=older,
                                   next_url=url_for("feed_profile", username=person["username"], before=older,
                                                    partial=1) if older else "")
        return feed_render(posts, profile={**person, **feed_store.stats(person["id"])}, before=before, older=older)

    @app.get("/feed/posts/<int:post_id>")
    def feed_post(post_id):
        posts = feed_store.posts(g.user["id"], post_id=post_id)
        if not posts:
            flash("That post was deleted.", "error")
            return redirect(url_for("feed"), code=303)
        notification_store.read_for_post(g.user["id"], post_id)
        return feed_render(posts, single=True)

    @app.post("/feed/posts/<int:post_id>/edit")
    def feed_edit(post_id):
        post = feed_store.post(post_id)
        wants_json = request.headers.get("X-Requested-With") == "fetch"
        body = request.form.get("body", "").strip().replace("\r\n", "\n")
        error = None
        if post is None:
            error = "That post was deleted."
        elif post["user_id"] != g.user["id"]:
            error = "You can only edit your own posts."
        elif len(body) > FEED_POST_LIMIT:
            error = f"Posts can be at most {FEED_POST_LIMIT:,} characters."
        elif not body and not feed_store.posts(g.user["id"], post_id=post_id)[0]["media"]:
            error = "A post without photos or videos needs some text."
        if error:
            if wants_json:
                return jsonify(error=error), 400
            flash(error, "error")
            return redirect(url_for("feed"), code=303)
        feed_store.edit_post(post_id, body)
        if wants_json:
            return jsonify(html=str(feed_text(body)) if body else "")
        flash("Post updated.", "success")
        return feed_back(post_id, request.form.get("single") == "1")

    @app.post("/feed/posts")
    def feed_create():
        wants_json = request.headers.get("X-Requested-With") == "fetch"
        body = request.form.get("body", "").strip().replace("\r\n", "\n")
        uploads = [upload for upload in request.files.getlist("media") if upload.filename]

        def fail(message):
            if wants_json:
                return jsonify(error=message), 400
            flash(message, "error")
            session["feed_form"] = {"body": body}
            return redirect(url_for("feed"), code=303)

        if not body and not uploads:
            return fail("Write something or add a photo or video.")
        if len(body) > FEED_POST_LIMIT:
            return fail(f"Posts can be at most {FEED_POST_LIMIT:,} characters.")
        if len(uploads) > FEED_MAX_FILES:
            return fail(f"Add at most {FEED_MAX_FILES} photos and videos to one post.")
        saved = []
        try:
            for upload in uploads:
                saved.append(save_feed_upload(upload))
        except ValueError as error:
            for _kind, stored, _mime in saved:
                (feed_media_dir / stored).unlink(missing_ok=True)
            return fail(str(error))
        post_id = feed_store.add_post(g.user["id"], body, saved)
        link = url_for("feed_post", post_id=post_id)
        name = g.user["username"]
        mentioned = mentioned_ids(body)
        what = snippet(body) if body else ("shared a video" if any(k == "video" for k, _s, _m in saved)
                                          else "shared a photo")
        notification_store.add(mentioned, g.user["id"], "mention", f"{name} mentioned you in a post: “{what}”",
                               link, post_id)
        everyone = [user["id"] for user in user_store.all() if user["is_active"] and user["id"] not in mentioned]
        notification_store.add(everyone, g.user["id"], "post",
                               f"{name} posted: “{what}”" if body else f"{name} {what}.", link, post_id)
        if wants_json:
            return jsonify(url=url_for("feed", _anchor=f"post-{post_id}"))
        flash("Posted.", "success")
        return feed_back(post_id)

    @app.post("/feed/posts/<int:post_id>/delete")
    def feed_delete(post_id):
        post = feed_store.post(post_id)
        if post is None:
            flash("That post was already deleted.", "error")
        elif not can_change(post["user_id"]):
            abort(403, "You can only delete your own posts.")
        else:
            feed_store.delete_post(post_id)
            flash("Post deleted.", "success")
        return redirect(url_for("feed"), code=303)

    @app.post("/feed/posts/<int:post_id>/like")
    def feed_like(post_id):
        post = feed_store.post(post_id)
        wants_json = request.headers.get("X-Requested-With") == "fetch"
        if post is None:
            if wants_json:
                return jsonify(error="That post was deleted."), 404
            flash("That post was deleted.", "error")
            return redirect(url_for("feed"), code=303)
        liked = feed_store.toggle_like(post_id, g.user["id"])
        if liked:
            notification_store.add([post["user_id"]], g.user["id"], "like",
                                   f"{g.user['username']} liked your post" + (f": “{snippet(post['body'])}”"
                                                                             if post["body"] else "."),
                                   url_for("feed_post", post_id=post_id), post_id)
        if wants_json:
            likers = feed_store.posts(g.user["id"], post_id=post_id)[0]["likers"]
            return jsonify(liked=liked, count=len(likers), likers=likers)
        return feed_back(post_id, request.form.get("single") == "1")

    @app.post("/feed/posts/<int:post_id>/comments")
    def feed_comment(post_id):
        post = feed_store.post(post_id)
        body = request.form.get("body", "").strip().replace("\r\n", "\n")
        single = request.form.get("single") == "1"
        wants_json = request.headers.get("X-Requested-With") == "fetch"
        error = None
        if post is None:
            error = "That post was deleted."
        elif not body:
            error = "Write a comment first."
        elif len(body) > FEED_COMMENT_LIMIT:
            error = f"Comments can be at most {FEED_COMMENT_LIMIT:,} characters."
        if error:
            if wants_json:
                return jsonify(error=error), 400
            flash(error, "error")
            return feed_back(post_id if post else None, single and post is not None)

        comment_id = feed_store.add_comment(post_id, g.user["id"], body)
        name, link = g.user["username"], url_for("feed_post", post_id=post_id)
        mentioned = mentioned_ids(body)
        notification_store.add(mentioned, g.user["id"], "mention",
                               f"{name} mentioned you in a comment: “{snippet(body)}”", link, post_id)
        notification_store.add([post["user_id"]] if post["user_id"] not in mentioned else [], g.user["id"],
                               "comment", f"{name} commented on your post: “{snippet(body)}”", link, post_id)
        whose = "their" if post["user_id"] == g.user["id"] else f"{post['author']}'s"
        others = [uid for uid in feed_store.commenter_ids(post_id) if uid != post["user_id"] and uid not in mentioned]
        notification_store.add(others, g.user["id"], "comment",
                               f"{name} also commented on {whose} post: “{snippet(body)}”", link, post_id)
        if wants_json:
            comment = next(c for c in feed_store.posts(g.user["id"], post_id=post_id)[0]["comments"]
                           if c["id"] == comment_id)
            return jsonify(html=render_template("_feed_comment.html", comment=comment, post=post, single=single))
        return feed_back(post_id, single)

    @app.post("/feed/comments/<int:comment_id>/delete")
    def feed_comment_delete(comment_id):
        comment = feed_store.comment(comment_id)
        if comment is None:
            flash("That comment was already deleted.", "error")
            return redirect(url_for("feed"), code=303)
        post = feed_store.post(comment["post_id"])
        # People delete their own comments; the post's author and administrators can delete any on it.
        if not (can_change(comment["user_id"]) or (post and post["user_id"] == g.user["id"])):
            abort(403, "You can only delete your own comments.")
        feed_store.delete_comment(comment_id)
        flash("Comment deleted.", "success")
        return feed_back(comment["post_id"], request.form.get("single") == "1")

    @app.get("/feed/media/<int:media_id>")
    def feed_media(media_id):
        item = feed_store.media(media_id)
        path = feed_media_dir / item["stored"] if item else None
        if not path or not path.is_file():
            abort(404)
        # conditional=True answers Range requests, so videos can seek and stream.
        response = send_file(path, mimetype=item["mime"], conditional=True, max_age=7 * 24 * 3600)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "private, max-age=604800"
        return response

    # ---------------------------------------------------------- notifications

    @app.get("/notifications")
    def notifications():
        unread_only = request.args.get("show") == "unread"
        items = notification_store.for_user(g.user["id"], unread_only=unread_only)
        return render_template("notifications.html", items=items, unread_only=unread_only)

    @app.get("/notifications/<int:notification_id>/open")
    def notification_open(notification_id):
        item = notification_store.get(g.user["id"], notification_id)
        if item is None:
            flash("That notification is gone.", "error")
            return redirect(url_for("notifications"), code=303)
        notification_store.set_read(g.user["id"], notification_id)
        return redirect(safe_next(item["link"]) if item["link"] else url_for("notifications"))

    @app.post("/notifications/<int:notification_id>/read")
    def notification_read(notification_id):
        read = request.form.get("read", "1") == "1"
        notification_store.set_read(g.user["id"], notification_id, read)
        if request.headers.get("X-Requested-With") == "fetch":
            return jsonify(unread=notification_store.unread_count(g.user["id"]))
        return redirect(url_for("notifications", show=request.form.get("show") or None), code=303)

    @app.post("/notifications/read-all")
    def notifications_read_all():
        notification_store.read_all(g.user["id"])
        if request.headers.get("X-Requested-With") == "fetch":
            return jsonify(unread=0)
        flash("All notifications are marked as read.", "success")
        return redirect(safe_next(request.form.get("next", "")) if request.form.get("next")
                        else url_for("notifications"), code=303)

    @app.post("/notifications/clear")
    def notifications_clear():
        notification_store.clear_read(g.user["id"])
        flash("Read notifications were removed.", "success")
        return redirect(url_for("notifications"), code=303)

    @app.get("/notifications/poll")
    def notifications_poll():
        """For the bell in the top bar: the unread count and anything new since the last check."""
        after = request.args.get("after", default=0, type=int)
        fresh = notification_store.for_user(g.user["id"], unread_only=True, after_id=after, limit=5) if after else []
        return jsonify(
            unread=notification_store.unread_count(g.user["id"]),
            latest=notification_store.latest_id(g.user["id"]),
            fresh=[{"id": item["id"], "message": item["message"],
                    "url": url_for("notification_open", notification_id=item["id"])} for item in fresh],
        )

    @app.get("/notes")
    def notes():
        all_notes = load_notes(g.user["id"])
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
                    sql("INSERT INTO notes (title, content, user_id) VALUES (?, ?, ?)") + returning_id,
                    (title, content, g.user["id"]),
                )
                note_id = cursor.fetchone()["id"] if returning_id else cursor.lastrowid
            invalidate_notes(g.user["id"])
            activity_log.add(g.user["id"], "notes", "created", title)
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
                        WHERE id = ? AND user_id = ?
                        """
                    ),
                    (title, content, note_id, g.user["id"]),
                )
            if cursor.rowcount == 0:
                abort(404)
            invalidate_notes(g.user["id"])
            activity_log.add(g.user["id"], "notes", "edited", title)
            flash("Note updated.", "success")
        return redirect(url_for("notes", id=note_id))

    @app.post("/notes/<int:note_id>/delete")
    def delete_note(note_id):
        with notes_connection() as connection:
            row = connection.execute(
                sql("SELECT title FROM notes WHERE id = ? AND user_id = ?"), (note_id, g.user["id"])
            ).fetchone()
            if row is None:
                abort(404)
            connection.execute(sql("DELETE FROM notes WHERE id = ?"), (note_id,))
        invalidate_notes(g.user["id"])
        activity_log.add(g.user["id"], "notes", "deleted", row["title"])
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

    def item_gone(relative):
        """The item was already deleted or renamed (a double click, Reload, or another
        device): go back to its folder and say so, instead of a bare Not Found page."""
        parent = PurePosixPath(relative).parent.as_posix() if relative else ""
        parent = "" if parent == "." else parent
        if parent and not resolve_path(parent).is_dir():
            parent = ""
        flash("That item no longer exists. It may have been deleted or renamed already.", "error")
        return redirect(url_for("index", path=parent or None), code=303)

    @app.post("/rename")
    def rename_item():
        source_relative = normalized_relative(request.form.get("path", ""))
        source = resolve_path(source_relative)
        if not source_relative or not source.exists():
            return item_gone(source_relative)
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
            return item_gone(relative)

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
        # 303 so Back or Reload shows the folder instead of sending the delete again.
        return redirect(url_for("index", path=parent if parent != "." else None), code=303)

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
        conversations = chat_store.conversations(g.user["id"])
        conversation_id = request.args.get("id", type=int)
        starting_new = request.args.get("new") == "1"
        selected = None
        if conversation_id is not None:
            selected = chat_store.conversation(conversation_id, g.user["id"])
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
        if conversation_id is None or chat_store.conversation(conversation_id, g.user["id"]) is None:
            conversation_id = chat_store.create(g.user["id"])
        user_message_id = chat_store.add(conversation_id, "user", text, "web")
        activity_log.add(g.user["id"], "chat", "sent a message", text)
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
        if chat_store.conversation(conversation_id, g.user["id"]) is None:
            abort(404)
        after_id = request.args.get("after", default=0, type=int)
        return jsonify(messages=chat_store.messages(conversation_id, after_id))

    @app.get("/chat/message/<int:message_id>/code.zip")
    def chat_code_zip(message_id):
        message = chat_store.message(message_id, g.user["id"])
        files = chat.code_files(message["content"]) if message and message["role"] == "assistant" else []
        if not files:
            abort(404)
        return send_file(
            io.BytesIO(chat.code_zip(files)),
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"chat-{message['conversation_id']}-code-{message_id}.zip",
        )

    @app.post("/chat/<int:conversation_id>/delete")
    def chat_delete(conversation_id):
        conversation = chat_store.conversation(conversation_id, g.user["id"])
        if conversation is None or not chat_store.delete(conversation_id, g.user["id"]):
            abort(404)
        activity_log.add(g.user["id"], "chat", "deleted a chat", conversation["title"])
        flash("Chat deleted.", "success")
        return redirect(url_for("chat_page"))

    def telegram_status(saved):
        """The connection state the Telegram bot last reported, for the Settings page."""
        if not chat.setting("TELEGRAM_BOT_TOKEN"):
            return {"state": "off", "message": "No bot token is set, so the bot is off."}
        try:
            status = json.loads(saved.get("TELEGRAM_STATUS") or "{}")
        except ValueError:
            status = {}
        if not status.get("state") or status["state"] == "off":
            return {"state": "pending", "message": "Waiting for the bot to connect (up to a minute after saving)."}
        if time.time() - status.get("at", 0) > 24 * 3600 and status["state"] != "running":
            status["message"] += " (last checked over a day ago)"
        return status

    def settings_view(values=None, errors=None, section=None):
        saved = settings_store.all()
        groups = []
        for group in SETTING_GROUPS:
            if section and group["section"] != section:
                continue
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
                if name == "LOCAL_AI_MODEL":
                    models = chat.parse_models((values or {}).get("LOCAL_AI_MODELS", "")) or chat.local_models()
                    chosen = (values or {}).get(name, current)
                    if chosen and chosen not in models:
                        models.append(chosen)
                    item["options"] = [(model, model) for model in models]
                if field.get("secret"):
                    # Never send secrets back to the browser; show only the last characters.
                    item["hint"] = f"…{current[-4:]}" if len(current) >= 12 else ""
                else:
                    item["value"] = (values or {}).get(name, current)
                fields.append(item)
            extra = {"status": telegram_status(saved)} if group.get("key") == "telegram" else {}
            groups.append({**group, **extra, "fields": fields})
        return groups

    def section_fields(section):
        return {field["name"]: field for group in SETTING_GROUPS if group["section"] == section
                for field in group["fields"]}

    def read_settings_form(fields):
        """Return ({name: value or None}, {name: error}) for these fields from the posted settings form."""
        saved = settings_store.all()
        changes, errors = {}, {}
        for name, field in fields.items():
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
                elif name == "LOCAL_AI_MODELS":
                    value = "\n".join(chat.parse_models(value))
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
            elif name == "LOCAL_AI_MODELS" and not all(MODEL_ID.fullmatch(model) for model in value.splitlines()):
                errors[name] = "Each line must be one model ID, such as qwen3/qwen3.6-35b."
            elif name == "LOCAL_AI_MODEL" and value not in (
                chat.parse_models(request.form.get("LOCAL_AI_MODELS", "")) or chat.DEFAULT_LOCAL_MODELS
            ):
                errors[name] = "Choose a model from the Models list."
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
            ("App version", APP_VERSION),
            ("Shared files folder", str(root)),
            ("Feed photos and videos folder", str(feed_media_dir)),
            ("Notes and chat database", "PostgreSQL" if database_url else "SQLite"),
            ("Session and cache store", "Redis" if cache is not None else "Signed cookies, no cache"),
            ("Fixed session key (FILES_SECRET_KEY)", "Set" if os.environ.get("FILES_SECRET_KEY") else "Not set"),
            ("Largest upload", "5 GB"),
        ]

    # ------------------------------------------------------- system & backups
    backup_setting = app.config.get("BACKUP_DIR") or os.environ.get("BACKUP_DIR", "")
    backup_dir = Path(backup_setting).resolve() if backup_setting else None

    def age_text(seconds):
        minutes = int(seconds // 60)
        if minutes < 1:
            return "just now"
        if minutes < 60:
            return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
        hours = minutes // 60
        if hours < 48:
            return f"{hours} hour{'s' if hours != 1 else ''} ago"
        return f"{hours // 24} days ago"

    def backup_schedule():
        """How often the backup service runs and how many dumps it keeps.

        Saved on Settings as backups/.schedule, which scripts/db-backup.sh reads before each run.
        """
        schedule = {"interval_hours": int(os.environ.get("BACKUP_INTERVAL_HOURS") or 6),
                    "keep": int(os.environ.get("BACKUP_KEEP") or 28)}
        try:
            for line in (backup_dir / ".schedule").read_text().splitlines():
                match = re.fullmatch(r"(interval_hours|keep)=(\d{1,4})", line.strip())
                if match and int(match[2]) > 0:
                    schedule[match[1]] = int(match[2])
        except (OSError, TypeError):
            pass
        return schedule

    def backup_files():
        """Dumps in the backup folder, newest first."""
        if backup_dir is None or not backup_dir.is_dir():
            return []
        now = time.time()
        files = []
        for path in backup_dir.iterdir():
            if path.is_file() and BACKUP_NAME.fullmatch(path.name):
                stat = path.stat()
                files.append({"name": path.name, "size": stat.st_size, "mtime": stat.st_mtime,
                              "age": age_text(now - stat.st_mtime),
                              "when": datetime.fromtimestamp(stat.st_mtime, timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                              "safety": path.name.startswith("pre-restore-")})
        return sorted(files, key=lambda item: item["mtime"], reverse=True)

    def run_system_checks():
        """Each check: {key, title, status (ok, warning or problem), detail}."""
        checks = []

        def add(key, title, status, detail):
            checks.append({"key": key, "title": title, "status": status, "detail": detail})

        try:
            with notes_connection() as connection:
                if database_url:
                    size = connection.execute("SELECT pg_database_size(current_database()) AS size").fetchone()["size"]
                    add("database", "Database", "ok", f"PostgreSQL is answering · {size / 1048576:.1f} MB")
                else:
                    connection.execute("SELECT 1").fetchone()
                    add("database", "Database", "ok", "SQLite is answering")
        except Exception as error:
            add("database", "Database", "problem", f"The database isn't answering: {error}")

        if cache is None:
            add("redis", "Redis", "ok", "Not used on this server")
        else:
            try:
                memory = cache.info("memory")
                used, limit = memory.get("used_memory", 0), memory.get("maxmemory", 0)
                if limit and used / limit >= 0.9:
                    add("redis", "Redis", "warning", f"Memory {used / limit:.0%} full; the oldest sessions will be dropped")
                else:
                    add("redis", "Redis", "ok", f"Answering · {used / 1048576:.1f} MB used"
                        + (f" of {limit / 1048576:.0f} MB" if limit else ""))
            except cache_errors as error:
                add("redis", "Redis", "problem", f"Redis isn't answering: {error}")

        usage = shutil.disk_usage(root)
        free_gb, free_share = usage.free / 1024 ** 3, usage.free / usage.total
        detail = f"{free_gb:.1f} GB free of {usage.total / 1024 ** 3:.0f} GB ({free_share:.0%})"
        if free_gb < 1 or free_share < 0.03:
            add("disk", "Disk space", "problem", f"Almost full: {detail}")
        elif free_gb < 5 or free_share < 0.10:
            add("disk", "Disk space", "warning", f"Getting full: {detail}")
        else:
            add("disk", "Disk space", "ok", detail)

        if backup_dir is None:
            add("backups", "Backups", "warning", "Backups aren't set up on this server (BACKUP_DIR).")
        else:
            dumps = [item for item in backup_files() if not item["safety"]]
            interval = backup_schedule()["interval_hours"] * 3600
            error_file = backup_dir / ".last-error"
            failed = error_file.is_file() and (not dumps or error_file.stat().st_mtime > dumps[0]["mtime"])
            if failed:
                add("backups", "Backups", "problem",
                    "The last backup failed: " + error_file.read_text(errors="replace").strip()[:300])
            elif not dumps:
                add("backups", "Backups", "problem", "There are no backups yet. Is the backup service running?")
            else:
                age = time.time() - dumps[0]["mtime"]
                detail = f"Last backup {dumps[0]['age']} · {len(dumps)} kept"
                if age > 2 * interval + 3600:
                    add("backups", "Backups", "problem", f"{detail}. The backup service may have stopped.")
                elif age > interval + 3600:
                    add("backups", "Backups", "warning", f"{detail}. The next one is late.")
                else:
                    add("backups", "Backups", "ok", detail)
        return checks

    def check_and_alert():
        """Runs the checks and tells administrators about each one that changed since the last run."""
        checks = run_system_checks()
        activity_log.prune()  # The check runs regularly, so old activity is cleared here too.
        convert_log.prune()
        try:
            previous = json.loads(settings_store.get(SYSTEM_CHECK_STATE) or "{}").get("status", {})
        except ValueError:
            previous = {}
        admins = user_store.admin_ids()
        with app.test_request_context():
            link = url_for("settings_section", section="system")
            for check in checks:
                before = previous.get(check["key"], "ok")
                if check["status"] == before:
                    continue
                if check["status"] == "ok":
                    message = f"System check: {check['title']} is OK again."
                else:
                    message = f"System check · {SYSTEM_STATUSES[check['status']]}: {check['title']}. {check['detail']}"
                notification_store.add(admins, None, "system", message, link)
        settings_store.save({SYSTEM_CHECK_STATE: json.dumps({
            "status": {check["key"]: check["status"] for check in checks},
            "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        })})
        return checks

    def last_automatic_check():
        try:
            return json.loads(settings_store.get(SYSTEM_CHECK_STATE) or "{}").get("checked_at")
        except ValueError:
            return None

    @app.cli.command("system-check")
    def system_check_command():
        """Runs the system checks once and alerts administrators about changes."""
        for check in check_and_alert():
            print(f"{SYSTEM_STATUSES[check['status']]:8} {check['title']}: {check['detail']}")

    # Only the web app checks automatically (SYSTEM_CHECK_MINUTES in docker-compose.yml),
    # not the Telegram bot, which also builds the app.
    check_minutes = int(app.config.get("SYSTEM_CHECK_MINUTES") or os.environ.get("SYSTEM_CHECK_MINUTES") or 0)
    if check_minutes > 0 and not app.config.get("TESTING"):
        def check_loop():
            time.sleep(60)  # Let the database and Redis settle after a start.
            while True:
                try:
                    check_and_alert()
                except Exception:
                    app.logger.exception("System check failed")
                time.sleep(check_minutes * 60)

        threading.Thread(target=check_loop, name="system-check", daemon=True).start()

    def system_summary():
        checks = run_system_checks()
        bad = [check for check in checks if check["status"] != "ok"]
        if bad:
            return f"{len(bad)} need{'s' if len(bad) == 1 else ''} attention: " + ", ".join(c["title"] for c in bad)
        dumps = [item for item in backup_files() if not item["safety"]]
        return "All checks OK" + (f" · last backup {dumps[0]['age']}" if dumps else "")

    def settings_summary():
        """One line per category for the Settings overview."""
        users = user_store.all()
        waiting = sum(1 for user in users if not user["is_active"])
        prefs = user_prefs(g.user)
        summary = {
            "account": "Profile photo set" if g.user["avatar"] else "No profile photo yet",
            "notifications": (f"{len(NOTIFY_KINDS) - len(prefs['mute'])} of {len(NOTIFY_KINDS)} kinds on"
                              + ("" if prefs["popups"] else " · pop-ups off")),
        }
        if g.user["is_admin"]:
            summary.update({
                "users": f"{len(users)} account{'s' if len(users) != 1 else ''}"
                         + (f" · {waiting} waiting for approval" if waiting else ""),
                "ai": f"{'Local AI' if chat.provider() == 'local' else 'Claude'} · {chat.assistant_name()}"
                      if chat.is_configured() else "Not set up yet",
                "telegram": {"running": "Connected", "off": "Off", "pending": "Connecting"}.get(
                    telegram_status(settings_store.all())["state"], "Not connected"),
                "server": f"Version {APP_VERSION}",
                "system": system_summary(),
                "activity": f"{activity_log.count_since(24)} in the last 24 hours",
            })
        return summary, waiting

    def render_settings(section, status=200, **context):
        sections = [item for item in SETTINGS_SECTIONS if g.user["is_admin"] or not item["admin"]]
        summary, waiting = settings_summary()
        return render_template(
            "settings.html", section=SECTION_KEYS.get(section), sections=sections, summary=summary,
            waiting=waiting, **context,
        ), status

    def section_context(section, values=None, errors=None):
        if section in ("ai", "telegram"):
            return {"groups": settings_view(values, errors, section)}
        if section == "users":
            return {"users": user_store.all(), "signup_mode": signup_mode(), "signup_modes": SIGNUP_MODES,
                    "post_counts": feed_store.post_counts()}
        if section == "server":
            return {"server": server_info()}
        if section == "activity":
            filters = {"username": request.args.get("user", "")[:60], "area": request.args.get("area", ""),
                       "query": request.args.get("q", "").strip()[:100]}
            if filters["area"] not in ACTIVITY_AREAS:
                filters["area"] = ""
            page_size = 100
            entries = activity_log.search(**filters, before_id=request.args.get("before", type=int),
                                          limit=page_size + 1)
            return {"entries": entries[:page_size], "more": len(entries) > page_size, "filters": filters,
                    "areas": ACTIVITY_AREAS, "people": activity_log.usernames(), "keep_days": ACTIVITY_KEEP_DAYS}
        if section == "system":
            return {"checks": run_system_checks(), "statuses": SYSTEM_STATUSES, "backups": backup_files(),
                    "schedule": backup_schedule(), "backup_ready": backup_dir is not None,
                    "backup_pending": backup_dir is not None and (backup_dir / ".backup-now").exists(),
                    "last_check": last_automatic_check(), "check_minutes": check_minutes}
        if section == "notifications":
            return {"prefs": user_prefs(g.user), "kinds": NOTIFY_KINDS}
        if section == "account":
            return {"stats": feed_store.stats(g.user["id"])}
        return {}

    @app.get("/settings")
    def settings_page():
        """The overview: one card per category. On phones it is the menu."""
        return render_settings(None)

    @app.get("/settings/<section>")
    def settings_section(section):
        if section not in SECTION_KEYS:
            abort(404)
        if SECTION_KEYS[section]["admin"]:
            admin_required()
        return render_settings(section, **section_context(section))

    @app.post("/settings/<section>")
    def save_settings(section):
        if section not in ("ai", "telegram"):
            abort(404)
        admin_required()
        fields = section_fields(section)
        changes, errors = read_settings_form(fields)
        if errors:
            flash("Nothing was saved. Fix the highlighted settings and try again.", "error")
            values = {name: request.form.get(name, "") for name, field in fields.items() if not field.get("secret")}
            return render_settings(section, 400, **section_context(section, values, errors))
        settings_store.save(changes)
        flash("Settings saved.", "success")
        return redirect(url_for("settings_section", section=section), code=303)

    @app.post("/settings/system/check")
    def system_check_now():
        admin_required()
        checks = check_and_alert()
        bad = sum(1 for check in checks if check["status"] != "ok")
        flash("Everything checked: all OK." if not bad else f"Checked: {bad} need attention.",
              "success" if not bad else "error")
        return redirect(url_for("settings_section", section="system"), code=303)

    @app.post("/settings/system/backup")
    def backup_now():
        """Asks the backup service for a backup; it looks for this file every 30 seconds."""
        admin_required()
        if backup_dir is None:
            abort(404)
        (backup_dir / ".backup-now").write_text(datetime.now(timezone.utc).isoformat())
        flash("Backup started. It appears in the list within a minute; reload to see it.", "success")
        return redirect(url_for("settings_section", section="system", _anchor="backups"), code=303)

    @app.post("/settings/system/schedule")
    def save_backup_schedule():
        admin_required()
        if backup_dir is None:
            abort(404)
        try:
            interval = int(request.form.get("interval_hours", ""))
            keep = int(request.form.get("keep", ""))
        except ValueError:
            interval = keep = 0
        if not (1 <= interval <= 168 and 1 <= keep <= 500):
            flash("Back up every 1 to 168 hours, and keep 1 to 500 backups.", "error")
        else:
            (backup_dir / ".schedule").write_text(f"interval_hours={interval}\nkeep={keep}\n")
            flash(f"Saved: a backup every {interval} hour{'s' if interval != 1 else ''}, keeping the newest {keep}.",
                  "success")
        return redirect(url_for("settings_section", section="system", _anchor="schedule"), code=303)

    @app.get("/settings/system/backups/<name>")
    def download_backup(name):
        admin_required()
        if backup_dir is None or not BACKUP_NAME.fullmatch(name) or not (backup_dir / name).is_file():
            abort(404)
        return send_file(backup_dir / name, mimetype="application/octet-stream", as_attachment=True,
                         download_name=name)

    @app.post("/settings/notifications")
    def save_notification_prefs():
        prefs = {"mute": [kind for kind in NOTIFY_KINDS if not request.form.get(f"kind_{kind}")],
                 "popups": bool(request.form.get("popups"))}
        user_store.set_prefs(g.user["id"], prefs)
        flash("Notification choices saved.", "success")
        return redirect(url_for("settings_section", section="notifications"), code=303)

    @app.post("/settings/account/profile")
    def save_profile():
        bio = " ".join(request.form.get("bio", "").split())
        if len(bio) > 160:
            flash("Keep the few words about you under 160 characters.", "error")
            return redirect(url_for("settings_section", section="account"), code=303)
        avatar = g.user["avatar"]
        upload = request.files.get("avatar")
        if upload and upload.filename:
            try:
                new_avatar = save_avatar(upload)
            except ValueError as error:
                flash(str(error), "error")
                return redirect(url_for("settings_section", section="account"), code=303)
            remove_avatar(avatar)
            avatar = new_avatar
        elif request.form.get("remove_avatar"):
            remove_avatar(avatar)
            avatar = ""
        user_store.set_profile(g.user["id"], bio, avatar)
        flash("Profile saved.", "success")
        return redirect(url_for("settings_section", section="account"), code=303)

    def convert_log_context():
        everyone = bool(g.user["is_admin"])
        return {"log_entries": convert_log.entries(None if everyone else g.user["id"]),
                "log_everyone": everyone, "tool_names": CONVERT_TOOL_NAMES}

    @app.get("/convert")
    def convert_page():
        return render_template("convert.html", **convert_log_context())

    @app.get("/convert/log")
    def convert_log_list():
        # The Convert page refreshes its log with this after each conversion.
        return render_template("_convert_log.html", **convert_log_context())

    @app.post("/convert/log/<int:entry_id>/delete")
    def convert_log_delete(entry_id):
        owner = convert_log.owner(entry_id)
        if owner is None or not (owner == g.user["id"] or g.user["is_admin"]):
            abort(404)
        convert_log.delete(entry_id)
        flash("Removed from the conversion log.", "success")
        return redirect(url_for("convert_page", _anchor="log"), code=303)

    @app.post("/convert/log/clear")
    def convert_log_clear():
        # Administrators can clear everyone's log; everyone else clears only their own.
        everyone = g.user["is_admin"] and request.form.get("scope") == "all"
        convert_log.clear(None if everyone else g.user["id"])
        flash("Conversion log cleared.", "success")
        return redirect(url_for("convert_page", _anchor="log"), code=303)

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
        file_names = [download_stem(upload.filename) + Path(upload.filename).suffix.lower() for upload in uploads]
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
                convert_log.add(g.user["id"], tool, file_names, str(error), ok=False)
                return fail(str(error))

            output = tempfile.TemporaryFile()
            with result.open("rb") as handle:
                shutil.copyfileobj(handle, output)
            output.seek(0)
        convert_log.add(g.user["id"], tool, file_names, download_name + (f". {note}" if note else ""))

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