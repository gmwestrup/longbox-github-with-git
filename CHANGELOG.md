# Changelog

## v2.0

- **OPDS 1.2 catalog** at `/opds` — browse and download the library from
  Mihon/Tachiyomi, KOReader, Panels, Chunky, and other OPDS-aware readers.
- **Reader: two-page spread mode** — facing pages shown side by side;
  covers and landscape double-page spreads automatically display alone.
- **Reader: right-to-left (manga) mode.**
- Spread/RTL/fit-width settings are now **remembered per series**.
- **Up Next** shelf on the home screen — finishing an issue surfaces the
  next unread one in that series.
- **Missing-issue detection** on each series page (e.g. "Missing issues:
  #4") to help spot gaps in a run.
- **Resume** button on series pages jumps straight to the first unread issue.
- Library **sort** (A–Z / recently added / most unread) and an
  **unread-only** filter, plus series/issue/finished totals in the header.
- Covers are now **pre-generated in the background** after every scan,
  so the grid is instant on first load.
- **Fix:** the archive page-list cache keyed on file path only, so
  replacing a file in place (e.g. swapping in a repaired archive) could
  keep serving stale page data until restart. Now keyed on path + mtime.

## v1.0

- Initial release: cover-grid library, Comics/Magazines sections,
  per-series shelves with read/unread state, full page-by-page web
  reader (keyboard, click zones, touch, fit modes, page slider,
  preloading), reading progress with a Continue Reading shelf,
  Recently Added shelf, search, one-click rescan.
- Windows (`.pyw`, double-click) and Docker editions from one source file.
- Library mounted/opened read-only; Longbox never modifies source files.
