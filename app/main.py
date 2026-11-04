#!/usr/bin/env python3
"""Recomp Launcher — a Steam-style library front end for recomp
game packages.

Scans the folders you choose for recomp packages (PlayStation, NES,
SNES, Game Boy / Color, GBA, Nintendo 64, Genesis, Master System,
Game Gear, Virtual Boy), shows them as a box-art grid, launches a
game with a click, shows your RetroAchievements progress per game
(from your retroachievements.org profile via the Web API), holds
your RA Connect token once and writes retroachievements.toml into
game folders that don't have one, and records gameplay with
Steam-style game capture — the game's own frames, tapped inside
the game process before present, so recordings can't tear
(Linux; video only for now).

Nothing here phones home on its own: RetroAchievements is only
contacted when you have entered your own username + Web API key in
Settings, and only to read your own profile.
"""
import atexit
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bigpicture  # noqa: E402
import metadata  # noqa: E402
import ra as ra_mod  # noqa: E402
import recording  # noqa: E402
import scanner  # noqa: E402

from PySide6.QtCore import Qt, QThread, QTimer, Signal  # noqa: E402
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QInputDialog,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QMessageBox, QPushButton, QScrollArea, QSpinBox, QSplitter,
    QTextEdit, QVBoxLayout, QWidget,
)

