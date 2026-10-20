"""AI chat shared by the web chat page and the Telegram bot.

Replies come from Claude (Anthropic) or from a local, OpenAI-compatible server such as
Open WebUI, vLLM, or Ollama, picked on the Settings page.
"""

import io
import json
import os
import posixpath
import re
import time
import urllib.error
import urllib.request

import anthropic

DEFAULT_PROVIDER = "local"
DEFAULT_LOCAL_URL = "https://ai.esm-solution.com/api/v1"
DEFAULT_LOCAL_MODELS = ["qwen3/qwen3.6-35b", "qwen3/qwen3.8-27b"]
DEFAULT_LOCAL_MODEL = DEFAULT_LOCAL_MODELS[0]
LOCAL_TIMEOUT = 300
# Cloudflare blocks Python's default "Python-urllib" user agent (error 1010).
USER_AGENT = "NetworkFiles/1.20 (+https://github.com/karndiy/netappz)"
DEFAULT_MODEL = "claude-opus-5-5"
# Claude Opus 5.5 defaults to "medium"; set it explicitly so it is easy to tune.
DEFAULT_EFFORT = "medium"
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
MAX_TOKENS = 64000
# Models that take the server-side refusal fallback; others (Haiku, older models) reject it.
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
TEST_MESSAGE = "This is a connection test from the Network Files settings page. Reply with one short sentence."

SYSTEM_PROMPT = """You are a helpful assistant inside a private home and office app called Network Files. \
The person you are talking with owns this app and uses it to share files, keep notes, and convert documents on their network.

They talk to you from two places that share the same conversation history: a chat page in the app, and a Telegram bot \
on their phone. Reply in the language they write in (often Thai or English). Be friendly, direct, and concise, \
and go into detail when the question needs it.

You cannot see or change the files, notes, or conversions in the app; if asked, explain how they can do it themselves.

When you write code, put each file in its own fenced code block and write the file name after the language \
on the opening fence, for example ```python app.py or ```html templates/index.html. The app offers these blocks \
as a zip file to download, using those names."""

TELEGRAM_FORMAT_NOTE = (
    "This reply will be shown in Telegram as plain text. Do not use Markdown formatting "
    "such as headings, bold, or tables; use short paragraphs and simple dashes for lists. "
    "Code is the exception: keep code in fenced blocks with file names, because it is also sent as a zip file."
)

# Code blocks in replies become files in a downloadable zip.
CODE_BLOCK = re.compile(r"^```[ \t]*([^\n`]*)\n(.*?)^```[ \t]*$", re.S | re.M)
FILE_NAME = re.compile(r"^[\w.@+-]+(/[\w.@+-]+)*$")
NAME_COMMENT = re.compile(r"^\s*(?:#|//|--|;|/\*|<!--)\s*(?:file(?:name)?\s*:\s*)?([\w./@+-]+\.\w+)\s*(?:\*/|-->)?\s*$", re.I)
EXTENSIONS = {
    "python": "py", "py": "py", "javascript": "js", "js": "js", "jsx": "jsx", "typescript": "ts", "ts": "ts",
    "tsx": "tsx", "html": "html", "css": "css", "scss": "scss", "json": "json", "yaml": "yml", "yml": "yml",
    "toml": "toml", "ini": "ini", "xml": "xml", "sql": "sql", "bash": "sh", "sh": "sh", "shell": "sh",
    "zsh": "sh", "powershell": "ps1", "ps1": "ps1", "bat": "bat", "c": "c", "cpp": "cpp", "c++": "cpp",
    "h": "h", "csharp": "cs", "cs": "cs", "java": "java", "kotlin": "kt", "go": "go", "rust": "rs",
    "php": "php", "ruby": "rb", "swift": "swift", "dart": "dart", "lua": "lua", "r": "r", "vue": "vue",
    "markdown": "md", "md": "md", "dockerfile": "Dockerfile", "makefile": "Makefile", "text": "txt", "txt": "txt",
}


def safe_file_name(name):
    """A relative path that stays inside the zip, or "" when the name can't be used."""
    name = posixpath.normpath(name.strip().strip("'\"").replace("\\", "/")).lstrip("/")
    if not name or name.startswith("..") or not FILE_NAME.fullmatch(name) or len(name) > 200:
        return ""
    return name


