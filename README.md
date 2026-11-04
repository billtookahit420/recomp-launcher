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

Controller detection is automatic and continuous: the pad does
not need to be plugged in or awake before you open Big Picture —
connect or wake it at any time and it is picked up within about a
second (and if it sleeps or is unplugged, detection resumes the
same way). The Big Picture top bar always shows the controller
state: the connected pad's name, or "No controller detected".
The 8BitDo Ultimate 2's SDL mapping is built in, so that pad
classifies correctly even on systems whose SDL database predates
it.

While a game you launched is running, the controller belongs to
the game: Big Picture's pad input pauses and the top bar shows
"▶ <game> running — controller is the game's" (like Steam), so
playing can't navigate the library or launch a second title
behind the game. Input resumes when the game exits — and
whatever you're still holding at that moment won't fire; it
only counts once you release it and press again.

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

Recording is **Steam-style game capture**: the launcher taps the
game's own frames inside the game process, before they are
presented — the same trick Steam's game recording uses. Screen
capture (any screen recorder) can tear on X11 because the
compositor's presented frames themselves tear; frames tapped
before present cannot tear by construction.

Tick **"Record this session"** on a game (or the **Record**
button, which arms the next launch), then launch it. The launcher
starts the game with the capture library (`bin/librecompcapture.so`)
preloaded and a `recomp-recorder` helper encoding the frames to
MP4 (NVENC on NVIDIA hardware, libx264 otherwise; needs `ffmpeg`
installed). The recording stops and finalizes by itself when the
game exits. Settings → Recording has the output folder (default
`~/Videos/Recomp Recordings`) and framerate (default 60), and
shows whether game capture is ready.

There is **no screen-capture fallback**: a game whose frames
can't be tapped (no OpenGL present path — e.g. a Vulkan-only
build) reports "recording unavailable for this game" and plays
normally. Recordings are **video only** for now — the runtime
exposes no clean post-mix audio hook, and rather than fake it,
this version says so; the "Record game audio" setting is shown
greyed out (your choice is kept) until the audio tap lands.
Files are named `<Game> - <date time>.mp4`.

Recording is **Linux-only** in this version, and the launcher
says so on Windows rather than pretending.

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
Zelda64Recomp runtime. Recording is the launcher's own game
capture (librecompcapture + recomp-recorder, sources in `bin/`),
encoding with ffmpeg. Box art in your library comes from your own
packages (originally fetched from the libretro-thumbnails
project).
