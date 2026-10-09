from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hoiko_linux.controller import execute
from hoiko_linux.core import RAW_TO_HOIKO
from hoiko_linux.runtime import InputPacer, RunControl


class Clock:
    def __init__(self):
        self.now = 0
        self.on_sleep = lambda: None

    def time(self): return self.now

    def sleep(self, seconds):
        self.now += seconds
        self.on_sleep()


class Game:
    game = 2

    def __init__(self, clock, hold=7):
        self.clock = clock
        self.memory = self
        self.match = 0x20000
        self.tick_offset = 100
        self.count = 7
        self.hold = hold
        self.pieces = [0, 1, 2, 3, 4, 5]
        self.board = [0] * 32
        self.pos = dict(x=4, y=20, ghost_y=20, rotation=0, locked=0, piece=0)

    def byte(self, address): return self.count
    def pointer(self, address): return 0x900000 if self.hold < 7 else 0
    def int32(self, address):
        return RAW_TO_HOIKO.index(self.hold) if address == 0x900008 else 0
    def player(self, player): return 0x10000
    def match_pointer(self): return self.match
    def match_frames(self): return self.tick_offset + int(self.clock.now * 60)
    def offline_status(self, player): return dict(allowed=True)
    def position(self, player): return self.pos.copy()
    def snapshot(self, player):
        return dict(board=self.board.copy(), pieces=self.pieces.copy(), hold=self.hold,
                    next_count=self.count, position=self.position(player))


