"""Recomp Launcher — gamepad input for Big Picture mode.

Polls the first SDL2 game controller (via ctypes on the system
libSDL2 — no extra Python dependency) and turns raw pad state into
navigation actions:

    d-pad / left stick  -> "left" "right" "up" "down"
    A                   -> "launch"
    B                   -> "back"
    LB / RB             -> "prev_section" / "next_section"

The mapping logic (edge detection, stick dead-zone + repeat) lives
in GamepadPoller and only depends on a backend that returns
PadState snapshots, so it is unit-testable with a scripted fake
backend on machines with no pad attached. If SDL2 or a controller
is missing, Big Picture falls back to the keyboard, which always
works.

The real backend is SELF-HEALING about discovery. A controller
that is asleep, unplugged, or simply not yet enumerated when Big
Picture opens is picked up automatically: whenever no controller
is open, read() re-runs discovery (throttled to ~1 attempt/sec),
and a controller that disappears drops back to discovery the same
way. (The original backend scanned exactly once at construction,
so a pad that wasn't ready at that instant was invisible forever.)
"""
import ctypes
import ctypes.util
import time

# SDL_GameControllerButton
_BTN = {"a": 0, "b": 1, "start": 6, "lb": 9, "rb": 10,
        "dpad_up": 11, "dpad_down": 12, "dpad_left": 13,
        "dpad_right": 14}
_AXIS_LEFTX, _AXIS_LEFTY = 0, 1
_INIT_GAMECONTROLLER = 0x00002000
_INIT_JOYSTICK = 0x00000200

_BUTTON_ACTIONS = {
    "dpad_left": "left", "dpad_right": "right",
    "dpad_up": "up", "dpad_down": "down",
    "a": "launch", "start": "launch", "b": "back",
    "lb": "prev_section", "rb": "next_section",
}
_STICK_DEADZONE = 0.45
_STICK_REPEAT_S = 0.17
_REDISCOVER_S = 1.0     # min seconds between discovery/init attempts

# Mapping insurance, embedded verbatim from the community
# SDL_GameControllerDB (github.com/gabomdq/SDL_GameControllerDB,
# fetched 2026-11-04): the Linux entries for the 8BitDo Ultimate 2
# (wired / 2.4 GHz dongle / Bluetooth GUID variants), plus its
# Windows entry — the same pad presents a different GUID there.
# SDL's built-in database does not always carry this pad, and
# without a mapping SDL won't classify it as a game controller
# at all.
_EXTRA_MAPPINGS = (
    "03000000c82d00001260000000000000,8BitDo Ultimate 2,a:b0,b:b1,"
    "back:b10,dpdown:h0.4,dpleft:h0.8,dpright:h0.2,dpup:h0.1,"
    "guide:b12,leftshoulder:b6,leftstick:b13,lefttrigger:b8,"
    "leftx:a0,lefty:a1,paddle1:b17,paddle2:b16,paddle3:b2,"
    "paddle4:b5,rightshoulder:b7,rightstick:b14,righttrigger:b9,"
    "rightx:a3,righty:a4,start:b11,x:b3,y:b4,platform:Windows,",
    "03000000c82d00000b31000014010000,8BitDo Ultimate 2,a:b0,b:b1,"
    "back:b6,dpdown:h0.4,dpleft:h0.8,dpright:h0.2,dpup:h0.1,guide:b8,"
    "leftshoulder:b4,leftstick:b9,lefttrigger:a2,leftx:a0,lefty:a1,"
    "rightshoulder:b5,rightstick:b10,righttrigger:a5,rightx:a3,"
    "righty:a4,start:b7,x:b2,y:b3,platform:Linux,",
    "03000000c82d00001260000011010000,8BitDo Ultimate 2,a:b0,b:b1,"
    "back:b10,dpdown:h0.4,dpleft:h0.8,dpright:h0.2,dpup:h0.1,"
    "guide:b12,leftshoulder:b6,leftstick:b13,lefttrigger:a5,"
    "leftx:a0,lefty:a1,paddle1:b17,paddle2:b16,paddle3:b2,"
    "paddle4:b5,rightshoulder:b7,rightstick:b14,righttrigger:a4,"
    "rightx:a2,righty:a3,start:b11,x:b3,y:b4,platform:Linux,",
    "05000000c82d00001260000001000000,8BitDo Ultimate 2,a:b0,b:b1,"
    "back:b10,dpdown:h0.4,dpleft:h0.8,dpright:h0.2,dpup:h0.1,"
    "guide:b12,leftshoulder:b6,leftstick:b13,lefttrigger:a5,"
    "leftx:a0,lefty:a1,paddle1:b17,paddle2:b16,paddle3:b2,"
    "paddle4:b5,rightshoulder:b7,rightstick:b14,righttrigger:a4,"
    "rightx:a2,righty:a3,start:b11,x:b3,y:b4,platform:Linux,",
)


class PadState:
    def __init__(self, buttons=(), lx=0.0, ly=0.0):
        self.buttons = set(buttons)     # names from _BTN
        self.lx = lx                    # -1.0 .. 1.0
        self.ly = ly


