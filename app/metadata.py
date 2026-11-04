"""Recomp Launcher — game metadata.

Fills the detail panel's info block (synopsis + year, genre,
developer, publisher, players) from, in priority order:

  1. the user's own edits (stored per game, always win — handled
     by the caller through effective());
  2. libretro-database — RetroArch's metadata database, free, no
     key. Its on-disk layout (inspected against the real repo,
     2026-11-03):
       * PlayStation has NO per-field files for genre/publisher/
         releaseyear/maxusers; everything lives in ONE merged
         datacenter file, metadat/developer/Sony - PlayStation.dat,
         whose game blocks carry name + serial + description (a
         real prose synopsis) + developer/publisher/releaseyear/
         users/genre, keyed by the game-level serial. PSX matching
         here is by serial first — the packages carry the serial —
         with a title fallback.
       * Every other system is split into per-field DAT sets
         (metadat/genre, .../developer, .../publisher,
         .../releaseyear, .../maxusers), each entry keyed by a
         `comment` holding the No-Intro title plus the ROM crc.
         The packages cannot supply a ROM crc, so matching is by
         title: every entry whose tag-stripped core title equals
         the package title is a candidate (regional variants,
         revisions), and a field is only taken when ALL candidates
         that carry it agree — "Sonic the Hedgehog" is Action /
         Sega R&D9 / Sega / 1991 / 1 player on Genesis by
         consensus, while Super Mario World's year stays empty
         because its European (1992) and USA (1991) entries
         disagree. Nothing shown rather than a guess.
       * Cartridge systems have NO prose descriptions anywhere in
         libretro-database (there is no descriptions set; the
         no-intro/redump DATs' description field just repeats the
         name), so a cartridge synopsis can only come from the
         steps below. The Virtual Boy releaseyear file is also
         partial (a subset of the library), so VB years often
         stay empty here.
  3. RetroAchievements API fields (Publisher/Developer/Genre/
     Released) — merged in by the caller from the progress call;
  4. the package's own game.toml (a players value, where present);
  5. Wikipedia's REST summary — synopsis only, and only on a
     confident exact-title match of a video-game article.

Downloaded DAT files are cached under the app data dir
(cache/metadb/<kind>/<file>.dat). A download fetches only the
systems present in the library. Failures are reported plainly and
keep any previously cached file — no fake data, ever.
"""
import json
import re
import threading
import urllib.parse
import urllib.request
from pathlib import Path

RAW_BASE = ("https://raw.githubusercontent.com/libretro/"
            "libretro-database/master/metadat")
WIKI_API = "https://en.wikipedia.org/api/rest_v1/page/summary/"

FIELDS = ("synopsis", "year", "genre", "developer", "publisher",
          "players")
SOURCE_DB_SERIAL = "libretro-database (serial match)"
SOURCE_DB_TITLE = "libretro-database (title match)"
SOURCE_RA = "RetroAchievements"
SOURCE_PACKAGE = "package game.toml"
SOURCE_WIKI = "Wikipedia"
SOURCE_MANUAL = "your edit"

# system key -> DAT file name inside every metadat/<kind>/ folder.
SYSTEM_DB_FILE = {
    "psx": "Sony - PlayStation.dat",
    "nes": "Nintendo - Nintendo Entertainment System.dat",
    "snes": "Nintendo - Super Nintendo Entertainment System.dat",
    "gb": "Nintendo - Game Boy.dat",
    "gbc": "Nintendo - Game Boy Color.dat",
    "gba": "Nintendo - Game Boy Advance.dat",
    "n64": "Nintendo - Nintendo 64.dat",
    "genesis": "Sega - Mega Drive - Genesis.dat",
    "sms": "Sega - Master System - Mark III.dat",
    "gg": "Sega - Game Gear.dat",
    "vb": "Nintendo - Virtual Boy.dat",
}
# Per-field DAT folders and the metadata field each one carries.
# (releasemonth exists too; the block's field is Year, so months
# are not downloaded.)
FIELD_KINDS = {
    "genre": "genre",
    "developer": "developer",
    "publisher": "publisher",
    "releaseyear": "year",
    "maxusers": "players",
}
# PlayStation's fields all live in the merged datacenter file,
# which sits in the developer/ folder of the repo.
PSX_MERGED_KIND = "developer"

