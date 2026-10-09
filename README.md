# Network Files

A responsive Flask file manager for desktop PCs, tablets, and phones. No sign-in or password is required. Anyone who can reach it can browse, upload, download, rename, and delete shared files and folders. It supports browsing folders, creating folders, uploading files or a selected folder, downloading individual files, and downloading folders as ZIP archives.

Open **Notes** from the app header, or visit `/notes`, to create, edit, and delete basic text notes. Notes are stored in a SQLite database under Flask's `instance/` folder by default; set `NOTES_DATABASE` to choose another location. Notes are shared with any device that can access this no-login app.

Open **Update log** from the app header, or visit `/updates`, to review the included patch notes. This is a changelog page; it does not automatically check for or install new versions.

Open **Convert** from the app header, or visit `/convert`, to convert Word to PDF, PDF to Word, images to PDF, or PDF pages to images. Uploads go to a temporary folder that is deleted after each conversion; nothing is saved to `shared/`. Word to PDF needs LibreOffice, which the Docker image includes; the other tools only need the Python packages in `requirements.txt`. Each conversion accepts up to 200 MB.

Open **Convert → Edit PDF**, or visit `/convert/edit`, to edit a PDF in the browser, much like Acrobat's Edit PDF mode: change existing lines of text, add text, images and a drawn signature, draw, add shapes, highlight, comment, white out, redact (content under a redaction box is permanently removed), rotate/reorder/duplicate/delete/insert pages, and add a watermark or page numbers. Edits save automatically on the server for 12 hours, so a reload keeps them; **Download** builds the edited PDF and **Save to Files** puts a copy in `shared/`. Thai text is supported.

## Run

Python 3.9 or newer is recommended.

```sh
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 app.py
```

The app listens on `0.0.0.0:5000`. On each PC, phone, or tablet connected to the same reachable network, open `http://<computer-LAN-IP>:5000` using a modern web browser. The server computer must remain on and running the app; allow inbound TCP port 5000 through its firewall if required. Folder selection depends on browser support; uploading individual files works through the device's file picker.

Files are stored in the `shared/` directory next to `app.py` by default. Set `FILES_ROOT` to use a different storage directory. Set `FILES_SECRET_KEY` to a persistent random value if sessions should remain valid after a restart; otherwise a new key is generated at startup.

Because there is no authentication, only run this on a private, trusted network and stop the server when finished. Anyone who can connect to the app can access and modify the shared files. Do not expose it to the public internet. HTTP traffic is not encrypted; use a VPN or HTTPS reverse proxy for untrusted networks.

## Open as netappz.local

With Docker, the app also listens on port 80, so devices on the LAN can open `http://netappz.local`. The name is announced with Bonjour (mDNS) by `scripts/netappz-mdns.sh`, which follows the Mac's IP if it changes. To start it at every login:

```sh
cp scripts/local.netappz.mdns.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/local.netappz.mdns.plist
```

To stop it, run `launchctl unload ~/Library/LaunchAgents/local.netappz.mdns.plist`. `.local` names work on macOS, iOS, iPadOS, Windows 10 and later, and Linux with Avahi. Some Android phones can't open `.local` names; use `http://<computer-LAN-IP>` there.

## Run with Docker

```sh
docker-compose up -d --build
```

This starts three containers:

- `network-files`: the Flask app (gunicorn) on port 5000
- `network-files-db`: PostgreSQL 16, which stores notes (named volume `pgdata`)
- `network-files-redis`: Redis 7, which stores sessions and caches the notes list (named volume `redisdata`)

Then open `http://<computer-LAN-IP>:5000`. Shared files persist in `./shared` on the host. Postgres and Redis are only reachable from inside the Compose network. Optionally set `FILES_SECRET_KEY`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, and `POSTGRES_DB` in your shell or a `.env` file before the first start; the defaults are `notes`. Stop with `docker-compose down` (add `-v` to also delete the database and Redis volumes).

## Chat with Claude (web and Telegram)

Open **Chat** from the app header, or visit `/chat`, to talk with Claude. Conversations are stored in the database and shared with an optional Telegram bot, so you can continue the same chat from your phone anywhere with internet.

You can do steps 2 to 4 on the **Settings** page instead of editing `.env`.

1. Copy `.env.example` to `.env` next to `docker-compose.yml`.
2. Set `ANTHROPIC_API_KEY` (create one at https://platform.claude.com/settings/keys). Usage is billed to that key.
3. Optional Telegram bot: talk to @BotFather in Telegram, send `/newbot`, and paste the token into `TELEGRAM_BOT_TOKEN`.
4. Run `docker-compose up -d`, then message your bot. It replies with your Telegram user ID; put that ID in `TELEGRAM_ALLOWED_USERS` (comma-separated for several people) and run `docker-compose up -d` again.

The bot uses long polling: it connects out to Telegram, so you do not need a public address, port forwarding, or HTTPS. Only allowed Telegram users get answers. In Telegram, `/new` starts a new conversation; other messages continue the most recent one. Set `CHAT_MODEL` or `CHAT_EFFORT` (`low`, `medium`, `high`) to tune the model; the default is `claude-opus-5-5` at `medium` effort.

The web Chat page has no sign-in, like the rest of the app: anyone who can open the app can chat and spend API credit. Keep it on a trusted network.

## Settings page

Open **Settings** from the app header, or visit `/settings`, to change the Anthropic API key, chat model, effort, extra instructions for Claude, the Telegram bot token, and the allowed Telegram user IDs. Saved values are stored in the database, override the matching `.env` values, and apply right away to both the web chat and the Telegram bot (the bot picks up a new token within about a minute). Removing a saved value falls back to `.env`. API keys and tokens are never shown in full, but like the rest of the app the page has no sign-in: anyone who can open it can change them.

Storage, database, Redis, and `FILES_SECRET_KEY` are listed on the page for reference; change them in `.env` or `docker-compose.yml` and restart.

## Database and Redis settings

- `DATABASE_URL`, e.g. `postgresql://user:pass@host:5432/notes`: store notes in PostgreSQL. If unset, notes use SQLite at `NOTES_DATABASE`.
- `REDIS_URL`, e.g. `redis://host:6379/0`: keep sessions in Redis and cache the notes list for up to 5 minutes, cleared on every change. If unset, sessions use signed cookies and nothing is cached.
