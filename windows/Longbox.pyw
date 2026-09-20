#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LONGBOX v2 — a self-hosted comic & magazine server
===================================================
v2: OPDS catalog for reading apps (Mihon/Tachiyomi, KOReader, Panels,
Chunky…), two-page spread + right-to-left manga modes remembered per
series, Up Next shelf, missing-issue detection, library sort/filter,
background cover pre-generation, page-cache correctness fix.

A lean, single-user replacement for Komga: point it at a library laid out
as  Section/Series/Issue.cbz  (exactly what the Comic & Magazine Organizer
produces), and it gives you:

  * a cover-grid library with Comics / Magazines sections
  * per-series shelves with read/unread state
  * a full page-by-page web reader (keyboard, click zones, touch,
    fit modes, page slider, preloading)
  * reading progress + Continue Reading shelf, stored in SQLite
  * Recently Added shelf, search, one-click rescan

The library is only ever READ. All state (database, cover cache) lives in
a separate data folder.

Requires:  Python 3.9+,  pip install flask pillow
Optional:  pip install rarfile  (+ unrar on PATH)  -> .cbr support
           pip install pymupdf                     -> .pdf support
"""

import os, re, io, json, time, sqlite3, threading, webbrowser, shutil
from pathlib import Path
from functools import lru_cache

from flask import Flask, request, jsonify, Response, send_file, abort
from PIL import Image

# ----------------------------------------------------------------------------
# Environment & optional deps
# ----------------------------------------------------------------------------
IS_DOCKER = os.environ.get("LONGBOX_DOCKER") == "1"
PORT      = int(os.environ.get("LONGBOX_PORT", "8767"))

DATA_DIR  = Path("/config") if IS_DOCKER else \
            Path(os.environ.get("LONGBOX_DATA", Path.home() / ".longbox"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
COVER_DIR = DATA_DIR / "covers"; COVER_DIR.mkdir(exist_ok=True)
DB_PATH   = DATA_DIR / "longbox.db"
CFG_PATH  = DATA_DIR / "config.json"

try:
    import rarfile
    HAVE_RAR = shutil.which("unrar") is not None or shutil.which("unrar-free") is not None
except Exception:
    rarfile, HAVE_RAR = None, False

try:
    import fitz                      # PyMuPDF
    HAVE_PDF = True
except Exception:
    fitz, HAVE_PDF = None, False

BOOK_EXTS  = {".cbz", ".cbr", ".pdf"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"}
MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
        ".avif": "image/avif"}

def load_cfg() -> dict:
    if IS_DOCKER:
        return {"library": "/library"}
    if CFG_PATH.exists():
        try:
            return json.loads(CFG_PATH.read_text())
        except Exception:
            pass
    return {"library": ""}

def save_cfg(cfg: dict):
    if not IS_DOCKER:
        CFG_PATH.write_text(json.dumps(cfg, indent=2))

CFG = load_cfg()

# ----------------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------------
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con

def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS books(
            id INTEGER PRIMARY KEY,
            path TEXT UNIQUE NOT NULL,
            section TEXT NOT NULL,
            series TEXT NOT NULL,
            title TEXT NOT NULL,
            number TEXT,
            sort_key REAL DEFAULT 0,
            year INT, month INT,
            pages INT DEFAULT 0,
            size INT, mtime REAL, added REAL
        );
        CREATE INDEX IF NOT EXISTS idx_books_series ON books(section, series);
        CREATE TABLE IF NOT EXISTS series_settings(
            section TEXT NOT NULL, series TEXT NOT NULL,
            rtl INT DEFAULT 0, spread INT DEFAULT 0, fitw INT DEFAULT 0,
            PRIMARY KEY(section, series)
        );
        CREATE TABLE IF NOT EXISTS progress(
            book_id INTEGER PRIMARY KEY REFERENCES books(id) ON DELETE CASCADE,
            page INT DEFAULT 0,
            completed INT DEFAULT 0,
            updated REAL
        );
        """)

# ----------------------------------------------------------------------------
# Archive access
# ----------------------------------------------------------------------------
def natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower()
            for t in re.split(r"(\d+)", s)]

@lru_cache(maxsize=256)
def _page_list(path: str, mtime: float) -> tuple:
    """Sorted image entry names inside an archive (or page count for PDF)."""
    ext = Path(path).suffix.lower()
    if ext == ".cbz":
        import zipfile
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist()
                     if Path(n).suffix.lower() in IMAGE_EXTS
                     and not Path(n).name.startswith(".")]
    elif ext == ".cbr" and rarfile and HAVE_RAR:
        with rarfile.RarFile(path) as rf:
            names = [n for n in rf.namelist()
                     if Path(n).suffix.lower() in IMAGE_EXTS]
    elif ext == ".pdf" and HAVE_PDF:
        with fitz.open(path) as doc:
            return tuple(str(i) for i in range(doc.page_count))
    else:
        return tuple()
    return tuple(sorted(names, key=natural_key))

def page_list(path: str) -> tuple:
    try:
        return _page_list(path, os.path.getmtime(path))
    except OSError:
        return tuple()

def read_page(path: str, n: int) -> tuple[bytes, str]:
    """Return (image bytes, mimetype) for page n of a book."""
    ext = Path(path).suffix.lower()
    names = page_list(path)
    if not names or n < 0 or n >= len(names):
        raise IndexError("page out of range")
    if ext == ".cbz":
        import zipfile
        with zipfile.ZipFile(path) as zf:
            data = zf.read(names[n])
    elif ext == ".cbr":
        with rarfile.RarFile(path) as rf:
            data = rf.read(names[n])
    elif ext == ".pdf":
        with fitz.open(path) as doc:
            pix = doc.load_page(n).get_pixmap(matrix=fitz.Matrix(2, 2))
            return pix.tobytes("png"), "image/png"
    else:
        raise IndexError("unsupported")
    return data, MIME.get(Path(names[n]).suffix.lower(), "image/jpeg")

def make_cover(book_id: int, path: str) -> Path:
    out = COVER_DIR / f"{book_id}.jpg"
    if out.exists():
        return out
    try:
        data, _ = read_page(path, 0)
        img = Image.open(io.BytesIO(data)).convert("RGB")
        img.thumbnail((440, 660))
        img.save(out, "JPEG", quality=82)
    except Exception:
        img = Image.new("RGB", (440, 660), (20, 24, 31))
        img.save(out, "JPEG", quality=82)
    return out

# ----------------------------------------------------------------------------
# ComicInfo + numbering helpers (for display & sort)
# ----------------------------------------------------------------------------
def read_comicinfo_light(path: str) -> dict:
    ext = Path(path).suffix.lower()
    try:
        data = None
        if ext == ".cbz":
            import zipfile
            with zipfile.ZipFile(path) as zf:
                for n in zf.namelist():
                    if n.lower().endswith("comicinfo.xml"):
                        data = zf.read(n); break
        elif ext == ".cbr" and rarfile and HAVE_RAR:
            with rarfile.RarFile(path) as rf:
                for n in rf.namelist():
                    if n.lower().endswith("comicinfo.xml"):
                        data = rf.read(n); break
        if not data:
            return {}
        import xml.etree.ElementTree as ET
        root = ET.fromstring(data)
        def g(t):
            el = root.find(t)
            return el.text.strip() if el is not None and el.text else None
        return {"number": g("Number"), "title": g("Title"),
                "year": g("Year"), "month": g("Month")}
    except Exception:
        return {}

