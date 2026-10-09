"""Read-only game memory access; failures never silently become zero values."""
import ctypes as c
import errno
import os
from pathlib import Path
import struct
import time


class IOVec(c.Structure):
    _fields_ = [("base", c.c_void_p), ("length", c.c_size_t)]


class ProcessMemory:
    def __init__(self, pid):
        self.pid = int(pid)
        self.libc = c.CDLL(None, use_errno=True)
        self.libc.process_vm_readv.argtypes = [c.c_int, c.POINTER(IOVec), c.c_ulong,
                                             c.POINTER(IOVec), c.c_ulong, c.c_ulong]
        self.libc.process_vm_readv.restype = c.c_ssize_t

    def read(self, address, size):
        if address < 0x10000 or size < 1 or size > 64 * 1024 * 1024:
            raise ValueError(f"Invalid read address/size: {address:#x}/{size}")
        buf = c.create_string_buffer(size)
        local, remote = IOVec(c.addressof(buf), size), IOVec(address, size)
        got = self.libc.process_vm_readv(self.pid, c.byref(local), 1, c.byref(remote), 1, 0)
        if got != size:
            number = c.get_errno() if got < 0 else errno.EIO
            raise OSError(number, f"Cannot read PID {self.pid} at {address:#x}: {os.strerror(number)}")
        return buf.raw

    def byte(self, a): return self.read(a, 1)[0]
    def int32(self, a): return struct.unpack("<i", self.read(a, 4))[0]
    def pointer(self, a): return struct.unpack("<Q", self.read(a, 8))[0]

    def image(self, executable):
        for line in Path(f"/proc/{self.pid}/maps").read_text().splitlines():
            columns = line.split(maxsplit=5)
            if len(columns) == 6 and int(columns[2], 16) == 0 and \
                    Path(columns[5]).name.casefold() == executable.casefold():
                base = int(columns[0].split("-")[0], 16)
                if self.read(base, 2) != b"MZ":
                    raise RuntimeError("Game image does not have a PE header")
                return base, Path(columns[5])
        raise RuntimeError(f"Cannot find {executable} mapping in PID {self.pid}")


def find_game(game=2):
    stem = "PuyoPuyoTetris2" if game == 2 else "puyopuyotetris"
    found = []
    for path in Path("/proc").iterdir():
        if not path.name.isdigit(): continue
        try:
            name = (path / "comm").read_text().strip()
            if name.casefold() == stem[:15].casefold(): found.append(int(path.name))
        except OSError: pass
    if len(found) > 1:
        raise RuntimeError("Multiple game processes found; select one with --pid")
    if not found:
        raise RuntimeError("Game is not running; start it in Steam first")
    return found[0]