CACHE_DIR = None        # main.py points this at DATA_DIR/cache/metadb


def download_plan(system):
    """[(kind, filename)] needed to serve metadata for a system."""
    fname = SYSTEM_DB_FILE.get(system)
    if not fname:
        return []
    if system == "psx":
        return [(PSX_MERGED_KIND, fname)]
    return [(kind, fname) for kind in FIELD_KINDS]


def _fetch_bytes(url, timeout=90):
    req = urllib.request.Request(
        url, headers={"User-Agent": "RecompLauncher/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# ------------------------------------------------------------------
# clrmamepro DAT parsing
# ------------------------------------------------------------------
_SCALARS = ("name", "comment", "serial", "description", "developer",
            "publisher", "genre", "releaseyear", "users")
_BLOCK_RE = re.compile(r"^game \(\n(.*?)^\)\s*$", re.S | re.M)
_VALUE_RE = {
    key: re.compile(r"^\s+" + key + r'\s+"(.*?)"', re.M)
    for key in _SCALARS
}
_BARE_RE = {
    key: re.compile(r"^\s+" + key + r"\s+(\S+)", re.M)
    for key in _SCALARS
}


def parse_dat(text):
    """Parse a libretro-database DAT into a list of entry dicts
    (scalar fields only; rom crc/serial are not needed — matching
    is by game serial or title)."""
    entries = []
    for m in _BLOCK_RE.finditer(text):
        blk = m.group(1)
        entry = {}
        for key in _SCALARS:
            vm = _VALUE_RE[key].search(blk) or _BARE_RE[key].search(blk)
            if vm:
                entry[key] = vm.group(1).strip()
        if entry.get("name") or entry.get("comment"):
            entries.append(entry)
    return entries


# ------------------------------------------------------------------
# title normalization + consensus matching
# ------------------------------------------------------------------
_INVERTED_ARTICLE_RE = re.compile(r",\s*(the|a|an)\b", re.I)
_PAREN_RE = re.compile(r"\s*\([^)]*\)")
_BRACKET_RE = re.compile(r"\s*\[[^\]]*\]")


def core_title(title):
    """Tag-stripped core title for comparing a package title with
    No-Intro DAT comments: region/revision parentheticals dropped,
    inverted articles ('Legend of Zelda, The') un-inverted,
    punctuation flattened, a leading article removed."""
    s = title or ""
    s = _INVERTED_ARTICLE_RE.sub(" ", s)
    s = _PAREN_RE.sub(" ", s)
    s = _BRACKET_RE.sub(" ", s)
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    for art in ("the ", "a ", "an "):
        if s.startswith(art):
            s = s[len(art):]
            break
    return s


def _entry_title(entry):
    return entry.get("comment") or entry.get("name") or ""


def consensus(entries, title):
    """Per-field consensus over every entry whose core title
    equals the package title's. A field is returned only when all
    candidates carrying it agree on its value."""
    want = core_title(title)
    if not want:
        return {}, []
    cands = [e for e in entries if core_title(_entry_title(e)) == want]
    out = {}
    for field in ("genre", "developer", "publisher", "releaseyear",
                  "users", "description"):
        values = {e[field] for e in cands if e.get(field)}
        if len(values) == 1:
            out[field] = values.pop()
    return out, [_entry_title(e) for e in cands]


def _serial_key(serial):
    return re.sub(r"[^A-Z0-9]", "", (serial or "").upper())


# ------------------------------------------------------------------
# the database (parsed files are cached in memory; thread-safe)
# ------------------------------------------------------------------
class LibretroDB:
    def __init__(self, cache_dir=None):
        self.cache_dir = Path(cache_dir) if cache_dir else CACHE_DIR
        self._lock = threading.Lock()
        self._entries = {}          # (kind, system) -> [entry, ...]

    # ---------------------------------------------------- download
    def cached_systems(self):
        out = []
        if self.cache_dir is None:
            return out
        for system in SYSTEM_DB_FILE:
            plan = download_plan(system)
            if plan and all((self.cache_dir / kind / fname).is_file()
                            for kind, fname in plan):
                out.append(system)
        return out

    def download(self, systems, fetch=None):
        """Fetch (or refresh) the DAT files for `systems`.
        Returns {system: error-string} for the failures only; a
        failed refresh keeps the previously cached file."""
        fetch = fetch or _fetch_bytes
        errors = {}
        if self.cache_dir is None:
            return {s: "no cache directory configured" for s in systems}
        for system in systems:
            for kind, fname in download_plan(system):
                url = (f"{RAW_BASE}/{urllib.parse.quote(kind)}/"
                       f"{urllib.parse.quote(fname)}")
                target = self.cache_dir / kind / fname
                try:
                    data = fetch(url)
                    if not data or b"game (" not in data[:200000]:
                        raise ValueError("downloaded file does not "
                                         "look like a DAT")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    tmp = target.with_suffix(".part")
                    tmp.write_bytes(data)
                    tmp.replace(target)
                    with self._lock:
                        self._entries.pop((kind, system), None)
                except Exception as e:  # noqa: BLE001
                    errors.setdefault(system, str(e))
        return errors

    # ------------------------------------------------------- parse
    def _load(self, kind, system):
        key = (kind, system)
        with self._lock:
            if key in self._entries:
                return self._entries[key]
        entries = []
        fname = SYSTEM_DB_FILE.get(system)
        if fname and self.cache_dir is not None:
            path = self.cache_dir / kind / fname
            try:
                entries = parse_dat(path.read_text(errors="replace"))
            except Exception:  # noqa: BLE001
                entries = []
        with self._lock:
            self._entries[key] = entries
        return entries

    # ------------------------------------------------------ lookup
    def lookup(self, game):
        """libretro-database metadata for a scanner game record.
        Returns (values, sources): values maps field -> string,
        sources maps field -> source label. Empty when no cached
        data or no confident match."""
        system = game.get("system")
        title = game.get("title_override") or game.get("title") or ""
        values, sources = {}, {}
        if system == "psx":
            entries = self._load(PSX_MERGED_KIND, "psx")
            entry = None
            serial = _serial_key(game.get("serial"))
            if serial:
                for e in entries:
                    if _serial_key(e.get("serial")) == serial:
                        entry = e
                        break
            if entry is not None:
                self._take(entry, values, sources, SOURCE_DB_SERIAL)
                return values, sources
            got, _names = consensus(entries, title)
            self._take(got, values, sources, SOURCE_DB_TITLE)
            return values, sources
        raw_of_field = {"genre": "genre", "developer": "developer",
                        "publisher": "publisher",
                        "year": "releaseyear", "players": "users"}
        for kind, field in FIELD_KINDS.items():
            entries = self._load(kind, system)
            if not entries:
                continue
            got, _names = consensus(entries, title)
            raw = got.get(raw_of_field[field])
            if raw:
                values[field] = raw
                sources[field] = SOURCE_DB_TITLE
        return values, sources

    @staticmethod
    def _take(source, values, sources, label):
        mapping = {"description": "synopsis",
                   "releaseyear": "year", "users": "players"}
        for key, val in (source or {}).items():
            field = mapping.get(key, key)
            if field in FIELDS and val and field not in values:
                values[field] = val
                sources[field] = label


# ------------------------------------------------------------------
# package-local data (players from the package's own game.toml)
# ------------------------------------------------------------------
def package_players(game):
    """A players value from the package's own game.toml, when the
    package carries one (the PSX converter writes players = N)."""
    try:
        text = (Path(game["folder"]) / "game.toml").read_text(
            errors="replace")
    except Exception:  # noqa: BLE001
        return None
    m = re.search(r"^\s*players\s*=\s*(\d+)", text, re.M)
    return m.group(1) if m else None


def local_lookup(game):
    """Source 4 (package data) — players only."""
    players = package_players(game)
    if players:
        return {"players": players}, {"players": SOURCE_PACKAGE}
    return {}, {}


# ------------------------------------------------------------------
# Wikipedia synopsis (last resort, confident matches only)
# ------------------------------------------------------------------
def wikipedia_synopsis(title, fetch=None):
    """The lead extract of the game's Wikipedia article, or None.
    Tries '<Title> (video game)' then the bare title; accepts only
    a non-disambiguation article whose title core matches and
    whose own description says it is a video game."""
    fetch = fetch or _fetch_bytes
    want = core_title(title)
    if not want:
        return None
    for candidate in (f"{title} (video game)", title):
        url = WIKI_API + urllib.parse.quote(
            candidate.replace(" ", "_"), safe="")
        try:
            data = json.loads(fetch(url))
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(data, dict):
            continue
        if data.get("type") == "disambiguation":
            continue
        extract = (data.get("extract") or "").strip()
        if not extract:
            continue
        if core_title(data.get("title")) != want:
            continue
        # A series/franchise article is not this game's synopsis
        # (bare "Sonic the Hedgehog" resolves to the franchise
        # page) — reject it rather than show a wrong text.
        desc_l = (data.get("description") or "").lower()
        lead_l = extract[:200].lower()
        if "franchise" in desc_l or "series" in desc_l:
            continue
        if "franchise" in lead_l or " series" in lead_l:
            continue
        haystack = (desc_l + " " + lead_l)
        if "video game" not in haystack:
            continue
        return _trim_synopsis(extract)
    return None


def _trim_synopsis(text, limit=700):
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text.rfind(". ", 0, limit)
    if cut > limit // 3:
        return text[:cut + 1]
    return text[:limit].rsplit(" ", 1)[0] + "…"


# ------------------------------------------------------------------
# merge + presentation helpers
# ------------------------------------------------------------------
def new_meta():
    return {"values": {}, "sources": {}, "db_done": False,
            "wiki_done": False}


def merge_meta(game, values, sources):
    """Fill only still-empty auto fields of game['meta']."""
    meta = game.get("meta")
    if not isinstance(meta, dict) or "values" not in meta:
        meta = new_meta()
        game["meta"] = meta
    for field, val in (values or {}).items():
        if field in FIELDS and val and not meta["values"].get(field):
            meta["values"][field] = val
            meta["sources"][field] = sources.get(field, "")
    return meta


def effective(game):
    """The metadata the UI renders: manual edits win over auto
    values, field by field. Returns (values, sources) covering all
    six fields ('' when unknown)."""
    meta = game.get("meta") or {}
    auto = meta.get("values") or {}
    auto_src = meta.get("sources") or {}
    manual = game.get("meta_manual") or {}
    values, sources = {}, {}
    for field in FIELDS:
        if field in manual:
            values[field] = manual[field]
            sources[field] = SOURCE_MANUAL
        elif auto.get(field):
            values[field] = auto[field]
            sources[field] = auto_src.get(field, "")
        else:
            values[field] = ""
            sources[field] = ""
    return values, sources


def players_text(raw):
    raw = str(raw or "").strip()
    if not raw:
        return ""
    return f"{raw} player" if raw == "1" else f"{raw} players"


def facts_line(values):
    """'1995 · Platform · 1 player' — only the known parts."""
    parts = [values.get("year", ""), values.get("genre", ""),
             players_text(values.get("players", ""))]
    return " · ".join(p for p in parts if p)


def byline(values):
    """'Ubi Pictures · Ubi Soft' (developer · publisher)."""
    parts = [values.get("developer", ""), values.get("publisher", "")]
    return " · ".join(p for p in parts if p)


def compact_line(game):
    """Big Picture's one-line metadata summary for a game."""
    values, _sources = effective(game)
    facts = facts_line(values)
    syn = values.get("synopsis", "")
    if facts and syn:
        return f"{facts} — {syn}"
    return facts or syn


def source_summary(sources):
    """'libretro-database (serial match) · Wikipedia' for the
    detail panel's provenance line."""
    seen = []
    for field in FIELDS:
        src = sources.get(field)
        if src and src not in seen:
            seen.append(src)
    return " · ".join(seen)
