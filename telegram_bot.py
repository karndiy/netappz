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

    def send(self, chat_id, text):
        for start in range(0, len(text), TELEGRAM_LIMIT):
            self.call("sendMessage", chat_id=chat_id, text=text[start:start + TELEGRAM_LIMIT])


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
        store.add(conversation_id, "assistant", reply, "telegram")
        telegram.send(chat_id, reply)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Creating the app connects to the database and makes chat.setting() read saved settings.
    store = create_app().extensions["chat"]
    telegram = None
    offset = None
    while True:
        token = chat.setting("TELEGRAM_BOT_TOKEN")
        if telegram is None or telegram.token != token:
            telegram, offset = None, None
            if not token:
                log.info("No Telegram bot token is set; the bot is off. Checking again in 30 s.")
                time.sleep(30)
                continue
            try:
                me = Telegram(token).call("getMe")
            except (urllib.error.URLError, TimeoutError, OSError, RuntimeError) as error:
                log.warning("Telegram did not accept the bot token (%s); retrying in 30 s", error)
                time.sleep(30)
                continue
            telegram = Telegram(token)
            log.info("Telegram bot @%s is running", me.get("username"))
            if not allowed_users():
                log.warning("No allowed Telegram users yet: the bot will refuse everyone and show their user ID.")

        try:
            params = {"timeout": 50, "allowed_updates": ["message"]}
            if offset is not None:
                params["offset"] = offset
            updates = telegram.call("getUpdates", http_timeout=60, **params)
        except (urllib.error.URLError, TimeoutError, OSError, RuntimeError) as error:
            log.warning("Telegram polling failed (%s); retrying in 5 s", error)
            time.sleep(5)
            continue
        allowed = allowed_users()
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
