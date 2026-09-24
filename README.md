# ColdSnake

A python icedrive client.

Upload-only mirroring, folder listing and proof-of-work login for Icedrive, driven
directly against the v3 mobile API that Icedrive's own apps use - plus download and
restore, a recoverable trash, version history, and batched deletes. No WebDAV, no GUI,
no third-party binaries, no dependencies beyond the standard library.

Status: **pre-1.0, working and in daily use** for scheduled backups of ~150 GB.
Verified against a live Icedrive account (login, listing, chunked uploads,
verification, idempotent re-runs, trash/restore, version history, batch delete).
85 unit tests.

## Why this exists

- Icedrive **disabled WebDAV for new users on 2026-04-15** and is gradually
  sunsetting it for existing users.
- Icedrive has **no public API** - the rclone feature request has been open since
  2019 and Icedrive's own answers are "planned, eventually".
- Their Linux client is a Qt/WebEngine **GUI** app that mounts over FUSE. It cannot
  be run unattended on a headless server, and a user asking for a Linux CLI in
  Icedrive's own forum got no answer.

ColdSnake talks to the same endpoints those apps use, from the command line, so a
headless host can back itself up on a schedule.

## Install

```bash
pip install .            # or: pipx install .
coldsnake --version
```

Python **3.11+** (uses `tomllib`), standard library only.

Running from a checkout without installing:

```bash
PYTHONPATH=src python3 -m coldsnake.cli --help
```

### Self-contained venv install

To keep everything under one directory (no site-packages, no pipx) and still have
`coldsnake` on PATH, use a launcher script:

```bash
python3 -m venv ~/.local/share/coldsnake/venv
~/.local/share/coldsnake/venv/bin/pip install --upgrade ~/src/ColdSnake
```

`~/.local/bin/coldsnake` (chmod 0755):

```sh
#!/bin/sh
# activate the venv, then run the CLI from it
VENV="$HOME/.local/share/coldsnake/venv"
[ -x "$VENV/bin/coldsnake" ] || { echo "coldsnake: no venv at $VENV" >&2; exit 2; }
. "$VENV/bin/activate"
exec "$VENV/bin/coldsnake" "$@"
```

That gives interactive use the venv's `python`/`pip` while keeping the install
contained; systemd should call the venv binary directly (`ExecStart=…/venv/bin/coldsnake`)
since a unit needs no shell activation.

## Credentials

Resolved in this order:

1. `ICEDRIVE_EMAIL` / `ICEDRIVE_PASSWORD` environment variables
2. `[auth]` in the config file
3. `~/.config/icedrive/credentials` - lines `ICEDRIVE_EMAIL=…`, `ICEDRIVE_PASSWORD=…`

Whichever you use, keep the file at mode `0600`. **Never commit credentials** - the
repo's `.gitignore` blocks `config.toml`, `credentials`, `.env`, `*.creds` and
`*.key` for that reason. Copy `config.example.toml` and fill it in *outside* the repo.

Three more files live under `~/.config/coldsnake/` and are safe to delete:

| file | purpose |
|---|---|
| `token` (0600) | cached bearer token + account info, so runs rarely need to log in |
| `device-id` | stable client id, sent as `X-Icedrive-Device-Id` |
| `upload-journal.json` | chunks already sent, so a killed run resumes (safe to delete) |

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

`remote` is a folder name created at the **root of your Icedrive** if it does not
exist; the local tree is mirrored inside it.

## Usage

```bash
coldsnake login                       # verify credentials, print the account
coldsnake account                     # plan, storage quota, bandwidth usage
coldsnake check                       # pre-flight only: service + sources. no uploads
coldsnake ls                          # list the remote root
coldsnake ls --folder-id 12345        # list one remote folder
coldsnake mirror --dry-run            # report what would upload, change nothing
coldsnake mirror                      # every [[mirror]] in the config
coldsnake mirror --local /srv/data/music --remote music
coldsnake mirror --allow-empty        # tolerate an empty source directory
coldsnake mirror --exclude '*.tmp' --exclude '.stfolder/*' --prune
coldsnake mirror --prune-delete       # prune permanently instead of trashing
coldsnake mirror --no-upload-journal  # do not resume partial uploads next run
coldsnake download --remote Sync --local /tmp/restore          # whole tree
coldsnake download --remote Sync --file docs/report.pdf --local /tmp/restore
coldsnake download --remote Sync --file docs/report.pdf --version 1 --local /tmp/old
coldsnake trash                       # list the remote trash (id, name, size, date)
coldsnake restore --id 12345          # bring a trashed item back (--id repeatable)
coldsnake versions --remote Sync --file docs/report.pdf
```

