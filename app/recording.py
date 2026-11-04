"""Steam-style game capture: the launcher records a game's own
frames, tapped inside the game process before present — never the
screen, so a recording cannot tear.

How it works (Linux only):
  * When a launch has recording requested, the launcher starts the
    game with ``librecompcapture.so`` (in ../bin) in LD_PRELOAD and
    RECOMP_CAPTURE=1 in its environment. The library hooks the GL
    present calls (SDL_GL_SwapWindow / glXSwapBuffers /
    eglSwapBuffers), reads each finished frame back, and publishes
    it into a POSIX shared-memory ring.
  * Games that present through Vulkan can't be hooked that way,
    so the same launch also makes the implicit Vulkan layer
    ``VK_LAYER_RECOMP_CAPTURE`` (also in ../bin) discoverable and
    names it for the loader; the layer wraps vkQueuePresentKHR
    and publishes into the same ring. The manifest's
    enable_environment keeps the layer dormant unless
    RECOMP_CAPTURE=1, and none of these variables are set on a
    launch that isn't recording.
  * A small helper, ``recomp-recorder`` (also in ../bin), maps the
    ring and encodes the frames to MP4 with ffmpeg — NVENC when a
    real NVIDIA GPU is present, libx264 otherwise.
  * A game that never publishes (no present path either tap can
    reach — a shell launcher, a non-game executable) simply has
    no tap: the capture reports itself unavailable for that game
    and the game plays on untouched. There is deliberately no
    screen-capture fallback.

Phase 1 is VIDEO ONLY: the runtime exposes no clean post-mix audio
hook for these packages, so recordings have no audio track yet.

Recordings land in the folder from Settings (default
~/Videos/Recomp Recordings) as ``<Game> - YYYY-MM-DD HH-MM-SS.mp4``.
"""

from __future__ import annotations

import atexit
import os
import re
import shutil
import signal
import struct
import subprocess
import threading
import time
from pathlib import Path

IS_LINUX = os.name == "posix"

_HERE = Path(__file__).resolve().parent
BIN_DIR = _HERE.parent / "bin"

NEGOTIATE_SECONDS = 5.0

# recomp_capture_shm.h v1 header (first 4096 bytes of the segment)
_HDR = struct.Struct("<QIIQIIIIIII")  # through video_slot_bytes @52
_HDR_TAIL = struct.Struct("<QQQQQ")   # video_offset .. consumer hb


def default_recordings_dir() -> str:
    return str(Path.home() / "Videos" / "Recomp Recordings")


def find_capture_lib():
    p = BIN_DIR / "librecompcapture.so"
    return p if p.is_file() else None


def find_capture_layer():
    """The Vulkan capture layer (library + manifest), or None.
    Optional next to find_capture_lib(): GL games record without
    it, so its absence never makes capture itself unavailable —
    Vulkan-presented games just report unavailable, as before."""
    lib = BIN_DIR / "libVkLayer_recomp_capture.so"
    manifest = BIN_DIR / "VkLayer_recomp_capture.json"
    return lib if lib.is_file() and manifest.is_file() else None


def find_recorder_binary():
    p = BIN_DIR / "recomp-recorder"
    return p if p.is_file() else None


def find_ffmpeg():
    return shutil.which("ffmpeg")


def capture_available() -> bool:
    return bool(IS_LINUX and find_capture_lib()
                and find_recorder_binary() and find_ffmpeg())


def unavailable_reason() -> str:
    if not IS_LINUX:
        return "game capture is Linux-only for now"
    missing = []
    if not find_capture_lib():
        missing.append("capture library (bin/librecompcapture.so)")
    if not find_recorder_binary():
        missing.append("recorder helper (bin/recomp-recorder)")
    if not find_ffmpeg():
        missing.append("ffmpeg (install it: sudo apt install ffmpeg)")
    return "game capture unavailable — missing " + ", ".join(missing)


def read_capture_header(shm_name: str):
    """Read the capture stream header from /dev/shm/<name>.
    Returns a dict or None when no live stream exists."""
    path = Path("/dev/shm") / shm_name
    try:
        with open(path, "rb") as f:
            raw = f.read(4096)
    except OSError:
        return None
    if len(raw) < 4096:
        return None
    (magic, version, _hb, _total, state, fps, _fmt, width, height,
     _slots, _slot_bytes) = _HDR.unpack_from(raw, 0)
    if magic != 0x3150414352454352 or version != 1:
        return None
    (video_offset, published, dropped, prod_hb,
     _cons_hb) = _HDR_TAIL.unpack_from(raw, 56)
    return {
        "state": state, "fps": fps, "width": width, "height": height,
        "video_offset": video_offset, "published": published,
        "dropped": dropped, "producer_hb_ns": prod_hb,
    }


