#!/usr/bin/env python3
"""Native GUI smoke check. Opens a temporary panel; never creates a controller."""
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
with tempfile.TemporaryDirectory(prefix='hoiko-panel-check-') as config:
    os.environ['XDG_CONFIG_HOME'] = config
    from hoiko_linux.gui import Panel, GLib, Gtk
    from hoiko_linux import settings
    app = Panel()
    app.set_application_id('io.hoiko.PanelSmokeCheck')
    failures = []
    output = ROOT / 'build/gui-check'
    output.mkdir(parents=True, exist_ok=True)

    def check():
        try:
            assert app.poll() is True, 'Panel polling must continue after the first update'
            app.speed_scale.set_value(37)
            assert app.values()['speed'] == 37
            app.speed_spin.set_text('23')
            app.speed_spin.update()
            assert app.values()['speed'] == 23
            app.speed_spin.set_value(10)
            app.config_widgets['td'].set_active(True)
            app.config_widgets['td_loop'].set_active(True)
            assert app.values()['td_loop']
            app.config_widgets['hold'].set_active(False)
            assert not app.values()['td'] and not app.values()['td_loop']
            app.config_widgets['hold'].set_active(True)
            assert not app.values()['practice']
            assert not app.config_widgets['practice_player'].get_sensitive()
            app.config_widgets['practice'].set_active(True)
            assert app.values()['practice'] and app.config_widgets['practice_player'].get_sensitive()
            assert '--practice' in settings.run_arguments(app.values())
            app.config_widgets['player'].set_value(1)
            assert app.values()['practice_player'] != app.values()['player']
            app.config_widgets['player'].set_value(2)
            app.config_widgets['practice_player'].set_value(1)
            app.events.put((app.process,'{"event":"state","status":"陪练减速练习","practice":{"status":"1P 堆高 17 行 · 每块额外间隔 2 秒","effective_speed":10,"piece_delay":2}}'))
            assert app.poll() is True
            assert '块内操作速度 10%' in app.practice_status.get_text()
            assert '每块额外间隔 2 秒' in app.practice_status.get_text()
            GLib.timeout_add(300, capture, 0)
        except Exception as exc:
            failures.append(repr(exc)); app.close_requested()
        return False

    def capture(page):
        try:
            paint = Gtk.WidgetPaintable.new(app.window)
            snapshot = Gtk.Snapshot()
            paint.snapshot(snapshot, app.window.get_width(), app.window.get_height())
            node = snapshot.to_node()
            texture = app.window.get_renderer().render_texture(node, None)
            texture.save_to_png(str(output / f'panel-{page}.png'))
            if page < app.notebook.get_n_pages()-1:
                app.notebook.set_current_page(page+1)
                GLib.timeout_add(300, capture, page+1)
            else:
                print('GUI smoke check passed: slider, keyboard input, dependencies, practice controls/status, all tabs.',flush=True)
                app.close_requested()
        except Exception as exc:
            failures.append(repr(exc)); app.close_requested()
        return False

    GLib.timeout_add(1000, check)
    app.run([])
    if failures: raise RuntimeError(failures)
