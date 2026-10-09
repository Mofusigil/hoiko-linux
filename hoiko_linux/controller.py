"""Xbox-compatible virtual Linux gamepad, independent of desktop display APIs."""
import fcntl
import os
from pathlib import Path
import struct
import time

EV_SYN, EV_KEY, EV_ABS = 0, 1, 3
BUTTONS = {"a": 304, "b": 305, "x": 307, "y": 308, "lb": 310,
           "rb": 311, "back": 314, "start": 315, "guide": 316,
           "ls": 317, "rs": 318}
DIRECTIONS = {"left": (16, -1), "right": (16, 1), "up": (17, -1), "down": (17, 1)}


def iow(number, size=4): return (1 << 30) | (size << 16) | (ord("U") << 8) | number
def io(number): return (ord("U") << 8) | number


class Gamepad:
    def __init__(self):
        self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        self.closed, self.pressed = False, set()
        try:
            for event in (EV_KEY, EV_ABS): fcntl.ioctl(self.fd, iow(100), event)
            for button in BUTTONS.values(): fcntl.ioctl(self.fd, iow(101), button)
            for axis in (0, 1, 2, 3, 4, 5, 16, 17):
                fcntl.ioctl(self.fd, iow(103), axis)
                lo, hi = (-1, 1) if axis >= 16 else ((0, 255) if axis in (2, 5) else (-32768, 32767))
                info = struct.pack("<H2x6i", axis, 0, lo, hi, 0, 0, 0)
                fcntl.ioctl(self.fd, iow(4, len(info)), info)
            setup = struct.pack("<HHHH80sI", 3, 0x045E, 0x028E, 0x0114,
                                b"Hoiko Linux Virtual Xbox 360", 0)
            fcntl.ioctl(self.fd, iow(3, len(setup)), setup)
            fcntl.ioctl(self.fd, io(1))
            time.sleep(0.6)
        except BaseException:
            self.close()
            raise

    def event(self, kind, code, value):
        data = struct.pack("@llHHi", 0, 0, kind, code, value)
        if os.write(self.fd, data) != len(data): raise OSError("Partial uinput event write")

    def press(self, button):
        self.release()
        buttons = (button,) if isinstance(button, str) else tuple(button)
        for button in buttons:
            if button in DIRECTIONS:
                axis, value = DIRECTIONS[button]
                self.event(EV_ABS, axis, value)
            else:
                self.event(EV_KEY, BUTTONS[button], 1)
            self.pressed.add(button)
        self.event(EV_SYN, 0, 0)

    def release(self):
        if self.closed: return
        for button in self.pressed:
            if button in DIRECTIONS: self.event(EV_ABS, DIRECTIONS[button][0], 0)
            else: self.event(EV_KEY, BUTTONS[button], 0)
        if self.pressed: self.event(EV_SYN, 0, 0)
        self.pressed.clear()

    def close(self):
        if getattr(self, "closed", True): return
        try:
            self.release()
            try: fcntl.ioctl(self.fd, io(2))
            except OSError: pass  # A failed setup may not have created a device.
        finally:
            self.closed = True
            os.close(self.fd)

    def __enter__(self): return self
    def __exit__(self, *args): self.close()