APP_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("RECOMP_LAUNCHER_DATA", str(APP_DIR)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
SETTINGS_FILE = DATA_DIR / "settings.json"
CACHE_DIR = DATA_DIR / "cache"
LOG_DIR = CACHE_DIR / "logs"
scanner.LIBRARY_FILE = DATA_DIR / "library.json"
metadata.CACHE_DIR = DATA_DIR / "cache" / "metadb"

ART_W, ART_H = 168, 224

STYLE = """
QMainWindow, QDialog { background: #17181c; color: #e8e8ea; }
QWidget { color: #e8e8ea; }
QListWidget, QLineEdit, QComboBox, QSpinBox {
    background: #22242a; border: 1px solid #34363e; border-radius: 4px;
    padding: 4px; }
QListWidget::item:selected { background: #2f4d7a; }
QPushButton {
    background: #2a2d34; border: 1px solid #3c3f47; border-radius: 4px;
    padding: 6px 12px; }
QPushButton:hover { background: #343842; }
QPushButton:disabled { color: #777; }
QPushButton#play { background: #2e6b30; border-color: #3f8a42;
    font-weight: bold; }
QPushButton#play:hover { background: #38823b; }
QPushButton#record[recording="true"] { background: #7a2027;
    border-color: #a33338; font-weight: bold; }
QLabel#rec { color: #ff5f57; font-weight: bold; }
QLabel#muted { color: #9a9da5; }
QLabel#gametitle { font-size: 20px; font-weight: bold; }
QGroupBox { border: 1px solid #34363e; border-radius: 4px;
    margin-top: 8px; padding-top: 8px; }
QScrollArea { border: none; background: #17181c; }
QScrollArea > QWidget > QWidget { background: #17181c; }
"""


# ------------------------------------------------------------------
# settings
# ------------------------------------------------------------------
def default_settings():
    return {
        "roots": [],
        "start_in": "desktop",
        "ra_username": "",
        "ra_web_api_key": "",
        "ra_connect_token": "",
        "hardcore": True,
        "recording": {
            "folder": recording.default_recordings_dir(),
            "framerate": 60,
            "audio": True,
        },
    }


def load_settings():
    s = default_settings()
    stored = None
    try:
        stored = json.loads(SETTINGS_FILE.read_text())
    except Exception:  # noqa: BLE001
        stored = None
    if isinstance(stored, dict):
        s.update({k: v for k, v in stored.items() if k != "recording"})
        rec_in = stored.get("recording")
        if isinstance(rec_in, dict):
            s["recording"].update(rec_in)
    # A hand-edited or corrupt settings.json must never crash the
    # app (stress-tested: an int username killed startup, a string
    # framerate killed the Settings dialog). Coerce every field the
    # UI consumes back to its expected type.
    if isinstance(s.get("roots"), list):
        # Drop blanks (an empty root would scan the process's
        # working directory — stress-tested) and duplicates.
        s["roots"] = list(dict.fromkeys(
            r for r in s["roots"] if isinstance(r, str) and r.strip()))
    else:
        s["roots"] = []
    for key in ("ra_username", "ra_web_api_key", "ra_connect_token"):
        if not isinstance(s.get(key), str):
            s[key] = ""
    s["hardcore"] = bool(s.get("hardcore", True))
    if s.get("start_in") not in ("desktop", "bigpicture"):
        s["start_in"] = "desktop"
    rec = s["recording"]
    if not isinstance(rec.get("folder"), str) or not rec["folder"]:
        rec["folder"] = recording.default_recordings_dir()
    try:
        rec["framerate"] = int(rec.get("framerate", 60))
    except (TypeError, ValueError):
        rec["framerate"] = 60
    rec["framerate"] = max(24, min(240, rec["framerate"]))
    # The old screen-capture source option is gone (game capture is
    # the only recording method now). The audio preference stays in
    # the schema — recordings are video-only until the audio tap
    # lands, and the Settings toggle is shown disabled rather than
    # silently dropping the user's saved choice.
    rec.pop("source", None)
    rec["audio"] = bool(rec.get("audio", True))
    return s


def save_settings(s):
    SETTINGS_FILE.write_text(json.dumps(s, indent=2))


# ------------------------------------------------------------------
# workers — every QThread is held in a set on the window until it
# finishes (the converter apps' crash fix): dropping the last
# reference to a running QThread aborts Qt.
# ------------------------------------------------------------------
class FuncWorker(QThread):
    done = Signal(str, object)      # key, result-or-exception

    def __init__(self, key, fn):
        super().__init__()
        self.key = key
        self.fn = fn

    def run(self):
        try:
            self.done.emit(self.key, self.fn())
        except Exception as e:  # noqa: BLE001
            self.done.emit(self.key, e)


class ImageWorker(QThread):
    done = Signal(str, bytes)       # url, image bytes (b"" on failure)

    def __init__(self, url):
        super().__init__()
        self.url = url

    def run(self):
        try:
            req = urllib.request.Request(
                self.url, headers={"User-Agent": "RecompLauncher/1.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = resp.read()
        except Exception:  # noqa: BLE001
            data = b""
        self.done.emit(self.url, data)


# ------------------------------------------------------------------
# worker lifetime registry — quitting while a scan, an RA fetch or
# a badge download is still running must not take the process down
# with it (Qt aborts when a QThread object is destroyed mid-run).
# Every worker is registered here; MainWindow.closeEvent drains
# them, and an atexit hook drains any survivors as a last resort.
# ------------------------------------------------------------------
_LIVE_WORKERS = set()
_ORPHANED_WORKERS = []          # kept referenced if a drain times out


def drain_live_workers(budget=45.0):
    """Wait (bounded) for every registered worker thread to end.
    Workers that outlive the budget are kept referenced forever
    rather than destroyed while still running."""
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        live = [w for w in list(_LIVE_WORKERS) if w.isRunning()]
        if not live:
            return True
        for w in live:
            w.wait(50)
        time.sleep(0.01)
    live = [w for w in list(_LIVE_WORKERS) if w.isRunning()]
    _ORPHANED_WORKERS.extend(live)
    return not live


atexit.register(lambda: drain_live_workers(budget=45.0))


# ------------------------------------------------------------------
# tiles
# ------------------------------------------------------------------
def placeholder_pixmap(title, system_label):
    pix = QPixmap(ART_W, ART_H)
    pix.fill(QColor("#26282e"))
    p = QPainter(pix)
    p.setPen(QColor("#565a63"))
    p.setFont(QFont("Sans", 11))
    p.drawText(pix.rect().adjusted(8, 8, -8, -30), Qt.AlignCenter
               | Qt.TextWordWrap, title)
    p.setFont(QFont("Sans", 9))
    p.setPen(QColor("#45484f"))
    p.drawText(pix.rect().adjusted(8, 0, -8, -8), Qt.AlignHCenter
               | Qt.AlignBottom, system_label)
    p.end()
    return pix


class Tile(QWidget):
    clicked = Signal(object)
    double_clicked = Signal(object)

    def __init__(self, game, pixmap):
        super().__init__()
        self.game = game
        self.setFixedSize(ART_W + 16, ART_H + 58)
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 4)
        self.art = QLabel()
        self.art.setFixedSize(ART_W, ART_H)
        self.art.setAlignment(Qt.AlignCenter)
        self.art.setPixmap(pixmap)
        v.addWidget(self.art)
        title = QLabel(scanner.display_title(game))
        title.setWordWrap(True)
        title.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        title.setFixedHeight(30)
        v.addWidget(title)
        syslabel = QLabel(scanner.SYSTEMS[game["system"]][0])
        syslabel.setObjectName("muted")
        syslabel.setAlignment(Qt.AlignHCenter)
        v.addWidget(syslabel)
        self.set_selected(False)

    def set_selected(self, on):
        self.art.setStyleSheet(
            "border: 2px solid #4d9fff;" if on
            else "border: 2px solid transparent;")

    def mousePressEvent(self, event):  # noqa: N802
        self.clicked.emit(self.game)

    def mouseDoubleClickEvent(self, event):  # noqa: N802
        self.double_clicked.emit(self.game)


# ------------------------------------------------------------------
# settings dialog
# ------------------------------------------------------------------
class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(560, 660)
        self.settings = settings
        v = QVBoxLayout(self)

        lib = QGroupBox("Library folders (scanned for recomp packages)")
        lv = QVBoxLayout(lib)
        self.roots = QListWidget()
        self.roots.addItems(settings.get("roots", []))
        lv.addWidget(self.roots)
        row = QHBoxLayout()
        add = QPushButton("Add folder…")
        add.clicked.connect(self._add_root)
        rem = QPushButton("Remove selected")
        rem.clicked.connect(
            lambda: self.roots.takeItem(self.roots.currentRow()))
        row.addWidget(add)
        row.addWidget(rem)
        row.addStretch(1)
        lv.addLayout(row)
        start_row = QHBoxLayout()
        start_row.addWidget(QLabel("Start in:"))
        self.start_in = QComboBox()
        self.start_in.addItems(["Desktop", "Big Picture"])
        self.start_in.setCurrentText(
            "Big Picture" if settings.get("start_in") == "bigpicture"
            else "Desktop")
        start_row.addWidget(self.start_in)
        start_row.addStretch(1)
        lv.addLayout(start_row)
        v.addWidget(lib)

        ra_box = QGroupBox("RetroAchievements")
        form = QFormLayout(ra_box)
        self.ra_user = QLineEdit(settings.get("ra_username", ""))
        form.addRow("Username:", self.ra_user)
        self.ra_key = QLineEdit(settings.get("ra_web_api_key", ""))
        self.ra_key.setEchoMode(QLineEdit.Password)
        form.addRow("Web API key:", self.ra_key)
        note = QLabel("For viewing achievements. Get it at "
                      "retroachievements.org → Settings → API Keys. "
                      "This is NOT the token used in-game.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        form.addRow(note)
        self.ra_token = QLineEdit(settings.get("ra_connect_token", ""))
        self.ra_token.setEchoMode(QLineEdit.Password)
        form.addRow("Connect token:", self.ra_token)
        note2 = QLabel("The in-game login token (the one that goes in "
                       "retroachievements.toml). Saved once here, the "
                       "launcher writes it into game folders that "
                       "don't have a toml yet. It never overwrites an "
                       "existing toml.")
        note2.setObjectName("muted")
        note2.setWordWrap(True)
        form.addRow(note2)
        self.hardcore = QCheckBox("Hardcore mode in supplied tomls")
        self.hardcore.setChecked(settings.get("hardcore", True))
        form.addRow(self.hardcore)
        v.addWidget(ra_box)

        rec = settings.get("recording", {})
        rbox = QGroupBox("Recording (game capture)")
        rform = QFormLayout(rbox)
        folder_row = QHBoxLayout()
        self.rec_folder = QLineEdit(
            rec.get("folder") or recording.default_recordings_dir())
        pick = QPushButton("Pick…")
        pick.clicked.connect(lambda: self._pick_dir(self.rec_folder))
        folder_row.addWidget(self.rec_folder)
        folder_row.addWidget(pick)
        rform.addRow("Output folder:", folder_row)
        self.rec_fps = QSpinBox()
        self.rec_fps.setRange(24, 240)
        self.rec_fps.setValue(int(rec.get("framerate", 60)))
        rform.addRow("Framerate:", self.rec_fps)
        self.rec_audio = QCheckBox("Record game audio")
        self.rec_audio.setChecked(bool(rec.get("audio", True)))
        self.rec_audio.setEnabled(False)
        rform.addRow(self.rec_audio)
        audio_note = QLabel("Audio isn't captured yet — recordings "
                            "are video-only for now, so this is "
                            "greyed out. Your choice is kept and "
                            "will apply when the audio tap lands.")
        audio_note.setObjectName("muted")
        audio_note.setWordWrap(True)
        rform.addRow(audio_note)
        cap_note = QLabel("Recording taps the game's own frames "
                          "inside the game process (Steam-style) — "
                          "never the screen, so recordings can't "
                          "tear. Tick “Record this session” on a "
                          "game before launching it. Video only in "
                          "this version (no audio yet). A game with "
                          "no capture tap says recording is "
                          "unavailable for it and plays normally.")
        cap_note.setObjectName("muted")
        cap_note.setWordWrap(True)
        rform.addRow(cap_note)
        state = QLabel("Game capture: "
                       + ("ready — capture library, recorder and "
                          "ffmpeg found"
                          if recording.capture_available()
                          else recording.unavailable_reason()))
        state.setObjectName("muted")
        state.setWordWrap(True)
        rform.addRow(state)
        v.addWidget(rbox)

        meta_box = QGroupBox("Game metadata")
        mv = QVBoxLayout(meta_box)
        self.meta_status = QLabel("")
        self.meta_status.setWordWrap(True)
        mv.addWidget(self.meta_status)
        self.meta_btn = QPushButton("Download / refresh game metadata")
        self.meta_btn.clicked.connect(self._meta_clicked)
        mv.addWidget(self.meta_btn)
        meta_note = QLabel(
            "Facts and synopses come from the libretro-database "
            "(RetroArch's metadata database — free, no account). "
            "Only the systems in your library are downloaded. "
            "Metadata you type yourself in a game's “Edit "
            "metadata…” dialog always wins and is never "
            "overwritten by a refresh.")
        meta_note.setObjectName("muted")
        meta_note.setWordWrap(True)
        mv.addWidget(meta_note)
        v.addWidget(meta_box)
        # Wired by MainWindow after construction.
        self.meta_status_fn = None
        self.meta_download_fn = None

        buttons = QDialogButtonBox(QDialogButtonBox.Save
                                   | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        v.addWidget(buttons)

    def _meta_clicked(self):
        if self.meta_download_fn is not None:
            self.meta_download_fn()

    def set_meta_status(self, text):
        self.meta_status.setText(text)

    def reload(self, settings):
        """Refresh every field from *settings*. The dialog is created
        once and reused for the app's lifetime (see
        MainWindow.open_settings), so each open reloads the current
        settings and unsaved edits from a cancelled visit are lost,
        exactly as with a freshly constructed dialog."""
        self.settings = settings
        self.roots.clear()
        self.roots.addItems(settings.get("roots", []))
        self.start_in.setCurrentText(
            "Big Picture" if settings.get("start_in") == "bigpicture"
            else "Desktop")
        self.ra_user.setText(settings.get("ra_username", ""))
        self.ra_key.setText(settings.get("ra_web_api_key", ""))
        self.ra_token.setText(settings.get("ra_connect_token", ""))
        self.hardcore.setChecked(settings.get("hardcore", True))
        rec = settings.get("recording", {})
        self.rec_folder.setText(
            rec.get("folder") or recording.default_recordings_dir())
        self.rec_fps.setValue(int(rec.get("framerate", 60)))
        self.rec_audio.setChecked(bool(rec.get("audio", True)))
        if self.meta_status_fn is not None:
            self.meta_status.setText(self.meta_status_fn())

    def _add_root(self):
        d = QFileDialog.getExistingDirectory(self, "Add a library folder")
        if d:
            self.roots.addItem(d)

    def _pick_dir(self, edit):
        d = QFileDialog.getExistingDirectory(self, "Pick a folder")
        if d:
            edit.setText(d)

    def values(self):
        s = self.settings
        s["roots"] = [self.roots.item(i).text()
                      for i in range(self.roots.count())]
        s["start_in"] = ("bigpicture"
                         if self.start_in.currentText() == "Big Picture"
                         else "desktop")
        s["ra_username"] = self.ra_user.text().strip()
        s["ra_web_api_key"] = self.ra_key.text().strip()
        s["ra_connect_token"] = self.ra_token.text().strip()
        s["hardcore"] = self.hardcore.isChecked()
        s["recording"] = {
            "folder": self.rec_folder.text().strip(),
            "framerate": self.rec_fps.value(),
            "audio": self.rec_audio.isChecked(),
        }
        return s


# ------------------------------------------------------------------
# metadata edit dialog
# ------------------------------------------------------------------
class MetadataDialog(QDialog):
    """Edit one game's metadata (synopsis, year, genre, developer,
    publisher, players). Created once and reused for the app's
    lifetime — the SettingsDialog lesson: a dialog whose buttons
    connect to Python callables is not destroyed after exec().

    The fields show the game's effective values (manual edits over
    downloaded data). On save, a field whose text still equals the
    downloaded value is left automatic; any other text becomes the
    game's manual value in library.json (game["meta_manual"]) and
    wins over every fetcher, across rescans and refreshes. Clearing
    a field stores an explicit blank, hiding a wrong downloaded
    value; "Reset to automatic" drops all manual values."""

    _ROWS = (("year", "Year"), ("genre", "Genre"),
             ("developer", "Developer"), ("publisher", "Publisher"),
             ("players", "Players"))

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit metadata")
        self.resize(520, 430)
        v = QVBoxLayout(self)
        self.title_label = QLabel("")
        self.title_label.setStyleSheet(
            "font-size: 16px; font-weight: bold;")
        v.addWidget(self.title_label)
        form = QFormLayout()
        self.edits = {}
        syn = QTextEdit()
        syn.setMinimumHeight(90)
        self.edits["synopsis"] = syn
        form.addRow("Synopsis:", syn)
        for field, label in self._ROWS:
            edit = QLineEdit()
            self.edits[field] = edit
            form.addRow(label + ":", edit)
        v.addLayout(form)
        hint = QLabel(
            "Your values are kept across rescans and database "
            "refreshes, and always win over downloaded data. "
            "Clearing a field hides the downloaded value for it.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        v.addWidget(hint)
        row = QHBoxLayout()
        reset = QPushButton("Reset to automatic")
        reset.clicked.connect(self._do_reset)
        row.addWidget(reset)
        row.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.Save
                                   | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        row.addWidget(buttons)
        v.addLayout(row)
        self._auto = {}
        self._reset = False

    def load(self, game):
        values, _src = metadata.effective(game)
        meta = game.get("meta") or {}
        self._auto = dict(meta.get("values") or {})
        self._reset = False
        self.title_label.setText(scanner.display_title(game))
        for field, edit in self.edits.items():
            if field == "synopsis":
                edit.setPlainText(values.get(field, ""))
            else:
                edit.setText(values.get(field, ""))

    def _do_reset(self):
        self._reset = True
        self.accept()

    def apply_to(self, game):
        if self._reset:
            game["meta_manual"] = {}
            return
        manual = dict(game.get("meta_manual") or {})
        for field, edit in self.edits.items():
            text = (edit.toPlainText() if field == "synopsis"
                    else edit.text()).strip()
            if text == (self._auto.get(field) or ""):
                manual.pop(field, None)
            else:
                manual[field] = text
        game["meta_manual"] = manual


# ------------------------------------------------------------------
# main window
# ------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Recomp Launcher")
        self.resize(1500, 950)
        self.settings = load_settings()
        self.games = scanner.load_library()
        self.selected = None
        self.ra_client_override = None      # tests inject fixtures here
        self._workers = set()
        self._img_workers = set()
        self._img_wanted = None
        self._ra_wanted = None
        self._completion_cache = None
        self._sessions = {}                 # pid -> (game, proc)
        self.recorder = recording.GameCapture()
        self._cap_reported = None   # negotiation state last announced
        self.bp = None                      # Big Picture window
        self._settings_dlg = None           # created once, reused
        self._bp_autostart_done = False
        self._closing = False
        self._scan_active = False
        self._scan_pending = False
        self._metadb = None                 # metadata.LibretroDB (lazy)
        self._metadb_running = False
        self._meta_inflight = set()         # (kind, folder) markers
        self._meta_dlg = None               # created once, reused
        self._meta_status = ""
        self._build_ui()
        self._refresh_sidebar()
        self._refresh_grid()
        if self.games:
            self._select_game(self.games[0])
        if self.settings.get("roots"):
            self.rescan()
        elif not self.games:
            self.statusBar().showMessage(
                "Add a library folder in Settings to find your games.")

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(500)

    # ------------------------------------------------------------ UI
    def _build_ui(self):
        central = QWidget()
        root = QVBoxLayout(central)

        top = QHBoxLayout()
        title = QLabel("Recomp Launcher")
        title.setObjectName("gametitle")
        top.addWidget(title)
        top.addStretch(1)
        self.rec_label = QLabel("")
        self.rec_label.setObjectName("rec")
        top.addWidget(self.rec_label)
        self.record_btn = QPushButton("● Record")
        self.record_btn.setObjectName("record")
        self.record_btn.clicked.connect(self.toggle_record)
        top.addWidget(self.record_btn)
        open_rec = QPushButton("Open recordings folder")
        open_rec.clicked.connect(self.open_recordings)
        top.addWidget(open_rec)
        rescan_btn = QPushButton("Rescan")
        rescan_btn.clicked.connect(self.rescan)
        top.addWidget(rescan_btn)
        bp_btn = QPushButton("Big Picture")
        bp_btn.clicked.connect(self.open_big_picture)
        top.addWidget(bp_btn)
        add_btn = QPushButton("Add folder…")
        add_btn.clicked.connect(self.add_folder)
        top.addWidget(add_btn)
        settings_btn = QPushButton("Settings")
        settings_btn.clicked.connect(self.open_settings)
        top.addWidget(settings_btn)
        root.addLayout(top)

        split = QSplitter(Qt.Horizontal)
        side = QWidget()
        sv = QVBoxLayout(side)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search games…")
        self.search.textChanged.connect(self._refresh_grid)
        sv.addWidget(self.search)
        self.sidebar = QListWidget()
        self.sidebar.currentRowChanged.connect(
            lambda _row: self._refresh_grid())
        sv.addWidget(self.sidebar)
        split.addWidget(side)
        split.addWidget(self._grid_area())
        split.addWidget(self._detail_panel())
        split.setSizes([240, 760, 380])
        root.addWidget(split)
        self.setCentralWidget(central)

    def _grid_area(self):
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.scroll.setWidget(self.grid_host)
        self.scroll.viewport().installEventFilter(self)
        return self.scroll

    def eventFilter(self, obj, event):  # noqa: N802
        if obj is self.scroll.viewport() and event.type() == event.Type.Resize:
            self._layout_grid()
        return super().eventFilter(obj, event)

    def _detail_panel(self):
        panel = QWidget()
        v = QVBoxLayout(panel)
        self.d_art = QLabel()
        self.d_art.setAlignment(Qt.AlignCenter)
        self.d_art.setFixedHeight(300)
        v.addWidget(self.d_art)
        self.d_title = QLabel("No game selected")
        self.d_title.setObjectName("gametitle")
        self.d_title.setWordWrap(True)
        v.addWidget(self.d_title)
        self.d_system = QLabel("")
        self.d_system.setObjectName("muted")
        v.addWidget(self.d_system)
        self.d_folder = QLabel("")
        self.d_folder.setObjectName("muted")
        self.d_folder.setWordWrap(True)
        v.addWidget(self.d_folder)
        self.d_toml = QLabel("")
        v.addWidget(self.d_toml)
        self.d_meta_facts = QLabel("")
        self.d_meta_facts.setWordWrap(True)
        self.d_meta_facts.setStyleSheet(
            "font-size: 14px; font-weight: bold;")
        v.addWidget(self.d_meta_facts)
        self.d_meta_byline = QLabel("")
        self.d_meta_byline.setWordWrap(True)
        self.d_meta_byline.setObjectName("muted")
        v.addWidget(self.d_meta_byline)
        self.d_synopsis = QLabel("")
        self.d_synopsis.setWordWrap(True)
        v.addWidget(self.d_synopsis)
        meta_row = QHBoxLayout()
        self.d_meta_src = QLabel("")
        self.d_meta_src.setObjectName("muted")
        self.d_meta_src.setWordWrap(True)
        meta_row.addWidget(self.d_meta_src, 1)
        self.meta_edit_btn = QPushButton("Edit metadata…")
        self.meta_edit_btn.clicked.connect(self.open_metadata_editor)
        meta_row.addWidget(self.meta_edit_btn)
        v.addLayout(meta_row)
        self.play_btn = QPushButton("▶  Play")
        self.play_btn.setObjectName("play")
        self.play_btn.clicked.connect(
            lambda: self.selected and self.launch(self.selected))
        v.addWidget(self.play_btn)
        self.record_session = QCheckBox("Record this session")
        self.record_session.toggled.connect(
            lambda *_: self._update_record_ui())
        v.addWidget(self.record_session)
        id_row = QHBoxLayout()
        self.d_ra_id = QLabel("RA game: —")
        self.d_ra_id.setObjectName("muted")
        id_row.addWidget(self.d_ra_id)
        set_id = QPushButton("Set RA game ID…")
        set_id.clicked.connect(self.set_ra_id)
        id_row.addWidget(set_id)
        v.addLayout(id_row)
        v.addWidget(QLabel("Achievements"))
        self.d_ra_summary = QLabel("")
        self.d_ra_summary.setWordWrap(True)
        v.addWidget(self.d_ra_summary)
        self.ach_list = QListWidget()
        v.addWidget(self.ach_list, 1)
        return panel

    # ------------------------------------------------------- library
    def rescan(self):
        if self._closing:
            return
        roots = self.settings.get("roots", [])
        if not roots:
            self.statusBar().showMessage(
                "No library folders set — add one in Settings.")
            return
        if self._scan_active:
            # Coalesce instead of spawning a thread per call: one
            # follow-up scan runs when the current one finishes, no
            # matter how often Rescan is hit (stress-tested: a 30x
            # rescan spam used to run 30 concurrent scan threads).
            self._scan_pending = True
            return
        self._scan_active = True
        worker = FuncWorker("scan", lambda: scanner.rescan(roots))
        self._run_worker(worker, self._scan_done)

    def _scan_done(self, key, result):
        self._scan_active = False
        try:
            if isinstance(result, Exception):
                self.statusBar().showMessage(f"Scan failed: {result}")
                return
            self.games = result
            self._refresh_sidebar()
            self._refresh_grid()
            self.statusBar().showMessage(
                f"Library: {len(self.games)} game(s) found.")
            if self.bp is not None and self.bp.isVisible():
                self.bp.refresh()
            if self.settings.get("start_in") == "bigpicture" \
                    and not self._bp_autostart_done and self.games \
                    and not self._closing:
                self._bp_autostart_done = True
                self.open_big_picture()
            if self.selected:
                still = [g for g in self.games
                         if g["folder"] == self.selected["folder"]]
                if still:
                    self._select_game(still[0])
        finally:
            if self._scan_pending and not self._closing:
                self._scan_pending = False
                self.rescan()

    def add_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Add a library folder")
        if d and d not in self.settings.setdefault("roots", []):
            self.settings["roots"].append(d)
            save_settings(self.settings)
            self.rescan()

    def open_settings(self):
        # The Settings dialog is created ONCE and kept alive on the
        # window for the app's whole lifetime. Stress testing found
        # that destroying this dialog after a modal exec() — its child
        # buttons are connected to Python callables — intermittently
        # corrupts the heap and crashes the process at exit (SIGSEGV /
        # glibc malloc aborts). Reusing one retained instance avoids
        # the destruction entirely; reload() refreshes its fields.
        dlg = self._settings_dlg
        if dlg is None:
            dlg = SettingsDialog(self.settings, self)
            self._settings_dlg = dlg
        else:
            dlg.reload(self.settings)
        dlg.meta_status_fn = self.metadata_status_text
        dlg.meta_download_fn = self.start_metadata_download
        self._sync_meta_dialog()
        if dlg.exec():
            old_roots = self.settings.get("roots")
            self.settings = dlg.values()
            save_settings(self.settings)
            if self.settings.get("roots") != old_roots:
                self.rescan()
            if self.selected:
                self._refresh_detail(self.selected)
            self._update_record_ui()

    def _filtered_games(self):
        row = self.sidebar.currentRow()
        system = None
        if row > 0 and row - 1 < len(self._sidebar_systems):
            system = self._sidebar_systems[row - 1]
        q = self.search.text().strip().lower()
        out = []
        for g in self.games:
            if system and g["system"] != system:
                continue
            if q and q not in scanner.display_title(g).lower():
                continue
            out.append(g)
        return out

    def _refresh_sidebar(self):
        counts = {}
        for g in self.games:
            counts[g["system"]] = counts.get(g["system"], 0) + 1
        self._sidebar_systems = [s for s in scanner.SYSTEM_ORDER
                                 if s in counts]
        self.sidebar.blockSignals(True)
        self.sidebar.clear()
        self.sidebar.addItem(f"All Games ({len(self.games)})")
        for s in self._sidebar_systems:
            self.sidebar.addItem(f"{scanner.SYSTEMS[s][0]} ({counts[s]})")
        self.sidebar.setCurrentRow(0)
        self.sidebar.blockSignals(False)

    def _art_pixmap(self, game):
        if game.get("art"):
            pix = QPixmap(game["art"])
            if not pix.isNull():
                return pix.scaled(ART_W, ART_H, Qt.KeepAspectRatio,
                                  Qt.SmoothTransformation)
        return placeholder_pixmap(scanner.display_title(game),
                                  scanner.SYSTEMS[game["system"]][0])

    def _refresh_grid(self):
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.tiles = []
        for g in self._filtered_games():
            tile = Tile(g, self._art_pixmap(g))
            tile.clicked.connect(self._select_game)
            tile.double_clicked.connect(self.launch)
            tile.set_selected(self.selected is not None
                              and g["folder"] == self.selected["folder"])
            self.tiles.append(tile)
        self._layout_grid()

    def _layout_grid(self):
        if not hasattr(self, "tiles"):
            return
        width = max(self.scroll.viewport().width(), 200)
        cols = max(1, width // (ART_W + 24))
        for i, tile in enumerate(self.tiles):
            self.grid.addWidget(tile, i // cols, i % cols)

    # -------------------------------------------------------- detail
    def _select_game(self, game):
        self.selected = game
        for tile in getattr(self, "tiles", []):
            tile.set_selected(tile.game["folder"] == game["folder"])
        self._refresh_detail(game)

    def _toml_status(self, game):
        if not game.get("ra_supported", True):
            return "RetroAchievements: not supported by this runtime"
        if game.get("has_ra_toml"):
            return "RetroAchievements: has own toml"
        if self.settings.get("ra_connect_token"):
            return ("RetroAchievements: no toml — the launcher will "
                    "supply yours on Play")
        return ("RetroAchievements: no toml — add your Connect token "
                "in Settings to supply one automatically")

    def _refresh_detail(self, game):
        pix = QPixmap(game["art"]) if game.get("art") else QPixmap()
        if pix.isNull():
            pix = placeholder_pixmap(scanner.display_title(game),
                                     scanner.SYSTEMS[game["system"]][0])
        self.d_art.setPixmap(pix.scaled(280, 300, Qt.KeepAspectRatio,
                                        Qt.SmoothTransformation))
        self.d_title.setText(scanner.display_title(game))
        self.d_system.setText(scanner.SYSTEMS[game["system"]][0])
        self.d_folder.setText(game["folder"])
        self.d_toml.setText(self._toml_status(game))
        self.d_ra_id.setText(
            f"RA game: {game['ra_game_id']}" if game.get("ra_game_id")
            else "RA game: not matched")
        self._refresh_metadata(game)
        self._ensure_metadata(game)
        self._load_achievements(game)

    def set_ra_id(self):
        if not self.selected:
            return
        value, ok = QInputDialog.getInt(
            self, "Set RA game ID",
            "RetroAchievements game ID for "
            f"{scanner.display_title(self.selected)} "
            "(see the number in the game's retroachievements.org URL):",
            value=self.selected.get("ra_game_id") or 0, minValue=0)
        if ok:
            self.selected["ra_game_id"] = value or None
            scanner.save_library(self.games)
            self._refresh_detail(self.selected)

    # ---------------------------------------------------- Big Picture
    def open_big_picture(self):
        if self.bp is None:
            self.bp = bigpicture.BigPictureWindow(
                games_provider=lambda: self.games,
                on_launch=self._bp_launch,
                on_back=self._bp_back,
                on_focus=self._bp_focus_changed,
                input_suspended=lambda: bool(self._sessions),
                running_title=self._running_game_title)
        self.bp.open()

    def _running_game_title(self):
        """Display title of a game currently running through the
        launcher (Big Picture's running-state line), or None.
        Read live from the session table on every pad poll tick,
        so it tracks launches/exits from either view."""
        for game, _proc in self._sessions.values():
            return scanner.display_title(game)
        return None

    def _bp_launch(self, game):
        # Same launch path as the desktop view: token supply,
        # session tracking and recording all behave identically.
        self._select_game(game)
        self.launch(game)

    def _bp_back(self):
        self.show()
        self.raise_()
        self.activateWindow()

    # ------------------------------------------------------ metadata
    def _metadb_instance(self):
        if self._metadb is None:
            self._metadb = metadata.LibretroDB()
        return self._metadb

    def _game_by_folder(self, folder):
        return next((g for g in self.games
                     if g["folder"] == folder), None)

    def _refresh_metadata(self, game):
        """Render the metadata block (facts / byline / synopsis /
        provenance) for *game* from its stored values — manual
        edits over downloaded data (metadata.effective)."""
        values, sources = metadata.effective(game)
        facts = metadata.facts_line(values)
        byline = metadata.byline(values)
        syn = values.get("synopsis", "")
        self.d_meta_facts.setText(facts)
        self.d_meta_facts.setVisible(bool(facts))
        self.d_meta_byline.setText(byline)
        self.d_meta_byline.setVisible(bool(byline))
        self.d_synopsis.setText(syn)
        self.d_synopsis.setVisible(bool(syn))
        if any(values.values()):
            summary = metadata.source_summary(sources)
            self.d_meta_src.setText(
                f"Metadata: {summary}" if summary else "")
        else:
            self.d_meta_src.setText("No metadata for this game yet.")
        self.meta_edit_btn.setEnabled(game is not None)

    def _ensure_metadata(self, game):
        """Kick off the local metadata lookup for a game whose
        database pass hasn't run yet (worker thread; the parse of
        a multi-MB DAT never blocks the UI). Results merge into
        game['meta'] and persist in library.json."""
        if self._closing or game is None:
            return
        meta = game.get("meta")
        if not isinstance(meta, dict) or "values" not in meta:
            meta = metadata.new_meta()
            game["meta"] = meta
        if meta.get("db_done"):
            self._maybe_wiki(game)
            return
        folder = game["folder"]
        if ("db", folder) in self._meta_inflight:
            return
        self._meta_inflight.add(("db", folder))
        db = self._metadb_instance()
        snap = {k: game.get(k) for k in
                ("system", "title", "title_override", "serial",
                 "folder")}
        worker = FuncWorker("meta",
                            lambda: self._meta_lookup(db, snap))
        self._run_worker(
            worker,
            lambda k, r, f=folder: self._meta_db_done(f, r))

    @staticmethod
    def _meta_lookup(db, snap):
        values, sources = db.lookup(snap)
        local_v, local_s = metadata.local_lookup(snap)
        for field, val in local_v.items():
            if field not in values:
                values[field] = val
                sources[field] = local_s[field]
        return values, sources

    def _meta_db_done(self, folder, result):
        self._meta_inflight.discard(("db", folder))
        if self._closing:
            return
        game = self._game_by_folder(folder)
        if game is None:
            return
        if not isinstance(result, Exception):
            values, sources = result
            metadata.merge_meta(game, values, sources)
        meta = game.get("meta")
        if not isinstance(meta, dict) or "values" not in meta:
            meta = metadata.new_meta()
            game["meta"] = meta
        meta["db_done"] = True
        scanner.save_library(self.games)
        self._meta_ui_refresh(game)
        self._maybe_wiki(game)

    def _meta_ui_refresh(self, game):
        if self.selected is not None \
                and self.selected["folder"] == game["folder"]:
            self._refresh_metadata(game)
        if self.bp is not None and self.bp.isVisible():
            focused = self.bp.focused_game()
            if focused is not None \
                    and focused["folder"] == game["folder"]:
                self.bp.refresh_meta()

    def _maybe_wiki(self, game):
        """Wikipedia is the last resort for a synopsis, and only
        after the database pass ran (spec order 2–4 before 5)."""
        if self._closing:
            return
        meta = game.get("meta") or {}
        if not meta.get("db_done") or meta.get("wiki_done"):
            return
        values, _src = metadata.effective(game)
        if values.get("synopsis"):
            meta["wiki_done"] = True
            return
        folder = game["folder"]
        if ("wiki", folder) in self._meta_inflight:
            return
        self._meta_inflight.add(("wiki", folder))
        title = scanner.display_title(game)
        worker = FuncWorker(
            "wiki", lambda: metadata.wikipedia_synopsis(title))
        self._run_worker(
            worker,
            lambda k, r, f=folder: self._wiki_done(f, r))

    def _wiki_done(self, folder, result):
        self._meta_inflight.discard(("wiki", folder))
        if self._closing:
            return
        game = self._game_by_folder(folder)
        if game is None:
            return
        meta = game.get("meta")
        if not isinstance(meta, dict) or "values" not in meta:
            meta = metadata.new_meta()
            game["meta"] = meta
        if isinstance(result, str) and result:
            metadata.merge_meta(
                game, {"synopsis": result},
                {"synopsis": metadata.SOURCE_WIKI})
        meta["wiki_done"] = True
        scanner.save_library(self.games)
        self._meta_ui_refresh(game)

    def open_metadata_editor(self):
        game = self.selected
        if game is None or self._closing:
            return
        if self._meta_dlg is None:
            self._meta_dlg = MetadataDialog(self)
        self._meta_dlg.load(game)
        if self._meta_dlg.exec() == QDialog.Accepted:
            self._meta_dlg.apply_to(game)
            scanner.save_library(self.games)
            self._refresh_metadata(game)
            if self.bp is not None and self.bp.isVisible():
                self.bp.refresh_meta()

    # ------------------------------------------- metadata download
    def metadata_status_text(self):
        if self._metadb_running:
            return "Downloading game metadata…"
        cached = self._metadb_instance().cached_systems()
        if not cached:
            return ("Metadata database: not downloaded yet. The "
                    "download is free and needs no account; only "
                    "the systems in your library are fetched.")
        names = ", ".join(scanner.SYSTEMS[s][0]
                          for s in scanner.SYSTEM_ORDER if s in cached)
        return f"Metadata database downloaded for: {names}."

    def _set_meta_status(self, text):
        self._meta_status = text
        if self._settings_dlg is not None:
            self._settings_dlg.set_meta_status(text)

    def _sync_meta_dialog(self):
        dlg = self._settings_dlg
        if dlg is None:
            return
        dlg.meta_btn.setEnabled(not self._metadb_running)
        dlg.set_meta_status(self._meta_status
                            or self.metadata_status_text())

    def start_metadata_download(self):
        """Settings' Download / refresh button. Spam-safe: a
        second click while a download runs coalesces (the button
        is also disabled while running)."""
        if self._closing or self._metadb_running:
            return
        systems = sorted({g["system"] for g in self.games
                          if g.get("system")
                          in metadata.SYSTEM_DB_FILE})
        if not systems:
            self._set_meta_status(
                "No games in the library yet — nothing to "
                "download metadata for.")
            self._sync_meta_dialog()
            return
        self._metadb_running = True
        self._set_meta_status("Downloading game metadata…")
        self._sync_meta_dialog()
        db = self._metadb_instance()
        worker = FuncWorker("metadb", lambda: db.download(systems))
        self._run_worker(worker, self._metadb_download_done)

    def _metadb_download_done(self, key, result):
        self._metadb_running = False
        if isinstance(result, Exception):
            self._set_meta_status(
                f"Metadata download failed: {result}. Any "
                "previously downloaded data was kept.")
        elif result:
            bad = ", ".join(scanner.SYSTEMS.get(s, (s,))[0]
                            for s in sorted(result))
            first = next(iter(result.values()))
            self._set_meta_status(
                f"Metadata download failed for: {bad} ({first}). "
                "Any previously downloaded data was kept.")
        else:
            self._set_meta_status("Metadata database is up to date.")
        self._sync_meta_dialog()
        if self._closing:
            return
        # Re-run every game's lookup against the fresh data.
        db = self._metadb_instance()
        snaps = [{k: g.get(k) for k in
                  ("system", "title", "title_override", "serial",
                   "folder")} for g in self.games]
        self._meta_inflight.add(("all", ""))

        def work():
            return {s["folder"]: self._meta_lookup(db, s)
                    for s in snaps}

        worker = FuncWorker("metaall", work)
        self._run_worker(worker, self._meta_all_done)

    def _meta_all_done(self, key, result):
        self._meta_inflight.discard(("all", ""))
        if self._closing or isinstance(result, Exception):
            return
        for game in self.games:
            got = result.get(game["folder"])
            if got is None:
                continue
            values, sources = got
            meta = game.get("meta")
            if not isinstance(meta, dict) or "values" not in meta:
                meta = metadata.new_meta()
                game["meta"] = meta
            # Fresh database/package values replace the old ones;
            # RA-sourced fields survive a refresh, and a Wikipedia
            # synopsis survives only when the database has none.
            keep_v, keep_s = {}, {}
            for field, val in meta["values"].items():
                src = meta["sources"].get(field)
                if src == metadata.SOURCE_RA or (
                        field == "synopsis"
                        and src == metadata.SOURCE_WIKI
                        and "synopsis" not in values):
                    keep_v[field] = val
                    keep_s[field] = src
            meta["values"], meta["sources"] = keep_v, keep_s
            metadata.merge_meta(game, values, sources)
            meta["db_done"] = True
        scanner.save_library(self.games)
        if self.selected:
            self._refresh_metadata(self.selected)
        if self.bp is not None and self.bp.isVisible():
            self.bp.refresh_meta()

    def _bp_focus_changed(self, game):
        if game is not None:
            self._ensure_metadata(game)

    def closeEvent(self, event):  # noqa: N802
        # Quitting mid-scan / mid-fetch must not abort the process:
        # stop the timers, close Big Picture, stop any recording
        # (a detached recorder would otherwise keep recording
        # forever), then drain the worker threads before Qt tears
        # anything down.
        self._closing = True
        if hasattr(self, "_timer"):
            self._timer.stop()
        if self.bp is not None:
            self.bp.close()
        if self.recorder.is_running():
            self.recorder.stop()
            self._update_record_ui()
        drain_live_workers(budget=45.0)
        super().closeEvent(event)

    # -------------------------------------------------------- launch
    def launch(self, game):
        if self._closing:
            return
        want_record = self.record_session.isChecked()
        if want_record:
            self.record_session.setChecked(False)
        # Game capture is armed BEFORE the process starts: the tap
        # lives in the game's own process via LD_PRELOAD, so the
        # environment has to be there at exec time.
        cap_env = None
        shm_name = None
        if want_record and recording.capture_available():
            shm_name = (f"recomp-capture-{os.getpid()}-"
                        f"{int(time.time() * 1000)}")
            fps = int(self.settings.get("recording", {})
                      .get("framerate", 60))
            cap_env = {
                "RECOMP_CAPTURE": "1",
                "RECOMP_CAPTURE_SHM": shm_name,
                "RECOMP_CAPTURE_FPS": str(fps),
                "LD_PRELOAD": str(recording.find_capture_lib()),
            }
        try:
            proc, supplied = scanner.launch_game(game, self.settings,
                                                 LOG_DIR,
                                                 extra_env=cap_env)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Launch failed",
                                f"Could not start "
                                f"{scanner.display_title(game)}:\n{e}")
            return
        self._sessions[proc.pid] = (game, proc)
        msg = f"Launched {scanner.display_title(game)}."
        if supplied:
            msg += (" Wrote retroachievements.toml from your saved "
                    "token first.")
            self.d_toml.setText(self._toml_status(game))
        self.statusBar().showMessage(msg)
        if want_record:
            if shm_name is not None:
                try:
                    out = self.recorder.start(
                        self.settings, scanner.display_title(game),
                        tied_pid=proc.pid, shm_name=shm_name)
                    self._cap_reported = "pending"
                    msg += (f" Recording: starting game capture → "
                            f"{out.name}…")
                    self.statusBar().showMessage(msg)
                except Exception as e:  # noqa: BLE001
                    self.statusBar().showMessage(
                        msg + f" Recording failed to start: {e}")
            else:
                self.statusBar().showMessage(
                    msg + " Recording unavailable: "
                    + recording.unavailable_reason())
        self._update_record_ui()

    # ----------------------------------------------------- recording
    def toggle_record(self):
        # Game capture can only start WITH a game launch (the tap
        # is preloaded into the game's process), so the toolbar
        # button stops a live capture, or arms/disarms the next
        # launch — it never records the desktop.
        if self.recorder.is_running():
            out = self.recorder.output
            stats = self.recorder.stop()
            if stats and stats.get("frames"):
                self.statusBar().showMessage(
                    f"Recording saved: {out}")
            else:
                self.statusBar().showMessage(
                    "Recording stopped — no frames were captured.")
            self._cap_reported = None
        elif self.record_session.isChecked():
            self.record_session.setChecked(False)
            self.statusBar().showMessage(
                "Recording disarmed — the next launch won't record.")
        else:
            if not recording.capture_available():
                QMessageBox.information(
                    self, "Recording unavailable",
                    recording.unavailable_reason())
                return
            self.record_session.setChecked(True)
            self.statusBar().showMessage(
                "Recording armed — the next game you launch will be "
                "recorded with game capture.")
        self._update_record_ui()

    def open_recordings(self):
        folder = Path(self.settings.get("recording", {}).get("folder")
                      or recording.default_recordings_dir())
        folder.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(folder)  # noqa: S606
        else:
            import subprocess
            subprocess.Popen(["xdg-open", str(folder)])

    def _update_record_ui(self):
        running = self.recorder.is_running()
        armed = self.record_session.isChecked()
        if running:
            text = "■ Stop recording"
        elif armed:
            text = "● Record (armed)"
        else:
            text = "● Record"
        self.record_btn.setText(text)
        self.record_btn.setProperty("recording", "true" if running
                                    else "false")
        self.record_btn.style().unpolish(self.record_btn)
        self.record_btn.style().polish(self.record_btn)
        if not running:
            self.rec_label.setText("")

    def _tick(self):
        # Capture negotiation: announce the outcome once — live
        # game capture, or honestly unavailable for this game (no
        # fallback, no silent nothing).
        if self.recorder.state in ("pending", "live"):
            state = self.recorder.poll()
            if state != self._cap_reported:
                self._cap_reported = state
                if state == "live":
                    self.statusBar().showMessage(
                        f"Recording {self.recorder.game_title} — "
                        f"game capture → "
                        f"{self.recorder.output.name}")
                elif state == "unavailable":
                    self.statusBar().showMessage(
                        "Recording unavailable for this game — it "
                        "has no capture tap. The game plays "
                        "normally.")
                    self._update_record_ui()
        # REC elapsed display.
        if self.recorder.is_running():
            secs = int(self.recorder.elapsed())
            label = ("● REC" if self.recorder.state == "live"
                     else "● CAPTURE…")
            self.rec_label.setText(
                f"{label} {secs // 3600:02d}:"
                f"{(secs % 3600) // 60:02d}:{secs % 60:02d}")
        elif self.rec_label.text():
            self._update_record_ui()
        # Watch game sessions; a recording tied to a game stops when
        # that game exits.
        for pid, (game, proc) in list(self._sessions.items()):
            if proc.poll() is not None:
                del self._sessions[pid]
                if self.bp is not None and self.bp.isVisible():
                    # Couch flow: hand focus back to Big Picture
                    # when a game exits.
                    self.bp.raise_()
                    self.bp.activateWindow()
                    self.bp.setFocus()
                if self.recorder.tied_pid == pid \
                        and self.recorder.state in (
                            "pending", "live", "done", "unavailable"):
                    out = self.recorder.output
                    stats = self.recorder.stop()
                    self._cap_reported = None
                    if stats and stats.get("frames"):
                        self.statusBar().showMessage(
                            f"{scanner.display_title(game)} exited "
                            f"— recording saved: {out} (game "
                            f"capture, {stats['frames']} frames, "
                            f"{stats['encoder']})")
                    elif self.recorder.state == "unavailable":
                        self.statusBar().showMessage(
                            f"{scanner.display_title(game)} exited "
                            f"— recording was unavailable for this "
                            f"game.")
                    else:
                        self.statusBar().showMessage(
                            f"{scanner.display_title(game)} exited "
                            f"— recording produced no frames.")
                    self._update_record_ui()

    # ------------------------------------------------- RA / Phase 2
    def _ra_client(self):
        if self.ra_client_override is not None:
            return self.ra_client_override
        user = self.settings.get("ra_username", "").strip()
        key = self.settings.get("ra_web_api_key", "").strip()
        if not user or not key:
            return None
        return ra_mod.RAClient(user, key)

    def _run_worker(self, worker, handler):
        self._workers.add(worker)
        _LIVE_WORKERS.add(worker)
        worker.done.connect(handler)
        worker.finished.connect(lambda w=worker: self._worker_done(w))
        worker.start()

    def _worker_done(self, worker):
        self._workers.discard(worker)
        _LIVE_WORKERS.discard(worker)
        worker.deleteLater()

    def _load_achievements(self, game):
        if self._closing:
            return
        self.ach_list.clear()
        if not game.get("ra_supported", True):
            self.d_ra_summary.setText(
                "This package's runtime has no RetroAchievements "
                "integration, so there are no achievements to show.")
            return
        client = self._ra_client()
        if client is None:
            self.d_ra_summary.setText(
                "Add your RetroAchievements username and Web API key "
                "in Settings to see achievements here.")
            return
        self._ra_wanted = game["folder"]
        if game.get("ra_game_id"):
            self.d_ra_summary.setText("Loading achievements…")
            worker = FuncWorker(
                "progress",
                lambda: client.get_game_progress(game["ra_game_id"]))
            self._run_worker(worker, self._progress_done)
        else:
            self.d_ra_summary.setText("Matching this game on "
                                      "RetroAchievements…")
            worker = FuncWorker("match", lambda: self._match_game(
                client, game))
            self._run_worker(worker, self._match_done)

    def _match_game(self, client, game):
        """Runs on a worker thread: resolve the RA game ID from the
        user's completion progress first, then the console's game
        list (disk-cached for a week)."""
        if self._completion_cache is None:
            cache_file = CACHE_DIR / "completion.json"
            try:
                self._completion_cache = json.loads(
                    cache_file.read_text())
            except Exception:  # noqa: BLE001
                self._completion_cache = client.get_completion_progress()
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(
                    json.dumps(self._completion_cache))
        console_id = scanner.SYSTEMS[game["system"]][1]
        game_id = ra_mod.match_game(scanner.display_title(game),
                                    console_id, self._completion_cache)
        if game_id is None:
            list_file = CACHE_DIR / f"gamelist-{console_id}.json"
            game_list = None
            try:
                if time.time() - list_file.stat().st_mtime < 7 * 86400:
                    game_list = json.loads(list_file.read_text())
            except Exception:  # noqa: BLE001
                game_list = None
            if game_list is None:
                game_list = client.get_game_list(console_id)
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                list_file.write_text(json.dumps(game_list))
            game_id = ra_mod.match_game(scanner.display_title(game),
                                        console_id,
                                        self._completion_cache,
                                        game_list)
        return game_id

    def _match_done(self, key, result):
        game = self.selected
        if game is None or self._ra_wanted != game["folder"]:
            return
        if isinstance(result, Exception):
            self.d_ra_summary.setText(f"RetroAchievements: {result}")
            return
        if result is None:
            self.d_ra_summary.setText(
                "No match found on RetroAchievements for this title. "
                "If it has a set there, use “Set RA game ID…” with "
                "the number from its retroachievements.org URL.")
            return
        game["ra_game_id"] = result
        scanner.save_library(self.games)
        self.d_ra_id.setText(f"RA game: {result}")
        self._load_achievements(game)

    def _progress_done(self, key, result):
        game = self.selected
        if game is None or self._ra_wanted != game["folder"]:
            return
        if isinstance(result, Exception):
            self.d_ra_summary.setText(f"RetroAchievements: {result}")
            return
        progress = result
        ra_meta = progress.get("meta") or {}
        if ra_meta:
            # RA's own fields fill whatever the metadata database
            # left empty (merge never overwrites, and a manual
            # edit still wins at render time).
            metadata.merge_meta(
                game, ra_meta,
                {f: metadata.SOURCE_RA for f in ra_meta})
            scanner.save_library(self.games)
            self._refresh_metadata(game)
        if progress["num_awarded"]:
            self.d_ra_summary.setText(
                f"{progress['num_awarded']} of "
                f"{progress['num_achievements']} unlocked · "
                f"{progress['points_earned']} / "
                f"{progress['points_total']} points")
        else:
            self.d_ra_summary.setText(
                f"0 of {progress['num_achievements']} unlocked · "
                f"0 / {progress['points_total']} points — "
                "no progress yet")
        self.ach_list.clear()
        for ach in progress["achievements"]:
            state = ("★ " if ach["hardcore"] else "✓ ") if ach["earned"] \
                else ""
            when = f"\n   unlocked {ach['date_earned'][:10]}" \
                if ach["earned"] and ach["date_earned"] else ""
            item = QListWidgetItem(
                f"{state}{ach['title']} — {ach['points']} pts\n"
                f"   {ach['description']}{when}")
            item.setData(Qt.UserRole, ach["id"])
            if not ach["earned"]:
                item.setForeground(QColor("#8a8d94"))
            self.ach_list.addItem(item)
            self._fetch_badge(game, ach)

    def _fetch_badge(self, game, ach):
        cache_file = CACHE_DIR / "badges" / (
            ach["badge_name"] + ("" if ach["earned"] else "_lock")
            + ".png")
        if cache_file.is_file():
            self._set_badge_icon(game, ach["id"],
                                 QPixmap(str(cache_file)))
            return
        url = ra_mod.badge_url(ach["badge_name"],
                               locked=not ach["earned"])
        if any(w.url == url for w in self._img_workers):
            return
        worker = ImageWorker(url)
        worker.game_folder = game["folder"]
        worker.ach_id = ach["id"]
        worker.cache_file = cache_file
        self._img_workers.add(worker)
        _LIVE_WORKERS.add(worker)
        worker.done.connect(self._badge_done)
        worker.finished.connect(
            lambda w=worker: self._img_worker_done(w))
        worker.start()

    def _img_worker_done(self, worker):
        self._img_workers.discard(worker)
        _LIVE_WORKERS.discard(worker)
        worker.deleteLater()

    def _badge_done(self, url, data):
        worker = self.sender()
        if worker is None or not data:
            return
        pix = QPixmap()
        if not pix.loadFromData(data):
            return
        try:
            worker.cache_file.parent.mkdir(parents=True, exist_ok=True)
            worker.cache_file.write_bytes(data)
        except Exception:  # noqa: BLE001
            pass
        self._set_badge_icon(self.selected, worker.ach_id, pix)

    def _set_badge_icon(self, game, ach_id, pix):
        if game is None or self.selected is None \
                or game["folder"] != self.selected["folder"]:
            return
        # Achievements are listed in parse order; find by position is
        # fragile, so the list items carry their ID in UserRole data.
        for i in range(self.ach_list.count()):
            item = self.ach_list.item(i)
            if item.data(Qt.UserRole) == ach_id:
                item.setIcon(QIcon(pix.scaled(
                    48, 48, Qt.KeepAspectRatio, Qt.SmoothTransformation)))
                return


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLE)
    w = MainWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
