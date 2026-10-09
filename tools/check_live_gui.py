#!/usr/bin/env python3
"""Live panel regression. Prepare local 2P Tetris VS; stop other bots first.

Uses temporary preferences, plays a few pieces, tests pause/manual/speed/resume,
then stops and checks that its virtual controller is gone. Timeout: 90 seconds.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    if 'Hoiko Linux Virtual Xbox 360' in Path('/proc/bus/input/devices').read_text():
        raise RuntimeError('Stop the existing bot before starting the live panel check')
    with tempfile.TemporaryDirectory(prefix='hoiko-live-panel-') as directory:
        os.environ['XDG_CONFIG_HOME'] = directory
        from hoiko_linux.cli import attach
        from hoiko_linux.gui import Panel, GLib
        from hoiko_linux import settings
        game = attach(SimpleNamespace(pid=None, game=2, delta=None))
        status = game.offline_status(1)
        if status['online'] or status['local_mode'] != 3 or status['players'] != 2:
            raise RuntimeError('Prepare offline local two-player VS first')
        app = Panel()
        app.set_application_id('io.hoiko.LivePanelCheck')
        append_log = app.append_log
        def log_worker(text):
            print('worker:', text, flush=True)
            append_log(text)
        app.append_log = log_worker
        failures = []
        report = {}
        stage = 'start'
        entered = started = time.monotonic()
        baseline = original_x = original_count = 0
        last_status = None

        def advance(value):
            nonlocal stage, entered
            stage, entered = value, time.monotonic()
            print('panel check:', value, flush=True)

        def check():
            nonlocal baseline, original_x, original_count, last_status
            try:
                status = app.worker_status.get_text()
                if status != last_status:
                    print('status:', status, flush=True)
                    last_status = status
                if stage in ('playing', 'pausing', 'paused', 'manual-right', 'manual-left', 'resuming'):
                    assert app.running(), 'Worker exited: ' + status
                if time.monotonic() - started > 90:
                    raise AssertionError('Live panel check timed out at ' + stage)
                if stage == 'start':
                    app.set_preferences(dict(settings.DEFAULTS, speed=40, beam=40, depth=4))
                    app.start_worker(False)
                    advance('playing')
                elif stage == 'playing' and app.turns >= 4:
                    app.pause_worker()
                    advance('pausing')
                elif stage == 'pausing' and '自动操作已暂停' in app.worker_status.get_text():
                    baseline = app.turns
                    advance('paused')
                elif stage == 'paused' and time.monotonic() - entered > 1:
                    assert app.running() and app.turns == baseline, 'Bot played while paused'
                    position = game.position(1)
                    if position['locked']: return True
                    original_x = position['x']
                    original_count = game.memory.byte(game.player(1) + 7392)
                    app.send({'action': 'button', 'value': 'right'})
                    advance('manual-right')
                elif stage == 'manual-right' and time.monotonic() - entered > .5:
                    assert game.memory.byte(game.player(1) + 7392) == original_count, 'Piece changed during manual test'
                    assert game.position(1)['x'] == original_x + 1, 'Manual right was not acknowledged'
                    app.send({'action': 'button', 'value': 'left'})
                    advance('manual-left')
                elif stage == 'manual-left' and time.monotonic() - entered > .5:
                    assert game.position(1)['x'] == original_x, 'Manual left was not acknowledged'
                    app.speed_scale.set_value(23)
                    assert app.values()['speed'] == 23, 'Slider did not apply speed'
                    app.start_or_resume()
                    advance('resuming')
                elif stage == 'resuming' and app.turns >= baseline + 3:
                    report.update(paused_turn=baseline, resumed_turn=app.turns,
                                  manual_buttons=True, speed=app.values()['speed'])
                    app.stop_worker()
                    advance('stopping')
                elif stage == 'stopping' and not app.running():
                    advance('disconnected')
                elif stage == 'disconnected' and time.monotonic() - entered > .5:
                    assert 'Hoiko Linux Virtual Xbox 360' not in Path('/proc/bus/input/devices').read_text(), 'Controller was orphaned'
                    report['passed'] = True
                    print('PASS:', json.dumps(report), flush=True)
                    app.close_requested()
                    return False
            except Exception as exc:
                failures.append(repr(exc))
                app.close_requested()
                return False
            return True

        GLib.timeout_add(1000, check)
        print('Ready: waiting for local VS. 1P can confirm Continue / Game Start.', flush=True)
        app.run([])
        output = ROOT / 'build/final-check/panel-live.json'
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(dict(report, failures=failures), indent=2) + '\n')
        if failures: raise RuntimeError(failures)


if __name__ == '__main__': main()
