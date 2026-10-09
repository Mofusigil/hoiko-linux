"""Feedback-driven character selection for verified PPT2 menu layouts."""
import time


def character_buttons(character):
    if not 0 <= character <= 39: raise ValueError("Invalid PPT2 character")
    row = character // 10
    if row in (1, 2): return ("up",)
    if row == 3: return ("down",)
    if character == 7: return ("a",)
    return ("right",) if 2 <= character <= 6 else ("left",)


class AutoSelector:
    def __init__(self, game, pad, player):
        self.game, self.pad, self.player = game, pad, player
        self.last_press = 0
        self.observed = None
        self.attempts = 0

    def step(self):
        menu = self.game.character_selection()
        if menu is None:
            self.match_started()
            return None
        if self.player >= len(menu["characters"]): return None
        states = menu.get("states", ())
        if self.player >= len(states):
            return "Automatic selection needs a verified menu profile; select Tetris / Normal A manually"
        state = states[self.player]
        character = menu["characters"][self.player]
        identity = (menu["pointer"], character, state["phase"], state["tetris"], state["controls"])
        if identity != self.observed:
            self.observed, self.attempts = identity, 0
        phase = state["phase"]
        if phase == "ready":
            return f"Player {self.player + 1}: ready; waiting for the other player"
        if phase == "unknown":
            return f"Player {self.player + 1}: unknown menu flags {state['flags']}; waiting"
        if self.game.offline_status(self.player)["online"]: return None
        if not self.pad.focused():
            return "Focus PPT2 to let the bot select its character"
        if time.monotonic() - self.last_press < 0.35: return None
        if self.attempts >= 8:
            return f"Player {self.player + 1}: menu did not acknowledge input; check controller assignment"
        if phase == "character":
            buttons = character_buttons(character)
        elif phase == "style":
            buttons = ("a",) if state["tetris"] else ("right",)
        elif phase == "controls":
            # The gameplay backend implements the Normal A layout.
            buttons = ("b",) if not state["tetris"] else (("left",) if state["controls"] else ("a",))
        else:
            return None
        self.last_press = time.monotonic()
        self.attempts += 1
        try:
            self.pad.press(buttons)
            time.sleep(0.08)
        finally:
            self.pad.release()
        time.sleep(0.08)
        after = self.game.character_selection()
        if after:
            changed = [i for i, pair in enumerate(zip(menu["characters"], after["characters"]))
                       if pair[0] != pair[1]]
            after_states = after.get("states", ())
            changed += [i for i, pair in enumerate(zip(states, after_states)) if pair[0] != pair[1]]
            if changed and self.player not in changed:
                # Stop further automatic input after detecting a different slot.
                self.attempts = 8
                raise RuntimeError(f"Controller moved player {changed[0] + 1}, expected {self.player + 1}")
        return f"Player {self.player + 1}: {phase}, {'+'.join(buttons)} (waiting for acknowledgement)"

    def match_started(self):
        self.observed, self.attempts = None, 0