class GameMemory:
    def __init__(self, process, game=2, delta=0):
        self.memory, self.game, self.delta = process, game, delta
        self.base, self.executable = process.image(
            "PuyoPuyoTetris2.exe" if game == 2 else "puyopuyotetris.exe")
        self.addresses = {}
        self.character_layout = 2
        self.status_source = "original"
        self.menu_profile = None

    def address(self, name):
        defaults = {"match": 31393368, "frames": 32700636, "mode": 8160832,
                    "character_root": 32688528, "online_flag": 32670160,
                    "online_pointer": 31465304, "local": 32516708,
                    "players": 30927500, "tetris": 31260629} if self.game == 2 else {
                    "match": 4594464, "frames": 4594556, "online_flag": 5867852,
                    "local": 5716056, "players": 4529236, "tetris": 5868583}
        if name in self.addresses: return self.base + self.addresses[name]
        return self.global_address(defaults[name])

    def global_address(self, rva): return self.base + rva + self.delta

    def match_pointer(self):
        return self.memory.pointer(self.address("match"))

    def mode_id(self):
        if self.game != 2: return None
        return self.memory.int32(self.memory.pointer(self.address("mode")) + 488)

    def player_class(self, player):
        m = self.memory
        vtable = m.pointer(self.player(player))
        locator = m.pointer(vtable - 8)
        signature, _, _, type_rva = struct.unpack("<4I", m.read(locator, 16))
        if signature != 1: raise RuntimeError("Unexpected game RTTI layout")
        return m.read(self.base + type_rva + 16, 160).split(b'\0', 1)[0].decode("ascii")

    def character_selection(self):
        """Original character menu chains; no input is sent by this method."""
        if self.game != 2 or self.mode_id() != 14: return None
        if self.match_pointer(): return None
        m = self.memory
        try:
            pointer = m.pointer(self.address("character_root"))
            scene = m.pointer(pointer + 304)
            pointer = m.pointer(scene + 80)
            layouts = {2: (128, 112, (424, 1152)),
                       3: (120, 176, (880, 1072, 1232)),
                       4: (144, 112, (744, 1752, 1880, 2048))}
            mid, end, fields = layouts[self.character_layout]
            for offset in (mid, end): pointer = m.pointer(pointer + offset)
            characters = tuple(m.int32(pointer + offset) for offset in fields)
            if any(not 0 <= char <= 39 for char in characters): return None
            result = {"pointer": pointer, "characters": characters}
            if self.menu_profile:
                profile = self.menu_profile
                states = []
                for offset in profile["player_pointers"]:
                    ui = m.pointer(scene + offset)
                    flags = m.byte(ui + profile["phase"])
                    # Bit 0: character accepted; bit 1: style accepted;
                    # bit 2: controls accepted (ready). Read bytes, not ints:
                    # adjacent padding can contain stale heap addresses.
                    phases = {0: "character", 1: "style", 3: "controls", 7: "ready"}
                    states.append({"phase": phases.get(flags, "unknown"),
                                   "flags": flags,
                                   "tetris": bool(m.byte(ui + profile["style"]) & 0x40),
                                   "controls": m.byte(ui + profile["controls"])})
                result["states"] = tuple(states)
            return result
        except (OSError, ValueError):
            return None

    def player(self, player):
        if not 0 <= player < 4: raise ValueError("Player must be 0-3 internally")
        match = self.match_pointer()
        if not match: raise RuntimeError("No active match")
        m = self.memory
        container = m.pointer(match + (player + 22) * 24) if self.game == 2 else \
                    m.pointer(match + (player + 111) * 8)
        return m.pointer(container + (136 if self.game == 2 else 168))

    def position(self, player):
        m = self.memory
        pointer = m.pointer(self.player(player) + (7360 if self.game == 2 else 968))
        if pointer < 0x10000: raise RuntimeError("Waiting for a new falling piece")
        values = struct.unpack("<5i", m.read(pointer + 12, 20))
        # Original reads the low bytes of these integer fields.
        position = dict(zip(("x", "y", "ghost_y", "rotation", "locked"), (v & 255 for v in values)))
        # Despite the original GetGhostPositionY name, +20 is the drop distance.
        from .core import RAW_TO_HOIKO
        raw_piece = m.int32(pointer + 8)
        if raw_piece not in range(7): raise RuntimeError("Invalid active piece type")
        position["piece"] = RAW_TO_HOIKO[raw_piece]
        return position

    def match_frames(self):
        return self.memory.int32(self.match_pointer() + (772 if self.game == 2 else 1060))

    def frames(self):
        return self.memory.int32(self.address("frames"))

    def practice_board(self, player):
        """Read a human's stack even between pieces; reuse only verified offsets."""
        if not self.offline_status(player)["allowed"]:
            raise RuntimeError("Waiting for the practice player's offline Tetris field")
        m = self.memory
        main = self.player(player)
        if m.int32(main + (752 if self.game == 2 else 128)) > 1:
            raise RuntimeError("Practice player finished the match")
        board_offset = 7352 if self.game == 2 else 960
        board_pointer = m.pointer(m.pointer(main + board_offset) + 24)
        columns = struct.unpack("<10Q", m.read(board_pointer, 80))
        board = [0] * 32
        for x, column in enumerate(columns):
            cells = struct.unpack("<32i", m.read(column, 128))
            if any(value < -1 for value in cells):
                raise RuntimeError("Waiting for line-clear animation")
            if any(value > 65535 for value in cells):
                raise RuntimeError("Unexpected practice board cells")
            for y, value in enumerate(cells):
                if value >= 0: board[y] |= 512 >> x
        return board

    def offline_status(self, player):
        m = self.memory
        if self.game == 2 and self.status_source == "mode-and-class":
            network = m.pointer(self.address("network_manager"))
            online = bool(network and m.pointer(network + 24))
            mode = self.mode_id()
            local = 3 if mode == 14 else 0
            players = m.int32(self.address("players"))
            try:
                tetris = self.player_class(player) == ".?AVTetrisFieldClassic@Field@Tetris@@"
            except (OSError, RuntimeError, ValueError):
                tetris = False
        elif self.game == 2:
            online_flag = m.byte(self.address("online_flag"))
            online_pointer = m.pointer(self.address("online_pointer"))
            online = bool(online_flag and online_pointer and m.pointer(online_pointer + 24))
            local = m.byte(self.address("local"))
            players = m.int32(self.address("players"))
            tetris = bool(m.byte(self.address("tetris") + player * 192) & 64)
        else:
            online = bool(m.byte(self.address("online_flag")))
            local = m.byte(self.address("local"))
            players = m.int32(self.address("players"))
            tetris = bool(m.byte(self.address("tetris") + player * 104) & 64)
        return {"online": online, "local_mode": local, "players": players,
                "tetris": tetris, "allowed": not online and local == 3 and 2 <= players <= 4 and tetris}

    def snapshot(self, player=0):
        from .core import RAW_TO_HOIKO
        m = self.memory
        status = self.offline_status(player)
        if not status["allowed"]:
            raise RuntimeError(f"Waiting for an offline local Tetris VS match: {status}")
        frame = self.frames()
        main = self.player(player)
        game_state = m.int32(main + (752 if self.game == 2 else 128))
        if game_state > 1: raise RuntimeError("Player finished the match; waiting for the next game")
        board_offset, current_offset, hold_offset = (7352, 7360, 7368) if self.game == 2 else (960, 968, 976)
        board_pointer = m.pointer(m.pointer(main + board_offset) + 24)
        columns = struct.unpack("<10Q", m.read(board_pointer, 80))
        board = [0] * 32
        for x, column in enumerate(columns):
            cells = struct.unpack("<32i", m.read(column, 128))
            if any(value < -1 for value in cells):
                raise RuntimeError("Waiting for line-clear animation")
            if any(value > 65535 for value in cells):
                raise RuntimeError("Unexpected board cells; memory profile may be incompatible")
            for y, value in enumerate(cells):
                if value >= 0: board[y] |= 512 >> x
        hold_pointer = m.pointer(main + hold_offset)
        raw_hold = m.int32(hold_pointer + 8) if hold_pointer >= 0x800000 else -1
        if raw_hold not in range(-1, 7): raise RuntimeError("Invalid hold piece")
        match = self.match_pointer()
        container = m.pointer(match + (player + 22) * 24) if self.game == 2 else m.pointer(match + (player + 111) * 8)
        next_pointer = m.pointer(container + (152 if self.game == 2 else 184)) + (360 if self.game == 2 else 348)
        raw_next = [m.byte(next_pointer + i * 8) for i in range(5)]
        if any(value > 6 for value in raw_next): raise RuntimeError("Invalid next pieces")
        count_offset = 7392 if self.game == 2 else 984
        position = self.position(player)
        if position["x"] > 12 or position["y"] > 32 or position["rotation"] > 3 or position["locked"] > 1:
            raise RuntimeError("Invalid active piece position")
        state = {"board": board, "pieces": [position["piece"]] + [RAW_TO_HOIKO[v] for v in raw_next],
                 "hold": RAW_TO_HOIKO[raw_hold] if raw_hold >= 0 else 7,
                 "combo": m.byte(main + (7396 if self.game == 2 else 988)),
                 "btb": m.byte(main + (7397 if self.game == 2 else 989)),
                 "next_count": m.byte(main + count_offset), "position": position,
                 "frame": frame, "game_state": game_state}
        return state
