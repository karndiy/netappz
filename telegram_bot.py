"""Telegram bot that lets allowed users chat with the AI from anywhere.

It uses long polling: the bot connects out to Telegram, so the server needs no
public address or open ports. Conversations are shared with the web chat page.

Settings (on the app's Settings page, or as environment variables):
  TELEGRAM_BOT_TOKEN      token from @BotFather
  TELEGRAM_ALLOWED_USERS  comma-separated Telegram user IDs allowed to use the bot

Settings are re-read while the bot runs, so changes apply without a restart.
"""

import json
import logging
import secrets
import threading
import time
import urllib.error
import urllib.request

import chat
from app import create_app

API = "https://api.telegram.org/bot{token}/{method}"
TELEGRAM_LIMIT = 4096

log = logging.getLogger("telegram_bot")


class Telegram:
    def __init__(self, token):
        self.token = token

    def call(self, method, http_timeout=30, **params):
        request = urllib.request.Request(
            API.format(token=self.token, method=method),
            data=json.dumps(params).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=http_timeout) as response:
            payload = json.load(response)
        if not payload.get("ok"):
            raise RuntimeError(payload.get("description", "Telegram API error"))
        return payload["result"]

    def send_document(self, chat_id, filename, data, caption=""):
        """Upload a file (multipart/form-data, as Telegram requires for uploads)."""
        boundary = secrets.token_hex(16)
        fields = [("chat_id", str(chat_id))] + ([("caption", caption)] if caption else [])
        body = b"".join(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
            for name, value in fields
        )
        body += (
            f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{filename}"\r\n'
            "Content-Type: application/zip\r\n\r\n"
        ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
        request = urllib.request.Request(
            API.format(token=self.token, method="sendDocument"),
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.load(response)
        if not payload.get("ok"):
            raise RuntimeError(payload.get("description", "Telegram API error"))
        return payload["result"]

    def send(self, chat_id, text):
        for start in range(0, len(text), TELEGRAM_LIMIT):
            self.call("sendMessage", chat_id=chat_id, text=text[start:start + TELEGRAM_LIMIT])


STATUS_KEY = "TELEGRAM_STATUS"


class StatusReporter:
    """Saves the bot's connection state so the Settings page can show it."""

    def __init__(self, settings):
        self.settings = settings
        self.last = None

    def __call__(self, state, message):
        if (state, message) == self.last:
            return
        self.last = (state, message)
        try:
            self.settings.save({STATUS_KEY: json.dumps({"state": state, "message": message, "at": int(time.time())})})
        except Exception:
            log.exception("Could not save the Telegram bot status")


def polling_problem(error):
    if isinstance(error, urllib.error.HTTPError) and error.code == 409:
        return "conflict", (
            "Another program is reading this bot's messages with the same token (for example the Claude Code "
            "Telegram plugin, or a second copy of this app). Stop it, or give this app its own bot from @BotFather."
        )
    if isinstance(error, urllib.error.HTTPError) and error.code == 401:
        return "error", "Telegram no longer accepts this bot token. Get a new one from @BotFather."
    return "error", f"Can't reach Telegram right now ({error}). Retrying."


def allowed_users():
    raw = chat.setting("TELEGRAM_ALLOWED_USERS")
    return {int(part) for part in raw.replace(" ", "").split(",") if part.lstrip("-").isdigit()}


def keep_typing(telegram, chat_id, stop):
    # Telegram shows "typing…" for about 5 seconds per call.
    while not stop.is_set():
        try:
            telegram.call("sendChatAction", chat_id=chat_id, action="typing", http_timeout=10)
        except Exception:
            pass
        stop.wait(4)


def handle(telegram, store, message, allowed):
    chat_id = message["chat"]["id"]
    user_id = message.get("from", {}).get("id")
    text = (message.get("text") or "").strip()

    if user_id not in allowed:
        telegram.send(
            chat_id,
            "This bot is private.\n\nYour Telegram user ID is "
            f"{user_id}. To allow it, add it to TELEGRAM_ALLOWED_USERS on the server.",
        )
        log.info("Refused message from Telegram user %s", user_id)
        return
    if not text:
        telegram.send(chat_id, "I can read text messages only for now.")
        return

    command = text.split()[0].split("@")[0].lower() if text.startswith("/") else None
    if command == "/start":
        telegram.send(
            chat_id,
            "Hi! Send me a message to chat with the AI.\n\n"
            "This chat is shared with the Chat page in Network Files.\n"
            "/new starts a new conversation.",
        )
        return
    if command == "/new":
        store.create()
        telegram.send(chat_id, "Started a new conversation.")
        return

    conversation_id = store.latest_id() or store.create()
    store.add(conversation_id, "user", text, "telegram")
    history = store.messages(conversation_id)

    stop = threading.Event()
    typing = threading.Thread(target=keep_typing, args=(telegram, chat_id, stop), daemon=True)
    typing.start()
    try:
        reply = chat.complete_reply(history, for_telegram=True)
    except chat.ChatError as error:
        reply = None
        telegram.send(chat_id, str(error))
    finally:
        stop.set()
    if reply:
        message_id = store.add(conversation_id, "assistant", reply, "telegram")
        telegram.send(chat_id, reply)
        send_code_zip(telegram, chat_id, reply, f"chat-{conversation_id}-code-{message_id}.zip")


def send_code_zip(telegram, chat_id, reply, filename):
    files = chat.code_files(reply)
    if not files:
        return
    names = ", ".join(name for name, _ in files[:10]) + (" …" if len(files) > 10 else "")
    try:
        telegram.send_document(chat_id, filename, chat.code_zip(files), caption=f"Code files: {names}"[:1024])
    except Exception:
        log.exception("Could not send the code zip to Telegram")
        telegram.send(chat_id, "(Could not attach the code as a zip file. Open the Chat page in Network Files to download it.)")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Creating the app connects to the database and makes chat.setting() read saved settings.
    app = create_app()
    store = app.extensions["chat"]
    report = StatusReporter(app.extensions["settings"])
    telegram = None
    offset = None
    while True:
        token = chat.setting("TELEGRAM_BOT_TOKEN")
        if telegram is None or telegram.token != token:
            telegram, offset = None, None
            if not token:
                report("off", "No bot token is set, so the bot is off.")
                log.info("No Telegram bot token is set; the bot is off. Checking again in 30 s.")
                time.sleep(30)
                continue
            try:
                me = Telegram(token).call("getMe")
            except (urllib.error.URLError, TimeoutError, OSError, RuntimeError) as error:
                report("error", f"Telegram did not accept the bot token ({error}).")
                log.warning("Telegram did not accept the bot token (%s); retrying in 30 s", error)
                time.sleep(30)
                continue
            telegram = Telegram(token)
            username = me.get("username", "")
            log.info("Telegram bot @%s is running", username)
            if not allowed_users():
                log.warning("No allowed Telegram users yet: the bot will refuse everyone and show their user ID.")

        try:
            params = {"timeout": 50, "allowed_updates": ["message"]}
            if offset is not None:
                params["offset"] = offset
            updates = telegram.call("getUpdates", http_timeout=60, **params)
        except (urllib.error.URLError, TimeoutError, OSError, RuntimeError) as error:
            report(*polling_problem(error))
            log.warning("Telegram polling failed (%s); retrying in 5 s", error)
            time.sleep(5)
            continue
        allowed = allowed_users()
        if allowed:
            report("running", f"Connected as @{username}. Message the bot in Telegram to chat with the AI.")
        else:
            report("running", f"Connected as @{username}, but no allowed user IDs are set yet: message the bot to get your ID.")
        for update in updates:
            offset = update["update_id"] + 1
            message = update.get("message")
            if not message:
                continue
            try:
                handle(telegram, store, message, allowed)
            except Exception:
                log.exception("Could not handle Telegram message")


if __name__ == "__main__":
    main()
