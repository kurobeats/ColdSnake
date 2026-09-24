# ColdSnake
A python icedrive client.

Upload-only mirroring, folder listing and proof-of-work login for Icedrive, driven
directly against the v3 mobile API that Icedrive's own apps use.

## Why this exists

Icedrive's WebDAV service was disabled for new users on 2026-04-15 and is being
gradually sunset for existing users, and Icedrive has no public API (the feature
request to rclone has been open since 2019). The official Linux client is a
Qt/WebEngine GUI app that mounts over FUSE, which is not something you can run
unattended on a server. ColdSnake talks to the same endpoints the official apps
use, from the command line, with no GUI, no WebDAV and no third-party binaries.

## What it does

* **login** - proof-of-work challenge, no captcha service needed
* **ls** - list a remote folder
* **mirror** - upload-only mirror of local directory trees into Icedrive

## What it deliberately will not do

* **Never deletes or truncates anything remote.** No deletes at all. A bad run
  can waste bandwidth, not data.
* No encrypted-folder support (Icedrive's crypto is not implemented).

## Install

```bash
pip install .          # or: pipx install .
coldsnake --version
```

Python 3.11+ (uses `tomllib`), standard library only, no dependencies.

## Credentials

Resolved in this order:

1. `ICEDRIVE_EMAIL` / `ICEDRIVE_PASSWORD` environment variables
2. `[auth]` section in the config file
3. `~/.config/icedrive/credentials` (lines `ICEDRIVE_EMAIL=...`, `ICEDRIVE_PASSWORD=...`)

Keep any file holding the password at mode `0600`. **Never commit credentials** - the repo's `.gitignore` blocks `config.toml`, `credentials`, `.env`, `*.creds` and `*.key` for that reason; copy `config.example.toml` and fill it in outside the repo (e.g. `~/.config/coldsnake/config.toml`).

## Configure mirrors

`~/.config/coldsnake/config.toml`:

```toml
[auth]
email = "you@example.com"
# password = "..."        # prefer the env var or a 0600 file over this

[[mirror]]
local = "/srv/data/Sync"
remote = "Sync"

[[mirror]]
local = "/srv/data/Pictures"
remote = "Pictures"
```

## Usage

```bash
coldsnake login                              # verify credentials, print the account
coldsnake ls                                 # list the remote root
coldsnake ls --folder-id 12345               # list a specific folder
coldsnake mirror --dry-run                   # report what would upload
coldsnake mirror                             # every [[mirror]] in the config
coldsnake mirror --local /srv/data/music --remote music
```

Exit code is `0` on success, `1` if any file failed, `2` on auth failure, so it
drops straight into cron, systemd or a CI job.

## Behaviour worth knowing

* **Incremental.** Uploads only when the size or mtime differs. Icedrive
  preserves the mtime sent at upload time, so re-runs are cheap.
* **Streams.** File bodies are streamed through a single multipart request with a
  known `Content-Length`; process memory does not scale with file size.
* **Re-uploads overwrite.** Uploading a name that already exists in a folder
  replaces it server-side (same file id, no duplicate) - verified against the
  live API - so syncing repeatedly never leaves duplicates.
* **Verifies.** After uploading, each folder written to is re-listed and the
  sizes checked. Mismatches are reported as failures.
* **Retries.** 5xx/429/network errors retry with backoff; 401/403 triggers one
  fresh login. The API sits behind Cloudflare and does return intermittent
  `522`s, so this matters in practice.
* **Per-file isolation.** One unreadable or rejected file does not stop the run;
  it is logged and counted in the exit code.

### Throughput

Icedrive ingress appears capped at roughly **1.5-2 MB/s per account**: three
concurrent 20 MB uploads finished no faster than one, so ColdSnake uploads
sequentially on purpose. A 150 GB first pass therefore takes on the order of a
day, split across runs - it resumes where it left off.

## systemd

`systemd/` ships a `coldsnake.service` + `coldsnake.timer` pair (nightly 05:00)
and a `coldsnake@.service` template for running as another user. Install as user
units for the simplest setup:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/coldsnake.service systemd/coldsnake.timer ~/.config/systemd/user/
systemctl --user enable --now coldsnake.timer
```

Set `TimeoutStartSec=infinity` stays as shipped: a first full pass can run for
hours, and systemd will not start a second copy while one is still running.

## Protocol notes

Recovered from Icedrive's own clients (mobile API, `apis.icedrive.net/v3/mobile`):

```
POST /api  {app:ios, request:pow-new, scope:login}      -> proof-of-work challenge
POST /api  {password, email, pow_proof, request:login}  -> bearer token
GET  /collection?type=cloud&folderId=<id>               -> folder listing
POST /folder-create (multipart)                         -> create a folder
GET  /geo-fileserver-list?app=ios&pow_proof=<b64>       -> signed /deposit endpoints
POST <deposit endpoint> multipart {folderId, moddate, files[]} -> store the file
```

The proof-of-work is `sha256(challenge_bytes || nonce12 || counter_be32)` needing
at least `difficultyBits` leading zero bits; `pow_proof` is the base64 of the
challenge fields plus the winning nonce and hash.

## Limitations

* Unofficial API. Icedrive can change or block it at any time; treat failures as
  loud rather than silent (the exit code and logs are there for that).
* Folder deletion is not supported by this API (the call reports success and the
  folder stays); delete folders from the web UI.
* Download is not implemented yet.

## Tests

```bash
python -m unittest discover -s tests
```

## License

GPL-3.0-or-later, see LICENSE.
