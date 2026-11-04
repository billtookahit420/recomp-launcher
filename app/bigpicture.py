"""Recomp Launcher — Big Picture mode.

A fullscreen, couch-friendly second view of the same library. It
owns no library logic of its own: the games come from the main
window (games_provider), and launching goes through the main
window's launch path (on_launch), so token supply, session
tracking and recording behave identically in both views.

Navigation:
  keyboard   arrows move focus · Enter launches · Q / E or
             PageUp / PageDown switch system section ·
             Esc returns to the desktop view
  gamepad    d-pad / left stick move · A launches · B returns to
             the desktop view · LB / RB switch section
             (polled via gamepad.py; keyboard always works)

B and Esc SWITCH BACK to the desktop view — Big Picture is a view,
not a separate app, and closing it never quits the launcher.
"""
from datetime import datetime
from pathlib import Path

import metadata
import scanner
from gamepad import GamepadPoller, SdlBackend

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import (QColor, QFont, QGuiApplication, QPainter,
                         QPixmap)
from PySide6.QtWidgets import (QGridLayout, QHBoxLayout, QLabel,
                               QScrollArea, QVBoxLayout, QWidget)

ART_W, ART_H = 232, 310
TILE_W, TILE_H = ART_W + 24, ART_H + 64

BP_STYLE = """
QWidget { background: #101216; color: #e8e8ea; }
QLabel#bpsection { font-size: 30px; font-weight: bold; }
QLabel#bpclock { font-size: 22px; color: #9a9da5; }
QLabel#bphelp { font-size: 14px; color: #7d8087; }
QLabel#bpmeta { font-size: 15px; color: #9fb4d8; }
QScrollArea { border: none; background: #101216; }
"""


def _placeholder(title, system_label):
    pix = QPixmap(ART_W, ART_H)
    pix.fill(QColor("#23252b"))
    p = QPainter(pix)
    p.setPen(QColor("#5b5e66"))
    p.setFont(QFont("Sans", 15))
    p.drawText(pix.rect().adjusted(10, 10, -10, -40),
               Qt.AlignCenter | Qt.TextWordWrap, title)
    p.setFont(QFont("Sans", 11))
    p.setPen(QColor("#484b52"))
    p.drawText(pix.rect().adjusted(10, 0, -10, -10),
               Qt.AlignHCenter | Qt.AlignBottom, system_label)
    p.end()
    return pix


class BPTile(QWidget):
    def __init__(self, game):
        super().__init__()
        self.game = game
        self.setFixedSize(TILE_W, TILE_H)
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 12, 12, 6)
        self.art = QLabel()
        self.art.setFixedSize(ART_W, ART_H)
        self.art.setAlignment(Qt.AlignCenter)
        pix = QPixmap(game["art"]) if game.get("art") else QPixmap()
        if pix.isNull():
            pix = _placeholder(scanner.display_title(game),
                               scanner.SYSTEMS[game["system"]][0])
        else:
            pix = pix.scaled(ART_W, ART_H, Qt.KeepAspectRatio,
                             Qt.SmoothTransformation)
        self.art.setPixmap(pix)
        v.addWidget(self.art)
        title = QLabel(scanner.display_title(game))
        title.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        title.setWordWrap(True)
        title.setFixedHeight(36)
        title.setStyleSheet("font-size: 16px;")
        v.addWidget(title)
        self.set_focused(False)

    def set_focused(self, on):
        self.art.setStyleSheet(
            "border: 3px solid #4d9fff; background: #1b2735;"
            if on else "border: 3px solid transparent;")


