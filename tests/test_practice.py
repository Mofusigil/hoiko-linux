import contextlib
import io
import json
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hoiko_linux import settings
from hoiko_linux.controller import execute
from hoiko_linux.memory import GameMemory
from hoiko_linux.practice import PracticeGuard
from hoiko_linux.runtime import InputPacer, RunControl


def stack(height):
    return [512] * height + [0] * (32 - height)


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

    def __init__(self, clock):
        self.clock, self.memory = clock, self
        self.human_board, self.bot_board = stack(0), stack(0)
        self.error = None
        self.match, self.tick_offset, self.count = 0x30000, 100, 1
        self.reads, self.read_players = 0, []
        self.pos = dict(x=4, y=20, ghost_y=20, rotation=0, locked=0, piece=0)

    def match_pointer(self): return self.match
    def match_frames(self): return self.tick_offset + int(self.clock.now * 60)
    def player(self, player): return 0x10000 + player * 0x10000
    def offline_status(self, player): return dict(allowed=True)
    def int32(self, address): return 0
    def byte(self, address): return self.count
    def position(self, player): return self.pos.copy()
    def snapshot(self, player):
        return dict(board=self.bot_board.copy(), pieces=[0, 1, 2], hold=7,
                    next_count=self.count, position=self.position(player),
                    combo=0, btb=0, game_state=0)
    def practice_board(self, player):
        self.reads += 1
        self.read_players.append(player)
        if self.error: raise self.error
        return self.human_board.copy()


class Pad:
    def __init__(self, game):
        self.game, self.held, self.buttons, self.starts = game, False, [], []
        self.on_press = lambda button: None

    def focused(self): return True
    def press(self, button):
        self.held = True
        self.buttons.append(button)
        self.starts.append(self.game.clock.now)
        if button == 'right': self.game.pos['x'] += 1
        elif button == 'up': self.game.pos['locked'] = 1
        self.on_press(button)
    def release(self): self.held = False


