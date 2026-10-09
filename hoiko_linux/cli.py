import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
import queue
from .runtime import RunControl, InputPacer, speed_percent
from .core import Engine, PIECES, ROOT
from .memory import ProcessMemory, GameMemory, find_game
from .practice import PracticeGuard


def attach(args):
    process = ProcessMemory(args.pid or find_game(args.game))
    game = GameMemory(process, args.game, args.delta or 0)
    if args.delta is None:
        profiles = json.loads((ROOT / "profiles.json").read_text())
        fingerprint = hashlib.sha256(game.executable.read_bytes()).hexdigest()
        profile = next((p for p in profiles if p["game"] == args.game and p["sha256"] == fingerprint), None)
        if profile is None:
            raise RuntimeError(f"Unknown game build (SHA256 {fingerprint}); verify offsets before using --delta")
        game.delta = int(profile["delta"], 0)
        game.addresses = {name: int(value, 0) for name, value in profile.get("addresses", {}).items()}
        game.character_layout = profile.get("character_layout", 2)
        game.status_source = profile.get("status_source", "original")
        game.menu_profile = profile.get("menu")
    return game


def main():
    parser = argparse.ArgumentParser(description="Recovered Hoiko for Linux and offline Proton Tetris VS")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "watch", "run"):
        p = sub.add_parser(name)
        p.add_argument("--game", type=int, choices=(1, 2), default=2)
        p.add_argument("--pid", type=int)
        p.add_argument("--player", type=int, choices=(1, 2, 3, 4), default=None if name == 'run' else 2)
        p.add_argument("--delta", type=lambda x: int(x, 0))
        if name != "doctor": p.add_argument("--seconds", type=float, default=0)
        if name == "run":
            p.add_argument("--dry-run", action="store_true")
            p.add_argument("--beam", type=int, default=100)
            p.add_argument("--depth", type=int, default=6)
            p.add_argument("--budget", type=float, default=112)
            p.add_argument("--td", action="store_true")
            p.add_argument("--no-hold", action="store_true")
            p.add_argument("--s4w", action="store_true")
            p.add_argument("--td-loop", action="store_true")
            p.add_argument("--next", type=int, choices=range(2, 6), default=5, dest="visible_next")
            p.add_argument("--speed", type=speed_percent, default=100)
            p.add_argument("--practice", action="store_true", help="Add bounded delays between pieces when a human's stack is high")
            p.add_argument("--practice-player", type=int, choices=(1, 2, 3, 4),
                           help="Human player to protect; defaults to 1P (2P when the Bot controls 1P)")
            p.add_argument("--lifespan", type=int, choices=(0, 1, 3, 5, 10, 30, 60), default=0,
                           help="Per-match playing time in minutes; 0 is unlimited")
            p.add_argument("--control-stdin", action="store_true", help="Read GUI control JSON from stdin")
            p.add_argument("--telemetry", action="store_true", help="Emit snapshots for the desktop panel")
            p.add_argument("--paused", action="store_true", help="Connect controller without automatic play")
            p.add_argument("--backend", choices=("auto", "gamepad", "keyboard"), default="auto")
            p.add_argument("--keys", type=Path, help="JSON mapping of gamepad action names to X11 keys")
            p.add_argument("--no-auto-select", action="store_true")
            p.add_argument("--interactive", action="store_true", help="Accept menu buttons on stdin")
            p.add_argument("--focus", action="store_true", help="Bring the game window to the foreground")
    sub.add_parser("gui", help="Open the desktop control panel")
    p = sub.add_parser("solve")
    p.add_argument("file", type=Path)
    p.add_argument("--beam", type=int, default=100)
    p.add_argument("--depth", type=int, default=6)
    p = sub.add_parser("controller")
    p.add_argument("--seconds", type=float, default=60)
    args = parser.parse_args()
    if args.command == "gui":
        from .gui import launch
        launch(); return
    if args.command == "run" and args.interactive and args.control_stdin:
        parser.error("--interactive and --control-stdin cannot read stdin together")
    if args.command == "run" and args.td and (args.no_hold or args.visible_next < 5):
        parser.error("TD openers require Hold and 5 visible Next pieces")
    if args.command == "run" and args.practice and args.practice_player is not None and args.practice_player == args.player:
        parser.error("The practice player must differ from the Bot player")
    if args.command == "solve":
        data = json.loads(args.file.read_text())
        result = Engine(beam=args.beam, depth=args.depth).solve(**data, reset=True)
        print(json.dumps(result.as_dict(), indent=2)); return
    if args.command == "controller":
        from .controller import Gamepad
        with Gamepad():
            print("Virtual Xbox 360 connected; no buttons pressed", flush=True)
            time.sleep(args.seconds)
        return
    game = attach(args)
    print(f"Attached PID {game.memory.pid}, {game.executable.name}, base {game.base:#x}, delta {game.delta:#x}", flush=True)
    if args.command == "doctor":
        start = game.frames(); time.sleep(0.12); end = game.frames()
        import os
        try:
            fd = os.open('/dev/uinput', os.O_WRONLY | os.O_NONBLOCK)
            os.close(fd)
            uinput = {"available": True}
        except OSError as e:
            uinput = {"available": False, "error": str(e)}
        print(json.dumps({"memory_read": True, "frame_counter_advances": end > start,
                          "frame": end, "uinput": uinput, "mode": game.mode_id(),
                          "character_menu": game.character_selection(),
                          "match_pointer": hex(game.match_pointer()),
                          "player": args.player, "status": game.offline_status(args.player - 1)}, indent=2))
        return
    deadline = time.monotonic() + args.seconds if args.seconds else None
    player, last_status = (args.player or 1) - 1, None
    if args.command == "watch":
        while not deadline or time.monotonic() < deadline:
            try:
                state = game.snapshot(player)
                state["pieces"] = ''.join(PIECES[p] for p in state["pieces"])
                print(json.dumps(state), flush=True)
            except (RuntimeError, OSError, ValueError) as e:
                if str(e) != last_status: print(str(e), flush=True); last_status = str(e)
            time.sleep(0.5)
        return
    from .controller import Gamepad, execute
    engine = Engine(game=args.game, beam=args.beam, depth=args.depth, budget=args.budget, td=args.td, hold=not args.no_hold,
                    s4w=args.s4w, td_loop=args.td_loop)
    control = RunControl(args.speed)
    control.paused = args.paused
    if args.control_stdin: control.start(sys.stdin)
    practice = None
    pad = window = selector = None
    try:
        if not args.dry_run:
            if args.backend in ('auto', 'gamepad'):
                try: pad = Gamepad()
                except OSError as e:
                    if args.backend == 'gamepad': raise
                    print(f"uinput is unavailable ({e}); using keyboard on player 1", flush=True)
            if pad is None:
                from .keyboard import Keyboard
                if args.player not in (None, 1):
                    raise RuntimeError("Keyboard controls player 1; use --player 1 or enable uinput for another player")
                pad = window = Keyboard(game.memory.pid, json.loads(args.keys.read_text()) if args.keys else None)
                player = 0
            elif args.player is None: player = 1
        if isinstance(pad, Gamepad):
            from .keyboard import Keyboard
            window = Keyboard(game.memory.pid)
            pad.focused = window.focused
            if args.game == 2 and not args.no_auto_select:
                from .menu import AutoSelector
                selector = AutoSelector(game, pad, player)
        if args.practice:
            human = args.practice_player - 1 if args.practice_player else (0 if player != 0 else 1)
            if human == player:
                raise ValueError("The practice player must differ from the Bot player")
            practice = PracticeGuard(game, human)
    except BaseException:
        try:
            if pad: pad.close()
        finally:
            if window: window.close()
        raise
    pacer = InputPacer(control)
    last_count, needs_reset, played = None, True, 0
    last_menu_status = None
    pending_buttons = []
    last_activation = 0
    last_telemetry = 0
    life_match, life_started = None, 0
    previous_tick = None
    previous_match_frame = None
    def telemetry(state=None, status=None):
        nonlocal last_telemetry
        if args.telemetry and time.monotonic() - last_telemetry >= 0.2:
            data = {"event": "state", "state": state, "status": status,
                    "speed": control.speed, "paused": control.paused}
            if practice:
                data["practice"] = practice.as_dict(control.speed)
                if control.paused: data["practice"]["effective_speed"] = 0
            print(json.dumps(data), flush=True)
            last_telemetry = time.monotonic()
    practice_status = None
    def report_practice():
        nonlocal practice_status
        status = (practice.level, practice.error)
        if status != practice_status:
            print(practice.status, flush=True)
            practice_status = status
        telemetry(status=practice.status)
    if practice: practice.on_sample = report_practice
    try:
        print("Waiting for offline local VS; Ctrl+C stops and disconnects the pad", flush=True)
        while not deadline or time.monotonic() < deadline:
            try:
                control.check()
                if control.error:
                    print(f"Control error: {control.error}", flush=True); control.error = None
                while not control.commands.empty():
                    try: request = control.commands.get_nowait()
                    except queue.Empty: break
                    if window and request["action"] == "focus": window.activate()
                    elif pad and request["action"] == "button" and control.paused:
                        if window: window.activate(); time.sleep(0.2)
                        if pad.focused() and game.mode_id() == 14 and not game.offline_status(player)["online"]:
                            try: pad.press(request["value"]); time.sleep(0.08)
                            finally: pad.release()
                if control.paused:
                    if pad: pad.release()
                    try: telemetry(game.snapshot(player), "自动操作已暂停，手柄保持连接")
                    except (RuntimeError, OSError, ValueError): telemetry(status="自动操作已暂停，手柄保持连接")
                    time.sleep(0.05); continue
                if args.focus and window and not window.focused() and time.monotonic() - last_activation > 1:
                    window.activate()
                    last_activation = time.monotonic()
                if args.interactive:
                    import select
                    if select.select([sys.stdin], [], [], 0)[0]:
                        command = sys.stdin.readline().strip().lower()
                        if command == "pause": selector = None
                        elif command == "auto" and window:
                            from .menu import AutoSelector
                            selector = AutoSelector(game, pad, player)
                        elif command in ("left", "right", "up", "down", "a", "b", "y", "start"):
                            pending_buttons.append(command)
                    if pending_buttons and pad and pad.focused() and game.mode_id() == 14:
                        command = pending_buttons.pop(0)
                        pad.press(command); time.sleep(0.08); pad.release()
                        print(f"Menu button: {command}", flush=True)
                        time.sleep(0.2)
                if selector:
                    message = selector.step()
                    if message and message != last_menu_status:
                        print(message, flush=True)
                        last_menu_status = message
                state = game.snapshot(player)
                match = game.match_pointer()
                match_frame = game.match_frames()
                if match != life_match or (previous_match_frame is not None and match_frame < previous_match_frame):
                    life_match, life_started = match, time.monotonic()
                    needs_reset, last_count = True, None
                    if practice: practice.reset()
                previous_match_frame = match_frame
                tick = (match, match_frame)
                last_tick, previous_tick = previous_tick, tick
                if last_tick is None or tick == last_tick:
                    telemetry(state, "等待游戏继续 / 对局时钟暂停")
                    time.sleep(0.025); continue
                if practice:
                    practice.sample()
                    practice.update_bot(state["board"])
                    telemetry(state, practice.status)
                else:
                    telemetry(state, "运行中")
                if args.lifespan and time.monotonic() - life_started >= args.lifespan * 60:
                    raise RuntimeError("Match lifespan reached; waiting for the next match")
                if state["game_state"] > 1 or state["position"]["locked"]:
                    time.sleep(0.01); continue
                identity = (game.match_pointer(), state["next_count"], tuple(state["pieces"]), state["hold"])
                if identity == last_count: time.sleep(0.01); continue
                if selector: selector.match_started()
                if practice and not practice.ready(pacer.last + pacer.interval if pacer.last is not None else None):
                    time.sleep(0.025); continue
                result = engine.solve(state["board"], state["pieces"][:args.visible_next + 1], state["hold"],
                                      state["combo"], state["btb"], reset=needs_reset)
                if not result.commands: raise RuntimeError("No playable move")
                if pad: execute(game, pad, player, result, state, deadline, pacer, control, practice=practice)
                if practice: practice.move_finished()
                last_count, needs_reset = identity, False
                played += 1
                print(json.dumps({"turn": played, "commands": result.commands.decode(),
                                  "piece": PIECES[result.piece], "depth": result.depth,
                                  "nodes": result.nodes, "ms": round(result.elapsed_ms, 1)}), flush=True)
                last_status = None
            except (RuntimeError, OSError, TimeoutError, ValueError) as e:
                if pad: pad.release()
                needs_reset = True
                message = str(e)
                telemetry(status=message)
                if message != last_status: print(message, flush=True); last_status = message
                time.sleep(0.1)
    finally:
        try:
            if pad: pad.close()
        finally:
            if window: window.close()


if __name__ == "__main__":
    try: main()
    except KeyboardInterrupt: pass
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"hoiko: {exc}", file=sys.stderr); sys.exit(1)
