# Recomp Launcher

A Steam-style front end for your recomp game packages: one library
for every system, box art up front, click to play.

It scans the folders you give it for the packages your converter
tools produce — PlayStation, NES, SNES, Game Boy / Game Boy Color,
Game Boy Advance, Nintendo 64, Genesis, Master System, Game Gear
and Virtual Boy — and shows them in a cover-art grid with a section
per system. Double-click a game (or press Play) and it runs from
its own folder, exactly as it does standalone.

## Run it

- Linux: `./recomp-launcher.sh`
- Windows: `recomp-launcher.bat`

First run creates a small Python environment next to the app and
installs PySide6; after that it just opens.

## Two views: Desktop and Big Picture

The launcher has two first-class views of the same library, and
you can switch between them freely:

- **Desktop view** — the windowed library described above.
- **Big Picture** — a fullscreen, couch-friendly grid with large
  covers and one section per system. Open it with the **Big
  Picture** button; **Esc or gamepad B switches back** to the
  desktop view (it never quits the app). Settings → "Start in"
  chooses which view the launcher opens in.

Big Picture controls:

- Keyboard: arrows move · Enter plays · Q / E (or PageUp /
  PageDown) switch system · Esc goes back.
- Gamepad (any SDL2 controller): d-pad or left stick moves ·
  A plays · B goes back · LB / RB switch system.

Launching from Big Picture uses the exact same path as the
desktop view — RA token supply, sessions and recording behave
identically — and when a game you launched from Big Picture
exits, focus returns to Big Picture.

## RetroAchievements

Two separate things, both set once in **Settings**:

- **Achievements viewing** — enter your RA username and your
  **Web API key** (retroachievements.org → Settings → API Keys).
  Selecting a game then shows your progress from your RA profile:
  unlocked/total, points, and the achievement list with badges.
  Games are matched to their RA entries by title + system; if one
  doesn't match, "Set RA game ID…" in the detail panel lets you
  pin it by hand. The Nintendo 64 runtime has no RetroAchievements
  integration, so its games honestly show that instead of a fake
  progress view.
- **Token supply** — enter your **Connect token** (the one that
  goes in `retroachievements.toml`). When you launch a game that
  has no toml of its own, the launcher writes one from your saved
  credentials (hardcore on by default) so you can stop copying the
  file into every folder by hand. A folder that already has its
  own toml is never touched — standalone folders keep working
  exactly as before, with or without the launcher.

## Game metadata

Every game's detail panel shows a synopsis and its **year, genre,
developer, publisher and players**, and Big Picture shows a
compact version of the same line (year · genre · players —
synopsis lead) for the focused game.

The values come from, in priority order:

1. **Your own edits** — "Edit metadata…" in the detail panel
   edits all six fields for one game. Your values are stored in
   `library.json`, survive rescans and database refreshes, and
   always win. Clearing a field hides a wrong downloaded value;
   "Reset to automatic" in the dialog drops your edits for that
   game.
2. **libretro-database** — RetroArch's metadata database (free,
   no key). Open **Settings → Game metadata → "Download /
   refresh game metadata"** once; only the systems present in
   your library are downloaded, and the files are cached in
   `cache/metadb/` next to the app. If a download fails (no
   network), the old data is kept and the failure is stated
   plainly — nothing is ever faked. PlayStation games are matched
   by their serial; other systems are matched by title, and a
   field is only used when all of a game's regional/revision
   entries agree on it (when they disagree — say, a 1991 US
   release versus a 1992 European one — the field is left blank
   rather than guessed). The database carries a full prose
   synopsis for PlayStation games; for cartridge games it has
   facts but no synopsis text.
3. **RetroAchievements** — for games matched to an RA entry, RA's
   own Publisher / Developer / Genre / Released fields fill
   whatever the database left empty.
4. **The package itself** — a `players` value from the package's
   own `game.toml`, where it carries one.
5. **Wikipedia** — a synopsis only, and only when the steps above
   found none: the exact-title video-game article's lead, via the
   Wikipedia summary API. If the match isn't confident (including
   series/franchise articles that share the game's name), no
   synopsis is shown.

The detail panel names the source under the metadata block, so
you can always see where a value came from.

## Recording (Linux)

The launcher can drive **GPU Screen Recorder** — the same recorder
used for stream captures — from the **Record** button, or per game
with "Record this session" (recording then stops by itself when
the game exits). Settings → Recording has the output folder
(default `~/Videos/RecompLauncher`), framerate (default 60),
capture source and the desktop-audio toggle, and shows whether
GPU Screen Recorder was found.

Capture is **monitor/screen capture on purpose**: window capture
tears while these games run borderless; screen capture is the
setup that fixed that. Window capture is not offered.

Recording is **Linux-only**: GPU Screen Recorder has no Windows
version, and the launcher says so on Windows rather than
pretending. Files are named `<Game> - <date time>.mp4`.

## Your data

`settings.json`, `library.json` and a small `cache/` (cover/RA
caches plus the downloaded metadata database) live next to the
app — nothing is uploaded anywhere. RetroAchievements is only
contacted, with your own key, to read your own profile when you
select a game. Wikipedia is only contacted for a synopsis when a
selected game has none from the other sources. The app ships
with no credentials in it.

## Credits

Built to sit in front of the recomp converter family, which is
built on the work of Matthew Stanley (mstan) and the
RetroPortingToolkit (retroportingtoolkit.com; github.com/mstan,
github.com/RetroPortingToolKit) — PSXRecomp, NESRecomp,
gbrecompiled, SNESRecomp, gbarecomp, segagenesisrecomp,
smsggrecomp and vbrecomp (framework licenses: PolyForm
Noncommercial 1.0.0 / MIT as noted per project; recomp-ui MIT,
© 2026 Matthew Stanley) — and N64Recomp by Wiseguy (MIT) with the
Zelda64Recomp runtime. Recording uses GPU Screen Recorder by
dec05eba (GPLv3). Box art in your library comes from your own
packages (originally fetched from the libretro-thumbnails
project).
