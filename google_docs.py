"""Google Docs: turn an HTML page into a Google Doc in Drive and share it by link (request #43), and
keep a Google Sheet of rows up to date at the same link (request #46), its cells colored by status (request #47).

The app signs in as one Google account with an OAuth client and a refresh token, set under
Settings → Google Docs, so it needs no extra packages: only the Drive API (and the Sheets API, for
colors) over HTTPS.
"""

import csv
import io
import json
import logging
import secrets
import urllib.error
import urllib.parse
import urllib.request

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = ("https://www.googleapis.com/upload/drive/v3/files"
              "?uploadType=multipart&supportsAllDrives=true&fields=id,webViewLink")
UPDATE_URL = ("https://www.googleapis.com/upload/drive/v3/files/{file_id}"
              "?uploadType=multipart&supportsAllDrives=true&fields=id,webViewLink")
SHEETS_URL = "https://sheets.googleapis.com/v4/spreadsheets/{file_id}"
PERMISSIONS_URL = "https://www.googleapis.com/drive/v3/files/{file_id}/permissions?supportsAllDrives=true"
HEADER_COLOR = "#D9D9D9"
SETTINGS = ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN")
TIMEOUT = 30
logger = logging.getLogger(__name__)


class GoogleDocsError(Exception):
    """Something went wrong talking to Google; the message can be shown to people."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status  # Google's HTTP status, when it answered.


def configured(setting):
    """True when the client ID, client secret and refresh token are all set."""
    return all(setting(name) for name in SETTINGS)


def _call(request):
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read() or b"{}")
        except ValueError:
            detail = {}
        message = detail.get("error_description") or (detail.get("error") or {})
        if isinstance(message, dict):
            message = message.get("message", "")
        raise GoogleDocsError(f"Google said: {message or error.reason} (HTTP {error.code}).", error.code) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise GoogleDocsError("Could not reach Google. Check the server's internet connection.") from None
    except ValueError:
        raise GoogleDocsError("Google sent an answer the app could not read.") from None


def access_token(client_id, client_secret, refresh_token):
    data = urllib.parse.urlencode({"client_id": client_id, "client_secret": client_secret,
                                   "refresh_token": refresh_token, "grant_type": "refresh_token"}).encode()
    token = _call(urllib.request.Request(TOKEN_URL, data=data, method="POST")).get("access_token")
    if not token:
        raise GoogleDocsError("Google did not give the app access. Check the Google Docs settings.")
    return token


def _connect(setting):
    if not configured(setting):
        raise GoogleDocsError("Google Docs isn't connected. An administrator can set it up under "
                              "Settings → Google Docs.")
    return access_token(*(setting(name) for name in SETTINGS))


def _multipart(metadata, content_type, content):
    boundary = "netappz-" + secrets.token_hex(12)
    body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(metadata)}\r\n"
            f"--{boundary}\r\nContent-Type: {content_type}; charset=UTF-8\r\n\r\n{content}\r\n--{boundary}--\r\n")
    return body.encode("utf-8"), f"multipart/related; boundary={boundary}"


def _create_shared(setting, token, name, mime_type, content_type, content):
    """Make a Google file from content, let anyone with the link view it; its ID and link."""
    metadata = {"name": name, "mimeType": mime_type}
    folder = setting("GOOGLE_DRIVE_FOLDER")
    if folder:
        metadata["parents"] = [folder]
    body, multipart = _multipart(metadata, content_type, content)
    created = _call(urllib.request.Request(
        UPLOAD_URL, data=body, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": multipart},
    ))
    if not created.get("id"):
        raise GoogleDocsError("Google did not make the document.")
    _call(urllib.request.Request(
        PERMISSIONS_URL.format(file_id=urllib.parse.quote(created["id"], safe="")),
        data=json.dumps({"role": "reader", "type": "anyone"}).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    ))
    return created["id"], created.get("webViewLink")


def create_doc(setting, name, html):
    """Make a Google Doc named name from this HTML, let anyone with the link view it; its link."""
    token = _connect(setting)
    file_id, link = _create_shared(setting, token, name, "application/vnd.google-apps.document", "text/html", html)
    return link or f"https://docs.google.com/document/d/{file_id}/edit"


def csv_text(rows):
    """Rows as CSV for a Google Sheet. Cells people typed that start like a formula stay plain text."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    for row in rows:
        writer.writerow(["'" + cell if isinstance(cell, str) and cell[:1] in ("=", "+", "-", "@") else cell
                         for cell in row])
    return out.getvalue()