class PracticeFixture(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.game = Game(self.clock)
        self.guard = PracticeGuard(self.game, 0, self.clock.time)

    def sample(self, height):
        self.game.human_board = stack(height)
        self.guard.sample(force=True)

    def confirm(self, height):
        self.sample(height)
        self.clock.now += .16
        self.sample(height)


class PracticeTests(PracticeFixture):
    def test_low_stack_line_clear_animation_never_introduces_a_delay(self):
        self.sample(2)
        self.guard.move_finished()
        self.game.error = RuntimeError('Waiting for line-clear animation')
        for elapsed in (.05, .2, 1, 2):
            self.clock.now = elapsed
            self.guard.check(force=True)
            self.assertEqual(self.guard.piece_delay, 0)
            self.assertTrue(self.guard.ready())
            self.assertEqual(self.guard.as_dict(10)['effective_speed'], 10)
        self.game.error = None
        self.sample(3)
        self.assertEqual(self.guard.height, 3)
        self.assertEqual(self.guard.level, 'normal')

    def test_one_frame_height_spike_does_not_throttle(self):
        self.sample(3)
        self.sample(21)
        self.clock.now += .05
        self.sample(3)
        self.clock.now += .2
        self.sample(3)
        self.assertEqual(self.guard.piece_delay, 0)
        self.assertEqual(self.guard.level, 'normal')

    def test_confirmed_warning_delays_pieces_without_changing_key_speed(self):
        control = RunControl(10)
        pacer = InputPacer(control, self.clock.time, self.clock.sleep)
        self.confirm(14)
        self.assertEqual(self.guard.piece_delay, 1)
        self.assertEqual(pacer.interval, .5)
        self.guard.move_finished()
        self.assertFalse(self.guard.ready(1))
        self.clock.now = 1.99
        self.assertFalse(self.guard.ready(1))
        self.clock.now = 2
        self.assertTrue(self.guard.ready(1))
        control.apply(dict(action='speed', value=80))
        self.assertEqual(pacer.interval, .0625)
        self.assertEqual(self.guard.as_dict(control.speed)['effective_speed'], 80)

    def test_even_hidden_row_danger_has_a_bounded_nonzero_pace(self):
        self.confirm(21)
        self.assertEqual(self.guard.level, 'very_slow')
        self.assertEqual(self.guard.piece_delay, 2)
        self.guard.move_finished()
        self.assertFalse(self.guard.ready())
        self.clock.now += 2.01
        self.assertTrue(self.guard.ready())
        self.assertGreater(self.guard.as_dict(1)['effective_speed'], 0)

    def test_bot_self_protection_removes_the_extra_wait_immediately(self):
        self.confirm(17)
        self.guard.move_finished()
        for height in (14, 16):
            self.guard.update_bot(stack(height))
            self.assertFalse(self.guard.ready())
            self.assertEqual(self.guard.piece_delay, 2)
            self.assertNotIn('优先自保', self.guard.status)
        self.guard.update_bot(stack(17))
        self.assertTrue(self.guard.ready())
        self.assertEqual(self.guard.piece_delay, 0)
        self.assertIn('优先自保', self.guard.status)

    def test_recovery_downgrades_danger_then_requires_stable_safety(self):
        self.confirm(17)
        self.sample(13)
        self.assertEqual(self.guard.piece_delay, 1)
        self.sample(10)
        self.clock.now += 1.9
        self.sample(10)
        self.assertEqual(self.guard.level, 'slow')
        self.sample(11)
        self.sample(10)
        self.clock.now += 1.9
        self.sample(10)
        self.assertEqual(self.guard.level, 'slow')
        self.clock.now += .11
        self.sample(10)
        self.assertEqual(self.guard.level, 'normal')

    def test_failed_reads_keep_recent_decision_and_expire_stale_throttling(self):
        self.confirm(17)
        self.game.error = OSError('unreadable board')
        self.clock.now += .2
        self.guard.check(force=True)
        self.assertEqual(self.guard.piece_delay, 2)
        self.clock.now += 1
        self.guard.check(force=True)
        self.assertEqual(self.guard.piece_delay, 0)
        self.assertIsNone(self.guard.height)
        self.game.error = None
        self.sample(0)
        self.assertEqual(self.guard.level, 'normal')

    def test_invalid_read_is_not_a_high_stack(self):
        self.sample(0)
        for board in ([], [0] * 31, [2048] * 32):
            self.game.human_board = board
            self.guard.check(force=True)
            self.assertEqual(self.guard.level, 'normal')
            self.assertEqual(self.guard.piece_delay, 0)
            self.assertTrue(self.guard.ready())

    def test_new_match_or_clock_rollback_clears_previous_throttle(self):
        for restart in ('pointer', 'clock'):
            with self.subTest(restart=restart):
                self.confirm(17)
                self.guard.move_finished()
                if restart == 'pointer': self.game.match += 0x1000
                else: self.game.tick_offset -= 1
                self.sample(0)
                self.assertEqual(self.guard.level, 'normal')
                self.assertIsNone(self.guard.last_move)

    def test_sampling_is_bounded_but_forced_reads_refresh_height(self):
        self.sample(0)
        self.game.human_board = stack(17)
        for _ in range(10): self.guard.check()
        self.assertEqual(self.game.reads, 1)
        self.guard.check(force=True)
        self.assertEqual(self.game.reads, 2)
        self.assertEqual(self.guard.height, 17)
        self.assertEqual(self.guard.piece_delay, 0)
        self.clock.now += .16
        self.guard.check(force=True)
        self.assertEqual(self.guard.piece_delay, 2)

    def test_settings_default_off_roundtrip_and_explicit_object(self):
        self.assertFalse(settings.validate({'speed': 10})['practice'])
        self.assertNotIn('--practice', settings.run_arguments(settings.DEFAULTS))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            settings.save(dict(settings.DEFAULTS, practice=True), path)
            args = settings.run_arguments(settings.load(path))
            self.assertIn('--practice', args)
            self.assertEqual(args[args.index('--practice-player') + 1], '1')
        with self.assertRaises(ValueError): settings.validate(dict(practice=True, practice_player=2))
        for value in (0, 5, 1.5, True):
            with self.assertRaises(ValueError): settings.validate(dict(practice_player=value))


class PracticeExecutionTests(PracticeFixture):
    def setUp(self):
        super().setUp()
        self.pad, self.control = Pad(self.game), RunControl(10)
        self.initial = self.game.snapshot(1)
        for name, value in [('monotonic', self.clock.time), ('sleep', self.clock.sleep)]:
            patched = patch('hoiko_linux.controller.time.' + name, value)
            patched.start()
            self.addCleanup(patched.stop)

    def move(self, commands=b'ru', pacer=None, practice=True):
        result = SimpleNamespace(commands=commands, x=4 if b'r' in commands else 3,
                                 y=1, rotation=0, piece=0)
        execute(self.game, self.pad, 1, result, self.initial, pacer=pacer,
                control=self.control, practice=self.guard if practice else None)

    def test_critical_player_still_completes_move_at_selected_input_speed(self):
        self.confirm(17)
        pacer = InputPacer(self.control, self.clock.time, self.clock.sleep)
        self.move(pacer=pacer)
        self.assertEqual(self.pad.buttons, ['right', 'up'])
        self.assertAlmostEqual(self.pad.starts[1] - self.pad.starts[0], .5)
        self.assertFalse(self.pad.held)

    def test_danger_during_pacing_does_not_abort_commands(self):
        pacer = InputPacer(self.control, self.clock.time, self.clock.sleep)
        pacer.last = 0
        self.clock.on_sleep = lambda: setattr(self.game, 'human_board', stack(17))
        self.move(pacer=pacer)
        self.assertEqual(self.pad.buttons, ['right', 'up'])
        self.assertAlmostEqual(self.pad.starts[0], .5)
        self.assertAlmostEqual(self.pad.starts[1] - self.pad.starts[0], .5)
        self.assertEqual(self.guard.level, 'very_slow')
        self.assertFalse(self.pad.held)

    def test_line_clear_before_move_does_not_block_inputs(self):
        self.game.error = RuntimeError('Waiting for line-clear animation')
        self.move()
        self.assertEqual(self.pad.buttons, ['right', 'up'])
        self.assertFalse(self.pad.held)

    def test_read_error_while_button_held_does_not_abandon_landing(self):
        def animate(button):
            self.game.error = RuntimeError('Waiting for line-clear animation')
        self.pad.on_press = animate
        self.move()
        self.assertEqual(self.pad.buttons, ['right', 'up'])
        self.assertFalse(self.pad.held)

    def test_danger_between_actions_does_not_block_hard_drop(self):
        self.clock.on_sleep = lambda: setattr(self.game, 'human_board', stack(17))
        self.move()
        self.assertEqual(self.pad.buttons, ['right', 'up'])
        self.assertFalse(self.pad.held)

    def test_stop_still_interrupts_and_releases_keys(self):
        self.clock.on_sleep = lambda: setattr(self.control, 'stopped', True)
        with self.assertRaises(KeyboardInterrupt): self.move()
        self.assertFalse(self.pad.held)

    def test_disabled_mode_never_reads_the_opponent(self):
        self.game.error = AssertionError('Opponent should not be read')
        self.move(practice=False)
        self.assertEqual(self.game.reads, 0)
        self.assertEqual(self.pad.buttons, ['right', 'up'])


class PracticeMemoryTests(unittest.TestCase):
    def make_game(self, game_id=2):
        game = GameMemory.__new__(GameMemory)
        game.game = game_id
        main, board, columns = 0x10000, 0x20000, 0x30000
        game.player = lambda player: main
        game.offline_status = lambda player: dict(allowed=True)
        game.position = lambda player: self.fail('Practice must not require an active piece')
        cells = [[-1] * 32 for _ in range(10)]
        cells[0][13], cells[9][0] = 0, 1

        class Memory:
            def int32(self, address):
                expected = main + (752 if game_id == 2 else 128)
                if address != expected: raise AssertionError(address)
                return 0
            def pointer(self, address):
                pointers = {main + (7352 if game_id == 2 else 960): board, board + 24: columns}
                return pointers[address]
            def read(self, address, size):
                if address == columns and size == 80:
                    return struct.pack('<10Q', *(0x40000 + x * 128 for x in range(10)))
                return struct.pack('<32i', *cells[(address - 0x40000) // 128])
        game.memory = Memory()
        return game, cells

    def test_board_reads_work_between_pieces_on_both_layouts(self):
        for game_id in (1, 2):
            game, _ = self.make_game(game_id)
            board = game.practice_board(0)
            self.assertEqual(board[0], 1)
            self.assertEqual(board[13], 512)
            self.assertEqual(len(board), 32)

    def test_animation_invalid_cells_and_non_tetris_are_rejected(self):
        game, cells = self.make_game()
        for value in (-2, 65536):
            cells[0][0] = value
            with self.assertRaises(RuntimeError): game.practice_board(0)
        game.offline_status = lambda player: dict(allowed=False)
        with self.assertRaises(RuntimeError): game.practice_board(0)

    def test_low_real_board_with_clear_markers_does_not_pause_the_guard(self):
        game, cells = self.make_game()
        cells[0][13] = -1
        game.match_pointer = lambda: 0x40000
        game.match_frames = lambda: 100
        guard = PracticeGuard(game, 0)
        guard.check(force=True)
        self.assertEqual(guard.height, 1)
        cells[9][0] = -2
        guard.check(force=True)
        self.assertEqual(guard.height, 1)
        self.assertEqual(guard.piece_delay, 0)
        self.assertTrue(guard.ready())


class PracticeCliTests(PracticeFixture):
    def run_cli(self, flags):
        from hoiko_linux.cli import main
        self.game.executable = Path('test.exe')
        self.game.pid, self.game.base, self.game.delta = 1, 0, 0
        result = SimpleNamespace(commands=b'u', piece=0, depth=3, nodes=1, elapsed_ms=1)
        output = io.StringIO()
        def next_piece(*args, **kwargs):
            self.game.count = (self.game.count + 1) % 256
            return result
        with patch('sys.argv', ['hoiko', 'run', '--dry-run', '--seconds', '3', '--speed', '10'] + flags), \
                patch('hoiko_linux.cli.attach', return_value=self.game), \
                patch('hoiko_linux.cli.Engine') as engine, \
                patch('hoiko_linux.cli.PracticeGuard', side_effect=lambda game, player: PracticeGuard(game, player, self.clock.time)), \
                patch('hoiko_linux.cli.time.monotonic', self.clock.time), \
                patch('hoiko_linux.cli.time.sleep', self.clock.sleep), \
                contextlib.redirect_stdout(output):
            engine.return_value.solve.side_effect = next_piece
            main()
        return engine.return_value.solve.call_count, output.getvalue()

    def states(self, output):
        return [json.loads(line) for line in output.splitlines() if line.startswith('{"event":')]

    def test_cli_normal_mode_keeps_solving_without_opponent_reads(self):
        self.game.error = AssertionError('Normal mode must not read the opponent')
        calls, _ = self.run_cli(['--player', '2'])
        self.assertGreater(calls, 50)
        self.assertEqual(self.game.reads, 0)

    def test_cli_defaults_protect_other_player_and_continue_under_high_stack(self):
        for bot, human in ((1, 1), (2, 0)):
            with self.subTest(bot=bot):
                self.setUp()
                self.game.human_board = stack(17)
                calls, output = self.run_cli(['--practice', '--player', str(bot), '--telemetry'])
                self.assertGreaterEqual(calls, 3)
                self.assertLess(calls, 50)
                self.assertEqual(set(self.game.read_players), {human})
                self.assertTrue(any(state['practice']['piece_delay'] == 2 for state in self.states(output)))
                self.assertTrue(all(state['practice']['effective_speed'] == 10 for state in self.states(output)))

    def test_cli_recovers_and_emits_extra_wait_and_restored_pace(self):
        self.game.human_board = stack(17)
        def recover():
            if self.clock.now >= .4: self.game.human_board = stack(10)
        self.clock.on_sleep = recover
        calls, output = self.run_cli(['--practice', '--player', '2', '--practice-player', '1', '--telemetry'])
        self.assertGreater(calls, 3)
        states = [state['practice'] for state in self.states(output)]
        self.assertTrue(any(state['level'] == 'very_slow' and state['piece_delay'] == 2 for state in states))
        self.assertTrue(any(state['height'] == 10 and state['level'] == 'normal' and state['piece_delay'] == 0 for state in states))
        self.assertTrue(all(state['effective_speed'] == 10 for state in states))

    def test_cli_unreadable_low_board_keeps_playing(self):
        self.game.error = RuntimeError('Waiting for line-clear animation')
        calls, output = self.run_cli(['--practice', '--player', '2', '--telemetry'])
        self.assertGreater(calls, 50)
        self.assertTrue(all(state['practice']['piece_delay'] == 0 for state in self.states(output)))

    def test_cli_threatened_bot_keeps_normal_piece_rate(self):
        self.game.human_board, self.game.bot_board = stack(17), stack(17)
        calls, _ = self.run_cli(['--practice', '--player', '2'])
        self.assertGreater(calls, 50)

    def test_manual_pause_is_preserved_and_is_the_only_zero_speed_report(self):
        calls, output = self.run_cli(['--practice', '--player', '2', '--telemetry', '--paused'])
        self.assertEqual(calls, 0)
        self.assertTrue(self.states(output))
        self.assertTrue(all(state['paused'] and state['practice']['effective_speed'] == 0 for state in self.states(output)))

    def test_cli_rejects_bot_as_practice_object_before_attaching(self):
        from hoiko_linux.cli import main
        with patch('sys.argv', ['hoiko', 'run', '--practice', '--player', '2', '--practice-player', '2']), \
                patch('hoiko_linux.cli.attach') as attach, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as result: main()
        self.assertEqual(result.exception.code, 2)
        attach.assert_not_called()


if __name__ == '__main__': unittest.main()
