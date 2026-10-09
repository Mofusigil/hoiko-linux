"""Panel preferences; original executable and config.dll remain untouched."""
import json
import os
from pathlib import Path

DEFAULTS = dict(player=2, speed=10, beam=100, depth=6, budget=112,
                visible_next=5, hold=True, td=False, td_loop=False, s4w=False,
                auto_select=True, lifespan=0, practice=False, practice_player=1)
SETTINGS_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "hoiko/settings.json"


def validate(data):
    if not isinstance(data, dict): raise ValueError("Settings must be an object")
    result = DEFAULTS.copy()
    for key, value in data.items():
        if key not in result: continue
        if isinstance(result[key], bool):
            if not isinstance(value, bool): raise ValueError(f"{key} must be true or false")
        else:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{key} must be a number")
        result[key] = value
    bounds = dict(player=(1, 4), speed=(1, 100), beam=(1, 500), depth=(1, 14),
                  budget=(1, 10000), visible_next=(2, 5), lifespan=(0, 60), practice_player=(1, 4))
    for key, (lo, hi) in bounds.items():
        if not lo <= result[key] <= hi: raise ValueError(f"{key} must be {lo}–{hi}")
        if key not in ("speed", "budget") and int(result[key]) != result[key]:
            raise ValueError(f"{key} must be an integer")
        if key not in ("speed", "budget"): result[key] = int(result[key])
    if result['lifespan'] not in (0, 1, 3, 5, 10, 30, 60): raise ValueError("Invalid lifespan")
    if not result['hold'] or result['visible_next'] < 5: result['td'] = False
    if not result['td']: result['td_loop'] = False
    if result['practice'] and result['practice_player'] == result['player']:
        raise ValueError("陪练对象必须与 Bot 玩家位置不同")
    return result


def load(path=SETTINGS_PATH):
    if not path.exists(): return DEFAULTS.copy()
    return validate(json.loads(path.read_text()))


def save(data, path=SETTINGS_PATH):
    data = validate(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def original_config(path):
    values = dict(line.strip().split(',', 1) for line in path.read_text().splitlines() if ',' in line)
    data = DEFAULTS.copy()
    mapping = dict(player='player', speed='speed', beamSize='beam', viewNext='visible_next',
                   useHold='hold', useTDOpeners='td', loopTemplate='td_loop',
                   useS4W='s4w', autoSelect='auto_select', lifespan='lifespan')
    notes = []
    for old, new in mapping.items():
        if old not in values: continue
        value = values[old]
        data[new] = value.lower() == 'true' if isinstance(DEFAULTS[new], bool) else int(value)
    if 'player' in values:
        data['player'] += 1  # Original ConfigAI stores a zero-based player slot.
    if data['visible_next'] > 5:
        data['visible_next'] = 5; notes.append('当前内存适配只读取 5 个 Next，已将 12 调整为 5。')
    if data['lifespan'] == 1440: data['lifespan'] = 0
    if values.get('useOffsetOff') == 'true': notes.append('相杀外し尚未移植，未启用。')
    if values.get('usePCstack') == 'true': notes.append('PC stack 需要 12 Next，未启用。')
    if values.get('threadCount', '1') != '1': notes.append('恢复核心采用串行搜索，线程数为 1。')
    # The supplied binary returns constants for these hidden settings.
    notes.append('原版隐藏的打法 / 最小深度 / 接管时间未作为有效参数导入。')
    return validate(data), notes


def run_arguments(data):
    s = validate(data)
    args = ['run', '--backend', 'gamepad', '--control-stdin', '--telemetry']
    for key, flag in [('player','--player'), ('speed','--speed'), ('beam','--beam'),
                      ('depth','--depth'), ('budget','--budget'), ('visible_next','--next'),
                      ('lifespan','--lifespan')]: args += [flag, str(s[key])]
    for key, flag in [('td','--td'), ('td_loop','--td-loop'), ('s4w','--s4w')]:
        if s[key]: args.append(flag)
    if not s['hold']: args.append('--no-hold')
    if not s['auto_select']: args.append('--no-auto-select')
    if s['practice']: args += ['--practice', '--practice-player', str(s['practice_player'])]
    return args