Global flags: `--verbose` (per-file decisions), `--config PATH`,
`--no-token-cache` (always log in), `--version`. They work before or after the
sub-command.

### Exit codes

| code | meaning |
|---|---|
| 0 | success |
| 1 | ran, but some files failed |
| 2 | refused to start: bad config, missing/unreadable/empty source, auth failure |
| 3 | service unavailable - the run stopped (nothing attempted, or it aborted part-way), retry later |

That makes it safe to drop into cron, systemd, or a CI job.

### Pre-flight (runs before every mirror)

A long run is never started on a whim:

1. **Service health** - one authenticated call. If Icedrive is up but degraded
   (`Fatal error encountered`, `Service temporarily unavailable`), the run stops in
   under a second with exit 3 instead of spending hours on failed uploads.
2. **Sources** - missing, unreadable or **empty** source directories are refused
   (`--allow-empty` overrides). An empty source means the mirror would do nothing -
   or, with `--prune`, trash everything.
3. **Config** - the same local path listed twice is refused.
4. **Storage** - free space is checked against the bytes actually pending, once the
   plan is known (see the quota caveat).

`coldsnake check` runs the pre-flight alone, which makes it a useful monitoring
probe.

## Behaviour worth knowing

* **Incremental.** Uploads only when size or mtime differ (mtime tolerance 2 s).
  Icedrive preserves the upload-time mtime, so a second run is nearly free: a
  2 583-file tree re-checks in ~56 s and uploads nothing.
* **Additive.** No remote item is removed unless `--prune` is passed - and then it
  is trashed (recoverable) unless `--prune-delete` is set. A file removed locally
  stays remote by default.
* **Deletes are revertible and batched.** `coldsnake trash` lists what a prune (or
  your own `Client.trash`) put in the remote trash, `coldsnake restore --id <id>`
  puts it back where it was, and `--prune-delete` erases a whole batch with one
  `POST /erase` carrying comma-joined ids.
* **Version history is readable.** Icedrive keeps previous revisions of an
  overwritten file: `coldsnake versions --remote NAME --file RELPATH` lists them and
  `coldsnake download ... --version N` fetches one, byte-for-byte from that
  revision's own signed URL.
* **Chunked and resumable.** Files larger than `chunk_size` (8 MiB by default) are
  uploaded as ranged chunks: a stall costs one chunk, not a 4 GB file.
* **Streams.** File bodies are streamed through one request with a known
  `Content-Length`; process memory does not scale with file size.
* **Verifies.** After uploading, each folder written to is re-listed and file sizes
  are compared. A download is sized against the listing and resumed from its
  partial `.tmp` on retry. Mismatches are reported as failures, not swallowed.
* **Retries.** Transport errors (5xx/429/522, timeouts) retry with backoff; an
  auth error triggers one fresh login; API-reported outages retry slowly (30 s ×
  attempt) rather than hammering.
* **Per-file isolation.** One bad file does not stop the run; it is logged, counted,
  and reflected in the exit code.
* **Re-uploads overwrite.** Uploading a name that already exists replaces it
  server-side (same file id, no duplicate), so repeated syncs never create
  duplicates.

### Throughput (measured against a live account)

Icedrive ingress is capped at roughly **1.5-2 MB/s per account**: three concurrent
20 MB uploads finished no faster than one, so ColdSnake uploads sequentially on
purpose. Small-file rates are bound by round trips, so connections are kept alive.

| workload | measured |
|---|---|
| 2 583 files / 283 MB | ~10 min uploading, then 56 s for a no-op re-check |
| 107.6 MB single file (13 chunks) | 69 s → 1.56 MB/s |
| 26 MB / 4 chunks, 134 MB / 16 chunks | correct sizes, no chunk overhead |
| 147 GB across 4 trees | ~25 h first pass, dominated by a 106 GB music tree |

The first pass is long but **incremental and resumable**: a killed or failed run
continues where it left off, so nightly runs simply chip away at it. Steady-state
nightly runs are minutes.

## systemd

`systemd/` ships a `coldsnake.service` + `coldsnake.timer` pair (nightly 05:00) for a
per-user systemd setup, and `coldsnake@.service` + `coldsnake@.timer` templates for
running as another (dedicated) user system-wide.

```bash
mkdir -p ~/.config/systemd/user
cp systemd/coldsnake.service systemd/coldsnake.timer ~/.config/systemd/user/
systemctl --user enable --now coldsnake.timer
```