def _color(hex_color):
    return {key: int(hex_color[i:i + 2], 16) / 255 for key, i in (("red", 1), ("green", 3), ("blue", 5))}


def highlight_requests(sheet_id, rows, highlights):
    """Sheets API requests: a bold gray header row that stays at the top, and each cell in the columns of
    highlights ({column: {text: "#RRGGBB"}}) filled with its text's color. Older colors are cleared first."""
    requests = [
        {"updateSheetProperties": {"properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
                                   "fields": "gridProperties.frozenRowCount"}},
        {"repeatCell": {"range": {"sheetId": sheet_id}, "cell": {"userEnteredFormat": {}},
                        "fields": "userEnteredFormat.backgroundColor,userEnteredFormat.textFormat.bold"}},
        {"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {"userEnteredFormat": {"backgroundColor": _color(HEADER_COLOR),
                                                       "textFormat": {"bold": True}}},
                        "fields": "userEnteredFormat.backgroundColor,userEnteredFormat.textFormat.bold"}},
    ]
    for column, colors in sorted(highlights.items()):
        cells = []
        for row in rows[1:]:
            fill = colors.get(row[column]) if column < len(row) else None
            cells.append({"values": [{"userEnteredFormat": {"backgroundColor": _color(fill)}} if fill else {}]})
        if cells:
            requests.append({"updateCells": {
                "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": len(rows),
                          "startColumnIndex": column, "endColumnIndex": column + 1},
                "rows": cells, "fields": "userEnteredFormat.backgroundColor"}})
    return requests


def _highlight(token, file_id, rows, highlights):
    """Color the sheet's cells. The rows are saved even if this fails (say the Sheets API is off)."""
    url = SHEETS_URL.format(file_id=urllib.parse.quote(file_id, safe=""))
    headers = {"Authorization": f"Bearer {token}"}
    try:
        found = _call(urllib.request.Request(url + "?fields=sheets.properties.sheetId", headers=headers))
        sheet_id = ((found.get("sheets") or [{}])[0].get("properties") or {}).get("sheetId", 0)
        _call(urllib.request.Request(
            url + ":batchUpdate", method="POST", headers={**headers, "Content-Type": "application/json"},
            data=json.dumps({"requests": highlight_requests(sheet_id, rows, highlights)}).encode(),
        ))
    except GoogleDocsError as error:
        logger.warning("Could not color the Google Sheet (is the Google Sheets API on?): %s", error)


def save_sheet(setting, name, rows, file_id="", highlights=None):
    """Put rows into the Google Sheet file_id, keeping its link, or make a new shared one when there is
    none or it has gone, then color the cells in highlights. Its ID and link."""
    token = _connect(setting)
    content = csv_text(rows)
    file_id, link = _save_rows(setting, token, name, content, file_id)
    if highlights:
        _highlight(token, file_id, rows, highlights)
    return file_id, link


def _save_rows(setting, token, name, content, file_id):
    if file_id:
        body, multipart = _multipart({"name": name}, "text/csv", content)
        try:
            updated = _call(urllib.request.Request(
                UPDATE_URL.format(file_id=urllib.parse.quote(file_id, safe="")), data=body, method="PATCH",
                headers={"Authorization": f"Bearer {token}", "Content-Type": multipart},
            ))
        except GoogleDocsError as error:
            if error.status not in (403, 404):  # Deleted, or no longer the app's to change: make a new one.
                raise
        else:
            return file_id, updated.get("webViewLink") or f"https://docs.google.com/spreadsheets/d/{file_id}/edit"
    file_id, link = _create_shared(setting, token, name, "application/vnd.google-apps.spreadsheet",
                                   "text/csv", content)
    return file_id, link or f"https://docs.google.com/spreadsheets/d/{file_id}/edit"
