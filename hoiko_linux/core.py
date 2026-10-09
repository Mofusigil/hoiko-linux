import ctypes as c
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PIECES = "IJLOTSZ"
RAW_TO_HOIKO = (5, 6, 1, 2, 4, 3, 0)


class Result(c.Structure):
    _fields_ = [("commands", c.c_char * 48)] + [
        (name, c.c_int32) for name in
        ("x", "y", "rotation", "piece", "depth", "nodes", "beam", "clear_action")
    ] + [("board", c.c_int32 * 32), ("elapsed_ms", c.c_double)]

    def as_dict(self):
        return {"commands": self.commands.decode("ascii"),
                "placement": {"x": self.x, "y": self.y, "rotation": self.rotation,
                              "piece": PIECES[self.piece] if 0 <= self.piece < 7 else None},
                "depth": self.depth, "nodes": self.nodes, "beam": self.beam,
                "clear_action": self.clear_action, "board": list(self.board),
                "elapsed_ms": round(self.elapsed_ms, 3)}


class Engine:
    def __init__(self, game=2, beam=100, depth=6, budget=112, style=0, hold=True, td=False,
                 library=None, s4w=False, td_loop=False):
        if td_loop and not (td and hold):
            raise ValueError("TD loop requires TD openers and Hold")
        if td and not hold: raise ValueError("TD openers require Hold")
        library = Path(library or ROOT / "build/libhoiko_core.so")
        if not library.exists():
            raise RuntimeError("Native core is missing; run ./build.sh first")
        self.lib = c.CDLL(str(library))
        self.lib.hoiko_error.restype = c.c_char_p
        self.lib.hoiko_init.argtypes = [c.c_char_p, c.c_int, c.c_int, c.c_int, c.c_double,
                                      c.c_int, c.c_int, c.c_int]
        self.lib.hoiko_solve.argtypes = [c.POINTER(c.c_int32), c.POINTER(c.c_int32),
                                       c.c_int, c.c_int, c.c_int, c.c_int, c.c_int,
                                       c.POINTER(Result)]
        self._check(self.lib.hoiko_init(str(ROOT / "Hoiko_PPT_v0-beta1").encode(), game,
                                       beam, depth, budget, style, hold, td))
        self.lib.hoiko_options.argtypes = [c.c_int, c.c_int]
        self._check(self.lib.hoiko_options(s4w, td_loop))

    def _check(self, status):
        if status:
            raise RuntimeError(self.lib.hoiko_error().decode())

    def solve(self, board, pieces, hold=7, combo=0, btb=0, reset=False):
        if len(board) > 32:
            raise ValueError("Board must have at most 32 rows, bottom first")
        board = list(board) + [0] * (32 - len(board))
        if isinstance(pieces, str):
            pieces = [PIECES.index(x) for x in pieces.upper()]
        if isinstance(hold, str):
            hold = PIECES.index(hold.upper())
        result = Result()
        self._check(self.lib.hoiko_solve((c.c_int32 * 32)(*board),
                                       (c.c_int32 * len(pieces))(*pieces), len(pieces),
                                       hold, combo, btb, reset, c.byref(result)))
        return result