For a system-wide install as a dedicated user (e.g. `backup`), use the
`coldsnake@.service` template + `coldsnake@.timer`:

1. **Create the user and install ColdSnake into its venv** (the template calls the
   venv binary directly, so no launcher or shell activation is needed):

   ```bash
   sudo useradd -m -s /usr/sbin/nologin backup
   sudo -iu backup -- python3 -m venv ~/.local/share/coldsnake/venv
   sudo -iu backup -- ~/.local/share/coldsnake/venv/bin/pip install --upgrade \
       git+https://github.com/kurobeats/ColdSnake.git
   sudo -iu backup -- ~/.local/share/coldsnake/venv/bin/coldsnake --version
   ```

2. **Configure as that user** (`sudo -iu backup`), then `chmod 0600` the config:

   ```toml
   # ~backup/.config/coldsnake/config.toml
   [auth]
   email = "you@example.com"
   # or put ICEDRIVE_EMAIL / ICEDRIVE_PASSWORD in ~backup/.config/coldsnake/credentials

   [[mirror]]
   local = "/srv/data/Sync"      # the backup user must be able to read this
   remote = "Sync"
   ```

   If the sources live outside the backup user's home, grant read access to it:
   `sudo setfacl -R -m u:backup:rX /srv/data/Sync` (repeat after new subdirectories,
   or use a group).

3. **Install the units** (root): copy `systemd/coldsnake@.service` and
   `systemd/coldsnake@.timer` to `/etc/systemd/system/`, then:

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now coldsnake@backup.timer
   sudo systemctl start coldsnake@backup.service     # first run, watch it
   journalctl -u coldsnake@backup -f
   ```

   The template resolves the binary at `/home/%i/.local/share/coldsnake/venv/bin/coldsnake`
   and reads `EnvironmentFile=-/home/%i/.config/coldsnake/credentials` (optional,
   dash-prefixed: missing file is fine). One timer per user:
   `systemctl enable --now coldsnake@otheruser.timer` runs a second schedule.

`TimeoutStartSec=infinity` (as shipped) matters: a first full pass runs for hours,
and systemd will not start a second copy while one is running.

## Protocol notes

Recovered from Icedrive's own clients; the mobile API lives at
`https://apis.icedrive.net/v3/mobile` with `User-Agent: icedrive-ios/2.3.1`.

```
POST /api  {app:ios, request:pow-new, scope:login}      -> proof-of-work challenge
POST /api  {password, email, pow_proof, request:login}  -> bearer token
GET  /user-stats                                        -> storage + bandwidth usage
GET  /collection?type=cloud&folderId=<id>               -> folder listing
GET  /collection?type=trash&folderId=0                  -> trash listing
POST /api  {request:trash-add, items:file-<id>}         -> move an item to the trash
POST /api  {request:trash-restore, items:file-<id>}     -> restore it to its folder
POST /folder-create (multipart)                         -> create a folder
GET  /geo-fileserver-list?app=ios&pow_proof=<b64>       -> signed /deposit endpoints
POST <deposit endpoint> multipart {folderId, moddate, files[]}          -> whole file
POST <deposit endpoint> multipart {folderId, moddate, unique_upload_id,
                                   files[]} + Content-Range: bytes a-b/total -> chunk
GET  /download?id=<id>                                  -> signed node url for the file
GET  /version-list?id=<id>                              -> revision list, each with its own url
POST /erase  {items:file-<id>[,file-<id>…]}             -> delete one file or a batch
```

Details that matter:

* **Proof of work** - `sha256(challenge_bytes || nonce12 || counter_be32)` must have
  at least `difficultyBits` leading zero bits; `pow_proof` is the base64 of the
  challenge fields plus the winning nonce and hash.
* **Errors arrive as HTTP 200** with `{"error": true, "code": …, "message": …}`.
  Responses are validated; without that, a failed listing looks exactly like an empty
  remote folder and everything gets re-uploaded.
* **Chunk semantics** (verified live): ranges are keyed by `unique_upload_id`;
  re-sending a range is idempotent (the file does not grow); a gap is accepted
  silently; resuming with a *different* id is rejected (`Error handling upload
  parts`). So the id is derived from destination + size + mtime and reused across
  retries and runs, and the end-of-upload size check is mandatory.