NUM_RE  = re.compile(r"#\s*(\d+(?:\.\d+)?)([A-Za-z]?)")
DATE_RE = re.compile(r"\b((?:19|20)\d{2})-(\d{2})\b")
YEAR_RE = re.compile(r"\(((?:19|20)\d{2})\)")

def derive_meta(stem: str, info: dict) -> dict:
    number, year, month = info.get("number"), None, None
    for k in ("year", "month"):
        if str(info.get(k) or "").isdigit():
            if k == "year": year = int(info[k])
            else: month = int(info[k])
    m = DATE_RE.search(stem)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
    if year is None:
        m = YEAR_RE.search(stem)
        if m: year = int(m.group(1))
    if not number:
        m = NUM_RE.search(stem)
        if m: number = m.group(1) + m.group(2)
    # sort key: issue number wins; else date; else 0 (falls back to title sort)
    sort_key = 0.0
    if number:
        m = re.match(r"(\d+(?:\.\d+)?)", number)
        if m: sort_key = float(m.group(1))
    elif year:
        sort_key = year * 100 + (month or 0)
    return {"number": number, "year": year, "month": month, "sort_key": sort_key}

# ----------------------------------------------------------------------------
# Scanner
# ----------------------------------------------------------------------------
SCAN = {"running": False, "done": 0, "total": 0, "last": None}

