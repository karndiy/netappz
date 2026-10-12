"""AI chat shared by the web chat page and the Telegram bot.

Replies come from Claude (Anthropic) or from a local, OpenAI-compatible server such as
Open WebUI, vLLM, or Ollama, picked on the Settings page.
"""

import json
import os
import urllib.error
import urllib.request

import anthropic

DEFAULT_PROVIDER = "anthropic"
DEFAULT_LOCAL_URL = "https://ai.esm-solution.com/api/v1"
DEFAULT_LOCAL_MODEL = "qwen3/qwen3.6-35b"
LOCAL_TIMEOUT = 300
DEFAULT_MODEL = "claude-opus-5-5"
# Claude Opus 5.5 defaults to "medium"; set it explicitly so it is easy to tune.
DEFAULT_EFFORT = "medium"
MAX_TOKENS = 64000

SYSTEM_PROMPT = """You are a helpful assistant inside a private home and office app called Network Files. \
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


def provider():
    return "local" if setting("CHAT_PROVIDER", DEFAULT_PROVIDER) == "local" else "anthropic"


def is_configured():
    if provider() == "local":
        return bool(setting("LOCAL_AI_URL", DEFAULT_LOCAL_URL))
    return bool(setting("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def assistant_name():
    if provider() == "local":
        return setting("LOCAL_AI_MODEL", DEFAULT_LOCAL_MODEL)
    return "Claude"


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
    if provider() == "local":
        return (yield from stream_local_reply(history, for_telegram))
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


def local_messages(history, for_telegram=False):
    """OpenAI-style messages: the system prompt first, then the conversation."""
    instructions = system_prompt()
    if for_telegram:
        instructions += "\n\n" + TELEGRAM_FORMAT_NOTE
    return [{"role": "system", "content": instructions}] + [
        {"role": item["role"], "content": item["content"]} for item in history
    ]


class ThinkFilter:
    """Drops <think>...</think> reasoning that models such as Qwen3 put in their reply text."""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self):
        self.buffer = ""
        self.thinking = False
        self.started = False

    def feed(self, text):
        self.buffer += text
        out = []
        while self.buffer:
            if self.thinking:
                end = self.buffer.find(self.CLOSE)
                if end < 0:
                    # Keep a possible partial closing tag for the next chunk.
                    self.buffer = self.buffer[-(len(self.CLOSE) - 1):]
                    break
                self.buffer = self.buffer[end + len(self.CLOSE):].lstrip()
                self.thinking = False
                continue
            start = self.buffer.find(self.OPEN)
            if start >= 0:
                out.append(self.buffer[:start])
                self.buffer = self.buffer[start + len(self.OPEN):]
                self.thinking = True
                continue
            keep = next((n for n in range(len(self.OPEN) - 1, 0, -1) if self.buffer.endswith(self.OPEN[:n])), 0)
            out.append(self.buffer[: len(self.buffer) - keep])
            self.buffer = self.buffer[len(self.buffer) - keep:]
            break
        text = "".join(out)
        if not self.started:
            text = text.lstrip()
            self.started = bool(text)
        return text

    def flush(self):
        text, self.buffer = ("" if self.thinking else self.buffer), ""
        return text


def stream_local_reply(history, for_telegram=False):
    """Stream a reply from an OpenAI-compatible /chat/completions endpoint."""
    base_url = setting("LOCAL_AI_URL", DEFAULT_LOCAL_URL).rstrip("/")
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    api_key = setting("LOCAL_AI_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = json.dumps({
        "model": setting("LOCAL_AI_MODEL", DEFAULT_LOCAL_MODEL),
        "messages": local_messages(history, for_telegram),
        "stream": True,
    }).encode()
    request = urllib.request.Request(f"{base_url}/chat/completions", data=body, headers=headers, method="POST")

    parts, finish_reason = [], None
    think = ThinkFilter()
    try:
        with urllib.request.urlopen(request, timeout=LOCAL_TIMEOUT) as response:
            content_type = response.headers.get("Content-Type", "")
            if "text/event-stream" not in content_type:
                # Some servers ignore "stream" and send one JSON reply.
                payload = json.loads(response.read() or b"{}")
                choice = (payload.get("choices") or [{}])[0]
                text = think.feed((choice.get("message") or {}).get("content") or "") + think.flush()
                finish_reason = choice.get("finish_reason")
                if text:
                    parts.append(text)
                    yield {"type": "text", "text": text}
            else:
                for raw in response:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        event = json.loads(data)
                    except ValueError:
                        continue
                    if event.get("error"):
                        raise ChatError(f"The local AI returned an error: {local_error_text(event)}")
                    for choice in event.get("choices") or []:
                        text = think.feed((choice.get("delta") or {}).get("content") or "")
                        if text:
                            parts.append(text)
                            yield {"type": "text", "text": text}
                        finish_reason = choice.get("finish_reason") or finish_reason
                tail = think.flush()
                if tail:
                    parts.append(tail)
                    yield {"type": "text", "text": tail}
    except urllib.error.HTTPError as error:
        try:
            detail = local_error_text(json.loads(error.read() or b"{}"))
        except ValueError:
            detail = error.reason
        if error.code in (401, 403):
            raise ChatError("The local AI server rejected the API key. Check it on the Settings page.")
        if error.code == 404:
            raise ChatError(f"The local AI server could not find this model or address ({detail}). Check the Settings page.")
        raise ChatError(f"The local AI server returned an error ({error.code}): {detail}")
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        reason = getattr(error, "reason", error)
        raise ChatError(f"Could not reach the local AI server at {base_url} ({reason}).")

    if finish_reason == "length":
        notice = "(The reply was cut off because it reached the length limit.)"
        parts.append("\n\n" + notice)
        yield {"type": "text", "text": "\n\n" + notice}
    reply = "".join(parts).strip()
    if not reply:
        raise ChatError("The local AI server sent an empty reply.")
    return reply


def local_error_text(payload):
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return error.get("message") or str(error)
    return str(error or (payload.get("detail") if isinstance(payload, dict) else payload) or "unknown error")


def complete_reply(history, for_telegram=False):
    generator = stream_reply(history, for_telegram)
    while True:
        try:
            next(generator)
        except StopIteration as done:
            return done.value
