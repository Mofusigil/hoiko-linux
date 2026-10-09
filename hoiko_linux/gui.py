"""GTK4 desktop panel backed by the same CLI and recovered native core."""
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, Gdk, GLib, Gio

from .core import ROOT
from . import settings

CSS = b'''
window { background: #101619; color: #e7efef; font-family: "Noto Sans CJK SC", sans-serif; font-size: 18px; }
.header { padding: 16px 26px 12px; background: #151e22; border-bottom: 1px solid #2c383d; }
.brand { font-size: 32px; font-weight: 800; letter-spacing: 2px; }
.eyebrow { color: #76dfc4; font-size: 13px; letter-spacing: 2px; font-weight: 700; }
.muted { color: #a6bac2; font-size: 15px; }
.title { font-size: 20px; font-weight: 700; }
.card { background: #182226; border: 1px solid #2b3a40; border-radius: 12px; padding: 18px; }
.speed { color: #7be1c8; font-size: 40px; font-weight: 700; }
button { background: #27363d; color: #edf6f6; border: 1px solid #3b4e57; border-radius: 7px; padding: 10px 16px; box-shadow: none; }
button:hover { background: #354b54; }
button.suggested-action { background: #77dfc3; color: #0d2925; border-color: #77dfc3; font-weight: 700; }
button.destructive-action { color: #ffb1a8; }
button:disabled { opacity: 0.4; }
spinbutton, dropdown, entry { background: #111c20; color: #e7efef; border: 1px solid #3a4b52; border-radius: 6px; }
spinbutton button { padding: 5px 9px; border: none; }
scale trough { background: #30454d; min-height: 5px; }
scale highlight { background: #76dfc4; }
scale slider { background: #e2fff7; border: none; min-width: 17px; min-height: 17px; }
notebook stack { background: #101619; }
notebook > header { background: #101619; border-bottom: 1px solid #304047; }
notebook tab { padding: 10px 14px; color: #9db1b8; }
notebook tab:checked { color: #80e4cb; border-bottom: 2px solid #80e4cb; }
textview text { background: #10191d; color: #acc2ca; font-family: monospace; font-size: 14px; }
.status { color: #83e0c5; }
.warning { color: #e7bd74; }
separator { background: #2a3a41; }
'''


def label(text, css=None, wrap=False):
    widget = Gtk.Label(label=text, xalign=0)
    if css: widget.add_css_class(css)
    if wrap: widget.set_wrap(True); widget.set_max_width_chars(58)
    return widget


def box(vertical=True, spacing=12):
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL, spacing=spacing)


def margin(widget, amount):
    for side in ('top', 'bottom', 'start', 'end'): getattr(widget, 'set_margin_' + side)(amount)
    return widget


def button(text, callback, css=None):
    widget = Gtk.Button(label=text)
    widget.connect('clicked', callback)
    if css: widget.add_css_class(css)
    return widget


