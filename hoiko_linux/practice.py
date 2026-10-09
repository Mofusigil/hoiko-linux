"""Optional, bounded piece pacing without interrupting the Bot's moves."""
import time


def stack_height(board):
    if len(board) != 32 or any(type(row) is not int or not 0 <= row <= 1023 for row in board):
        raise ValueError("Invalid practice board")
    return max((y + 1 for y, row in enumerate(board) if row), default=0)


class PracticeGuard:
    SLOW_HEIGHT = 14
    VERY_SLOW_HEIGHT = 17
    SELF_PROTECT_HEIGHT = 17
    SAFE_HEIGHT = 10
    RECOVERY_SECONDS = 2
    CONFIRM_SECONDS = 0.15
    STALE_SECONDS = 1
    SAMPLE_INTERVAL = 0.05
    PIECE_DELAYS = {"normal": 0, "slow": 1.0, "very_slow": 2.0}

    def __init__(self, game, player, clock=time.monotonic):
        self.game, self.player, self.clock = game, player, clock
        self.on_sample = None
        self.reset()

    def reset(self):
        self.level = "normal"
        self.height = None
        self.bot_height = 0
        self.error = None
        self.safe_since = None
        self.candidate = None
        self.last_valid = None
        self.last_sample = None
        self.last_move = None
        self.match = None
        self.frame = None

    def update_bot(self, board):
        self.bot_height = stack_height(board)

    @property
    def piece_delay(self):
        # A threatened Bot must keep clearing its own board at the normal pace.
        return 0 if self.bot_height >= self.SELF_PROTECT_HEIGHT else self.PIECE_DELAYS[self.level]

    def ready(self, earliest_input=None):
        if self.last_move is None or not self.piece_delay:
            return True
        baseline = max(self.last_move, earliest_input if earliest_input is not None else self.last_move)
        return self.clock() >= baseline + self.piece_delay

    def move_finished(self):
        self.last_move = self.clock()

    @property
    def status(self):
        prefix = f"陪练 · {self.player + 1}P"
        height = f"堆高 {self.height} 行" if self.height is not None else "等待有效棋盘"
        if self.bot_height >= self.SELF_PROTECT_HEIGHT and self.level != "normal":
            action = "Bot 优先自保，保持正常节奏"
        elif self.piece_delay:
            action = f"减速练习 · 每块额外间隔 {self.piece_delay:g} 秒"
        else:
            action = "正常练习"
        if self.error:
            action += " · 棋盘暂不可读，沿用有效判断" if self.height is not None else " · 棋盘暂不可读，保持原节奏"
        return f"{prefix} {height} · {action}"

    def as_dict(self, configured):
        return dict(player=self.player + 1, level=self.level, height=self.height,
                    effective_speed=configured, piece_delay=self.piece_delay, status=self.status)

    def sample(self, force=False):
        now = self.clock()
        if not force and self.last_sample is not None and now - self.last_sample < self.SAMPLE_INTERVAL:
            return
        try:
            match = (self.game.match_pointer(), self.game.player(self.player))
            frame = self.game.match_frames()
            if match != self.match or (self.frame is not None and frame < self.frame):
                self.reset()
            self.match, self.frame = match, frame
            self.height = stack_height(self.game.practice_board(self.player))
            self.last_valid, self.error = now, None
        except (RuntimeError, OSError, ValueError) as exc:
            # Clearing-line markers and failed reads are not evidence of danger.
            self.error, self.candidate, self.safe_since = str(exc), None, None
            if self.last_valid is None or now - self.last_valid >= self.STALE_SECONDS:
                self.level, self.height = "normal", None
            self.last_sample = now
            if self.on_sample: self.on_sample()
            return
        self.last_sample = now
        target = "very_slow" if self.height >= self.VERY_SLOW_HEIGHT else "slow" if self.height >= self.SLOW_HEIGHT else "normal"
        ranks = {"normal": 0, "slow": 1, "very_slow": 2}
        if ranks[target] > ranks[self.level]:
            self.safe_since = None
            if self.candidate is None or self.candidate[0] != target:
                self.candidate = (target, now)
            elif now - self.candidate[1] >= self.CONFIRM_SECONDS:
                self.level, self.candidate = target, None
        else:
            self.candidate = None
            if self.level == "very_slow" and self.height < self.VERY_SLOW_HEIGHT:
                self.level = "slow"
            if self.level != "normal" and self.height <= self.SAFE_HEIGHT:
                if self.safe_since is None: self.safe_since = now
                if now - self.safe_since >= self.RECOVERY_SECONDS:
                    self.level, self.safe_since = "normal", None
            else:
                self.safe_since = None
        if self.on_sample: self.on_sample()

    def check(self, force=False):
        # Observations may adjust the next piece's pace, never abort this move.
        self.sample(force)
