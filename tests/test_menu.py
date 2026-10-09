import copy
import unittest
from unittest.mock import Mock, patch

from hoiko_linux.menu import AutoSelector
from hoiko_linux.memory import GameMemory


def menu(phase="character", character=7, tetris=True, controls=0):
    return {"pointer": 1234, "characters": (25, character), "states": (
        {"phase": "ready", "flags": 7, "tetris": True, "controls": 0},
        {"phase": phase, "flags": {"character": 0, "style": 1, "controls": 3, "ready": 7}.get(phase, 9),
         "tetris": tetris, "controls": controls})}


class MenuTests(unittest.TestCase):
    def setUp(self):
        self.game, self.pad = Mock(), Mock()
        self.game.offline_status.return_value = {"online": False}
        self.pad.focused.return_value = True
        self.game.character_selection.return_value = menu()
        self.selector = AutoSelector(self.game, self.pad, 1)
        self.clock = patch("hoiko_linux.menu.time.monotonic", side_effect=range(1, 1000))
        self.sleep = patch("hoiko_linux.menu.time.sleep")
        self.clock.start(); self.sleep.start()
        self.addCleanup(self.clock.stop); self.addCleanup(self.sleep.stop)

    def step(self, **kw):
        self.game.character_selection.return_value = menu(**kw)
        return self.selector.step()

    def test_full_sequence_with_puyo_and_nondefault_controls(self):
        self.step(character=6)
        self.step()
        self.step(phase="style", tetris=False)
        self.step(phase="style")
        self.step(phase="controls", controls=1)
        self.step(phase="controls")
        self.step(phase="ready")
        self.assertEqual([c.args[0] for c in self.pad.press.call_args_list],
                         [("right",), ("a",), ("right",), ("a",), ("left",), ("a",)])
        self.assertEqual(self.pad.release.call_count, 6)

    def test_resume_at_controls_and_retry_lost_press(self):
        self.step(phase="controls")
        self.step(phase="controls")
        self.assertEqual(self.pad.press.call_count, 2)
        self.step(phase="ready")
        self.assertEqual(self.pad.press.call_count, 2)

    def test_resume_at_puyo_controls_returns_to_character(self):
        self.step(phase="controls", tetris=False)
        self.pad.press.assert_called_once_with(("b",))

    def test_reused_menu_pointer_after_ready(self):
        self.step(phase="ready")
        self.step(phase="character")
        self.assertEqual(self.pad.press.call_count, 1)

    def test_stuck_menu_has_bounded_retries(self):
        for _ in range(20): self.step(phase="controls")
        self.assertEqual(self.pad.press.call_count, 8)
        self.assertIn("did not acknowledge", self.step(phase="controls"))

    def test_no_input_without_verified_state_or_focus_or_offline_mode(self):
        self.game.character_selection.return_value = {"pointer": 1234, "characters": (25, 7)}
        self.selector.step()
        self.step(phase="unknown")
        self.pad.focused.return_value = False
        self.step()
        self.pad.focused.return_value = True
        self.game.offline_status.return_value = {"online": True}
        self.step()
        self.pad.press.assert_not_called()

    def test_wrong_controller_detected_from_phase_change(self):
        before = menu()
        before["states"][0]["phase"] = "character"
        after = copy.deepcopy(before)
        after["states"][0]["phase"] = "style"
        self.game.character_selection.side_effect = [before, after]
        with self.assertRaisesRegex(RuntimeError, "Controller moved player 1"):
            self.selector.step()
        self.pad.release.assert_called_once()

    def test_button_released_on_error(self):
        self.pad.press.side_effect = OSError("disconnected")
        with self.assertRaises(OSError): self.step()
        self.pad.release.assert_called_once()


class MenuMemoryTests(unittest.TestCase):
    def test_profile_reads_byte_flags_not_neighboring_padding(self):
        game = GameMemory.__new__(GameMemory)
        game.game, game.character_layout = 2, 2
        game.menu_profile = {"player_pointers": [104, 112], "phase": 704, "style": 589, "controls": 683}
        game.mode_id = lambda: 14
        game.match_pointer = lambda: 0
        game.address = lambda _: 0x10000
        game.memory = Mock()
        game.memory.pointer.side_effect = {
            0x10000: 0x20000, 0x20000+304: 0x30000,
            0x30000+80: 0x40000, 0x40000+128: 0x50000,
            0x50000+112: 0x60000, 0x30000+104: 0x70000,
            0x30000+112: 0x80000}.__getitem__
        game.memory.int32.side_effect = {0x60000+424: 25, 0x60000+1152: 7}.__getitem__
        game.memory.byte.side_effect = {
            0x70000+704: 7, 0x70000+589: 64, 0x70000+683: 0,
            0x80000+704: 3, 0x80000+589: 64, 0x80000+683: 1}.__getitem__
        state = game.character_selection()
        self.assertEqual(state["states"][0]["phase"], "ready")
        self.assertEqual(state["states"][1]["phase"], "controls")
        self.assertTrue(state["states"][1]["tetris"])
        self.assertEqual(state["states"][1]["controls"], 1)