def scan_library():
    root = Path(CFG["library"])
    if not root.is_dir():
        SCAN["running"] = False
        return
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith((".", "@"))]
        for f in filenames:
            if Path(f).suffix.lower() in BOOK_EXTS:
                files.append(Path(dirpath) / f)
    SCAN.update(done=0, total=len(files))
    con = db()
    known = {r["path"]: r for r in con.execute(
        "SELECT id, path, size, mtime FROM books")}
    seen = set()
    now = time.time()
    for i, p in enumerate(files):
        sp = str(p)
        seen.add(sp)
        st = p.stat()
        old = known.get(sp)
        if old and old["size"] == st.st_size and abs(old["mtime"] - st.st_mtime) < 1:
            SCAN["done"] = i + 1
            continue                                   # unchanged
        rel = p.relative_to(root)
        parts = rel.parts
        section = parts[0] if len(parts) >= 3 and parts[0].lower() in ("comics", "magazines") \
                  else ("Comics" if len(parts) >= 2 else "Library")
        series = parts[-2] if len(parts) >= 2 else p.stem
        if series.lower() in ("comics", "magazines"):
            series = p.stem
        info = read_comicinfo_light(sp)
        meta = derive_meta(p.stem, info)
        try:
            pages = len(page_list(sp))
        except Exception:
            pages = 0
        title = info.get("title") or p.stem
        if old:
            con.execute("""UPDATE books SET section=?,series=?,title=?,number=?,
                sort_key=?,year=?,month=?,pages=?,size=?,mtime=? WHERE id=?""",
                (section, series, title, meta["number"], meta["sort_key"],
                 meta["year"], meta["month"], pages, st.st_size, st.st_mtime,
                 old["id"]))
            (COVER_DIR / f"{old['id']}.jpg").unlink(missing_ok=True)
        else:
            con.execute("""INSERT INTO books(path,section,series,title,number,
                sort_key,year,month,pages,size,mtime,added)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sp, section, series, title, meta["number"], meta["sort_key"],
                 meta["year"], meta["month"], pages, st.st_size, st.st_mtime, now))
        SCAN["done"] = i + 1
        if i % 50 == 0:
            con.commit()
    # remove books whose files vanished
    gone = [r["id"] for pth, r in known.items() if pth not in seen]
    for gid in gone:
        con.execute("DELETE FROM books WHERE id=?", (gid,))
        (COVER_DIR / f"{gid}.jpg").unlink(missing_ok=True)
    con.commit(); con.close()
    SCAN["running"] = False
    SCAN["last"] = now
    threading.Thread(target=pregen_covers, daemon=True).start()

COVERGEN = {"running": False}
def pregen_covers():
    """Warm the cover cache so the first library load is instant."""
    if COVERGEN["running"]:
        return
    COVERGEN["running"] = True
    try:
        con = db()
        rows = con.execute("SELECT id, path FROM books").fetchall()
        con.close()
        for r in rows:
            if not (COVER_DIR / f"{r['id']}.jpg").exists():
                make_cover(r["id"], r["path"])
    finally:
        COVERGEN["running"] = False

# ----------------------------------------------------------------------------
# Flask app & API
# ----------------------------------------------------------------------------
app = Flask(__name__)

def book_row(r, prog):
    p = prog.get(r["id"], {})
    return {"id": r["id"], "title": r["title"], "number": r["number"],
            "series": r["series"], "section": r["section"],
            "year": r["year"], "month": r["month"], "pages": r["pages"],
            "page": p.get("page", 0), "completed": bool(p.get("completed", 0))}

def get_settings(con, section, series):
    r = con.execute("""SELECT rtl,spread,fitw FROM series_settings
        WHERE section=? AND series=?""", (section, series)).fetchone()
    return ({"rtl": r["rtl"], "spread": r["spread"], "fitw": r["fitw"]} if r
            else {"rtl": 0, "spread": 0, "fitw": 0})

def issue_gaps(rows) -> list:
    """Missing whole issue numbers between the lowest and highest owned."""
    nums = set()
    for r in rows:
        m = re.match(r"^(\d+)", str(r["number"] or ""))
        if m:
            nums.add(int(m.group(1)))
    if len(nums) < 2:
        return []
    lo, hi = min(nums), max(nums)
    if hi - lo > 2000:
        return []
    return [n for n in range(lo, hi) if n not in nums][:30]

def all_progress(con):
    return {r["book_id"]: {"page": r["page"], "completed": r["completed"],
                           "updated": r["updated"]}
            for r in con.execute("SELECT * FROM progress")}

@app.get("/")
def index():
    return Response(HTML, mimetype="text/html")

@app.get("/api/state")
def api_state():
    con = db()
    total = con.execute("SELECT COUNT(*) c FROM books").fetchone()["c"]
    con.close()
    return jsonify(configured=bool(CFG["library"]), library=CFG["library"],
                   docker=IS_DOCKER, scanning=SCAN["running"],
                   scan_done=SCAN["done"], scan_total=SCAN["total"],
                   books=total, rar=bool(rarfile) and HAVE_RAR, pdf=HAVE_PDF)

@app.post("/api/config")
def api_config():
    if IS_DOCKER:
        return jsonify(error="Library path is fixed by the Docker mount"), 400
    path = request.json.get("library", "").strip()
    if not Path(path).is_dir():
        return jsonify(error="Folder not found: " + path), 400
    CFG["library"] = path
    save_cfg(CFG)
    start_scan()
    return jsonify(ok=True)

def start_scan():
    if SCAN["running"]:
        return False
    SCAN["running"] = True
    threading.Thread(target=scan_library, daemon=True).start()
    return True

@app.post("/api/rescan")
def api_rescan():
    if not CFG["library"]:
        return jsonify(error="No library configured"), 400
    start_scan()
    return jsonify(ok=True)

@app.get("/api/home")
def api_home():
    con = db()
    prog = all_progress(con)
    # continue reading: in-progress books, most recent first
    cont = []
    for bid, p in sorted(prog.items(), key=lambda kv: -(kv[1]["updated"] or 0)):
        if p["completed"] or p["page"] <= 0:
            continue
        r = con.execute("SELECT * FROM books WHERE id=?", (bid,)).fetchone()
        if r:
            cont.append(book_row(r, prog))
        if len(cont) >= 12:
            break
    recent = [book_row(r, prog) for r in con.execute(
        "SELECT * FROM books ORDER BY added DESC, id DESC LIMIT 12")]
    cont_ids = {b["id"] for b in cont}
    activity = {}
    for r in con.execute("""SELECT b.section sec, b.series ser,
            MAX(p.updated) u FROM progress p JOIN books b ON b.id=p.book_id
            WHERE p.completed=1 GROUP BY b.section, b.series"""):
        activity[(r["sec"], r["ser"])] = r["u"] or 0
    up_next = []
    for (sec, ser), _u in sorted(activity.items(), key=lambda kv: -kv[1]):
        rows = con.execute("""SELECT * FROM books WHERE section=? AND series=?
            ORDER BY sort_key, title COLLATE NOCASE""", (sec, ser)).fetchall()
        nxt = next((r for r in rows
                    if not prog.get(r["id"], {}).get("completed")
                    and not prog.get(r["id"], {}).get("page")), None)
        if nxt and nxt["id"] not in cont_ids:
            up_next.append(book_row(nxt, prog))
        if len(up_next) >= 12:
            break
    done = sum(1 for p in prog.values() if p["completed"])
    # series summaries
    series = []
    for r in con.execute("""
        SELECT section, series, COUNT(*) n, MIN(id) any_id, MAX(added) latest
        FROM books GROUP BY section, series
        ORDER BY series COLLATE NOCASE"""):
        ids = [x["id"] for x in con.execute(
            "SELECT id FROM books WHERE section=? AND series=?",
            (r["section"], r["series"]))]
        unread = sum(1 for i in ids if not prog.get(i, {}).get("completed"))
        # cover: first issue by sort order
        cov = con.execute("""SELECT id FROM books WHERE section=? AND series=?
            ORDER BY sort_key, title COLLATE NOCASE LIMIT 1""",
            (r["section"], r["series"])).fetchone()["id"]
        series.append({"section": r["section"], "series": r["series"],
                       "count": r["n"], "unread": unread, "cover": cov,
                       "latest": r["latest"]})
    total = con.execute("SELECT COUNT(*) c FROM books").fetchone()["c"]
    con.close()
    return jsonify(continue_reading=cont, up_next=up_next, recent=recent,
                   series=series,
                   totals={"books": total, "done": done,
                           "series": len(series)})

@app.get("/api/series")
def api_series():
    name, section = request.args.get("name", ""), request.args.get("section", "")
    con = db()
    prog = all_progress(con)
    rows = con.execute("""SELECT * FROM books WHERE series=? AND section=?
        ORDER BY sort_key, title COLLATE NOCASE""", (name, section)).fetchall()
    settings = get_settings(con, section, name)
    gaps = issue_gaps(rows)
    con.close()
    books = [book_row(r, prog) for r in rows]
    resume = next((b["id"] for b in books if not b["completed"]), None)
    return jsonify(books=books, settings=settings, gaps=gaps, resume=resume)

@app.post("/api/series_settings")
def api_series_settings():
    d = request.json
    with db() as con:
        cur = get_settings(con, d["section"], d["series"])
        for k in ("rtl", "spread", "fitw"):
            if k in d:
                cur[k] = 1 if d[k] else 0
        con.execute("""INSERT INTO series_settings(section,series,rtl,spread,fitw)
            VALUES(?,?,?,?,?) ON CONFLICT(section,series) DO UPDATE SET
            rtl=excluded.rtl, spread=excluded.spread, fitw=excluded.fitw""",
            (d["section"], d["series"], cur["rtl"], cur["spread"], cur["fitw"]))
    return jsonify(settings=cur)

@app.get("/api/book/<int:bid>")
def api_book(bid):
    con = db()
    r = con.execute("SELECT * FROM books WHERE id=?", (bid,)).fetchone()
    if not r:
        con.close(); abort(404)
    prog = all_progress(con)
    # prev/next issue in the same series
    sib = con.execute("""SELECT id FROM books WHERE series=? AND section=?
        ORDER BY sort_key, title COLLATE NOCASE""",
        (r["series"], r["section"])).fetchall()
    ids = [x["id"] for x in sib]
    i = ids.index(bid)
    out = book_row(r, prog)
    out["prev"] = ids[i-1] if i > 0 else None
    out["next"] = ids[i+1] if i < len(ids)-1 else None
    out["settings"] = get_settings(con, r["section"], r["series"])
    con.close()
    return jsonify(out)

@app.get("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify(books=[])
    con = db()
    prog = all_progress(con)
    rows = con.execute("""SELECT * FROM books
        WHERE series LIKE ? OR title LIKE ?
        ORDER BY series COLLATE NOCASE, sort_key LIMIT 100""",
        (f"%{q}%", f"%{q}%")).fetchall()
    con.close()
    return jsonify(books=[book_row(r, prog) for r in rows])

@app.get("/api/cover/<int:bid>")
def api_cover(bid):
    con = db()
    r = con.execute("SELECT path FROM books WHERE id=?", (bid,)).fetchone()
    con.close()
    if not r:
        abort(404)
    return send_file(make_cover(bid, r["path"]), mimetype="image/jpeg",
                     max_age=86400)

@app.get("/api/page/<int:bid>/<int:n>")
def api_page(bid, n):
    con = db()
    r = con.execute("SELECT path FROM books WHERE id=?", (bid,)).fetchone()
    con.close()
    if not r:
        abort(404)
    try:
        data, mt = read_page(r["path"], n)
    except Exception:
        abort(404)
    resp = Response(data, mimetype=mt)
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp

@app.post("/api/progress")
def api_progress():
    d = request.json
    bid, page = int(d["book_id"]), int(d.get("page", 0))
    completed = 1 if d.get("completed") else 0
    with db() as con:
        con.execute("""INSERT INTO progress(book_id,page,completed,updated)
            VALUES(?,?,?,?) ON CONFLICT(book_id) DO UPDATE SET
            page=excluded.page, completed=excluded.completed,
            updated=excluded.updated""", (bid, page, completed, time.time()))
    return jsonify(ok=True)

@app.post("/api/mark")
def api_mark():
    d = request.json
    read = 1 if d.get("read") else 0
    with db() as con:
        if "book_id" in d:
            ids = [int(d["book_id"])]
        else:
            ids = [r["id"] for r in con.execute(
                "SELECT id FROM books WHERE series=? AND section=?",
                (d["series"], d["section"]))]
        for bid in ids:
            pages = con.execute("SELECT pages FROM books WHERE id=?",
                                (bid,)).fetchone()["pages"]
            con.execute("""INSERT INTO progress(book_id,page,completed,updated)
                VALUES(?,?,?,?) ON CONFLICT(book_id) DO UPDATE SET
                page=excluded.page, completed=excluded.completed,
                updated=excluded.updated""",
                (bid, (pages - 1 if read else 0), read, time.time()))
    return jsonify(ok=True)

BOOK_MIME = {".cbz": "application/vnd.comicbook+zip",
             ".cbr": "application/vnd.comicbook-rar",
             ".pdf": "application/pdf"}

@app.get("/api/download/<int:bid>")
def api_download(bid):
    con = db()
    r = con.execute("SELECT path FROM books WHERE id=?", (bid,)).fetchone()
    con.close()
    if not r:
        abort(404)
    p = Path(r["path"])
    return send_file(p, mimetype=BOOK_MIME.get(p.suffix.lower(),
                     "application/octet-stream"),
                     as_attachment=True, download_name=p.name)

# ---------------------------------------------------------------------------
# OPDS 1.2 catalog — lets Mihon/Tachiyomi, KOReader, Panels, Chunky etc.
# browse and download straight from Longbox.
# ---------------------------------------------------------------------------
import urllib.parse as _up
from xml.sax.saxutils import escape as _xe

OPDS_NS = ('xmlns="http://www.w3.org/2005/Atom" '
           'xmlns:opds="http://opds-spec.org/2010/catalog"')
OPDS_MT_NAV = "application/atom+xml;profile=opds-catalog;kind=navigation"
OPDS_MT_ACQ = "application/atom+xml;profile=opds-catalog;kind=acquisition"

def _opds_feed(title, fid, entries, kind="navigation"):
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    body = (f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<feed {OPDS_NS}>'
            f'<id>longbox:{_xe(fid)}</id><title>{_xe(title)}</title>'
            f'<updated>{now}</updated>'
            f'<link rel="start" href="/opds" type="{OPDS_MT_NAV}"/>'
            + "".join(entries) + "</feed>")
    mt = OPDS_MT_NAV if kind == "navigation" else OPDS_MT_ACQ
    return Response(body, mimetype=mt)

def _nav_entry(title, href, sub=""):
    return (f'<entry><title>{_xe(title)}</title><id>longbox:{_xe(href)}</id>'
            f'<updated>{time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}</updated>'
            f'<content type="text">{_xe(sub)}</content>'
            f'<link rel="subsection" href="{_xe(href)}" type="{OPDS_MT_ACQ}"/>'
            f'</entry>')

def _book_entry(r):
    p = Path(r["path"])
    mt = BOOK_MIME.get(p.suffix.lower(), "application/octet-stream")
    upd = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(r["mtime"] or 0))
    return (f'<entry><title>{_xe(r["title"])}</title>'
            f'<id>longbox:book:{r["id"]}</id><updated>{upd}</updated>'
            f'<content type="text">{_xe(r["series"])}'
            f'{" #" + _xe(r["number"]) if r["number"] else ""}</content>'
            f'<link rel="http://opds-spec.org/image/thumbnail" '
            f'href="/api/cover/{r["id"]}" type="image/jpeg"/>'
            f'<link rel="http://opds-spec.org/image" '
            f'href="/api/cover/{r["id"]}" type="image/jpeg"/>'
            f'<link rel="http://opds-spec.org/acquisition" '
            f'href="/api/download/{r["id"]}" type="{mt}"/>'
            f'</entry>')

@app.get("/opds")
def opds_root():
    con = db()
    sections = [r["section"] for r in con.execute(
        "SELECT DISTINCT section FROM books ORDER BY section")]
    con.close()
    entries = [_nav_entry("Recently added", "/opds/recent", "Latest arrivals")]
    entries += [_nav_entry(sec, "/opds/section/" + _up.quote(sec),
                           "Browse " + sec) for sec in sections]
    return _opds_feed("Longbox", "root", entries)

@app.get("/opds/recent")
def opds_recent():
    con = db()
    rows = con.execute(
        "SELECT * FROM books ORDER BY added DESC, id DESC LIMIT 40").fetchall()
    con.close()
    return _opds_feed("Recently added", "recent",
                      [_book_entry(r) for r in rows], kind="acquisition")

@app.get("/opds/section/<path:section>")
def opds_section(section):
    con = db()
    rows = con.execute("""SELECT series, COUNT(*) n FROM books WHERE section=?
        GROUP BY series ORDER BY series COLLATE NOCASE""", (section,)).fetchall()
    con.close()
    entries = [_nav_entry(r["series"],
               "/opds/series/" + _up.quote(section) + "/" + _up.quote(r["series"]),
               f'{r["n"]} issues') for r in rows]
    return _opds_feed(section, "section:" + section, entries)

@app.get("/opds/series/<path:section>/<path:name>")
def opds_series(section, name):
    con = db()
    rows = con.execute("""SELECT * FROM books WHERE section=? AND series=?
        ORDER BY sort_key, title COLLATE NOCASE""", (section, name)).fetchall()
    con.close()
    return _opds_feed(name, f"series:{section}:{name}",
                      [_book_entry(r) for r in rows], kind="acquisition")

@app.get("/api/browse")
def api_browse():
    if IS_DOCKER:
        return jsonify(error="Library path is fixed by the Docker mount"), 400
    p = request.args.get("path", "")
    if not p:
        if os.name == "nt":
            import string
            from ctypes import windll
            drives, mask = [], windll.kernel32.GetLogicalDrives()
            for i, letter in enumerate(string.ascii_uppercase):
                if mask & (1 << i):
                    drives.append(letter + ":\\")
            return jsonify(path="", dirs=drives)
        p = "/"
    try:
        base = Path(p)
        dirs = sorted([d.name for d in base.iterdir()
                       if d.is_dir() and not d.name.startswith((".", "@", "$"))],
                      key=str.lower)
        return jsonify(path=str(base),
                       parent=str(base.parent) if base.parent != base else None,
                       dirs=dirs)
    except OSError as e:
        return jsonify(error=str(e)), 400

# ----------------------------------------------------------------------------
# Embedded UI
# ----------------------------------------------------------------------------
HTML = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Longbox</title>
<link href="https://fonts.googleapis.com/css2?family=Archivo+Black&family=Barlow:wght@400;500;600;700&family=Barlow+Condensed:wght@600;700&display=swap" rel="stylesheet">
<style>
:root{
  --ink:#14181F; --paper:#FAFAF7; --card:#FFF;
  --cyan:#00A6D6; --magenta:#E0218A; --yellow:#F5C518;
  --grey:#6B7280; --line:#D8D6CF; --ok:#188A4C; --bad:#C21807;
  --shadow:4px 4px 0 var(--ink);
}
*{box-sizing:border-box}
html,body{margin:0;background:var(--paper);color:var(--ink);
  font:15px/1.45 "Barlow",system-ui,sans-serif}
::selection{background:var(--yellow)}
a{color:inherit;text-decoration:none}

header{background:var(--ink);color:#fff;padding:16px 26px;
  background-image:radial-gradient(rgba(255,255,255,.14) 1.2px,transparent 1.3px);
  background-size:14px 14px;display:flex;align-items:center;gap:20px;flex-wrap:wrap;
  position:sticky;top:0;z-index:30}
h1{margin:0;font-family:"Archivo Black",sans-serif;font-size:clamp(20px,2.6vw,30px);
  letter-spacing:.5px;text-transform:uppercase;transform:skewX(-6deg);cursor:pointer;
  text-shadow:2px 2px 0 var(--cyan),4px 4px 0 var(--magenta)}
.hsearch{margin-left:auto;display:flex;gap:10px;align-items:center}
.hsearch input{padding:8px 12px;border:2px solid #fff;background:var(--ink);
  color:#fff;font:14px "Barlow";width:min(260px,40vw);outline:none}
.hsearch input:focus{background:#20242c}
.hbtn{border:2px solid #fff;background:transparent;color:#fff;cursor:pointer;
  font:700 12px "Barlow Condensed";letter-spacing:1.6px;text-transform:uppercase;
  padding:8px 14px}
.hbtn:hover{background:var(--yellow);color:var(--ink);border-color:var(--yellow)}
#scanchip{font:700 11px "Barlow Condensed";letter-spacing:1.4px;color:var(--yellow);
  text-transform:uppercase;display:none}

main{max-width:1500px;margin:26px auto 60px;padding:0 24px}
h2{font-family:"Barlow Condensed";font-weight:700;font-size:17px;letter-spacing:2.2px;
  text-transform:uppercase;margin:26px 0 14px;display:flex;align-items:center;gap:12px}
h2::after{content:"";flex:1;border-top:2px solid var(--ink)}

/* section tabs */
.sect{display:flex;gap:8px;margin:18px 0 6px;flex-wrap:wrap}
.sect button{border:2px solid var(--ink);background:#EFEDE6;padding:7px 16px;
  cursor:pointer;font:600 13px "Barlow Condensed";letter-spacing:1.4px;
  text-transform:uppercase}
.sect button.on{background:var(--yellow);font-weight:700}

/* cover grids */
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(158px,1fr));gap:22px}
.shelf{display:grid;grid-auto-flow:column;grid-auto-columns:150px;gap:18px;
  overflow-x:auto;padding:4px 2px 12px}
.bk{cursor:pointer;position:relative}
.bk .cov{position:relative;border:2px solid var(--ink);box-shadow:var(--shadow);
  background:#20242c;aspect-ratio:2/3;overflow:hidden;
  transition:transform .08s, box-shadow .08s}
.bk:hover .cov{transform:translate(-2px,-2px);box-shadow:6px 6px 0 var(--ink)}
.bk img{width:100%;height:100%;object-fit:cover;display:block}
.bk .nm{font:600 13px "Barlow";margin-top:8px;line-height:1.25;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.bk .sub{font:600 11px "Barlow Condensed";letter-spacing:1.2px;color:var(--grey);
  text-transform:uppercase;margin-top:2px}
.chip{position:absolute;top:-8px;right:-8px;background:var(--yellow);
  border:2px solid var(--ink);font:700 11px "Barlow Condensed";letter-spacing:.5px;
  padding:2px 8px;z-index:2}
.done{position:absolute;top:-8px;right:-8px;background:var(--ok);color:#fff;
  border:2px solid var(--ink);font:700 11px "Barlow Condensed";padding:2px 7px;z-index:2}
.pbar{position:absolute;left:0;right:0;bottom:0;height:6px;background:rgba(20,24,31,.55)}
.pbar i{display:block;height:100%;background:var(--cyan)}

/* series header */
.shead{display:flex;gap:24px;align-items:flex-start;flex-wrap:wrap;margin-top:22px}
.shead .cov{width:190px;flex:none;border:2px solid var(--ink);box-shadow:var(--shadow)}
.shead .cov img{width:100%;display:block;aspect-ratio:2/3;object-fit:cover}
.shead h2{margin:0 0 6px;font-size:26px}
.shead h2::after{display:none}
.smeta{font:600 13px "Barlow Condensed";letter-spacing:1.6px;text-transform:uppercase;
  color:var(--grey);margin-bottom:14px}
.btn{border:2px solid var(--ink);background:var(--yellow);color:var(--ink);
  font:700 13px "Barlow Condensed";letter-spacing:1.6px;text-transform:uppercase;
  padding:9px 16px;cursor:pointer;box-shadow:3px 3px 0 var(--ink);margin-right:10px}
.btn.ghost{background:#fff}
.btn:active{transform:translate(3px,3px);box-shadow:0 0 0 var(--ink)}
.crumb{font:700 12px "Barlow Condensed";letter-spacing:1.6px;text-transform:uppercase;
  color:var(--grey);cursor:pointer;margin-top:20px;display:inline-block}
.crumb:hover{color:var(--ink)}
.empty{border:2px dashed var(--line);padding:40px;text-align:center;color:var(--grey);
  margin-top:20px}
.gaps{background:#FFF6D6;border:2px solid var(--ink);box-shadow:3px 3px 0 var(--ink);
  padding:10px 14px;margin:14px 0 0;font:600 13px "Barlow";display:inline-block}
.gaps b{font-family:"Barlow Condensed";letter-spacing:1.4px;text-transform:uppercase}
.ctrl{display:flex;gap:14px;align-items:center;margin:0 0 16px;flex-wrap:wrap}
.ctrl select{border:2px solid var(--ink);background:#fff;padding:6px 10px;
  font:600 13px "Barlow Condensed";letter-spacing:1px;text-transform:uppercase;
  cursor:pointer}
.ctrl label{display:flex;gap:7px;align-items:center;font:600 13px "Barlow Condensed";
  letter-spacing:1px;text-transform:uppercase;cursor:pointer}
.ctrl input{width:15px;height:15px;accent-color:var(--ink)}
.stattag{font:600 12px "Barlow Condensed";letter-spacing:1.4px;color:var(--grey);
  text-transform:uppercase}

/* setup panel */
.panel{background:var(--card);border:2px solid var(--ink);box-shadow:var(--shadow);
  padding:22px;max-width:720px;margin:40px auto}
.panel h2::after{display:none}
.panel input[type=text]{width:100%;padding:10px 12px;border:2px solid var(--ink);
  font:14px "Barlow";margin:8px 0 14px}
.frow{display:flex;gap:0}
.frow input{flex:1}
.browse{border:2px solid var(--ink);border-left:none;background:#fff;padding:0 16px;
  font:700 13px "Barlow Condensed";letter-spacing:1px;cursor:pointer;
  text-transform:uppercase;margin:8px 0 14px}
.browse:hover{background:var(--yellow)}

/* ---------------- reader ---------------- */
#reader{position:fixed;inset:0;background:#0B0D10;display:none;z-index:100;
  user-select:none}
#reader.on{display:block}
#rstage{display:flex;align-items:center;justify-content:center;
  height:100%;width:100%;gap:0}
#rstage img{max-height:100%;max-width:100%;object-fit:contain;display:block}
#rstage.two img{max-width:50%}
#reader.fitw #rstage{height:auto;display:block}
#reader.fitw #rstage img{max-height:none;max-width:none;width:min(100%,1100px);
  height:auto;margin:0 auto}
#rscroll{position:absolute;inset:0;overflow:auto}
#rtop,#rbot{position:absolute;left:0;right:0;background:rgba(11,13,16,.92);
  color:#fff;display:flex;align-items:center;gap:16px;padding:10px 18px;z-index:5;
  transition:opacity .2s, transform .2s;border-bottom:2px solid #fff}
#rtop{top:0}
#rbot{bottom:0;top:auto;border-bottom:none;border-top:2px solid #fff}
#reader.zen #rtop{opacity:0;transform:translateY(-100%);pointer-events:none}
#reader.zen #rbot{opacity:0;transform:translateY(100%);pointer-events:none}
#rtop .t{font:700 15px "Barlow Condensed";letter-spacing:1.2px;text-transform:uppercase;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rbtn{border:2px solid #fff;background:transparent;color:#fff;cursor:pointer;
  font:700 12px "Barlow Condensed";letter-spacing:1.4px;text-transform:uppercase;
  padding:6px 12px;white-space:nowrap}
.rbtn:hover{background:var(--yellow);color:var(--ink);border-color:var(--yellow)}
.rbtn.on{background:var(--yellow);color:var(--ink);border-color:var(--yellow)}
#rcount{font:700 13px "Barlow Condensed";letter-spacing:1.4px;white-space:nowrap}
#rslider{flex:1;accent-color:var(--yellow)}
.zone{position:absolute;top:0;bottom:0;width:30%;z-index:3;cursor:pointer}
#zprev{left:0}#znext{right:0}
#zmid{left:30%;width:40%;cursor:default}
#rend{position:absolute;inset:0;display:none;align-items:center;justify-content:center;
  z-index:6;background:rgba(11,13,16,.9)}
#rend .box{background:var(--paper);color:var(--ink);border:2px solid #fff;
  box-shadow:6px 6px 0 #000;padding:28px 34px;text-align:center;max-width:420px}
#rend h3{font-family:"Barlow Condensed";letter-spacing:2px;text-transform:uppercase;
  margin:0 0 16px}
.toast{position:fixed;top:16px;right:16px;background:var(--ink);color:#fff;
  padding:12px 18px;border:2px solid #fff;box-shadow:var(--shadow);z-index:200;
  font:600 14px "Barlow";display:none;max-width:380px}
.modal{position:fixed;inset:0;background:rgba(20,24,31,.55);display:none;
  align-items:center;justify-content:center;z-index:150}
.modal.on{display:flex}
.mbox{background:var(--card);border:2px solid var(--ink);box-shadow:var(--shadow);
  width:min(560px,92vw);max-height:76vh;display:flex;flex-direction:column}
.mbox h3{margin:0;padding:14px 18px;font-family:"Barlow Condensed";font-size:16px;
  letter-spacing:1.8px;text-transform:uppercase;border-bottom:2px solid var(--ink)}
.mpath{padding:10px 18px;font-family:ui-monospace,Consolas,monospace;font-size:12.5px;
  background:#F2F0E9;border-bottom:1px solid var(--line);word-break:break-all}
.mlist{overflow:auto;flex:1;padding:6px 0;min-height:200px}
.mlist div{padding:7px 20px;cursor:pointer;font-size:14px}
.mlist div:hover{background:#FBF7E4}
.mfoot{display:flex;gap:10px;justify-content:flex-end;padding:12px 18px;
  border-top:2px solid var(--ink)}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
@media (max-width:640px){.grid{grid-template-columns:repeat(auto-fill,minmax(118px,1fr));gap:14px}
  .shelf{grid-auto-columns:118px}}
</style>
</head>
<body>
<header>
  <h1 onclick="location.hash='#/'">Longbox</h1>
  <span id="scanchip">Scanning…</span>
  <div class="hsearch">
    <input type="search" id="q" placeholder="Search series or issue…">
    <button class="hbtn" id="rescan">Rescan</button>
  </div>
</header>
<main id="app"></main>

<div id="reader">
  <div id="rscroll"><div id="rstage"></div></div>
  <div class="zone" id="zprev"></div>
  <div class="zone" id="zmid"></div>
  <div class="zone" id="znext"></div>
  <div id="rtop">
    <button class="rbtn" id="rback">&larr; Library</button>
    <div class="t" id="rtitle"></div>
    <button class="rbtn" id="rspread" style="margin-left:auto" title="Two-page spread">Spread</button>
    <button class="rbtn" id="rrtl" title="Right-to-left (manga)">RTL</button>
    <button class="rbtn" id="rfit">Fit width</button>
  </div>
  <div id="rbot">
    <span id="rcount"></span>
    <input type="range" id="rslider" min="0" value="0">
  </div>
  <div id="rend"><div class="box">
    <h3>End of issue</h3>
    <button class="btn" id="rnext2">Next issue &rarr;</button>
    <button class="btn ghost" id="rback2">Back to series</button>
  </div></div>
</div>
<div class="modal" id="modal">
  <div class="mbox">
    <h3>Choose your library folder</h3>
    <div class="mpath" id="mpath"></div>
    <div class="mlist" id="mlist"></div>
    <div class="mfoot">
      <button class="btn ghost" id="mcancel">Cancel</button>
      <button class="btn" id="mok">Use this folder</button>
    </div>
  </div>
</div>
<div class="toast" id="toast"></div>

<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function toast(m,ms=3500){const t=$('toast');t.textContent=m;t.style.display='block';
  clearTimeout(t._h);t._h=setTimeout(()=>t.style.display='none',ms);}
async function api(u,b){const r=await fetch(u,b?{method:'POST',
  headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}:{});
  const d=await r.json().catch(()=>({}));
  if(!r.ok){toast(d.error||'Request failed');throw new Error(d.error);}return d;}

let ST={configured:false}, sectFilter='All', sortBy='az', unreadOnly=false;

/* ---------- rendering helpers ---------- */
function bookCard(b,showSeries){
  const pct=b.pages?Math.round(100*(b.page+1)/b.pages):0;
  const label=b.number?('#'+b.number):(b.year?(b.year+(b.month?'-'+String(b.month).padStart(2,'0'):'')):'');
  return `<div class="bk" onclick="location.hash='#/read/${b.id}'">
    <div class="cov"><img loading="lazy" src="/api/cover/${b.id}">
      ${b.completed?'<span class="done">&#10003;</span>':''}
      ${!b.completed&&b.page>0?`<div class="pbar"><i style="width:${pct}%"></i></div>`:''}
    </div>
    <div class="nm">${esc(showSeries?b.series:b.title)}</div>
    <div class="sub">${esc(showSeries?(label||b.title):(label||''))}</div></div>`;
}
function seriesCard(s){
  return `<div class="bk" onclick="location.hash='#/series/${encodeURIComponent(s.section)}/${encodeURIComponent(s.series)}'">
    <div class="cov"><img loading="lazy" src="/api/cover/${s.cover}">
      ${s.unread?`<span class="chip">${s.unread} new</span>`:'<span class="done">&#10003;</span>'}
    </div>
    <div class="nm">${esc(s.series)}</div>
    <div class="sub">${s.count} issue${s.count>1?'s':''} &middot; ${esc(s.section)}</div></div>`;
}

/* ---------- views ---------- */
async function viewHome(){
  const d=await api('/api/home');
  let h='';
  if(d.continue_reading.length){
    h+='<h2>Continue reading</h2><div class="shelf">'+
       d.continue_reading.map(b=>bookCard(b,true)).join('')+'</div>';
  }
  if(d.up_next&&d.up_next.length){
    h+='<h2>Up next</h2><div class="shelf">'+
       d.up_next.map(b=>bookCard(b,true)).join('')+'</div>';
  }
  if(d.recent.length){
    h+='<h2>Recently added</h2><div class="shelf">'+
       d.recent.map(b=>bookCard(b,true)).join('')+'</div>';
  }
  const t=d.totals||{};
  const sections=['All',...new Set(d.series.map(s=>s.section))];
  h+=`<h2>Library <span class="stattag">${t.series||0} series &middot; ${t.books||0} issues &middot; ${t.done||0} finished</span></h2>`;
  h+='<div class="sect">'+sections.map(s=>
    `<button class="${s===sectFilter?'on':''}" onclick="sectFilter='${esc(s)}';route()">${esc(s)}</button>`).join('')+'</div>';
  h+=`<div class="ctrl">
    <select id="sortsel">
      <option value="az">Sort: A&ndash;Z</option>
      <option value="recent">Sort: Recently added</option>
      <option value="unread">Sort: Most unread</option>
    </select>
    <label><input type="checkbox" id="unreadonly" ${unreadOnly?'checked':''}> Unread only</label>
  </div>`;
  let vis=d.series.filter(s=>sectFilter==='All'||s.section===sectFilter);
  if(unreadOnly)vis=vis.filter(s=>s.unread>0);
  if(sortBy==='recent')vis=[...vis].sort((a,b)=>(b.latest||0)-(a.latest||0));
  else if(sortBy==='unread')vis=[...vis].sort((a,b)=>b.unread-a.unread);
  h+=vis.length?'<div class="grid">'+vis.map(seriesCard).join('')+'</div>'
    :'<div class="empty">No series here yet. Add files to the library folder and hit Rescan.</div>';
  $('app').innerHTML=h;
  $('sortsel').value=sortBy;
  $('sortsel').onchange=e=>{sortBy=e.target.value;viewHome();};
  $('unreadonly').onchange=e=>{unreadOnly=e.target.checked;viewHome();};
}
async function viewSeries(section,name){
  const d=await api(`/api/series?section=${encodeURIComponent(section)}&name=${encodeURIComponent(name)}`);
  const unread=d.books.filter(b=>!b.completed).length;
  const cov=d.books[0]?d.books[0].id:0;
  $('app').innerHTML=`<span class="crumb" onclick="location.hash='#/'">&larr; Library</span>
  <div class="shead">
    <div class="cov"><img src="/api/cover/${cov}"></div>
    <div style="flex:1;min-width:260px">
      <h2>${esc(name)}</h2>
      <div class="smeta">${esc(section)} &middot; ${d.books.length} issues &middot; ${unread} unread</div>
      ${d.resume?`<button class="btn" onclick="location.hash='#/read/${d.resume}'">Resume &#9654;</button>`:''}
      <button class="btn ghost" onclick="markSeries(true)">Mark all read</button>
      <button class="btn ghost" onclick="markSeries(false)">Mark all unread</button>
      ${d.gaps&&d.gaps.length?`<div class="gaps"><b>Missing issues:</b> ${d.gaps.map(g=>'#'+g).join(', ')}</div>`:''}
    </div>
  </div>
  <h2 style="margin-top:30px">Issues</h2>
  <div class="grid">${d.books.map(b=>bookCard(b,false)).join('')}</div>`;
  window.markSeries=async read=>{
    await api('/api/mark',{series:name,section,read}); route();};
}
async function viewSearch(q){
  const d=await api('/api/search?q='+encodeURIComponent(q));
  $('app').innerHTML=`<span class="crumb" onclick="location.hash='#/'">&larr; Library</span>
    <h2>Search: ${esc(q)}</h2>`+
    (d.books.length?'<div class="grid">'+d.books.map(b=>bookCard(b,true)).join('')+'</div>'
     :'<div class="empty">Nothing matched.</div>');
}
function viewSetup(){
  $('app').innerHTML=`<div class="panel">
    <h2>Point Longbox at your library</h2>
    <p>Choose the folder that contains your <b>Comics</b> and <b>Magazines</b>
    folders (the output of the Comic &amp; Magazine Organizer works perfectly).
    Longbox only ever reads it.</p>
    <div class="frow"><input type="text" id="libpath" placeholder="D:\\Komga\\Library">
      <button class="browse" id="libbrowse">Browse</button></div>
    <button class="btn" id="libsave">Save &amp; scan</button></div>`;
  $('libsave').onclick=async ()=>{
    await api('/api/config',{library:$('libpath').value});
    toast('Scanning library…'); pollScan();};
  $('libbrowse').onclick=()=>browse(p=>$('libpath').value=p);
}
let pickCb=null,pickPath='';
async function browse(cb,start=''){
  pickCb=cb; await openBrowse(start);
}
async function openBrowse(p){
  const d=await api('/api/browse?path='+encodeURIComponent(p));
  pickPath=d.path; $('mpath').textContent=d.path||'(choose a drive)';
  let rows='';
  if(d.parent)rows+=`<div data-p="${esc(d.parent)}">&uarr; ..</div>`;
  rows+=d.dirs.map(n=>{
    const full=d.path?(d.path.replace(/[\\/]+$/,'')+(d.path.includes('\\')?'\\':'/')+n):n;
    return `<div data-p="${esc(full)}">&#128193; ${esc(n)}</div>`;}).join('');
  $('mlist').innerHTML=rows||'<div style="color:var(--grey)">No subfolders</div>';
  document.querySelectorAll('#mlist div[data-p]').forEach(el=>
    el.onclick=()=>openBrowse(el.dataset.p));
  $('modal').classList.add('on');
}
document.addEventListener('click',e=>{
  if(e.target.id==='mcancel')$('modal').classList.remove('on');
  if(e.target.id==='mok'){if(pickCb)pickCb(pickPath);$('modal').classList.remove('on');}
});

/* ---------- router ---------- */
async function route(){
  closeReader(false);
  if(!ST.configured){viewSetup();return;}
  const h=location.hash||'#/';
  const m=h.match(/^#\/read\/(\d+)/);
  if(m){openReader(+m[1]);return;}
  const s=h.match(/^#\/series\/([^/]+)\/(.+)$/);
  if(s){viewSeries(decodeURIComponent(s[1]),decodeURIComponent(s[2]));return;}
  const q=h.match(/^#\/search\/(.+)$/);
  if(q){viewSearch(decodeURIComponent(q[1]));return;}
  viewHome();
}
window.addEventListener('hashchange',route);
$('q').addEventListener('keydown',e=>{
  if(e.key==='Enter'&&$('q').value.trim())
    location.hash='#/search/'+encodeURIComponent($('q').value.trim());});
$('rescan').onclick=async ()=>{await api('/api/rescan');toast('Rescanning…');pollScan();};

async function pollScan(){
  const s=await api('/api/state');
  ST=s;
  $('scanchip').style.display=s.scanning?'inline':'none';
  if(s.scanning){
    $('scanchip').textContent=`Scanning ${s.scan_done}/${s.scan_total}`;
    setTimeout(pollScan,800);
  } else route();
}

/* ---------- reader ---------- */
let R={id:null,book:null,page:0,shown:1,timer:null,set:{rtl:0,spread:0,fitw:0}};
async function openReader(id){
  const b=await api('/api/book/'+id);
  R={id,book:b,page:(b.completed?0:b.page)||0,shown:1,timer:null,
     set:Object.assign({rtl:0,spread:0,fitw:0},b.settings||{})};
  $('reader').classList.add('on');
  $('rend').style.display='none';
  $('rtitle').textContent=`${b.series} — ${b.title}`;
  $('rslider').max=Math.max(0,b.pages-1);
  document.body.style.overflow='hidden';
  applySet(); show(R.page,false);
}
function closeReader(nav=true){
  if(!$('reader').classList.contains('on'))return;
  $('reader').classList.remove('on');
  document.body.style.overflow='';
  if(nav&&R.book)location.hash='#/series/'+encodeURIComponent(R.book.section)+'/'+
    encodeURIComponent(R.book.series);
}
function applySet(){
  $('reader').classList.toggle('fitw',!!R.set.fitw);
  $('rfit').classList.toggle('on',!!R.set.fitw);
  $('rspread').classList.toggle('on',!!R.set.spread);
  $('rrtl').classList.toggle('on',!!R.set.rtl);
  $('rfit').textContent=R.set.fitw?'Fit page':'Fit width';
}
function saveSet(){if(R.book)api('/api/series_settings',
  {section:R.book.section,series:R.book.series,...R.set});}
const pageURL=n=>`/api/page/${R.id}/${n}`;
function loadImg(n){return new Promise(res=>{const im=new Image();
  im.onload=()=>res(im);im.onerror=()=>res(im);im.src=pageURL(n);});}
const landscape=im=>im.naturalWidth>0&&im.naturalWidth>im.naturalHeight;
let showSeq=0;
async function show(n,save=true){
  const b=R.book, seq=++showSeq;
  n=Math.max(0,Math.min(n,b.pages-1));
  R.page=n; R.shown=1;
  const stage=$('rstage');
  if(R.set.spread&&!R.set.fitw){
    const a=await loadImg(n);
    if(seq!==showSeq)return;                       // user moved on
    let pair=null;
    if(n>0&&!landscape(a)&&n+1<b.pages){
      const c=await loadImg(n+1);
      if(seq!==showSeq)return;
      if(!landscape(c))pair=c;
    }
    stage.classList.toggle('two',!!pair);
    stage.replaceChildren(...(pair?(R.set.rtl?[pair,a]:[a,pair]):[a]));
    R.shown=pair?2:1;
  } else {
    stage.classList.remove('two');
    const a=new Image(); a.src=pageURL(n);
    stage.replaceChildren(a);
  }
  $('rcount').textContent=R.shown===2?`${n+1}\u2013${n+2} / ${b.pages}`:`${n+1} / ${b.pages}`;
  $('rslider').value=n;
  $('rscroll').scrollTop=0;
  new Image().src=pageURL(n+R.shown);               // preload ahead
  new Image().src=pageURL(n+R.shown+1);
  const lastShown=n+R.shown-1, done=lastShown>=b.pages-1&&b.pages>0;
  if(save){
    clearTimeout(R.timer);
    R.timer=setTimeout(()=>api('/api/progress',
      {book_id:R.id,page:lastShown,completed:done}),400);
  }
  if(done)api('/api/progress',{book_id:R.id,page:lastShown,completed:true});
}
function nextPage(){
  if(R.page+R.shown>R.book.pages-1){
    $('rend').style.display='flex';
    $('rnext2').style.display=R.book.next?'inline-block':'none';return;
  }
  show(R.page+R.shown);
}
function prevPage(){
  $('rend').style.display='none';
  show(R.page-((R.set.spread&&!R.set.fitw&&R.page>1)?2:1));
}
$('znext').onclick=()=>R.set.rtl?prevPage():nextPage();
$('zprev').onclick=()=>R.set.rtl?nextPage():prevPage();
$('zmid').onclick=()=>$('reader').classList.toggle('zen');
$('rback').onclick=()=>closeReader();
$('rback2').onclick=()=>closeReader();
$('rnext2').onclick=()=>{if(R.book.next)location.hash='#/read/'+R.book.next;};
$('rslider').oninput=e=>show(+e.target.value);
$('rfit').onclick=()=>{R.set.fitw=R.set.fitw?0:1;if(R.set.fitw)R.set.spread=0;
  applySet();saveSet();show(R.page,false);};
$('rspread').onclick=()=>{R.set.spread=R.set.spread?0:1;if(R.set.spread)R.set.fitw=0;
  applySet();saveSet();show(R.page,false);};
$('rrtl').onclick=()=>{R.set.rtl=R.set.rtl?0:1;
  applySet();saveSet();show(R.page,false);};
document.addEventListener('keydown',e=>{
  if(!$('reader').classList.contains('on'))return;
  const fwd=()=>{e.preventDefault();nextPage();}, back=()=>{e.preventDefault();prevPage();};
  if(e.key===' ')fwd();
  else if(e.key==='ArrowRight')R.set.rtl?back():fwd();
  else if(e.key==='ArrowLeft')R.set.rtl?fwd():back();
  else if(e.key==='Escape')closeReader();
  else if(e.key.toLowerCase()==='f')$('rfit').click();
  else if(e.key.toLowerCase()==='s')$('rspread').click();
});
let touchX=null;
$('rscroll').addEventListener('touchstart',e=>touchX=e.touches[0].clientX,{passive:true});
$('rscroll').addEventListener('touchend',e=>{
  if(touchX===null)return;
  const dx=e.changedTouches[0].clientX-touchX;
  if(Math.abs(dx)>60){const fwdSwipe=dx<0;
    (fwdSwipe!==!!R.set.rtl?nextPage:prevPage)();}
  touchX=null;},{passive:true});

pollScan();
</script>
</body>
</html>'''

# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------
def main():
    init_db()
    url = f"http://127.0.0.1:{PORT}"
    print("=" * 60)
    print("  LONGBOX v2 — self-hosted comic & magazine server")
    print(f"  OPDS catalog for reading apps: http://<this-host>:{PORT}/opds")
    print(f"  Open {url}" + ("" if IS_DOCKER else " (opening browser…)"))
    print(f"  Data folder: {DATA_DIR}")
    print("=" * 60)
    if CFG["library"]:
        SCAN["running"] = True
        threading.Thread(target=scan_library, daemon=True).start()
    if not IS_DOCKER and os.environ.get("LONGBOX_NO_BROWSER") != "1":
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="0.0.0.0" if IS_DOCKER else "127.0.0.1", port=PORT, debug=False)

if __name__ == "__main__":
    main()
