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
is missing, the backend simply reports unavailable and Big Picture
falls back to the keyboard, which always works.
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


class PadState:
    def __init__(self, buttons=(), lx=0.0, ly=0.0):
        self.buttons = set(buttons)     # names from _BTN
        self.lx = lx                    # -1.0 .. 1.0
        self.ly = ly


class SdlBackend:
    """Real controller via the system SDL2 shared library."""

    def __init__(self):
        self.available = False
        self._sdl = None
        self._ctrl = None
        try:
            libname = ctypes.util.find_library("SDL2") \
                or "libSDL2-2.0.so.0"
            sdl = ctypes.CDLL(libname)
            if sdl.SDL_Init(_INIT_GAMECONTROLLER | _INIT_JOYSTICK) != 0:
                return
            sdl.SDL_GameControllerOpen.restype = ctypes.c_void_p
            for i in range(sdl.SDL_NumJoysticks()):
                if sdl.SDL_IsGameController(i):
                    self._ctrl = sdl.SDL_GameControllerOpen(i)
                    if self._ctrl:
                        break
            if not self._ctrl:
                return
            self._sdl = sdl
            self.available = True
        except Exception:  # noqa: BLE001
            self.available = False

    def read(self):
        if not self.available:
            return None
        sdl = self._sdl
        try:
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
            self.available = False
            return None


class FakeBackend:
    """Scripted backend for tests: each read() returns the next
    scripted PadState, then keeps returning the last one."""

    def __init__(self, script):
        self.script = list(script)
        self.available = True
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

    def poll(self, now=None):
        now = time.monotonic() if now is None else now
        state = self.backend.read()
        if state is None:
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
