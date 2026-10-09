import io
import tempfile
import unittest
from pathlib import Path

from hoiko_linux.runtime import RunControl, InputPacer, speed_percent
from hoiko_linux import settings


class Clock:
    def __init__(self): self.now=0
    def time(self): return self.now
    def sleep(self,seconds): self.now+=seconds


class RuntimeTests(unittest.TestCase):
    def test_exact_five_and_ten_percent_cadences(self):
        for speed,expected in [(5,1),(10,.5),(100,.05)]:
            clock=Clock();pacer=InputPacer(RunControl(speed),clock.time,clock.sleep)
            pacer.wait(lambda:None);pacer.wait(lambda:None)
            self.assertAlmostEqual(clock.now,expected)

    def test_speed_change_takes_effect_during_wait(self):
        clock=Clock();control=RunControl(5);pacer=InputPacer(control,clock.time,clock.sleep)
        pacer.wait(lambda:None)
        def active():
            if clock.now>=.1:control.apply({'action':'speed','value':100})
        pacer.wait(active)
        self.assertLess(clock.now,.12)

    def test_stop_interrupts_long_delay(self):
        clock=Clock();control=RunControl(1);pacer=InputPacer(control,clock.time,clock.sleep)
        pacer.wait(lambda:None);control.stopped=True
        with self.assertRaises(KeyboardInterrupt):pacer.wait(lambda:None)
        self.assertEqual(clock.now,0)

    def test_invalid_speed_never_applied(self):
        control=RunControl(10)
        for value in (0,-1,101,float('nan'),float('inf')):
            with self.assertRaises(ValueError):control.apply({'action':'speed','value':value})
        self.assertEqual(control.speed,10)

    def test_eof_stops_worker_and_malformed_commands_do_not_crash(self):
        control=RunControl();control.read(io.StringIO('bad\n{"action":"speed","value":5}\n'))
        self.assertTrue(control.stopped);self.assertEqual(control.speed,5);self.assertIsNotNone(control.error)

    def test_preferences_roundtrip_and_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'settings.json'
            values=dict(settings.DEFAULTS,speed=5,hold=False,td=True,td_loop=True)
            settings.save(values,path);loaded=settings.load(path)
            self.assertEqual(loaded['speed'],5)
            self.assertFalse(loaded['td']);self.assertFalse(loaded['td_loop'])
            args=settings.run_arguments(loaded)
            self.assertEqual(args[args.index('--speed')+1],'5')
            self.assertIn('--no-hold',args)

    def test_original_config_player_and_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'config.dll'
            path.write_text('player,1\nviewNext,12\nspeed,25\nlifespan,1440\nuseOffsetOff,true')
            data,notes=settings.original_config(path)
            self.assertEqual(data['player'],2);self.assertEqual(data['visible_next'],5)
            self.assertEqual(data['lifespan'],0);self.assertTrue(notes)

    def test_partial_original_config_preserves_default_player(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'config.dll';path.write_text('speed,25\n')
            data,_=settings.original_config(path)
            self.assertEqual(data['player'],settings.DEFAULTS['player'])

    def test_settings_shape_and_integer_arguments(self):
        for value in ([],None,'settings'):
            with self.assertRaises(ValueError):settings.validate(value)
        data=settings.validate(dict(player=2.0,beam=40.0,depth=4.0))
        args=settings.run_arguments(data)
        self.assertEqual(args[args.index('--player')+1],'2')
        self.assertEqual(args[args.index('--beam')+1],'40')

class ControllerPacingTests(unittest.TestCase):
    def test_slow_inputs_are_released_between_actions(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from hoiko_linux.controller import execute
        clock=Clock();control=RunControl(5)
        class Memory:
            def int32(self, address):return 0
            def byte(self, address):return 1
        class Game:
            game=2;memory=Memory();x=4
            def match_pointer(self):return 0x20000
            def match_frames(self):return int(clock.now*60)
            def offline_status(self, player):return {'allowed':True}
            def player(self, player):return 0x10000
            def position(self, player):return dict(x=self.x,y=20,rotation=0,locked=0,piece=0)
            def snapshot(self, player):
                return dict(board=[0]*32,pieces=[0,1,2],hold=7,next_count=1,position=self.position(player))
        game=Game()
        class Pad:
            held=False
            def __init__(self):self.starts=[];self.releases=[]
            def focused(self):return True
            def press(self, button):
                self.held=True;self.starts.append(clock.now)
                game.x += 1 if button=='right' else -1
            def release(self):
                if self.held:self.releases.append(clock.now)
                self.held=False
        pad=Pad();pacer=InputPacer(control,clock.time,clock.sleep)
        with patch('hoiko_linux.controller.time.monotonic',clock.time), patch('hoiko_linux.controller.time.sleep',clock.sleep):
            execute(game,pad,1,SimpleNamespace(commands=b'rr',piece=0),game.snapshot(1),pacer=pacer,control=control)
        self.assertEqual(len(pad.starts),2)
        self.assertAlmostEqual(pad.starts[1]-pad.starts[0],1)
        self.assertLess(max(b-a for a,b in zip(pad.starts,pad.releases)),.04)
        self.assertFalse(pad.held)
