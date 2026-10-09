import json
import os
import random
import unittest
from pathlib import Path

from hoiko_linux.core import Engine, ROOT


def shapes():
    import re
    text = (ROOT / "native/generated/core.inc").read_text()
    data = re.search(r"g_04000158\[896\] = \{([^}]+)\}", text).group(1)
    import struct
    raw = bytes(int(v) for v in data.split(','))
    return [struct.unpack_from('<8i', raw, i * 32) for i in range(28)]


class CoreTests(unittest.TestCase):
    def engine(self, **kw):
        return Engine(beam=20, depth=3, library=os.environ.get("HOIKO_TEST_LIBRARY"), **kw)

    def check_placement(self, before, result):
        shape = shapes()[result.piece * 4 + result.rotation]
        placed = before.copy()
        cells = 0
        for dy, mask in enumerate(shape[4:]):
            if not mask: continue
            shifted = mask << -result.x if result.x < 0 else mask >> result.x
            y = result.y - dy
            self.assertTrue(0 <= y < 32)
            self.assertTrue(0 <= shifted <= 1023)
            self.assertEqual(placed[y] & shifted, 0, 'Placement overlaps the existing board')
            cells += shifted.bit_count()
            placed[y] |= shifted
        self.assertEqual(cells, 4)
        cleared = sum(row == 1023 for row in placed)
        expected = [row for row in placed if row != 1023] + [0] * cleared
        self.assertEqual(list(result.board), expected)
        self.assertEqual(sum(x.bit_count() for x in expected),
                         sum(x.bit_count() for x in before) + 4 - cleared * 10)
        return cleared

    def test_empty_board(self):
        result = self.engine().solve([0]*32, "ITOLS", reset=True)
        self.assertTrue(result.commands.endswith(b'u'))
        self.assertGreater(result.nodes, 0)
        self.assertEqual(result.depth, 3)
        self.check_placement([0]*32, result)

    def test_tetris_well(self):
        before = [1022]*4 + [0]*28
        result = self.engine(hold=False, style=1).solve(before, "ISZJO", reset=True)
        self.assertEqual(self.check_placement(before, result), 4)
        self.assertEqual(sum(result.board), 0)

    def test_seeded_game_with_hold(self):
        rng = random.Random(20261003)
        queue = []
        for _ in range(30):
            bag = list(range(7)); rng.shuffle(bag); queue.extend(bag)
        engine, board, hold, offset = self.engine(), [0]*32, 7, 0
        cleared_total = 0
        for turn in range(60):
            current = queue[offset]
            result = engine.solve(board, queue[offset:offset+5], hold=hold, reset=turn==0)
            self.assertTrue(result.commands)
            cleared_total += self.check_placement(board, result)
            if result.commands.startswith(b'f'):
                self.assertEqual(hold, 7); hold, consumed = current, 2
            elif result.commands.startswith(b'h'):
                self.assertEqual(result.piece, hold); hold, consumed = current, 1
            else:
                self.assertEqual(result.piece, current); consumed = 1
            offset += consumed; board = list(result.board)
        self.assertGreater(cleared_total, 0)

    def test_template_cannot_lock_below_board(self):
        import ctypes as c
        import struct
        class Slot(c.Structure):
            _fields_ = [("u", c.c_uint64), ("kind", c.c_uint8)]
        engine = self.engine()
        lock = getattr(engine.lib, "_ZN9recovered10m_06000257EPK4Slot")
        lock.argtypes = [c.POINTER(Slot)]; lock.restype = Slot
        storage = c.create_string_buffer(256)
        board = c.addressof(storage) + 64
        mino = c.create_string_buffer(32)
        shape = c.create_string_buffer(struct.pack('<8i', *shapes()[0]))
        # Horizontal I occupies y-1. A TD candidate at y=0 has bottom=-1.
        struct.pack_into('<ii', mino, 0, 0, 0)
        struct.pack_into('<Q', mino, 24, c.addressof(shape))
        before = storage.raw
        result = lock((Slot*2)(Slot(board,1),Slot(c.addressof(mino),1)))
        self.assertEqual(c.c_int64(result.u).value, -1)
        self.assertEqual(storage.raw, before)
        # Preserve legal partial lock-out placements in the hidden rows.
        vertical = c.create_string_buffer(struct.pack('<8i', *shapes()[1]))
        struct.pack_into('<ii', mino, 0, 0, 22)
        struct.pack_into('<Q', mino, 24, c.addressof(vertical))
        result = lock((Slot*2)(Slot(board,1),Slot(c.addressof(mino),1)))
        self.assertEqual(c.c_int64(result.u).value, 0)
        self.assertEqual([c.c_int32.from_address(board+y*4).value for y in range(19,23)], [128]*4)

    def test_panel_strategy_options(self):
        for options in ({"hold": False}, {"s4w": True}, {"td": True, "td_loop": True}):
            result = self.engine(**options).solve([0]*32, "ITOLSJ", reset=True)
            self.check_placement([0]*32, result)
            if options.get("hold") is False:
                self.assertNotIn(b'f', result.commands)
                self.assertNotIn(b'h', result.commands)
        with self.assertRaises(ValueError): self.engine(td_loop=True)

    def test_invalid_state(self):
        engine = self.engine()
        with self.assertRaises(RuntimeError): engine.solve([2048], "ITOLS", reset=True)


if __name__ == '__main__': unittest.main()