class Pad:
    def __init__(self, game):
        self.game = game
        self.held = False
        self.buttons = []
        self.on_release = lambda: None
        self.on_press = lambda button: None

    def focused(self): return True

    def press(self, button):
        self.held = True
        self.buttons.append(button)
        game = self.game
        if button == 'right': game.pos['x'] += 1
        elif button == 'left': game.pos['x'] -= 1
        elif button == 'up': game.pos['locked'] = 1
        elif button == 'lb':
            current = game.pos['piece']
            if game.hold == 7:
                game.pos['piece'] = game.pieces[1]
                game.pieces = game.pieces[1:] + [6]
                game.count = (game.count + 1) % 8
            else:
                game.pos['piece'] = game.hold
                game.pieces[0] = game.hold
            game.hold = current
            game.pos.update(x=4, y=20, ghost_y=20, rotation=0)
        self.on_press(button)

    def release(self):
        if self.held:
            self.held = False
            self.on_release()


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.game = Game(self.clock)
        self.pad = Pad(self.game)
        self.control = RunControl(100)
        self.initial = self.game.snapshot(1)
        for name, value in [('monotonic', self.clock.time), ('sleep', self.clock.sleep)]:
            patched = patch('hoiko_linux.controller.time.' + name, value)
            patched.start()
            self.addCleanup(patched.stop)

    def run_move(self, commands=b'rru', piece=0, pacer=None):
        result = SimpleNamespace(commands=commands, piece=piece, rotation=0, y=1,
                                 x=3 + commands.count(b'r') - commands.count(b'l'))
        execute(self.game, self.pad, 1, result,
                self.initial, pacer=pacer, control=self.control)

    def test_stale_search_with_same_piece_type_sends_nothing(self):
        self.game.count = 0
        with self.assertRaisesRegex(RuntimeError, 'changed during search'): self.run_move()
        self.assertEqual(self.pad.buttons, [])

    def test_changed_board_during_search_sends_nothing(self):
        self.game.board[0] = 512
        with self.assertRaisesRegex(RuntimeError, 'changed during search'): self.run_move()
        self.assertEqual(self.pad.buttons, [])

    def test_new_piece_between_actions_stops_old_commands(self):
        self.pad.on_release = lambda: setattr(self.game, 'count', 0)
        with self.assertRaisesRegex(RuntimeError, 'Piece changed'): self.run_move()
        self.assertEqual(self.pad.buttons, ['right'])
        self.assertFalse(self.pad.held)

    def test_new_piece_during_low_speed_wait_sends_nothing(self):
        self.control.speed = 1
        pacer = InputPacer(self.control, self.clock.time, self.clock.sleep)
        pacer.last = 0
        self.clock.on_sleep = lambda: setattr(self.game, 'count', 0)
        with self.assertRaisesRegex(RuntimeError, 'Piece changed'): self.run_move(pacer=pacer)
        self.assertLess(self.clock.now, .02)
        self.assertEqual(self.pad.buttons, [])

    def test_new_piece_while_pressed_releases_button(self):
        self.clock.on_sleep = lambda: setattr(self.game, 'count', 0)
        with self.assertRaisesRegex(RuntimeError, 'Piece changed'): self.run_move()
        self.assertFalse(self.pad.held)
        self.assertEqual(self.pad.buttons, ['right'])

    def test_empty_hold_accepts_wrapped_queue_counter(self):
        self.run_move(b'frru', piece=1)
        self.assertEqual(self.game.count, 0)
        self.assertEqual(self.game.hold, 0)
        self.assertEqual(self.pad.buttons, ['lb', 'right', 'right', 'up'])
        self.assertFalse(self.pad.held)

    def test_swap_hold_does_not_consume_queue(self):
        self.game.hold = 2
        self.initial = self.game.snapshot(1)
        self.run_move(b'hru', piece=2)
        self.assertEqual(self.game.count, 7)
        self.assertEqual(self.pad.buttons, ['lb', 'right', 'up'])
        self.assertFalse(self.pad.held)

    def test_game_pause_releases_button(self):
        self.game.match_frames = lambda: 100
        self.pad.on_press = lambda button: self.game.pos.update(x=4)
        with self.assertRaisesRegex(RuntimeError, 'Game is paused'): self.run_move()
        self.assertFalse(self.pad.held)
        self.assertLess(self.clock.now, .21)

    def test_restarted_match_releases_button(self):
        self.clock.on_sleep = lambda: setattr(self.game, 'tick_offset', 0)
        with self.assertRaisesRegex(RuntimeError, 'Match restarted'): self.run_move()
        self.assertFalse(self.pad.held)

    def test_stop_releases_button(self):
        self.clock.on_sleep = lambda: setattr(self.control, 'stopped', True)
        with self.assertRaises(KeyboardInterrupt): self.run_move()
        self.assertFalse(self.pad.held)

    def test_hard_drop_requires_lock_or_queue_acknowledgement(self):
        self.pad.on_press = lambda button: self.game.pos.update(locked=0, y=21)
        with self.assertRaisesRegex(TimeoutError, 'did not acknowledge'): self.run_move(b'u')
        self.assertFalse(self.pad.held)

    def test_manual_move_waits_for_fresh_piece(self):
        self.game.pos['x'] = 5
        self.initial = self.game.snapshot(1)
        with self.assertRaisesRegex(RuntimeError, 'Waiting for a fresh piece'): self.run_move()
        self.assertEqual(self.pad.buttons, [])

    def test_hold_can_recover_a_manually_moved_piece(self):
        self.game.pos.update(x=5, rotation=3)
        self.game.hold = 2
        self.initial = self.game.snapshot(1)
        self.run_move(b'hru', piece=2)
        self.assertEqual(self.pad.buttons, ['lb', 'right', 'up'])

    def test_wrong_horizontal_landing_does_not_hard_drop(self):
        self.pad.on_press = lambda button: self.game.pos.update(x=self.game.pos['x'] + 1)
        with self.assertRaisesRegex(RuntimeError, 'planned landing'): self.run_move(b'ru')
        self.assertEqual(self.pad.buttons, ['right'])
        self.assertFalse(self.pad.held)

    def test_changed_landing_height_does_not_hard_drop(self):
        self.pad.on_press = lambda button: self.game.pos.update(ghost_y=19)
        with self.assertRaisesRegex(RuntimeError, 'planned landing'): self.run_move(b'ru')
        self.assertEqual(self.pad.buttons, ['right'])

    def test_long_clear_waits_for_queue_with_up_released(self):
        self.pad.on_press = lambda button: self.game.pos.update(locked=0)
        def animate():
            if self.clock.now > .05: self.assertFalse(self.pad.held)
            if self.clock.now >= 1.1: self.game.count = 0
        self.clock.on_sleep = animate
        self.run_move(b'u')
        self.assertGreaterEqual(self.clock.now, 1.1)
        self.assertFalse(self.pad.held)

    def test_connection_failure_closes_created_controller(self):
        from hoiko_linux.cli import main
        class Controller:
            closed = False
            def close(pad): Controller.closed = True
        with patch('sys.argv', ['hoiko', 'run', '--backend', 'gamepad']), \
                patch('hoiko_linux.cli.attach', return_value=SimpleNamespace(
                    memory=SimpleNamespace(pid=1), executable=SimpleNamespace(name='test.exe'), base=0, delta=0)), \
                patch('hoiko_linux.cli.Engine'), \
                patch('hoiko_linux.controller.Gamepad', Controller), \
                patch('hoiko_linux.keyboard.Keyboard', side_effect=RuntimeError('No display')):
            with self.assertRaisesRegex(RuntimeError, 'No display'): main()
        self.assertTrue(Controller.closed)