class Panel(Gtk.Application):
    def __init__(self):
        super().__init__(application_id='io.hoiko.ControlPanel', flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.connect('activate', self.activate_panel)
        self.events = queue.Queue()
        self.observations = queue.Queue(maxsize=1)
        self.observer_stop = threading.Event()
        self.process = None
        self.paused = False
        self.closing = False
        self.monitor_player = 1
        self.config_widgets = {}
        self.stopping = False
        self.turns = 0
        self.started = 0
        self.save_source = None
        self.load_error = None
        try: self.preferences = settings.load()
        except (OSError, ValueError, TypeError) as exc:
            self.preferences = settings.DEFAULTS.copy(); self.load_error = str(exc)
        self.monitor_player = self.preferences['player'] - 1

    def activate_panel(self, *_):
        if hasattr(self, 'window'): self.window.present(); return
        Gtk.Settings.get_default().set_property('gtk-theme-name', 'Adwaita')
        Gtk.Settings.get_default().set_property('gtk-application-prefer-dark-theme', True)
        provider = Gtk.CssProvider(); provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.window = Gtk.ApplicationWindow(application=self, title='Hoiko · 控制面板')
        self.window.set_default_size(920, 940)
        self.window.connect('close-request', self.close_requested)
        root = box(spacing=0); self.window.set_child(root)
        header = box(False, 18); header.add_css_class('header')
        identity = box(spacing=2); identity.append(label('HOIKO', 'brand')); identity.append(label('LINUX  /  PPT2  /  CONTROL PANEL', 'eyebrow'))
        header.append(identity)
        description = box(spacing=4); description.set_hexpand(True)
        description.append(label('从原版控制面板还原', 'title'))
        description.append(label('本地 VS · 原生搜索核心 · 精细速度控制', 'muted'))
        header.append(description)
        self.connection = label('正在检测游戏…', 'status'); header.append(self.connection); root.append(header)
        content = margin(box(spacing=14), 22); content.set_vexpand(True); root.append(content)
        left = box(spacing=14); left.set_hexpand(True); content.append(left)
        speed_card = box(spacing=10); speed_card.add_css_class('card'); left.append(speed_card)
        line = box(False); line.append(label('操作速度', 'title'))
        spacer = Gtk.Box(); spacer.set_hexpand(True); line.append(spacer)
        self.speed_text = label('', 'speed'); line.append(self.speed_text); speed_card.append(line)
        row = box(False)
        self.speed_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 1, 100, 1)
        self.speed_scale.set_draw_value(False); self.speed_scale.set_hexpand(True); row.append(self.speed_scale)
        self.speed_spin = Gtk.SpinButton.new_with_range(1,100,1); self.speed_spin.set_digits(0); self.speed_spin.set_numeric(True)
        self.speed_spin.set_tooltip_text('直接输入 1–100，按 Enter 应用；↑ / ↓ 微调')
        self.speed_scale.set_tooltip_text('拖动调速；聚焦后用方向键微调')
        row.append(self.speed_spin)
        speed_card.append(row)
        self.speed_hint = label('', 'muted', True); self.speed_hint.set_tooltip_text('只改变 Bot 输入节奏，游戏重力与搜索质量不变。'); speed_card.append(self.speed_hint)
        self.speed_spin.set_value(self.preferences['speed']); self.speed_scale.set_value(self.preferences['speed'])
        self.speed_spin.connect('value-changed', self.speed_changed)
        self.speed_scale.connect('value-changed', lambda scale: self.speed_spin.set_value(round(scale.get_value())))
        self.speed_changed(self.speed_spin)
        notebook = Gtk.Notebook(); self.notebook = notebook; notebook.set_vexpand(True); left.append(notebook)
        basic = margin(box(spacing=12), 16)
        self.add_number(basic,'player','玩家位置',1,4,1,'控制器分配由游戏决定；通常使用 2P。')
        self.add_number(basic,'visible_next','可见 Next 数量',2,5,1,'当前版本可读取未来 5 块；不猜测隐藏队列。')
        self.add_check(basic,'hold','允许 Hold','关闭后不使用暂存，并关闭 TD 开局和循环。')
        self.add_check(basic,'auto_select','自动选择角色与 Tetris','识别角色、玩法、操作方式三层菜单。')
        strategies = margin(box(spacing=16),16)
        self.add_check(strategies,'td','TD 开局','需要 Hold 和 5 个 Next；恢复自原版模板。')
        self.add_check(strategies,'td_loop','循环 TD 模板','依赖 TD 开局；相当于原版 TD loop。')
        self.add_check(strategies,'s4w','Side 4 Wide 评估','启用已恢复的 S4W 评分；对手威胁观测尚未接入。')
        basic_scroll = Gtk.ScrolledWindow(); basic_scroll.set_policy(Gtk.PolicyType.NEVER,Gtk.PolicyType.AUTOMATIC)
        basic_scroll.set_min_content_height(200); basic_scroll.set_child(basic)
        basic_scroll.set_overlay_scrolling(False)
        notebook.append_page(basic_scroll, label('基本设置'))
        strategies.append(label('TD 开局需要 Hold 与 5 个 Next；循环模板需要启用 TD。S4W 的对手威胁观测尚未移植。','muted',True))
        strategy_scroll=Gtk.ScrolledWindow();strategy_scroll.set_policy(Gtk.PolicyType.NEVER,Gtk.PolicyType.AUTOMATIC)
        strategy_scroll.set_overlay_scrolling(False);strategy_scroll.set_child(strategies)
        notebook.append_page(strategy_scroll,label('策略模板'))
        practice = margin(box(spacing=16),16)
        self.add_check(practice,'practice','启用陪练模式','保护指定的人类玩家；关闭后使用原来的对战行为。')
        self.add_number(practice,'practice_player','陪练对象 / 人类玩家',1,4,1,'请选择人类玩家的位置，与 Bot 控制的玩家位置不同。')
        self.practice_status = label('开始运行后将监测人类玩家。' if self.preferences['practice'] else '陪练未启用。','status',True)
        practice.append(self.practice_status)
        practice.append(label('堆高稳定达到 14 行：每块额外间隔 1 秒。达到 17 行：每块额外间隔 2 秒，持续出块。','muted',True))
        practice.append(label('降至 10 行及以下并稳定 2 秒后，自动恢复所选速度。陪练设置在下次启动时应用。','muted',True))
        practice.append(label('块内移动和落块保持所选速度；Bot 自身达到 17 行时优先自保，恢复正常节奏。','muted',True))
        practice.append(label('消行动画和读取失败不会触发自动暂停，也不会作为堆高变高的依据。','muted',True))
        practice_scroll = Gtk.ScrolledWindow();practice_scroll.set_policy(Gtk.PolicyType.NEVER,Gtk.PolicyType.AUTOMATIC)
        practice_scroll.set_overlay_scrolling(False);practice_scroll.set_child(practice)
        notebook.append_page(practice_scroll,label('陪练模式'))
        advanced = margin(box(spacing=12),16)
        self.add_number(advanced,'beam','搜索宽度 Beam',1,500,10,'越大搜索候选越多。')
        self.add_number(advanced,'depth','搜索深度',1,14,1,'Linux 核心的实际搜索深度上限。')
        self.add_number(advanced,'budget','搜索预算 / ms',1,10000,5,'软预算；高深度仍可能超过此值。')
        row = box(False); row.append(label('每局运行时限 / 分钟'))
        self.life_values = [0,1,3,5,10,30,60]
        self.life = Gtk.DropDown.new_from_strings(['不限','1','3','5','10','30','60'])
        self.life.set_selected(self.life_values.index(self.preferences['lifespan'])); self.life.set_hexpand(True)
        row.append(self.life); advanced.append(row); self.life.connect('notify::selected', self.configuration_changed)
        advanced.append(label('对应原版 Lifespan。达到时限停止本局操作，下一局重新计时。','muted',True))
        advanced.append(Gtk.Separator())
        advanced.append(label('搜索线程：1 · 当前为串行恢复核心','muted'))
        advanced.append(label('运行中可随时调速。搜索与模板设置在下次启动时应用。','warning',True))
        import_row = box(False)
        import_row.append(button('导入原版 config.dll', self.import_original))
        import_row.append(button('恢复默认', self.reset_preferences)); advanced.append(import_row)
        advanced_scroll = Gtk.ScrolledWindow(); advanced_scroll.set_policy(Gtk.PolicyType.NEVER,Gtk.PolicyType.AUTOMATIC)
        advanced_scroll.set_overlay_scrolling(False); advanced_scroll.set_min_content_height(200); advanced_scroll.set_child(advanced)
        notebook.append_page(advanced_scroll,label('搜索与运行'))
        original = margin(box(spacing=12),16)
        inventory = [
            ('已接通','player / next / speed / Hold / TD open / 自动选择 / S4W / TD loop / Beam / Lifespan / 手柄按钮'),
            ('相杀外し · 尚未移植','原版主动接垃圾与反击时机，需要对手攻击、垃圾队列和延迟控制。'),
            ('PC stack · 暂不可用','原版要求 next = 12；当前适配只有 5 个可见 Next。'),
            ('thread · 固定为 1','原版 1–2 线程；Linux 恢复核心尚未移植双线程调度。'),
            ('原版隐藏 / 无效选项','DPC 勾选后立即取消；PlayStyle 固定返回 VS；MinDepth 返回 10；TakeOverTime 返回 5。'),
            ('原版内部模拟','独立 Tetriz 模拟器未移植；按你的要求，此面板不显示实时棋盘预览。')]
        for title,text in inventory:
            original.append(label(title,'title')); original.append(label(text,'muted',True))
        original.append(button('打开完整逆向清单', self.open_inventory))
        scroll = Gtk.ScrolledWindow(); scroll.set_policy(Gtk.PolicyType.NEVER,Gtk.PolicyType.AUTOMATIC); scroll.set_overlay_scrolling(False); scroll.set_child(original)
        notebook.append_page(scroll,label('原版设置清单'))
        actions = box(False,8)
        self.connect_button = button('连接手柄', lambda _: self.start_worker(True))
        self.start_button = button('开始运行', self.start_or_resume,'suggested-action')
        self.pause_button = button('暂停自动',self.pause_worker)
        self.stop_button = button('断开 / 停止',self.stop_worker,'destructive-action')
        for b in (self.connect_button,self.start_button,self.pause_button,self.stop_button): actions.append(b)
        left.append(actions)
        self.worker_status = label('未连接 · 可先调整设置，再开始运行','muted',True); left.append(self.worker_status)
        self.stats = label('已落块  0   ·   搜索深度  —   ·   耗时  —', 'muted')
        left.append(self.stats)
        manual = box(False, 6)
        manual.append(label('手动控制', 'muted'))
        self.manual_buttons = []
        for text,value in [('←','left'),('↓','down'),('↑','up'),('→','right'),('A','a'),('B','b'),('Y','y')]:
            b=button(text,lambda _,v=value:self.send({'action':'button','value':v})); b.set_tooltip_text('暂停自动后使用：'+value)
            self.manual_buttons.append(b);manual.append(b)
        focus_button=button('返回游戏',lambda _:self.send({'action':'focus'}))
        focus_button.set_hexpand(True);manual.append(focus_button)
        left.append(manual)
        footer = margin(box(spacing=6),16); root.append(footer)
        self.log = Gtk.TextView(editable=False, cursor_visible=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        scroll = Gtk.ScrolledWindow(); scroll.set_min_content_height(65); scroll.set_max_content_height(65); scroll.set_child(self.log)
        footer.append(scroll)
        self.append_log('控制面板已就绪。拖动滑条或直接输入速度，运行中即时生效。')
        if self.load_error: self.append_log('配置读取失败，已使用默认值：'+self.load_error)
        self.apply_dependencies();self.update_actions()
        GLib.timeout_add(100,self.poll)
        threading.Thread(target=self.observe_game,daemon=True).start()
        self.window.present()

    def add_number(self,parent,key,title,lo,hi,step,hint):
        row=box(False);text=box(spacing=2);text.set_hexpand(True)
        text.append(label(title));text.append(label(hint,'muted',True));row.append(text)
        spin=Gtk.SpinButton.new_with_range(lo,hi,step);spin.set_value(self.preferences[key]);spin.set_valign(Gtk.Align.CENTER)
        row.append(spin);parent.append(row);self.config_widgets[key]=spin
        spin.connect('value-changed',self.configuration_changed)

    def add_check(self,parent,key,title,hint):
        toggle=Gtk.CheckButton(label=title);toggle.set_active(self.preferences[key]);toggle.set_tooltip_text(hint)
        parent.append(toggle);self.config_widgets[key]=toggle;toggle.connect('toggled',self.configuration_changed)

    def values(self):
        data=self.preferences.copy();data['speed']=self.speed_spin.get_value()
        for key,widget in self.config_widgets.items():
            data[key]=widget.get_active() if isinstance(widget,Gtk.CheckButton) else widget.get_value_as_int()
        if hasattr(self,'life'):data['lifespan']=self.life_values[self.life.get_selected()]
        return settings.validate(data)

    def apply_dependencies(self):
        widgets=self.config_widgets
        if 'td_loop' not in widgets:return
        if 'practice_player' in widgets:
            widgets['practice_player'].set_sensitive(widgets['practice'].get_active() and not self.running())
        can_td=widgets['hold'].get_active() and widgets['visible_next'].get_value_as_int()>=5
        widgets['td'].set_sensitive(can_td and not self.running())
        if not can_td:widgets['td'].set_active(False)
        can_loop=can_td and widgets['td'].get_active()
        widgets['td_loop'].set_sensitive(can_loop and not self.running())
        if not can_loop:widgets['td_loop'].set_active(False)

    def configuration_changed(self,*_):
        widgets=self.config_widgets
        if all(key in widgets for key in ('practice','practice_player','player')) and widgets['practice'].get_active():
            bot=widgets['player'].get_value_as_int()
            if widgets['practice_player'].get_value_as_int()==bot:
                widgets['practice_player'].set_value(1 if bot!=1 else 2)
        self.apply_dependencies()
        if hasattr(self,'practice_status') and not self.running():
            self.practice_status.set_text('陪练已开启 · 开始运行后监测人类玩家。' if widgets['practice'].get_active() else '陪练未启用。')
        if 'player' in self.config_widgets:self.monitor_player=self.config_widgets['player'].get_value_as_int()-1
        self.schedule_save()

    def speed_changed(self,spin):
        value=spin.get_value();self.speed_scale.set_value(value)
        self.speed_text.set_text(f'{value:g}%')
        self.speed_hint.set_text(f'约 {20*value/100:g} 次操作 / 秒 · 按键间隔 {5000/value:.0f} ms')
        self.send({'action':'speed','value':value});self.schedule_save()

    def schedule_save(self):
        if self.save_source:GLib.source_remove(self.save_source)
        self.save_source=GLib.timeout_add(500,self.save_preferences)

    def save_preferences(self):
        self.save_source=None
        try:settings.save(self.values())
        except (OSError,ValueError) as exc:
            if hasattr(self,'log'):self.append_log('保存设置失败：'+str(exc))
        return False

    def open_inventory(self,*_):
        try: Gio.AppInfo.launch_default_for_uri((ROOT/'docs/UI_REVERSE_ENGINEERING.md').as_uri(),None)
        except GLib.Error as exc: self.append_log('无法打开说明文件：'+str(exc))

    def import_original(self,*_):
        if self.running():return
        try:
            data,notes=settings.original_config(ROOT/'Hoiko_PPT_v0-beta1/config.dll')
            self.set_preferences(data)
            for note in notes:self.append_log(note)
            self.append_log('已导入原版有效设置。原文件未修改。')
        except (OSError,ValueError) as exc:self.append_log('导入失败：'+str(exc))

    def reset_preferences(self,*_):
        if not self.running():self.set_preferences(settings.DEFAULTS)

    def set_preferences(self,data):
        self.preferences=data.copy()
        for key,widget in self.config_widgets.items():
            if isinstance(widget,Gtk.CheckButton):widget.set_active(data[key])
            else:widget.set_value(data[key])
        self.life.set_selected(self.life_values.index(data['lifespan']))
        self.speed_spin.set_value(data['speed']);self.apply_dependencies();self.schedule_save()

    def running(self):return self.process is not None and self.process.poll() is None

    def start_worker(self,paused=False):
        if self.running():return
        try:
            config=self.values();settings.save(config)
            command=[sys.executable,'-u','-m','hoiko_linux.cli']+settings.run_arguments(config)
            if paused:command.append('--paused')
            self.process=subprocess.Popen(command,cwd=ROOT,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                          stderr=subprocess.STDOUT,text=True,bufsize=1,start_new_session=True)
            self.paused=paused;self.stopping=False;self.turns=0;self.started=time.monotonic()
            self.worker_status.set_text('手柄已连接，自动操作暂停' if paused else '正在连接游戏…')
            self.practice_status.set_text('等待读取陪练对象的棋盘…' if config['practice'] else '陪练未启用。')
            def reader(process):
                for line in process.stdout:self.events.put((process,line.rstrip()))
                process.stdout.close()
            threading.Thread(target=reader,args=(self.process,),daemon=True).start()
            self.update_actions()
            if not paused:GLib.timeout_add(1000,lambda:(self.send({'action':'focus'}),False)[1])
        except (OSError,ValueError) as exc:self.append_log('启动失败：'+str(exc))

    def start_or_resume(self,*_):
        if not self.running():self.start_worker(False)
        else:
            self.paused=False;self.send({'action':'resume'});self.send({'action':'focus'})
            self.worker_status.set_text('自动操作已恢复');self.update_actions()

    def pause_worker(self,*_):
        self.paused=True;self.send({'action':'pause'});self.update_actions()
        self.worker_status.set_text('暂停请求已发送 · 当前方块操作完成后暂停')

    def send(self,data):
        if not self.running():return
        try:self.process.stdin.write(json.dumps(data)+'\n');self.process.stdin.flush()
        except (BrokenPipeError,OSError,ValueError):pass

    def stop_worker(self,*_):
        if not self.running():return
        process=self.process;self.stopping=True;self.send({'action':'stop'});self.update_actions()
        self.worker_status.set_text('正在释放按键并断开手柄…')
        def interrupt():
            if process.poll() is None:process.send_signal(signal.SIGINT)
            return False
        GLib.timeout_add(1500,interrupt)

    def update_actions(self):
        active=self.running()
        self.connect_button.set_sensitive(not active)
        self.start_button.set_label('继续运行' if active else '开始运行')
        self.start_button.set_sensitive((not active or self.paused) and not self.stopping)
        self.pause_button.set_sensitive(active and not self.paused and not self.stopping);self.stop_button.set_sensitive(active and not self.stopping)
        for widget in self.config_widgets.values():widget.set_sensitive(not active)
        self.life.set_sensitive(not active)
        for widget in self.manual_buttons:widget.set_sensitive(active and self.paused and not self.stopping)
        self.apply_dependencies()

    def append_log(self,text):
        buffer=self.log.get_buffer();buffer.insert(buffer.get_end_iter(),time.strftime('%H:%M:%S  ')+text+'\n')
        if buffer.get_line_count()>220:
            start=buffer.get_start_iter();ok,end=buffer.get_iter_at_line(40)
            if ok:buffer.delete(start,end)
        self.log.scroll_to_iter(buffer.get_end_iter(),0,False,0,1)

    def poll(self):
        for _ in range(150):
            try:process,line=self.events.get_nowait()
            except queue.Empty:break
            if process is not self.process:continue
            try:data=json.loads(line)
            except ValueError:data=None
            if isinstance(data,dict) and 'turn' in data:
                self.turns=data['turn'];self.stats.set_text(f"已落块  {self.turns}   ·   深度  {data['depth']}   ·   耗时  {data['ms']} ms   ·   节点  {data['nodes']:,}")
            elif isinstance(data,dict) and data.get('event')=='state':
                if data.get('status'):self.worker_status.set_text(data['status'])
                if data.get('practice'):
                    practice=data['practice']
                    self.practice_status.set_text(f"{practice['status']} · 块内操作速度 {practice['effective_speed']:g}%")
            elif line:self.append_log(line)
        try:
            info=self.observations.get_nowait()
            self.connection.set_text(info['status'])
        except queue.Empty:pass
        if self.process is not None and self.process.poll() is not None:
            code=self.process.returncode
            if self.process.stdin:self.process.stdin.close()
            self.process=None;self.paused=False;self.stopping=False;self.update_actions()
            self.practice_status.set_text('已停止 · 陪练保护未运行。')
            self.worker_status.set_text('已停止 · 手柄已断开' if code==0 else f'进程已退出（{code}），请查看日志')
        if self.closing and not self.running():self.observer_stop.set();self.quit();return False
        return True

    def observe_game(self):
        from .cli import attach
        game=None
        while not self.observer_stop.is_set():
            try:
                if game is None:game=attach(SimpleNamespace(pid=None,game=2,delta=None))
                game.frames()
                info={'status':'● 游戏已连接'}
            except (OSError,ValueError,RuntimeError) as exc:
                game=None;info={'status':'○ 等待 PPT2' if 'not running' in str(exc) else '○ 无法读取游戏'}
            try:self.observations.put_nowait(info)
            except queue.Full:pass
            self.observer_stop.wait(.2 if game else 2)

    def close_requested(self,*_):
        self.save_preferences();self.closing=True;self.stop_worker()
        if not self.running():self.observer_stop.set();self.quit()
        return True


def launch():
    Panel().run([])