def output_path_for(settings, game_title: str) -> Path:
    rec = (settings or {}).get("recording", {})
    folder = Path(rec.get("folder") or default_recordings_dir())
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H-%M-%S")
    safe = re.sub(r"[^\w\- ]+", "", game_title or "Game").strip() \
        or "Game"
    return folder / f"{safe} - {stamp}.mp4"


_STATS_RE = re.compile(
    r"encoder=(\S+) frames=(\d+) repeated=(\d+) "
    r"producer_dropped=(\d+) audio_frames=(\d+) duration=([\d.]+)s")


def parse_stats(lines):
    for line in reversed(lines):
        m = _STATS_RE.search(line)
        if m:
            return {
                "encoder": m.group(1),
                "frames": int(m.group(2)),
                "repeated": int(m.group(3)),
                "producer_dropped": int(m.group(4)),
                "audio_frames": int(m.group(5)),
                "duration": float(m.group(6)),
            }
    return None


_LIVE = set()


def _kill_live():
    for cap in list(_LIVE):
        try:
            cap.stop()
        except Exception:  # noqa: BLE001
            pass


atexit.register(_kill_live)


class GameCapture:
    """One game-capture recording: the recomp-recorder helper
    process plus negotiation against the game's SHM stream.

    States: idle -> pending (helper running, waiting for the game
    to publish) -> live (stream confirmed) -> done | unavailable.
    """

    def __init__(self):
        self.proc = None
        self.started_at = None
        self.output = None
        self.game_title = ""
        self.tied_pid = None
        self.shm_name = None
        self.state = "idle"
        self.stats = None
        self._lines = []
        self._reader = None
        self._deadline = 0.0

    # ----------------------------------------------------------
    def _drain(self):
        try:
            for line in self.proc.stdout:
                self._lines.append(line.rstrip())
        except Exception:  # noqa: BLE001
            pass

    def start(self, settings, game_title, tied_pid=None,
              shm_name=None):
        """Spawn recomp-recorder for a game launched with the
        capture environment. Returns the output path."""
        if self.is_running():
            self.stop()
        rec_bin = find_recorder_binary()
        if rec_bin is None or not shm_name:
            raise RuntimeError(unavailable_reason())
        self.output = output_path_for(settings, game_title)
        self.game_title = game_title or ""
        self.tied_pid = tied_pid
        self.shm_name = shm_name
        self.stats = None
        self._lines = []
        fps = int((settings or {}).get("recording", {})
                  .get("framerate", 60))
        argv = [str(rec_bin), "--shm", shm_name,
                "--out", str(self.output), "--fps", str(fps)]
        if tied_pid:
            argv += ["--game-pid", str(tied_pid)]
        self.proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, start_new_session=True)
        self.started_at = time.monotonic()
        self._deadline = self.started_at + NEGOTIATE_SECONDS
        self.state = "pending"
        self._reader = threading.Thread(target=self._drain,
                                        daemon=True)
        self._reader.start()
        _LIVE.add(self)
        return self.output

    def poll(self):
        """Advance negotiation; call regularly. Returns the state."""
        if self.state == "pending":
            hdr = read_capture_header(self.shm_name) \
                if self.shm_name else None
            if hdr and hdr["state"] == 1:
                self.state = "live"
            elif self.proc is not None and self.proc.poll() is not None:
                self.state = "unavailable"
            elif time.monotonic() > self._deadline:
                self.state = "unavailable"
                # The helper would give up on its own (its stream
                # wait times out); stop it now so nothing lingers.
                try:
                    os.killpg(self.proc.pid, signal.SIGINT)
                except (ProcessLookupError, PermissionError):
                    pass
        if self.state == "live" and self.proc is not None \
                and self.proc.poll() is not None:
            self.state = "done"
            self._finish_stats()
        return self.state

    def _finish_stats(self):
        if self._reader is not None:
            self._reader.join(timeout=2)
        self.stats = parse_stats(self._lines)

    # ----------------------------------------------------------
    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None \
            and self.state in ("pending", "live")

    def elapsed(self) -> float:
        return time.monotonic() - self.started_at \
            if self.started_at else 0.0

    def stop(self):
        """SIGINT the helper (it finalizes the MP4 cleanly), with
        SIGTERM/SIGKILL escalation. Returns the parsed stats."""
        proc, self.proc = self.proc, None
        if proc is not None:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGINT)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    # Finalizing drains the encoder queue; give it
                    # the SIGINT a real chance before escalating.
                    try:
                        proc.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except (ProcessLookupError,
                                PermissionError):
                            pass
                        proc.wait(timeout=10)
            self._finish_stats()
        if self.state in ("pending", "live"):
            self.state = "done"
        _LIVE.discard(self)
        return self.stats