class SdlBackend:
    """Real controller via the system SDL2 shared library.

    Discovery is not a one-shot: construction makes a best-effort
    first attempt, and every read() with no controller open retries
    (throttled). `available` means the SDL input subsystem is up;
    `connected` / `controller_name` describe the pad itself.
    """

    def __init__(self):
        self._sdl = None
        self._ctrl = None
        self.controller_name = None
        self._next_try = 0.0
        self._try_open(time.monotonic())

    @property
    def available(self):
        return self._sdl is not None

    @property
    def connected(self):
        return self._ctrl is not None

    # ------------------------------------------------------ internals
    def _load_sdl(self):
        """Load libSDL2 and init the controller subsystem once.
        Returns the library handle, or None if either step failed
        (the caller schedules a retry — failure is not permanent)."""
        libname = ctypes.util.find_library("SDL2") \
            or "libSDL2-2.0.so.0"
        sdl = ctypes.CDLL(libname)
        sdl.SDL_GameControllerOpen.restype = ctypes.c_void_p
        sdl.SDL_GameControllerName.restype = ctypes.c_char_p
        if sdl.SDL_Init(_INIT_GAMECONTROLLER | _INIT_JOYSTICK) != 0:
            return None
        for line in _EXTRA_MAPPINGS:
            try:
                sdl.SDL_GameControllerAddMapping(line.encode())
            except Exception:  # noqa: BLE001
                pass
        return sdl

    def _try_open(self, now):
        """One discovery pass: init SDL if needed, then open the
        first device SDL classifies as a game controller."""
        if self._sdl is None:
            try:
                self._sdl = self._load_sdl()
            except Exception:  # noqa: BLE001
                self._sdl = None
            if self._sdl is None:
                self._next_try = now + _REDISCOVER_S
                return
        sdl = self._sdl
        try:
            sdl.SDL_GameControllerUpdate()   # hotplug/state refresh
            for i in range(sdl.SDL_NumJoysticks()):
                if sdl.SDL_IsGameController(i):
                    ctrl = sdl.SDL_GameControllerOpen(i)
                    if ctrl:
                        self._ctrl = ctrl
                        name = sdl.SDL_GameControllerName(ctrl)
                        self.controller_name = (
                            name.decode("utf-8", "replace")
                            if name else "Controller")
                        return
        except Exception:  # noqa: BLE001
            pass
        self._next_try = now + _REDISCOVER_S

    def _drop(self):
        """Close the current controller and return to discovery."""
        if self._sdl is not None and self._ctrl is not None:
            try:
                self._sdl.SDL_GameControllerClose(self._ctrl)
            except Exception:  # noqa: BLE001
                pass
        self._ctrl = None
        self.controller_name = None
        self._next_try = 0.0    # next read() may retry at once

    # ---------------------------------------------------------- read
    def read(self):
        if self._ctrl is None:
            now = time.monotonic()
            if now >= self._next_try:
                self._try_open(now)
            if self._ctrl is None:
                return None
        sdl = self._sdl
        try:
            if not sdl.SDL_GameControllerGetAttached(self._ctrl):
                self._drop()
                return None
            sdl.SDL_GameControllerUpdate()
            buttons = {name for name, code in _BTN.items()
                       if sdl.SDL_GameControllerGetButton(
                           self._ctrl, code)}
            lx = sdl.SDL_GameControllerGetAxis(
                self._ctrl, _AXIS_LEFTX) / 32767.0
            ly = sdl.SDL_GameControllerGetAxis(
                self._ctrl, _AXIS_LEFTY) / 32767.0
            return PadState(buttons, lx, ly)
        except Exception:  # noqa: BLE001
            self._drop()
            return None


class FakeBackend:
    """Scripted backend for tests: each read() returns the next
    scripted PadState, then keeps returning the last one."""

    def __init__(self, script):
        self.script = list(script)
        self.available = True
        self.connected = True
        self.controller_name = "Fake gamepad"
        self._i = 0

    def read(self):
        if self._i < len(self.script):
            state = self.script[self._i]
            self._i += 1
            return state
        return self.script[-1] if self.script else PadState()


class GamepadPoller:
    """Edge detection + stick repeat over a backend's PadStates."""

    def __init__(self, backend):
        self.backend = backend
        self._prev_buttons = set()
        self._stick_dir = None
        self._stick_next = 0.0

    @property
    def available(self):
        return getattr(self.backend, "available", False)

    @property
    def connected(self):
        return getattr(self.backend, "connected", False)

    @property
    def controller_name(self):
        return getattr(self.backend, "controller_name", None)

    def poll(self, now=None):
        now = time.monotonic() if now is None else now
        state = self.backend.read()
        if state is None:
            # No device right now (never found, or just unplugged):
            # forget held inputs so a button held at disconnect
            # can't wedge the edge detector when a pad returns.
            self._prev_buttons = set()
            self._stick_dir = None
            return []
        actions = []
        for name in sorted(state.buttons - self._prev_buttons):
            if name in _BUTTON_ACTIONS:
                actions.append(_BUTTON_ACTIONS[name])
        self._prev_buttons = set(state.buttons)

        direction = None
        if state.lx <= -_STICK_DEADZONE:
            direction = "left"
        elif state.lx >= _STICK_DEADZONE:
            direction = "right"
        elif state.ly <= -_STICK_DEADZONE:
            direction = "up"
        elif state.ly >= _STICK_DEADZONE:
            direction = "down"
        if direction is None:
            self._stick_dir = None
        elif direction != self._stick_dir or now >= self._stick_next:
            actions.append(direction)
            self._stick_dir = direction
            self._stick_next = now + _STICK_REPEAT_S
        return actions