class BigPictureWindow(QWidget):
    def __init__(self, games_provider, on_launch, on_back,
                 poller=None, on_focus=None):
        super().__init__(None, Qt.Window | Qt.FramelessWindowHint)
        self.setStyleSheet(BP_STYLE)
        self.games_provider = games_provider
        self.on_launch = on_launch
        self.on_back = on_back
        self.on_focus = on_focus    # focused game changed (metadata)
        self.poller = poller or GamepadPoller(SdlBackend())
        self.sections = []          # ["all", system keys present]
        self.section_idx = 0
        self.focus_idx = 0
        self.tiles = []
        self._build_ui()
        self._pad_timer = QTimer(self)
        self._pad_timer.timeout.connect(self._poll_pad)
        self._clock_timer = QTimer(self)
        self._clock_timer.timeout.connect(self._update_clock)
        self._update_clock()

    # ------------------------------------------------------------ UI
    def _build_ui(self):
        v = QVBoxLayout(self)
        top = QHBoxLayout()
        brand = QLabel("Recomp Launcher")
        brand.setStyleSheet("font-size: 22px; font-weight: bold;")
        top.addWidget(brand)
        top.addSpacing(30)
        self.section_label = QLabel("")
        self.section_label.setObjectName("bpsection")
        top.addWidget(self.section_label)
        top.addStretch(1)
        self.clock = QLabel("")
        self.clock.setObjectName("bpclock")
        top.addWidget(self.clock)
        v.addLayout(top)
        self.meta_label = QLabel("")
        self.meta_label.setObjectName("bpmeta")
        v.addWidget(self.meta_label)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.host = QWidget()
        self.grid = QGridLayout(self.host)
        self.grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.scroll.setWidget(self.host)
        v.addWidget(self.scroll, 1)
        help_label = QLabel(
            "←→↑↓ move · Enter / A: play · Q / E or LB / RB: switch "
            "system · Esc / B: back to desktop view")
        help_label.setObjectName("bphelp")
        v.addWidget(help_label)

    def _update_clock(self):
        self.clock.setText(datetime.now().strftime("%H:%M"))

    # --------------------------------------------------------- state
    def current_section(self):
        if not self.sections:
            return "all"
        return self.sections[self.section_idx]

    def section_games(self):
        games = self.games_provider()
        section = self.current_section()
        if section == "all":
            return list(games)
        return [g for g in games if g["system"] == section]

    def focused_game(self):
        games = self.section_games()
        if not games:
            return None
        return games[min(self.focus_idx, len(games) - 1)]

    def refresh(self):
        games = self.games_provider()
        present = [s for s in scanner.SYSTEM_ORDER
                   if any(g["system"] == s for g in games)]
        current = self.current_section()
        self.sections = ["all"] + present
        self.section_idx = (self.sections.index(current)
                            if current in self.sections else 0)
        self._rebuild_tiles()

    def _rebuild_tiles(self):
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        games = self.section_games()
        self.focus_idx = max(0, min(self.focus_idx, len(games) - 1)) \
            if games else 0
        cols = self._cols()
        self._last_cols = cols
        self.tiles = []
        for i, g in enumerate(games):
            tile = BPTile(g)
            self.grid.addWidget(tile, i // cols, i % cols)
            self.tiles.append(tile)
        section = self.current_section()
        label = "All Games" if section == "all" \
            else scanner.SYSTEMS[section][0]
        self.section_label.setText(
            f"{label}  ({len(games)})")
        self._apply_focus()

    def _cols(self):
        width = max(self.width(), 800)
        return max(1, width // (TILE_W + 8))

    def refresh_meta(self):
        """Recompute the focused game's one-line metadata summary
        (year · genre · players — synopsis lead) from its stored
        values. Called whenever focus moves and whenever a
        metadata fetch lands for the focused game."""
        game = self.focused_game()
        text = metadata.compact_line(game) if game is not None else ""
        if text:
            fm = self.meta_label.fontMetrics()
            text = fm.elidedText(text, Qt.TextElideMode.ElideRight,
                                 max(240, self.width() - 60))
        self.meta_label.setText(text)

    def _apply_focus(self):
        for i, tile in enumerate(self.tiles):
            tile.set_focused(i == self.focus_idx)
        if 0 <= self.focus_idx < len(self.tiles):
            self.scroll.ensureWidgetVisible(
                self.tiles[self.focus_idx], 40, 40)
        self.refresh_meta()
        if self.on_focus is not None:
            game = self.focused_game()
            if game is not None:
                self.on_focus(game)

    # ----------------------------------------------------- navigation
    def handle_action(self, action):
        games = self.section_games()
        cols = self._cols()
        if action == "left":
            self.focus_idx = max(0, self.focus_idx - 1)
        elif action == "right":
            self.focus_idx = min(len(games) - 1, self.focus_idx + 1)
        elif action == "up":
            self.focus_idx = max(0, self.focus_idx - cols)
        elif action == "down":
            self.focus_idx = min(len(games) - 1, self.focus_idx + cols)
        elif action == "prev_section":
            self._switch_section(-1)
            return
        elif action == "next_section":
            self._switch_section(1)
            return
        elif action == "launch":
            game = self.focused_game()
            if game is not None:
                self.on_launch(game)
            return
        elif action == "back":
            self.go_back()
            return
        self._apply_focus()

    def _switch_section(self, delta):
        if not self.sections:
            return
        self.section_idx = (self.section_idx + delta) % len(self.sections)
        self.focus_idx = 0
        self._rebuild_tiles()

    def go_back(self):
        self.close()
        if self.on_back:
            self.on_back()

    def keyPressEvent(self, event):  # noqa: N802
        key = event.key()
        actions = {
            Qt.Key_Left: "left", Qt.Key_Right: "right",
            Qt.Key_Up: "up", Qt.Key_Down: "down",
            Qt.Key_Return: "launch", Qt.Key_Enter: "launch",
            Qt.Key_Escape: "back",
            Qt.Key_Q: "prev_section", Qt.Key_E: "next_section",
            Qt.Key_PageUp: "prev_section", Qt.Key_PageDown: "next_section",
        }
        action = actions.get(key)
        if action:
            self.handle_action(action)
            event.accept()
        else:
            super().keyPressEvent(event)

    def _poll_pad(self):
        if not self.isVisible() or not self.poller.available:
            return
        for action in self.poller.poll():
            self.handle_action(action)

    # ------------------------------------------------------------ open
    def open(self):
        self.refresh()
        # No window manager is guaranteed (and fullscreen flags are
        # only a hint without one): pin the window to the screen.
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            self.setGeometry(screen.geometry())
        self.showFullScreen()
        self.raise_()
        self.activateWindow()
        self.setFocus()
        self._pad_timer.start(50)
        self._clock_timer.start(10000)

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        # open() refreshes before the fullscreen size is known; lay
        # the grid out again if the column count changed.
        if self.tiles and self._cols() != getattr(self, "_last_cols", None):
            self._rebuild_tiles()

    def closeEvent(self, event):  # noqa: N802
        self._pad_timer.stop()
        self._clock_timer.stop()
        super().closeEvent(event)