def code_files(text):
    """[(file name, code)] for each fenced code block in a reply."""
    files, used = [], set()
    for number, match in enumerate(CODE_BLOCK.finditer(text or ""), 1):
        info, code = match.group(1).split(), match.group(2)
        if not code.strip():
            continue
        language = info[0].lower() if info else ""
        name = ""
        for word in info:
            word = word.split("=", 1)[1] if "=" in word else word
            if ("." in word or "/" in word) and safe_file_name(word):
                name = safe_file_name(word)
        lines = code.split("\n", 1)
        if not name and (comment := NAME_COMMENT.match(lines[0])):
            name = safe_file_name(comment.group(1))
        if not name and language in ("dockerfile", "makefile"):
            name = EXTENSIONS[language]
        if not name:
            name = f"snippet-{number}.{EXTENSIONS.get(language, 'txt')}"
        stem, dot, extension = name.rpartition(".") if "." in posixpath.basename(name) else (name, "", "")
        candidate, copy = name, 2
        while candidate in used:
            candidate = f"{stem}-{copy}{dot}{extension}"
            copy += 1
        used.add(candidate)
        files.append((candidate, code))
    return files


def code_zip(files):
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, code in files:
            archive.writestr(name, code)
    return buffer.getvalue()


class ChatError(Exception):
    """A readable message for the person chatting; detail is the technical cause, for the error log."""

    def __init__(self, message, detail=""):
        super().__init__(message)
        self.detail = detail


def error_detail(error):
    """The status code, request ID and message of an API error, for the error log."""
    parts = [type(error).__name__]
    if getattr(error, "status_code", None):
        parts.append(f"HTTP {error.status_code}")
    if getattr(error, "request_id", None):
        parts.append(f"request {error.request_id}")
    message = getattr(error, "message", None) or str(error)
    return " · ".join(parts) + (f": {message}" if message else "")


# Values saved on the Settings page; the app installs a lookup that reads them.
_saved_setting = None


def use_settings(lookup):
    global _saved_setting
    _saved_setting = lookup


# The app installs a function that saves failed replies to the Chat AI error log.
_error_log = None


def use_error_log(record):
    global _error_log
    _error_log = record


def log_error(source, message, detail=""):
    if _error_log is None:
        return
    try:
        _error_log(source=source, provider=provider(), model=assistant_model(), message=message, detail=detail)
    except Exception:
        pass  # A broken log must never hide the chat error itself.


def setting(name, default=""):
    """A saved setting, else the environment variable, else the default."""
    value = _saved_setting(name) if _saved_setting else None
    if value is None:
        value = os.environ.get(name, "")
    return value.strip() or default


def provider():
    return "local" if setting("CHAT_PROVIDER", DEFAULT_PROVIDER) == "local" else "anthropic"


def parse_models(text):
    """Model IDs from one-per-line (or comma-separated) text, without duplicates."""
    models = []
    for part in text.replace(",", "\n").splitlines():
        part = part.strip()
        if part and part not in models:
            models.append(part)
    return models


def local_models():
    """The models listed on the Settings page for the Local AI server."""
    return parse_models(setting("LOCAL_AI_MODELS")) or list(DEFAULT_LOCAL_MODELS)


