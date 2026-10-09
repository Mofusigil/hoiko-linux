#!/usr/bin/env python3
"""Bounded offline PPT2 test: compare planned landings with the actual board.

Prepare local two-player Tetris VS first, then run this instead of the normal
bot. It uses a temporary virtual controller and never advances 1P menus.
"""
import argparse
import json
from pathlib import Path
import struct
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hoiko_linux.cli import attach
from hoiko_linux.controller import Gamepad, execute
from hoiko_linux.core import Engine
from hoiko_linux.keyboard import Keyboard
from hoiko_linux.runtime import InputPacer, RunControl, speed_percent


def read_board(game, player):
    memory = game.memory
    pointer = memory.pointer(memory.pointer(game.player(player) + 7352) + 24)
    columns = struct.unpack('<10Q', memory.read(pointer, 80))
    board = [0] * 32
    for x, column in enumerate(columns):
        cells = struct.unpack('<32i', memory.read(column, 128))
        if any(value < -1 for value in cells):
            raise RuntimeError('Waiting for line-clear animation')
        for y, value in enumerate(cells):
            if value >= 0: board[y] |= 512 >> x
    return board


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--turns', type=int, default=20)
    parser.add_argument('--seconds', type=float, default=90)
    parser.add_argument('--speed', type=speed_percent, default=40)
    parser.add_argument('--td', action='store_true')
    parser.add_argument('--output', type=Path, default=ROOT / 'build/final-check/placements.json')
    args = parser.parse_args()
    if args.turns < 1 or args.seconds <= 0: parser.error('Use positive limits')
    if 'Hoiko Linux Virtual Xbox 360' in Path('/proc/bus/input/devices').read_text():
        raise RuntimeError('Stop the existing bot before starting the live check')
    game = attach(SimpleNamespace(pid=None, game=2, delta=None))
    status = game.offline_status(1)
    if status['online'] or status['local_mode'] != 3 or status['players'] != 2:
        raise RuntimeError('Prepare offline local two-player VS first')
    engine = Engine(beam=40, depth=4, td=args.td)
    control = RunControl(args.speed)
    pacer = InputPacer(control)
    records = []
    deadline = time.monotonic() + args.seconds
    previous_tick = None
    last_status = None

    class VerifiedPad(Gamepad):
        target = None

        def press(self, button):
            if button == 'up':
                position = game.position(1)
                target = self.target
                landing = (position['x'], position['y'] - position['ghost_y'],
                           position['rotation'], position['piece'])
                expected = (target.x + 1,
                            target.y - 1 - int(target.piece == 0 and target.rotation in (2, 3)),
                            target.rotation, target.piece)
                if landing != expected:
                    raise AssertionError(f'Landing mismatch: actual {landing}, planned {expected}; {target.commands!r}')
            super().press(button)

    window = Keyboard(game.memory.pid)
    try:
        with VerifiedPad() as pad:
            pad.focused = window.focused
            window.activate()
            print('Ready: waiting for local VS. 1P can confirm Continue / Game Start.', flush=True)
            while len(records) < args.turns and time.monotonic() < deadline:
                try:
                    state = game.snapshot(1)
                    tick = (game.match_pointer(), game.match_frames())
                    if previous_tick is None or tick == previous_tick or state['position']['locked']:
                        previous_tick = tick
                        time.sleep(.025)
                        continue
                    previous_tick = tick
                    result = engine.solve(state['board'], state['pieces'], hold=state['hold'],
                                          combo=state['combo'], btb=state['btb'], reset=True)
                    if not result.commands: raise AssertionError('No playable move')
                    pad.target = result
                    execute(game, pad, 1, result, state, deadline, pacer, control)
                    expires = min(deadline, time.monotonic() + 2)
                    while True:
                        try:
                            board = read_board(game, 1)
                            if board == list(result.board): break
                        except RuntimeError:
                            board = None
                        if time.monotonic() >= expires:
                            raise AssertionError(f'Board mismatch after {result.commands!r}: {board}, expected {list(result.board)}')
                        time.sleep(.01)
                    record = dict(turn=len(records) + 1, commands=result.commands.decode(),
                                  placement=dict(x=result.x, y=result.y, rotation=result.rotation, piece=result.piece),
                                  board=board, ms=round(result.elapsed_ms, 1))
                    records.append(record)
                    print(json.dumps({key: value for key, value in record.items() if key != 'board'}), flush=True)
                    last_status = None
                except (RuntimeError, OSError, TimeoutError) as exc:
                    pad.release()
                    if str(exc) != last_status:
                        print(str(exc), flush=True)
                        last_status = str(exc)
                    time.sleep(.1)
        if len(records) < args.turns:
            raise RuntimeError(f'Time limit: verified {len(records)}/{args.turns} placements')
        print(f'PASS: {len(records)} actual landings and boards matched the solver', flush=True)
    finally:
        window.close()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(records, indent=2) + '\n')


if __name__ == '__main__': main()