* **Client identity** - Icedrive's own clients send `X-App-Method: sync` and a stored
  `X-Icedrive-Device-Id`; ColdSnake sends both. The User-Agent matters: a desktop
  string is refused with `HTTP 403 code 5001` ("Official Icedrive mobile client
  required"), so the mobile one is required, not cosmetic.
* **No whole-tree listing** - the app's `collection-tree-full` is dead on this API
  (`code 2003` for every shape tried: routes, params, bodies, app identities and
  headers), and `/collection` accepts only `cloud`, `trash` and `shared`. Listing is
  therefore one call per folder.
* **Deleting is permanent, trashing is not** - `/erase` takes one or many ids and
  removes them for good, folders refuse to delete at all, and a trashed file keeps
  its original folder id so `trash-restore` puts it back. `POST /download-multi`,
  the older batch download route, answers `code 5000 "No files found"` for every id
  as of 2026-09-24, so `GET /download?id=` is the route that works and the batch one
  is kept only as a fallback.

## Capability comparison

Reviewed against Icedrive's GUI client to work out what a scheduled, headless sync
actually needs.

| capability | ColdSnake |
|---|---|
| proof-of-work login | yes |
| bearer token reuse | yes - cached 0600, re-login on auth error |
| device identity | yes |
| recursive listing, folder creation | yes - one call per folder (see below) |
| whole-tree listing | **no** - no `collection-tree-full`; listing stays one call per folder |
| streamed uploads, keep-alive | yes |
| chunked / resumable uploads | yes - stable id, idempotent range retries |
| cross-run upload resume | yes - chunk offsets journalled, so a killed run resumes (see `upload-journal.json`) |
| post-upload verification | yes - sizes, uploads and downloads (no hash exists to compare) |
| download | yes - `coldsnake download`, `GET /download?id=` signed node url (verified live), resumable via `Range` |
| version history download | yes - `coldsnake versions`, `download --version N` via the version's own url |
| trash / restore | yes - `coldsnake trash`, `coldsnake restore --id` (verified live) |
| batch delete | yes - one `POST /erase` with comma-joined ids (verified live) |
| storage quota check | yes - pre-flight gate + `coldsnake account` |
| retries, backoff, per-file isolation | yes |
| prune local deletions | yes - opt-in `--prune` trashes by default, `--prune-delete` erases, files only |
| exclusions / ignore patterns | yes - `--exclude` globs + `exclude` list in config |
| 2FA (TOTP / SMS / U2F) | **no** |
| encrypted folders (IceCrypto) | **no** |
| two-way sync, live folder watching | **no** |
| move / rename / file-exchange | **no** |
| sharing / public links | **no** |

No whole-tree call exists: `collection-tree-full` was probed across ~68 request
shapes and every one answers `code 2003 Invalid request`, so ColdSnake lists one
folder per call and never batches listings.

## Limitations

* **Unofficial API.** Icedrive can change or block it at any time. Failures are
  loud (exit codes, logs) rather than silent, which is the best that can be done.
* **Icedrive's ToS does not cover this.** There is no public-API or
  third-party-client clause, so nothing explicitly permits or forbids ColdSnake;
  a generic "no robots or retrieval applications" clause and a
  terminate-for-any-reason catch-all technically apply. The realistic risk is
  endpoint breakage or an account ban, not legal action. ColdSnake mirrors from
  live local trees, so the worst case costs a re-upload to another provider,
  never the data itself.
* **2FA is not implemented.** With 2FA enabled, `coldsnake login` cannot complete.
  The cached token means this only bites when the token is invalidated; otherwise
  use an account without 2FA for scheduled runs.
* **The mobile User-Agent is load-bearing.** A desktop `User-Agent` gets
  `HTTP 403` with `code 5001` ("Official Icedrive mobile client required"), so
  `icedrive-ios/2.3.1` plus `X-App-Method: sync` is what the API demands. It must
  not be "fixed" to a browser or desktop string.
* **Download is resumed and size-checked, not hash-checked.** `coldsnake download`
  asks `GET /download?id=<id>`, which returns a signed
  `https://<node>.icedrive.io/download?p=...` URL (verified live 2026-09-24), and
  streams it. The older batch route `/download-multi` stopped working the same day
  - it answers `{"code": 5000, "message": "No files found"}` for every file id,
  including one uploaded seconds earlier - so ColdSnake keeps it only as a
  fallback. Version history is fetched the same way: `coldsnake versions` reads
  `GET /version-list?id=<id>`, and `download --version N` streams that version's own
  pre-signed `url`, because a `&version=` parameter on `/download` is ignored and
  always serves the current bytes. The signed URL honours `Range` (the same trick
  go-icedrive uses), so a retry continues the partial `.tmp` instead of restarting
  a multi-GB file, and a server that ignores the range is detected and restarted
  rather than appended to.
  The finished length is compared against the size from the listing, so a truncated
  transfer fails instead of landing. There is no per-file hash to compare, so
  same-size corruption still passes.
* **Folders cannot be deleted** through this API: `/erase` reports success and the
  folder stays. Files can be permanently erased (`--prune-delete`, batch verified
  live) or moved to the trash and brought back with `coldsnake restore --id`
  (verified live). Because no folder can be deleted, empty remote directories
  linger, and the `folder-<id>` trash/restore prefixes are untested (a probe folder
  could not be cleaned up).
* **Quota numbers are unreliable.** On the account tested, `/user-stats` reports
  0 bytes used after 290 MB of uploads, so the storage gate is a safety net rather
  than a meter.
* **Cross-run resume** covers chunked uploads: `~/.config/coldsnake/upload-journal.json`
  records which chunk offsets landed, so a run killed mid-file re-sends only the
  missing chunks for the same upload id. `--no-upload-journal` disables it and the
  file can be deleted at any time. Files not larger than one chunk (streamed whole,
  ≤ 8 MiB by default) are still re-sent from the start. If every chunk was
  journalled but the run died before the journal was cleared, the folder is re-listed
  and the journal is believed only when the server's size agrees - otherwise the
  journal is dropped and the file is sent again next run. Journal entries for
  abandoned uploads are never garbage collected (they are tiny, and a changed file
  gets a new id anyway).
* **Additive unless `--prune`.** By default a file removed locally stays remote.
  With `--prune`, remote files with no local counterpart are **moved to the trash** -
  recoverable, files only, never folders, never outside the mirror's own subtree,
  and only after a complete fresh remote listing. `--prune-delete` erases them
  permanently instead (one batched `POST /erase`, per-file fallback). A wipe above
  a sanity threshold (50 files or a quarter of the mirror, whichever is larger)
  must be confirmed with `--prune-force`. Files matching your `--exclude` globs are
  never pruned. Trashed files are listed by `coldsnake trash` and brought back with
  `coldsnake restore --id <id>`.
* Single account, single config, no profiles. Proxy support is untested (urllib
  honours `http_proxy`/`https_proxy`).

## Roadmap

1. **Verify a content hash - blocked on whether the API has one.** Nothing is
   available to compare against today: the desktop client's upload request sends
   no hash field (its literal multipart fields are `unique_upload_id`, `files[]`,
   `X-Icedrive-Padding` and `Content-Range`), and listing entries carry no hash
   key. So integrity stays length-based. An earlier note here claiming the upload
   path accepts a `hashAlgorithm` field is **unconfirmed** - settling it needs one
   live probe (upload with the field, inspect the stored entry), not reverse
   engineering. Until then hashes would only catch same-size corruption.
2. Smaller items: confirm listing pagination on very large folders (~1 200 entries
   per folder is currently proven fine), structured/JSON run summaries for
   monitoring, PyPI packaging and CI.
3. **Missing features the app has and ColdSnake does not** (candidates, not
   commitments): two-way sync / live folder events, move / rename / file-exchange,
   sharing and public links, encrypted folders (IceCrypto), 2FA login, and folder
   delete (the API refuses it - `/erase` no-ops and the folder stays).

## Development

```bash
python -m unittest discover -s tests     # 85 tests, no dependencies
```

Layout:

```
src/coldsnake/client.py   API client: PoW login, listing, folders, chunked uploads,
                          trash/restore, versions, download, batch delete
src/coldsnake/sync.py     mirror logic, pre-flight checks, verification, prune
src/coldsnake/state.py    cross-run upload journal (which chunk offsets landed)
src/coldsnake/cli.py      argument parsing, config, credentials/token/device-id
tests/test_coldsnake.py   proof-of-work, chunk planning, payload validation,
                          pre-flight, mirror behaviour (against an in-memory client),
                          ranged download resume (against a local HTTP server)
tests/test_client_features.py  trash/restore, versions, batch delete, journal resume
tests/test_cli_features.py     new subcommands and flags (trash, restore, versions)
tests/test_sync_features.py    prune trashes by default, --prune-delete erases, guards
tests/test_state.py            upload-journal load/mark/clear, corrupt-file tolerance
```

Tests must pass before a change lands. The mirror tests use an in-memory fake, so
nothing touches the network; anything verified against the live API is noted in this
file rather than encoded as a test.

## Credits

Protocol details were reconstructed from Icedrive's own desktop client (its
embedded web UI and its strings) and cross-checked against
[`StarHack/go-icedrive`](https://github.com/StarHack/go-icedrive), an independent Go
client for the same mobile API. Neither is affiliated with this project.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
