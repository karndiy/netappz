"""YouTube search for Mediaz (1.75.0).

Search reads YouTube's own results page, so no API key is needed. Videos then play in the browser
through YouTube's embedded player; nothing is downloaded to the server.
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request

SEARCH_URL = "https://www.youtube.com/results"
TIMEOUT = 10
LIMIT = 20
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
# Links people paste: youtube.com/watch?v=…, youtu.be/…, /shorts/…, /embed/…, /live/… and music.youtube.com.
LINK = re.compile(
    r"^(?:https?://)?(?:www\.|m\.|music\.)?(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|embed/|live/|v/)"
    r"|youtube-nocookie\.com/embed/|youtu\.be/)([A-Za-z0-9_-]{11})(?:[?&#/].*)?$"
)


class YouTubeError(Exception):
    """Searching failed; the message can be shown to people."""


def video_id(text):
    """The video ID in a YouTube link (or a bare ID), or None."""
    text = (text or "").strip()
    if VIDEO_ID.fullmatch(text):
        return text
    match = LINK.match(text)
    return match.group(1) if match else None


def _text(value):
    """YouTube's text objects: {"simpleText": …} or {"runs": [{"text": …}, …]}."""
    if not isinstance(value, dict):
        return ""
    if "simpleText" in value:
        return str(value["simpleText"])
    return "".join(str(run.get("text", "")) for run in value.get("runs", []) if isinstance(run, dict))


def _renderers(node):
    """Every videoRenderer in YouTube's page data, in page order."""
    stack = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if isinstance(item.get("videoRenderer"), dict):
                yield item["videoRenderer"]
                continue
            stack.extend(reversed(list(item.values())))
        elif isinstance(item, list):
            stack.extend(reversed(item))


def parse_results(page, limit=LIMIT):
    """The videos in a YouTube results page: [{id, title, channel, duration, thumbnail}]."""
    match = re.search(r"(?:var ytInitialData|window\[\"ytInitialData\"\])\s*=\s*", page)
    if not match:
        raise YouTubeError("YouTube sent a page Mediaz could not read. Try again later.")
    try:
        data, _ = json.JSONDecoder().raw_decode(page, match.end())
    except ValueError:
        raise YouTubeError("YouTube sent a page Mediaz could not read. Try again later.") from None
    videos, seen = [], set()
    for renderer in _renderers(data):
        vid = renderer.get("videoId")
        if not isinstance(vid, str) or not VIDEO_ID.fullmatch(vid) or vid in seen:
            continue
        if not _text(renderer.get("lengthText")):
            continue  # Live streams and upcoming premieres have no length; skip them.
        seen.add(vid)
        videos.append({
            "id": vid,
            "title": _text(renderer.get("title")) or vid,
            "channel": _text(renderer.get("ownerText") or renderer.get("longBylineText")),
            "duration": _text(renderer.get("lengthText")),
            "thumbnail": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
        })
        if len(videos) >= limit:
            break
    return videos


def fetch(url):
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.8",
    })
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.read(4_000_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        raise YouTubeError(f"YouTube did not answer the search (HTTP {error.code}). Try again later.") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise YouTubeError("Could not reach YouTube. Check the server's internet connection.") from None


def search(query, limit=LIMIT):
    """Search YouTube for videos; a pasted link or video ID gives just that video."""
    query = (query or "").strip()[:200]
    if not query:
        return []
    found = video_id(query)
    if found:
        return [{"id": found, "title": f"YouTube video {found}", "channel": "", "duration": "",
                 "thumbnail": f"https://i.ytimg.com/vi/{found}/mqdefault.jpg"}]
    # sp=EgIQAQ== asks for videos only (no channels or playlists).
    url = f"{SEARCH_URL}?{urllib.parse.urlencode({'search_query': query, 'sp': 'EgIQAQ=='})}"
    return parse_results(fetch(url), limit)
