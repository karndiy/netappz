"""Google Docs: turn an HTML page into a Google Doc in Drive and share it by link (request #43).

The app signs in as one Google account with an OAuth client and a refresh token, set under
Settings → Google Docs, so it needs no extra packages: only the Drive API over HTTPS.
"""

import json
import secrets
import urllib.error
import urllib.parse
import urllib.request

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = ("https://www.googleapis.com/upload/drive/v3/files"
              "?uploadType=multipart&supportsAllDrives=true&fields=id,webViewLink")
PERMISSIONS_URL = "https://www.googleapis.com/drive/v3/files/{file_id}/permissions?supportsAllDrives=true"
SETTINGS = ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN")
TIMEOUT = 30


class GoogleDocsError(Exception):
    """Something went wrong talking to Google; the message can be shown to people."""


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
        raise GoogleDocsError(f"Google said: {message or error.reason} (HTTP {error.code}).") from None
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


def create_doc(setting, name, html):
    """Make a Google Doc named name from this HTML, let anyone with the link view it; its link."""
    if not configured(setting):
        raise GoogleDocsError("Google Docs isn't connected. An administrator can set it up under "
                              "Settings → Google Docs.")
    token = access_token(*(setting(name) for name in SETTINGS))
    metadata = {"name": name, "mimeType": "application/vnd.google-apps.document"}
    folder = setting("GOOGLE_DRIVE_FOLDER")
    if folder:
        metadata["parents"] = [folder]
    boundary = "netappz-" + secrets.token_hex(12)
    body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(metadata)}\r\n"
            f"--{boundary}\r\nContent-Type: text/html; charset=UTF-8\r\n\r\n{html}\r\n--{boundary}--\r\n")
    created = _call(urllib.request.Request(
        UPLOAD_URL, data=body.encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": f"multipart/related; boundary={boundary}"},
    ))
    if not created.get("id"):
        raise GoogleDocsError("Google did not make the document.")
    _call(urllib.request.Request(
        PERMISSIONS_URL.format(file_id=urllib.parse.quote(created["id"], safe="")),
        data=json.dumps({"role": "reader", "type": "anyone"}).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    ))
    return created.get("webViewLink") or f"https://docs.google.com/document/d/{created['id']}/edit"
