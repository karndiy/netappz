# Install Network Files

Step-by-step setup for Network Files, written so a person or an AI assistant (Claude or another
model) can follow it from top to bottom. Each step says what to run and how to check it worked.
Do the steps in order and stop at the first check that fails; the Troubleshooting section lists
the common causes.

## What you get

- `web` (container `network-files`): the Flask app, served by gunicorn on ports 80 and 5000.
- `db` (`network-files-db`): PostgreSQL 16 for accounts, notes, chats and everything else.
- `redis` (`network-files-redis`): Redis 7 for sign-in sessions and caching.
- `backup` (`network-files-backup`): dumps the database to `./backups` every 6 hours.
- `telegram` (`network-files-telegram`): optional Telegram bot; idle until a token is set.

Shared files are stored in `./shared` on the host.

## Rules for AI assistants

- Run every command from the project folder (the one containing `docker-compose.yml`).
- Never print, commit or send the contents of `.env`; it holds secrets.
- Do not run `docker-compose down -v` or delete `backups/`: that destroys the database and its backups.
- Do not expose the app to the public internet. It is meant for a private, trusted network.
- If a step needs a value only the owner has (an API key, a bot token, a password), ask for it
  instead of inventing one, or leave it empty: those settings are optional.

## 1. Check the requirements

Needed: Git, Docker, and Docker Compose (either `docker-compose` v1 or the `docker compose` plugin).

```sh
git --version
docker --version
docker-compose --version || docker compose version
docker info > /dev/null && echo "Docker is running"
```

Check: every command prints a version and the last one prints `Docker is running`. On macOS or
Windows, start Docker Desktop first if `docker info` fails.

If only `docker compose` (with a space) is available, use it wherever this guide says `docker-compose`.

## 2. Get the code

```sh
git clone https://github.com/karndiy/netappz.git
cd netappz
```

Skip this step if you already have the project folder. Check: `ls docker-compose.yml app.py` lists both files.

## 3. Create the settings file

```sh
cp .env.example .env
```

All values in `.env` are optional; the app starts with every one of them empty. Fill in what the owner wants:

| Setting | What it does |
| --- | --- |
| `ANTHROPIC_API_KEY` | Turns on the Chat page. Create a key at https://platform.claude.com/settings/keys. |
| `TELEGRAM_BOT_TOKEN` | Optional Telegram bot, from @BotFather (`/newbot`). |
| `TELEGRAM_ALLOWED_USERS` | Comma-separated Telegram user IDs allowed to use the bot. |
| `FILES_SECRET_KEY` | Keeps people signed in across restarts. Recommended. |

To set a strong `FILES_SECRET_KEY`:

```sh
key=$(python3 -c "import secrets; print(secrets.token_hex(32))")
sed -i.bak "s/^FILES_SECRET_KEY=.*/FILES_SECRET_KEY=$key/" .env && rm -f .env.bak
```

You can also add `POSTGRES_USER`, `POSTGRES_PASSWORD` and `POSTGRES_DB` (each defaults to `notes`).
Set them before the first start: the database keeps the values it was created with.
The API key, model and Telegram settings can be changed later on the Settings page instead.

Check: `ls .env` lists the file. Do not print its contents.

## 4. Build and start

```sh
mkdir -p shared backups
docker-compose up -d --build
```

The first build downloads LibreOffice, Thai fonts and OCR models, so it can take 10 minutes or more.

Check: after about a minute,

```sh
docker-compose ps
```

shows `network-files`, `network-files-db` and `network-files-redis` as `Up`, and `db`, `redis`
and `web` as `(healthy)`. If `web` says `(health: starting)`, wait 30 seconds and check again.

## 5. Check the app answers

```sh
curl -s http://localhost:5000/healthz
```

Check: it prints `{"status":"ok"}`. Any other answer lists the problem (database or Redis);
see Troubleshooting.

## 6. Create the administrator account

Open `http://localhost:5000` in a browser on the same computer. The very first visit shows a
form that creates the administrator account: choose a username and password. This step needs a
person; an AI assistant should hand over here and tell the owner to do it.

Other people open `http://<this-computer's-LAN-IP>:5000` (or `http://<LAN-IP>` on port 80) and
choose Sign up; the administrator approves new accounts under Settings.

Find the LAN IP with `ipconfig getifaddr en0` (macOS), `hostname -I` (Linux) or `ipconfig` (Windows).
Allow inbound TCP ports 80 and 5000 through the firewall if other devices can't connect.

## 7. Optional extras (macOS)

- Open the app as `http://netappz.local`: see "Open as netappz.local" in `README.md`.
- Start the app when the Mac turns on: see "Start when the Mac turns on" in `README.md`.
- Let Claude process requests from the Update log page: see "Requests processed by Claude" in `README.md`.

Check: after setting up netappz.local, `http://netappz.local` opens the sign-in page from another device on the network.

## Run the tests (optional)

```sh
docker run --rm -u root -v "$PWD:/app" -w /app network-files \
  sh -c "pip install -q pytest; python -m pytest -q -p no:cacheprovider test_app.py"
```

Check: the last line says `passed` with no failures.

## Run without Docker (development only)

Needs Python 3.9 or newer. Notes and accounts go to SQLite under `instance/`, sessions use
cookies, and Word to PDF and OCR only work if LibreOffice and Tesseract are installed.

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 app.py
```

Check: `curl -s http://localhost:5000/healthz` prints `{"status":"ok"}`.

## Everyday commands

| Task | Command |
| --- | --- |
| Stop | `docker-compose down` (keeps all data) |
| Start again | `docker-compose up -d` |
| Update to the newest version | `git pull && docker-compose up -d --build` |
| See the app's log | `docker-compose logs --tail 100 web` |
| Back up the database now | Settings → System & backups → Back up now |
| Restore the newest backup | `scripts/db-restore.sh` |

## Troubleshooting

- **Port 80 or 5000 is already in use:** stop the other program, or change the left-hand number
  under `ports:` in `docker-compose.yml` (for example `"8080:5000"`) and run `docker-compose up -d`.
- **`web` is unhealthy:** run `docker-compose logs --tail 100 web db redis` and read the last errors.
  The app waits for the database, so a slow first database start only delays it.
- **Chat doesn't answer:** set `ANTHROPIC_API_KEY` in `.env` and run `docker-compose up -d`,
  or enter the key on the Settings page.
- **Everyone is signed out after a restart:** set `FILES_SECRET_KEY` (step 3) and run `docker-compose up -d`.
- **Changed `POSTGRES_PASSWORD` after the first start and the app can't connect:** put the old value
  back. The password is fixed when the database is created.
