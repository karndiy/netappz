"""Claude chat shared by the web chat page and the Telegram bot."""

import os

import anthropic

DEFAULT_MODEL = "claude-opus-5-5"
# Claude Opus 5.5 defaults to "medium"; set it explicitly so it is easy to tune.
DEFAULT_EFFORT = "medium"
MAX_TOKENS = 64000

SYSTEM_PROMPT = """You are Claude, a helpful assistant inside a private home and office app called Network Files. \
The person you are talking with owns this app and uses it to share files, keep notes, and convert documents on their network.

They talk to you from two places that share the same conversation history: a chat page in the app, and a Telegram bot \
on their phone. Reply in the language they write in (often Thai or English). Be friendly, direct, and concise, \
and go into detail when the question needs it.

You cannot see or change the files, notes, or conversions in the app; if asked, explain how they can do it themselves."""

TELEGRAM_FORMAT_NOTE = (
    "This reply will be shown in Telegram as plain text. Do not use Markdown formatting "
    "such as headings, bold, tables, or code fences; use short paragraphs and simple dashes for lists."
)


class ChatError(Exception):
    pass


# Values saved on the Settings page; the app installs a lookup that reads them.
_saved_setting = None


def use_settings(lookup):
    global _saved_setting
    _saved_setting = lookup


def setting(name, default=""):
    """A saved setting, else the environment variable, else the default."""
    value = _saved_setting(name) if _saved_setting else None
    if value is None:
        value = os.environ.get(name, "")
    return value.strip() or default


def is_configured():
    return bool(setting("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


_clients = {}


def client():
    # One client per key, so a key changed on the Settings page takes effect at once.
    api_key = setting("ANTHROPIC_API_KEY")
    if api_key not in _clients:
        _clients.clear()
        _clients[api_key] = anthropic.Anthropic(api_key=api_key or None)
    return _clients[api_key]


def system_prompt():
    extra = setting("CHAT_INSTRUCTIONS")
    return f"{SYSTEM_PROMPT}\n\nThe owner's instructions for you:\n{extra}" if extra else SYSTEM_PROMPT


def to_api_messages(history, for_telegram=False):
    messages = [{"role": item["role"], "content": item["content"]} for item in history]
    if for_telegram and messages and messages[-1]["role"] == "user":
        # A mid-conversation system message keeps the cached history prefix intact.
        messages.append({"role": "system", "content": TELEGRAM_FORMAT_NOTE})
    return messages


def stream_reply(history, for_telegram=False):
    """Yield {"type": "text"} chunks as the reply arrives, or {"type": "replace"} when the
    partial text must be swapped (a refusal). The generator's return value is the full reply.

    Raises ChatError with a readable message when the reply cannot be produced.
    """
    if not is_configured():
        raise ChatError("Chat is not set up yet: add an Anthropic API key on the Settings page.")
    parts = []
    try:
        with client().beta.messages.stream(
            model=setting("CHAT_MODEL", DEFAULT_MODEL),
            max_tokens=MAX_TOKENS,
            system=system_prompt(),
            messages=to_api_messages(history, for_telegram),
            output_config={"effort": setting("CHAT_EFFORT", DEFAULT_EFFORT)},
            cache_control={"type": "ephemeral"},
            # If a safety classifier declines, retry on Anthropic's recommended model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            for event in stream:
                if event.type == "text":
                    parts.append(event.text)
                    yield {"type": "text", "text": event.text}
            message = stream.get_final_message()
    except anthropic.AuthenticationError:
        raise ChatError("The Anthropic API key was rejected. Check it on the Settings page.")
    except anthropic.PermissionDeniedError:
        raise ChatError("This API key is not allowed to use the chat model.")
    except anthropic.RateLimitError:
        raise ChatError("Claude is busy right now (rate limit). Try again in a minute.")
    except anthropic.BadRequestError as error:
        raise ChatError(f"Claude could not accept this request: {error.message}")
    except anthropic.APIStatusError as error:
        if error.status_code >= 500:
            raise ChatError("Claude is having trouble right now. Try again shortly.")
        raise ChatError(f"Claude returned an error: {error.message}")
    except anthropic.APIConnectionError:
        raise ChatError("Could not reach Claude. Check the server's internet connection.")

    if message.stop_reason == "refusal":
        notice = "Sorry, I can't help with that request."
        parts = [notice]
        yield {"type": "replace", "text": notice}
    elif message.stop_reason == "max_tokens":
        notice = "(The reply was cut off because it reached the length limit.)"
        parts.append("\n\n" + notice)
        yield {"type": "text", "text": "\n\n" + notice}
    return "".join(parts).strip()


def complete_reply(history, for_telegram=False):
    generator = stream_reply(history, for_telegram)
    while True:
        try:
            next(generator)
        except StopIteration as done:
            return done.value
