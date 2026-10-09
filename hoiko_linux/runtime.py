"""Small stdin control protocol and release-between-actions speed pacing."""
import json
import math
import queue
import threading
import time


def speed_percent(value):
    value = float(value)
    if not math.isfinite(value) or not 1 <= value <= 100:
        raise ValueError("Speed must be between 1 and 100 percent")
    return value


class RunControl:
    def __init__(self, speed=100):
        self.speed = speed_percent(speed)
        self.paused = False
        self.stopped = False
        self.commands = queue.Queue(maxsize=32)
        self.error = None

    def apply(self, data):
        action = data.get("action")
        if action == "speed": self.speed = speed_percent(data["value"])
        elif action == "pause": self.paused = True
        elif action == "resume": self.paused = False
        elif action == "stop": self.stopped = True
        elif action in ("focus", "button"):
            if action == "button" and data.get("value") not in ("left", "right", "up", "down", "a", "b", "y", "start"):
                raise ValueError("Unknown controller button")
            self.commands.put_nowait(data)
        else: raise ValueError("Unknown control action")

    def read(self, stream):
        try:
            for line in stream:
                try:
                    data = json.loads(line)
                    if not isinstance(data, dict): raise ValueError("Expected a control object")
                    self.apply(data)
                except (ValueError, KeyError, TypeError, queue.Full) as exc:
                    self.error = str(exc)
        finally:
            self.stopped = True  # Closing the panel cannot orphan an input bot.

    def start(self, stream):
        threading.Thread(target=self.read, args=(stream,), daemon=True).start()

    def check(self):
        if self.stopped: raise KeyboardInterrupt


class InputPacer:
    """100% preserves the established ~50 ms action cadence; 5% = 1 s.

    Delays happen with all buttons released, so slowing down never triggers
    the game's DAS/autorepeat by holding a direction for hundreds of ms.
    """
    def __init__(self, control, clock=time.monotonic, sleep=time.sleep):
        self.control, self.clock, self.sleep = control, clock, sleep
        self.last = None

    @property
    def interval(self): return 0.05 * 100 / self.control.speed

    def wait(self, active):
        while self.last is not None:
            self.control.check()
            active()
            remaining = self.interval - (self.clock() - self.last)
            if remaining <= 0: break
            self.sleep(min(remaining, 0.01))
        self.control.check()
        self.last = self.clock()
