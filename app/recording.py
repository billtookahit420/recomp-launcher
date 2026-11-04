"""Recomp Launcher — game recording via GPU Screen Recorder.

Drives the `gpu-screen-recorder` CLI (dec05eba's GPU Screen
Recorder — the recorder Duane uses on Kubuntu). Command shape and
signal behaviour are from the tool's own documentation:

    gpu-screen-recorder -w screen -f 60 -a default_output -o out.mp4

  -w screen        capture the monitor/screen. Duane's screen
                   tearing was fixed by MONITOR capture while games
                   run borderless — never window capture — so the
                   launcher only offers screen/monitor sources.
  -f <fps>         framerate.
  -a default_output  desktop audio (omitted = no audio track).
  -o <file.mp4>    output file.
  Stop             send SIGINT: the recorder finalizes and saves.

Linux only: GPU Screen Recorder has no Windows version, and the UI
says so on Windows instead of pretending.
"""
import atexit
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BINARY = "gpu-screen-recorder"
IS_LINUX = sys.platform.startswith("linux")

# A recorder left running when the launcher exits would record
# forever with nobody able to stop it from the UI — track live
# recorders and stop them all at interpreter exit (MainWindow's
# closeEvent stops its own recorder first; this is the backstop).
_LIVE_RECORDERS = set()


def _stop_live_recorders():
    for rec in list(_LIVE_RECORDERS):
        try:
            rec.stop()
        except Exception:  # noqa: BLE001
            pass


atexit.register(_stop_live_recorders)


def find_binary():
    return shutil.which(BINARY)


def available():
    return IS_LINUX and find_binary() is not None


def unavailable_reason():
    if not IS_LINUX:
        return ("Recording is Linux-only in this version — GPU Screen "
                "Recorder does not exist on Windows.")
    return ("GPU Screen Recorder is not installed. Install it with "
            "your package manager or from Flathub "
            "(com.dec05eba.gpu_screen_recorder), then restart the "
            "launcher.")


def default_recordings_dir():
    return str(Path.home() / "Videos" / "RecompLauncher")


def build_command(settings, game_title):
    """(argv, output Path) for one recording of `game_title`."""
    rec = settings.get("recording", {})
    out_dir = Path(rec.get("folder") or default_recordings_dir())
    out_dir = out_dir.expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H-%M-%S")
    safe = re.sub(r"[^A-Za-z0-9]+", "-", game_title or "Recording")
    safe = safe.strip("-") or "Recording"
    out_path = out_dir / f"{safe} - {stamp}.mp4"
    source = (rec.get("source") or "screen").strip() or "screen"
    fps = int(rec.get("framerate", 60) or 60)
    cmd = [find_binary() or BINARY, "-w", source, "-f", str(fps)]
    if rec.get("audio", True):
        cmd += ["-a", "default_output"]
    cmd += ["-o", str(out_path)]
    return cmd, out_path


class Recorder:
    """One recording at a time; tracks the process so the UI can
    show elapsed time and stop it cleanly."""

    def __init__(self):
        self.proc = None
        self.started_at = None
        self.output = None
        self.game_title = None
        self.tied_pid = None      # game process this recording follows

    def is_running(self):
        return self.proc is not None and self.proc.poll() is None

    def elapsed(self):
        if self.started_at is None:
            return 0.0
        return time.monotonic() - self.started_at

    def start(self, settings, game_title, tied_pid=None):
        if self.is_running():
            raise RuntimeError("a recording is already running")
        if not available():
            raise RuntimeError(unavailable_reason())
        cmd, out_path = build_command(settings, game_title)
        self.proc = subprocess.Popen(
            cmd, start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.started_at = time.monotonic()
        self.output = out_path
        self.game_title = game_title
        self.tied_pid = tied_pid
        _LIVE_RECORDERS.add(self)
        return out_path

    def stop(self):
        """SIGINT so the file is finalized and saved; escalate only
        if the recorder ignores it."""
        _LIVE_RECORDERS.discard(self)
        proc, self.proc = self.proc, None
        self.started_at = None
        self.tied_pid = None
        if proc is None:
            return
        if proc.poll() is None:
            try:
                proc.send_signal(signal.SIGINT)
                proc.wait(timeout=6)
            except Exception:  # noqa: BLE001
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except Exception:  # noqa: BLE001
                    try:
                        proc.kill()
                    except Exception:  # noqa: BLE001
                        pass