def execute(game, pad, player, result, initial, deadline=None, pacer=None, control=None, practice=None):
    """Send original commands with memory acknowledgements and bounded waits."""
    match = game.match_pointer()
    main = game.player(player)
    count_address = main + (7392 if game.game == 2 else 984)
    expected_count = initial["next_count"]
    expected_piece = initial["position"]["piece"]
    tick = game.match_frames()
    tick_changed = time.monotonic()
    def active():
        nonlocal tick, tick_changed
        if control: control.check()
        if deadline and time.monotonic() >= deadline: raise TimeoutError("Run duration ended")
        if not game.offline_status(player)["allowed"]:
            raise RuntimeError("Local offline match ended or mode changed")
        if game.match_pointer() != match or game.player(player) != main:
            raise RuntimeError("Match changed; recalculating")
        if game.memory.int32(main + (752 if game.game == 2 else 128)) > 1:
            raise RuntimeError("Player finished the match")
        now_tick = game.match_frames()
        if now_tick < tick:
            raise RuntimeError("Match restarted; recalculating")
        if now_tick != tick:
            tick, tick_changed = now_tick, time.monotonic()
        elif time.monotonic() - tick_changed > 0.2:
            raise RuntimeError("Game is paused; waiting for the match clock")
        if hasattr(pad, "focused") and not pad.focused():
            raise RuntimeError("Game window lost focus")
        if practice: practice.check()

    def same_piece(position=None):
        if game.memory.byte(count_address) != expected_count or (
                position is not None and position["piece"] != expected_piece):
            raise RuntimeError("Piece changed before commands finished; recalculating")

    commands = result.commands.decode("ascii")
    cursor = 0
    try:
        # Searching and pacing both take time. Never apply a plan to a board or
        # piece that has already changed, even when the new piece has the same type.
        active()
        current = game.snapshot(player)
        if any(current[key] != initial[key] for key in ("board", "pieces", "hold", "next_count")) or any(
                current["position"][key] != initial["position"][key] for key in ("x", "rotation", "piece")):
            raise RuntimeError("Game state changed during search; recalculating")
        # The recovered search starts at the spawn position. Hold creates a new
        # centered piece; other plans must wait after manual/interrupted movement.
        if (current["position"]["x"] != 4 or current["position"]["rotation"] != 0) and not commands.startswith(('f', 'h')):
            raise RuntimeError("Waiting for a fresh piece after interrupted movement")
        while cursor < len(commands):
            active()
            same_piece()
            if pacer:
                def paced_active():
                    active()
                    same_piece()
                pacer.wait(paced_active)
            same_piece()
            command = commands[cursor]
            expires = time.monotonic() + 0.8
            while True:
                active()
                try:
                    before = game.position(player)
                    break
                except RuntimeError as e:
                    if str(e) != "Waiting for a new falling piece" or time.monotonic() >= expires: raise
                    time.sleep(0.003)
            same_piece(before)
            if before["locked"]: raise RuntimeError("Piece locked before command finished")
            cursor += 1
            if command in ("d", "g"):
                if cursor >= len(commands): raise RuntimeError("Truncated soft drop command")
                target = ord(commands[cursor]) - 66
                cursor += 1
                if result.piece == 0 and before["rotation"] in (2, 3): target -= 1
                if before["y"] <= target: continue
                button, check = "down", lambda now: now["y"] <= target
            elif command == "l":
                button, check = "left", lambda now: now["x"] < before["x"]
            elif command == "r":
                button, check = "right", lambda now: now["x"] > before["x"]
            elif command == "a":
                button, check = "b", lambda now: now["rotation"] == (before["rotation"] + 1) % 4
            elif command == "b":
                button, check = "a", lambda now: now["rotation"] == (before["rotation"] - 1) % 4
            elif command in ("h", "f"):
                button = "lb"
                old_hold = game.memory.pointer(game.player(player) + (7368 if game.game == 2 else 976))
                old_value = game.memory.int32(old_hold + 8) if old_hold >= 0x800000 else -1
                def check(now):
                    ptr = game.memory.pointer(game.player(player) + (7368 if game.game == 2 else 976))
                    return ptr >= 0x800000 and game.memory.int32(ptr + 8) != old_value
            elif command == "u":
                # PPT stores the distance to the ghost, and its pivot differs
                # from the recovered shape's top-left reference by one cell.
                landing = (before["x"], before["y"] - before["ghost_y"], before["rotation"], before["piece"])
                expected = (result.x + 1, result.y - 1 - int(result.piece == 0 and result.rotation in (2, 3)),
                            result.rotation, result.piece)
                if landing != expected:
                    raise RuntimeError("Falling piece no longer matches the planned landing; recalculating")
                button, check = "up", lambda now: bool(now["locked"])
                drop_count = expected_count
            else:
                raise RuntimeError(f"Unknown original command {command!r}")
            # Refresh observations without interrupting the current placement.
            if practice: practice.check(force=True)
            pad.press(button)
            expires = time.monotonic() + (2 if button in ("down", "up") else 0.8)
            pressed = time.monotonic()
            while True:
                active()
                try: now = game.position(player)
                except RuntimeError as e:
                    if str(e) != "Waiting for a new falling piece": raise
                    now = None
                if command not in ("h", "f", "u"):
                    same_piece(now)
                    if now is not None and now["locked"]:
                        raise RuntimeError("Piece locked before command finished")
                held_long_enough = time.monotonic() - pressed >= 0.025
                if command == "u" and held_long_enough:
                    # A clear can remove the falling-piece pointer before its
                    # lock flag is observed. Await the next queue with Up released.
                    pad.release()
                if now is not None and check(now) and held_long_enough:
                    if command in ("h", "f"):
                        if now["piece"] != result.piece or now["locked"]:
                            raise RuntimeError("Piece changed during Hold; recalculating")
                        new_count = game.memory.byte(count_address)
                        if (command == "h" and new_count != expected_count) or (
                                command == "f" and new_count == expected_count):
                            raise RuntimeError("Unexpected queue change during Hold; recalculating")
                        expected_count, expected_piece = new_count, result.piece
                    break
                if command == "u" and held_long_enough and game.memory.byte(count_address) != drop_count: break
                if time.monotonic() >= expires: raise TimeoutError(f"Game did not acknowledge {command!r}")
                time.sleep(0.003)
            pad.release()
            time.sleep(0.022)
    finally:
        pad.release()