def is_configured():
    if provider() == "local":
        return bool(setting("LOCAL_AI_URL", DEFAULT_LOCAL_URL))
    return bool(setting("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def assistant_name():
    if provider() == "local":
        return setting("LOCAL_AI_MODEL", DEFAULT_LOCAL_MODEL)
    return "Claude"


def assistant_model():
    if provider() == "local":
        return setting("LOCAL_AI_MODEL", DEFAULT_LOCAL_MODEL)
    return setting("CHAT_MODEL", DEFAULT_MODEL)


def effort():
    value = setting("CHAT_EFFORT", DEFAULT_EFFORT)
    return value if value in EFFORTS else DEFAULT_EFFORT


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


def stream_reply(history, for_telegram=False, source=None):
    """Yield {"type": "text"} chunks as the reply arrives, or {"type": "replace"} when the
    partial text must be swapped (a refusal). The generator's return value is the full reply.

    Raises ChatError with a readable message when the reply cannot be produced, after
    saving it to the error log with where it came from (web, telegram or test).
    """
    source = source or ("telegram" if for_telegram else "web")
    try:
        if provider() == "local":
            return (yield from stream_local_reply(history, for_telegram))
        return (yield from stream_claude_reply(history, for_telegram))
    except ChatError as error:
        log_error(source, str(error), error.detail)
        raise
    except Exception as error:
        log_error(source, "Something went wrong while the AI was replying.", f"{type(error).__name__}: {error}")
        raise


def stream_claude_reply(history, for_telegram=False):
    if not is_configured():
        raise ChatError("Chat is not set up yet: add an Anthropic API key on the Settings page.")
    model = setting("CHAT_MODEL", DEFAULT_MODEL)
    options = {}
    if model in FALLBACK_MODELS:
        # If a safety classifier declines, retry on Anthropic's recommended model.
        options = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    parts = []
    try:
        with client().beta.messages.stream(
            model=model,
            max_tokens=MAX_TOKENS,
            system=system_prompt(),
            messages=to_api_messages(history, for_telegram),
            output_config={"effort": effort()},
            cache_control={"type": "ephemeral"},
            **options,
        ) as stream:
            for event in stream:
                if event.type == "text":
                    parts.append(event.text)
                    yield {"type": "text", "text": event.text}
            message = stream.get_final_message()
    except anthropic.AuthenticationError as error:
        raise ChatError("The Anthropic API key was rejected. Check it on the Settings page.", error_detail(error))
    except anthropic.PermissionDeniedError as error:
        raise ChatError("This API key is not allowed to use the chat model.", error_detail(error))
    except anthropic.NotFoundError as error:
        raise ChatError(f"Claude has no model called {model}. Pick another model on the Settings page.",
                        error_detail(error))
    except anthropic.RateLimitError as error:
        raise ChatError("Claude is busy right now (rate limit). Try again in a minute.", error_detail(error))
    except anthropic.BadRequestError as error:
        raise ChatError(f"Claude could not accept this request: {error.message}", error_detail(error))
    except anthropic.APIStatusError as error:
        if error.status_code >= 500:
            raise ChatError("Claude is having trouble right now. Try again shortly.", error_detail(error))
        raise ChatError(f"Claude returned an error: {error.message}", error_detail(error))
    except anthropic.APITimeoutError as error:
        raise ChatError("Claude took too long to answer. Try again shortly.", error_detail(error))
    except anthropic.APIConnectionError as error:
        raise ChatError("Could not reach Claude. Check the server's internet connection.", error_detail(error))

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
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream", "User-Agent": USER_AGENT}
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
                        raise ChatError(f"The local AI returned an error: {local_error_text(event)}",
                                        data[:500])
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
        raw = error.read() or b"{}"
        try:
            detail = local_error_text(json.loads(raw))
        except ValueError:
            detail = raw.decode("utf-8", "replace").strip()[:200] or error.reason
        log_detail = f"HTTP {error.code} from {base_url}/chat/completions: {detail}"
        if error.code == 403 and "error code:" in detail:
            raise ChatError(f"The local AI server's firewall (Cloudflare) blocked this server ({detail}).", log_detail)
        if error.code in (401, 403):
            raise ChatError("The local AI server rejected the API key. Check it on the Settings page.", log_detail)
        if error.code == 404:
            raise ChatError(f"The local AI server could not find this model or address ({detail}). Check the Settings page.",
                            log_detail)
        raise ChatError(f"The local AI server returned an error ({error.code}): {detail}", log_detail)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        reason = getattr(error, "reason", error)
        raise ChatError(f"Could not reach the local AI server at {base_url} ({reason}).",
                        f"{type(error).__name__}: {reason}")

    if finish_reason == "length":
        notice = "(The reply was cut off because it reached the length limit.)"
        parts.append("\n\n" + notice)
        yield {"type": "text", "text": "\n\n" + notice}
    reply = "".join(parts).strip()
    if not reply:
        raise ChatError("The local AI server sent an empty reply.", f"finish_reason: {finish_reason}")
    return reply


def local_error_text(payload):
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return error.get("message") or str(error)
    return str(error or (payload.get("detail") if isinstance(payload, dict) else payload) or "unknown error")


def complete_reply(history, for_telegram=False, source=None):
    generator = stream_reply(history, for_telegram, source)
    while True:
        try:
            next(generator)
        except StopIteration as done:
            return done.value


def test_connection():
    """Send a short message with the saved settings. Returns (seconds, reply); raises ChatError."""
    started = time.monotonic()
    reply = complete_reply([{"role": "user", "content": TEST_MESSAGE}], source="test")
    return time.monotonic() - started, reply
