"""Recomp Launcher — RetroAchievements Web API client + matching.

Endpoints and response shapes follow the official RA Web API v1
docs (api-docs.retroachievements.org, checked 2026-11-03):

  API_GetConsoleIDs.php                 [{ID, Name, IconURL, ...}]
  API_GetUserCompletionProgress.php     {Count, Total, Results: [
      {GameID, Title, ImageIcon, ConsoleID, ConsoleName, MaxPossible,
       NumAwarded, NumAwardedHardcore, MostRecentAwardedDate, ...}]}
      params: y=<web api key>, u=<username>, c=<count>, o=<offset>
  API_GetGameList.php                   [{ID, Title, ConsoleID,
      ConsoleName, ImageIcon, NumAchievements, ...}]
      params: y, i=<console id>, f=1 (only games with achievements)
  API_GetGameInfoAndUserProgress.php    {ID, Title, ConsoleID,
      ConsoleName, ImageIcon, ImageBoxArt, NumAchievements,
      Achievements: {<id>: {ID, Title, Description, Points, BadgeName,
      DisplayOrder, type, DateEarned?, DateEarnedHardcore?}},
      NumAwardedToUser, NumAwardedToUserHardcore, UserCompletion}

The Web API key is a different credential from the Connect token in
retroachievements.toml: the key comes from retroachievements.org ->
Settings, and only ever leaves this machine as the `y` parameter of
these read calls.

All network goes through an injectable opener so the parsing and
matching logic can be verified against recorded-shape fixtures with
no live account.
"""
import json
import re
import urllib.parse
import urllib.request

API_BASE = "https://retroachievements.org/API"
MEDIA_BASE = "https://media.retroachievements.org"


class RAError(Exception):
    pass


def _default_opener(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": "RecompLauncher/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


class RAClient:
    def __init__(self, username, web_api_key, opener=None):
        self.username = username
        self.web_api_key = web_api_key
        self._opener = opener or _default_opener

    def _get(self, endpoint, **params):
        q = {"y": self.web_api_key, "u": self.username}
        q.update({k: v for k, v in params.items() if v is not None})
        url = f"{API_BASE}/{endpoint}?{urllib.parse.urlencode(q)}"
        try:
            raw = self._opener(url)
        except Exception as e:  # noqa: BLE001
            raise RAError(f"RetroAchievements request failed: {e}") from e
        try:
            data = json.loads(raw)
        except Exception as e:  # noqa: BLE001
            raise RAError("RetroAchievements sent back unreadable "
                          f"data: {e}") from e
        if isinstance(data, dict) and data.get("Success") is False:
            raise RAError("RetroAchievements rejected the request — "
                          "check the username and Web API key in "
                          "Settings.")
        return data

    # ---------------------------------------------------------- API
    def get_console_ids(self):
        data = self._get("API_GetConsoleIDs.php", u=None)
        return [{"id": int(c["ID"]), "name": c["Name"]}
                for c in data if c.get("IsGameSystem", True)]

    def get_completion_progress(self):
        """Every game the user has any progress in, paged."""
        out = []
        offset = 0
        while True:
            data = self._get("API_GetUserCompletionProgress.php",
                             c=500, o=offset)
            results = data.get("Results", [])
            out.extend(results)
            offset += len(results)
            if not results or offset >= int(data.get("Total", 0)):
                return out

    def get_game_list(self, console_id):
        data = self._get("API_GetGameList.php", i=console_id, f=1)
        return data if isinstance(data, list) else []

    def get_game_progress(self, game_id):
        data = self._get("API_GetGameInfoAndUserProgress.php",
                         g=game_id)
        return parse_game_progress(data)


# ------------------------------------------------------------------
# parsing
# ------------------------------------------------------------------
def badge_url(badge_name, locked=False):
    suffix = "_lock" if locked else ""
    return f"{MEDIA_BASE}/Badge/{badge_name}{suffix}.png"


def parse_game_progress(data):
    """Normalize a GetGameInfoAndUserProgress response into the
    shape the detail panel renders."""
    achievements = []
    raw = data.get("Achievements") or {}
    items = raw.values() if isinstance(raw, dict) else raw
    for a in items:
        earned_hc = a.get("DateEarnedHardcore")
        earned = a.get("DateEarned") or earned_hc
        achievements.append({
            "id": int(a["ID"]),
            "title": a.get("Title", ""),
            "description": a.get("Description", ""),
            "points": int(a.get("Points", 0)),
            "badge_name": str(a.get("BadgeName", "")),
            "type": a.get("type"),
            "earned": bool(earned),
            "hardcore": bool(earned_hc),
            "date_earned": earned,
            "display_order": int(a.get("DisplayOrder", 0)),
        })
    achievements.sort(key=lambda a: (a["display_order"], a["id"]))
    # RA's own game fields — the launcher merges these into game
    # metadata for any field the database left empty.
    meta = {}
    for key, field in (("Publisher", "publisher"),
                       ("Developer", "developer"),
                       ("Genre", "genre")):
        val = str(data.get(key) or "").strip()
        if val:
            meta[field] = val
    released = str(data.get("Released") or "").strip()
    if len(released) >= 4 and released[:4].isdigit():
        meta["year"] = released[:4]
    return {
        "game_id": int(data["ID"]),
        "meta": meta,
        "title": data.get("Title", ""),
        "console_id": int(data.get("ConsoleID", 0)),
        "console_name": data.get("ConsoleName", ""),
        "image_icon": data.get("ImageIcon"),
        "num_achievements": int(data.get("NumAchievements",
                                         len(achievements))),
        "num_awarded": int(data.get("NumAwardedToUser", 0)),
        "num_awarded_hardcore": int(data.get("NumAwardedToUserHardcore",
                                             0)),
        "points_earned": sum(a["points"] for a in achievements
                             if a["earned"]),
        "points_total": sum(a["points"] for a in achievements),
        "user_completion": data.get("UserCompletion"),
        "achievements": achievements,
    }


# ------------------------------------------------------------------
# title matching
# ------------------------------------------------------------------
_PREFIX_RE = re.compile(r"^\s*~[^~]+~\s*")
_BRACKET_RE = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")


def normalize_title(title):
    """Lowercase core title: RA subset/hack prefixes, region and
    revision parentheticals, and punctuation all removed, so a
    package title and its RA entry compare equal."""
    s = _PREFIX_RE.sub("", title or "")
    s = _BRACKET_RE.sub(" ", s)
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if s.startswith("the "):
        s = s[4:]
    return s


def _is_subset(title):
    return "[" in (title or "") or "~" in (title or "")


def match_game(title, console_id, progress_entries, game_list=None):
    """Find the RA game ID for a library title on one console.

    Completion-progress entries win (he has actually played those);
    the console's full game list is the fallback. Base games beat
    subset/hack entries when several titles normalize the same.
    Returns the game ID or None."""
    want = normalize_title(title)
    if not want:
        return None

    def search(entries, id_key, title_key, console_key):
        hits = []
        for e in entries or []:
            if int(e.get(console_key, 0)) != int(console_id):
                continue
            if normalize_title(e.get(title_key, "")) == want:
                hits.append(e)
        if not hits:
            return None
        hits.sort(key=lambda e: _is_subset(e.get(title_key, "")))
        return int(hits[0][id_key])

    found = search(progress_entries, "GameID", "Title", "ConsoleID")
    if found is not None:
        return found
    return search(game_list, "ID", "Title", "ConsoleID")
