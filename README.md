# Longbox

A lean, single-user, self-hosted comic & magazine server — a Komga
alternative in one Python file. Point it at a library laid out as
`Comics/Series/Issue.cbz` and `Magazines/Title/Title 2024-01.cbz`
and get a cover-grid library, a full web reader, reading progress,
and an OPDS feed for your phone or e-reader.

Runs identically two ways from the same source: **Docker** (for a
NAS) or **Windows** (double-click a `.pyw` file). Your library is
always opened read-only — Longbox never modifies, moves, or deletes
your files.

> Pairs well with the Comic & Magazine Organizer (verifies, cleans up,
> and standardizes a messy library into the folder layout Longbox
> expects) — but Longbox works with any library already organized as
> `Section/Series/Issue`.

## Features

- **Library** — cover-grid home screen, Comics / Magazines sections,
  Continue Reading, **Up Next**, and Recently Added shelves, search,
  library sort (A–Z / recently added / most unread) and an
  unread-only filter.
- **Reader** — full page-by-page web reader: keyboard, click zones,
  touch swipe, page slider, next-page preloading. **Two-page spread**
  mode (covers and landscape double-pages auto-display alone) and
  **right-to-left (manga) mode**, both remembered per series. Fit
  width for magazines.
- **Progress** — per-issue reading progress saved automatically,
  Resume button, mark-a-series read/unread in one click.
- **Collector tools** — flags **missing issues** in a series' run
  (e.g. "Missing issues: #4").
- **OPDS 1.2 catalog** at `/opds` — browse and download straight from
  Mihon/Tachiyomi, KOReader, Panels, Chunky, and other OPDS clients.
- **Formats** — `.cbz` natively; `.cbr` (needs `unrar` on PATH) and
  `.pdf` (needs PyMuPDF) are optional extras, both included in the
  Docker image out of the box.
- **Safety** — the source library is mounted/opened read-only. All of
  Longbox's own state (SQLite database, cover cache) lives in a
  separate data folder.

## Expected library layout

```
Library/
├── Comics/
│   └── Saga/
│       ├── Saga #001 (2012).cbz
│       └── Saga #002 (2012).cbz
└── Magazines/
    └── WIRED/
        ├── WIRED 2024-01.cbz
        └── WIRED 2024-02.cbz
```

One folder = one series. Anything under `Comics/` sorts by issue
number; anything under `Magazines/` sorts by date. `ComicInfo.xml`
inside an archive is read and takes priority over the filename.

## Quickstart — Docker (recommended for a NAS)

```bash
git clone https://github.com/gmwestrup/longbox.git
cd longbox
```

Edit **one line** in `docker-compose.yml` — point the first volume at
your library folder (keep `:ro`, it's what makes this safe):

```yaml
volumes:
  - /volume1/Media/Komga-Library:/library:ro
  - ./longbox-data:/config
```

```bash
docker compose up -d --build
```

Open `http://<host-ip>:8767` — it scans automatically on first boot.

> **Asustor ADM / older Docker installs:** if `docker compose` (no
> hyphen) isn't available, use `sudo docker-compose up -d --build`
> instead.

## Quickstart — Windows

1. Install [Python 3](https://www.python.org/downloads/) — tick **Add
   Python to PATH** on the first installer screen.
2. `pip install -r requirements.txt` (or just `pip install flask
   pillow` for core comic support — see below for optional formats).
3. Double-click `windows/Longbox.pyw`. Your browser opens at
   `http://127.0.0.1:8767`; pick your library folder on first run.

Longbox's own data lives in `%USERPROFILE%\.longbox`.

### Optional format support

| Format | Needs |
|---|---|
| `.cbz` | nothing extra |
| `.cbr` | `pip install rarfile` **+** [UnRAR](https://www.rarlab.com/rar_add.htm) on PATH |
| `.pdf` | `pip install pymupdf` |

The Docker image includes both by default.

## Using the OPDS feed

Point any OPDS-compatible reader at:

```
http://<host-ip>:8767/opds
```

Tested with Mihon/Tachiyomi, KOReader, Panels, and Chunky. There's no
login — keep Longbox on your LAN and don't port-forward 8767.

## Reader controls

| Action | Input |
|---|---|
| Next / previous page | → / ← , click right/left edge, swipe |
| Toggle page-turn chrome | Click the middle of the page |
| Two-page spread | `S`, or the Spread button |
| Right-to-left (manga) | RTL button |
| Fit width | `F`, or the Fit button |
| Jump to page | Bottom slider |
| Exit reader | `Esc`, or Library button |

RTL flips which physical edge advances the page, so arrow keys and
swipe direction stay intuitive in manga mode.

## Project layout

```
longbox/
├── app/longbox.py        # the entire application — Flask backend + embedded UI
├── windows/Longbox.pyw   # identical source, .pyw extension for double-click launch
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── CHANGELOG.md
```

It's intentionally one file: no build step, no frontend framework,
easy to read top to bottom, easy to fork.

## FAQ

**Does this touch my original files?**
No. The library is mounted `:ro` in Docker and opened read-only on
Windows. Longbox only ever copies bytes out to serve pages/covers/
downloads — it never writes into the library folder.

**Multi-user / accounts?**
Not currently — this is a single-user LAN tool by design. If you need
per-user accounts or Kobo sync, Komga remains the better fit; the two
can run side by side against the same read-only library while you
decide.

**Can I run this alongside Komga?**
Yes — both only need read access to the library, so there's no conflict.

**How do I add new issues?**
Drop them into the library folder in the same `Section/Series/`
layout, then click **Rescan** in the header. Unchanged files are
skipped, so rescans stay fast even on large libraries.

## License

[MIT](LICENSE)
