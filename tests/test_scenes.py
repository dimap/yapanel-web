import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


def panel():
    p = server.Panel(Mock())
    p.written = None
    p._alarm_sounds_ready = lambda needed: setattr(p, "needed", needed)
    p._alarm_write = lambda conf, lines, extra="": setattr(p, "written", (conf, lines))
    p.scenes = lambda: {"scenes": [], "running": []}
    return p


class ScenesSave(unittest.TestCase):
    """Сценарии уходят на колонку данными: вид ноды, аргументы и связи, без символов, которые сломали бы конфиг."""

    def save(self, nodes, **scene):
        p = panel()
        p.scenes_save({"scenes": [{"id": "ab1", "name": "Ночь", "time": "23:00", "days": "12345", "start": "1", "nodes": nodes, **scene}]})
        return p

    def test_lines(self):
        p = self.save([{"id": "1", "kind": "vol", "level": 2, "ramp": 60, "next": "2", "x": 10, "y": 20},
                       {"id": "2", "kind": "query", "text": 'включи | "режим"\nне беспокоить', "next": "3"},
                       {"id": "3", "kind": "if", "check": "time", "arg": "22:00-07:00", "next": "4", "else": "99"},
                       {"id": "4", "kind": "ring", "color": "#6C47FF", "secs": 9999},
                       {"id": "5", "kind": "note", "text": "заметка", "next": "1"}])
        conf, lines = p.written
        self.assertEqual(conf, server.SCENES_CONF)
        self.assertEqual(lines, ["S|ab1|1|time|23:00|12345|1|Ночь", "N|ab1|1|vol|2|60|2||10|20",
                                 "N|ab1|2|query|включи    режим  не беспокоить||3||0|0",
                                 "N|ab1|3|if|time|22:00-07:00|4||0|0", "N|ab1|4|ring|6c47ff|600|||0|0",
                                 "N|ab1|5|note|заметка||||0|0"])

    def test_field_is_endless_both_ways(self):
        p = self.save([{"id": "1", "kind": "vol", "level": 2, "x": -340, "y": -99999}])
        self.assertEqual(p.written[1][1], "N|ab1|1|vol|2|0|||-340|-20000")

    def test_anim_and_repeat_cap(self):
        p = panel()
        p.patterns = lambda: {"ring": ["timer.led"], "clock": ["blink.binscled.zst"]}
        p.clock_frames = lambda name: [{"flag": 0, "v": [255] * len(server.CLOCK_LEDS), "ms": 100}]
        p.scenes_save({"scenes": [{"id": "ab1", "name": "А", "time": "23:00", "start": "1", "nodes": [
            {"id": "1", "kind": "anim", "anim": "ring:timer.led", "secs": 5, "next": "2"},
            {"id": "2", "kind": "anim", "anim": "clock:blink.binscled.zst", "secs": 3, "next": "3"},
            {"id": "3", "kind": "repeat", "count": 500}]}]})
        self.assertEqual(p.written[1][1:], ["N|ab1|1|anim|ring:timer.led|5|2||0|0", "N|ab1|2|anim|clock:blink.binscled.zst|3|3||0|0",
                                            "N|ab1|3|repeat|20||||0|0"])
        p.adb.push.assert_called_once()   # готовый скрипт для анимации часов
        self.assertTrue(str(p.adb.push.call_args.args[1]).endswith("/anim_blink.binscled.zst.sh"))
        for bad in ("ring:../../etc/passwd", "clock:timer.led", "x:timer.led"):
            with self.assertRaises(server.AdbError, msg=bad):
                p.scenes_save({"scenes": [{"id": "ab1", "name": "А", "time": "23:00", "start": "1", "nodes": [{"id": "1", "kind": "anim", "anim": bad}]}]})

    def test_voice_trigger_and_assistant_node(self):
        p = self.save([{"id": "1", "kind": "assistant", "mode": True, "next": "2"}, {"id": "2", "kind": "assistant", "mode": False}],
                      trigger="voice", time="", phrases=["Пора спать", "спать, сейчас", "a|b", " ", ""])
        self.assertEqual(p.written[1][:3], ["S|ab1|1|voice|Пора спать,спать  сейчас,a b|12345|1|Ночь",
                                            "N|ab1|1|assistant|on||2||0|0", "N|ab1|2|assistant|off||||0|0"])
        with self.assertRaises(server.AdbError):
            self.save([{"id": "1", "kind": "stop"}], trigger="voice", phrases=[" , "])
        q = server.Panel(Mock())
        q.adb.shell.return_value = ("S|ab1|1|voice|пора спать,спать|12345|1|Ночь\nN|ab1|1|assistant|on||||0|0\n<<<\n<<<\n<<<\n", "", 0)
        s = q.scenes()["scenes"][0]
        self.assertEqual((s["trigger"], s["time"], s["phrases"], s["nodes"][0]["mode"]), ("voice", "", ["пора спать", "спать"], True))

    def test_manual_trigger(self):
        p = self.save([{"id": "1", "kind": "stop"}], trigger="manual", time="12:00")
        self.assertEqual(p.written[1][0], "S|ab1|1|manual|||1|Ночь")

    def test_anim_my(self):
        p = panel()
        p.patterns = lambda: {"ring": ["my/Закат.led"], "clock": ["my/c1.binscled"]}
        p.clock_frames = lambda name: [{"flag": 0, "v": [255] * len(server.CLOCK_LEDS), "ms": 100}]
        p.scenes_save({"scenes": [{"id": "ab1", "name": "А", "time": "23:00", "start": "1", "nodes": [
            {"id": "1", "kind": "anim", "anim": "ring:my/Закат.led", "secs": 5, "next": "2"},
            {"id": "2", "kind": "anim", "anim": "clock:my/c1.binscled", "secs": 3}]}]})
        self.assertEqual(p.written[1][1], "N|ab1|1|anim|ring:my/Закат.led|5|2||0|0")
        self.assertTrue(str(p.adb.push.call_args.args[1]).endswith("/anim_my~c1.binscled.sh"))

    def test_boot_trigger_has_no_time(self):
        p = self.save([{"id": "1", "kind": "stop"}], trigger="boot", time="")
        self.assertEqual(p.written[1][0], "S|ab1|1|boot||12345|1|Ночь")

    def test_rejects(self):
        for nodes, kw in (([{"id": "1", "kind": "rm"}], {}), ([{"id": "1", "kind": "query", "text": " "}], {}),
                          ([{"id": "1", "kind": "ring", "color": "red"}], {}), ([{"id": "1", "kind": "stop"}], {"start": ""}),
                          ([{"id": "1", "kind": "if", "check": "rm -rf"}], {}), ([{"id": "1", "kind": "scene", "target": "ab1"}], {}),
                          ([{"id": "1", "kind": "stop"}, {"id": "1", "kind": "stop"}], {}), ([{"id": "1", "kind": "stop"}], {"time": "25:00"})):
            with self.assertRaises(server.AdbError, msg=(nodes, kw)):
                self.save(nodes, **kw)

    def test_parse_back(self):
        p = server.Panel(Mock())
        p.adb.shell.return_value = ("S|ab1|0|boot|||1|Утро\nN|ab1|1|vol|3|0|2||5|6\nN|ab1|2|if|chance|30|1||0|0\nN|zz|1|stop||||0|0\n"
                                    "<<<\nab1\n<<<\n28.10 начат\n<<<\nrun\n", "", 0)
        r = p.scenes()
        self.assertEqual((r["running"], r["daemon"], r["log"]), (["ab1"], True, ["28.10 начат"]))
        self.assertEqual(r["scenes"][0]["nodes"], [
            {"id": "1", "kind": "vol", "next": "2", "else": "", "x": 5, "y": 6, "level": 3, "ramp": 0},
            {"id": "2", "kind": "if", "next": "1", "else": "", "x": 0, "y": 0, "check": "chance", "arg": "30"}])

    def test_daemon_runs_nodes_as_data(self):
        script = server.alarm_script().decode()
        self.assertIn('if node "$kind" "$a" "$b" "$id" "$pc" < /dev/null; then pc=$nx; else pc=$el; fi', script)
        code = "\n".join(l for l in script.splitlines() if not l.lstrip().startswith("#"))
        self.assertNotIn("eval", code)


class SceneInterpreter(unittest.TestCase):
    """Демон проходит граф: повтор крутит тело нужное число раз, условие выбирает выход."""

    def run_graph(self, conf):
        import shutil, subprocess, tempfile, shlex
        sh = shutil.which("sh")
        if not sh:
            self.skipTest("нет sh")
        script = server.alarm_script().decode()
        part = script[script.index("# ---------- сценарии ----------"):script.index("# Строка конфига → fire")]
        d = Path(tempfile.mkdtemp())
        (d / "scenes.conf").write_bytes(conf.encode("utf-8"))
        stubs = (f'D={shlex.quote(d.as_posix())}; SCENES=$D/scenes.conf; SRUN=$D/run.; SLOG=$D/log; T=$D/trace\n'
                 'alice() { echo "alice $*" >> $T; }; aplay() { echo "aplay $*" >> $T; }; sleep() { :; }\n'
                 'playing() { return 1; }; online() { return 0; }; get_alsa() { echo 100; }; set_alsa() { :; }\n'
                 'alsa_of() { echo 50; }; ring() { :; }; ring_release() { n=blue:ring3; t=x; }; od() { echo 50; }\n')
        subprocess.run([sh, "-c", stubs + part + '\nrun_scene_worker s1'], check=True, timeout=20)
        return (d / "trace").read_text().splitlines() if (d / "trace").exists() else []

    def test_repeat_and_if(self):
        conf = ("S|s1|1|time|23:00||1|Тест\n"
                "N|s1|1|say|привет||2||0|0\n"
                "N|s1|2|repeat|2||3|4|0|0\n"
                "N|s1|3|query|круг||2||0|0\n"
                "N|s1|4|if|chance|100|5|6|0|0\n"
                "N|s1|5|query|да||||0|0\n"
                "N|s1|6|query|нет||||0|0\n"
                "N|s1|7|note|заметка||||0|0\n")
        self.assertEqual(self.run_graph(conf), ["alice Повтори за мной: привет", "alice круг", "alice круг", "alice да"])

    def test_endless_loop_stops(self):
        trace = self.run_graph("S|s1|1|time|23:00||1|Цикл\nN|s1|1|query|x||1||0|0\n")
        self.assertEqual(len(trace), 20)   # каждая нода — не больше 20 раз за запуск

    def test_repeat_twenty_rounds_is_allowed(self):
        conf = "S|s1|1|time|23:00||1|Т\nN|s1|1|repeat|20||2|3|0|0\nN|s1|2|query|круг||1||0|0\nN|s1|3|query|всё||||0|0\n"
        trace = self.run_graph(conf)
        self.assertEqual((trace.count("alice круг"), trace[-1]), (20, "alice всё"))

    def test_else_branch(self):
        conf = "S|s1|1|time|23:00||1|Т\nN|s1|1|if|chance|0|2|3|0|0\nN|s1|2|query|да||||0|0\nN|s1|3|query|нет||||0|0\n"
        self.assertEqual(self.run_graph(conf), ["alice нет"])

    def test_helpers_do_not_break_the_loop(self):
        # ring_release перебирает светодиоды в n — интерпретатор не должен от этого сбиться
        conf = "S|s1|1|time|23:00||1|Т\nN|s1|1|ring|ff0000|1|2||0|0\nN|s1|2|query|после||||0|0\n"
        self.assertEqual(self.run_graph(conf), ["alice после"])


class SceneProcessRegression(unittest.TestCase):
    """Статус и остановка на временном /proc, без сигналов настоящим процессам."""

    def setUp(self):
        import shutil, subprocess, tempfile
        from unittest.mock import patch
        shell = shutil.which('sh')
        if not shell:
            self.skipTest('Нужен sh')
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.d = Path(tmp.name)
        self.prefix = (self.d / 'run.').as_posix()
        proc = self.d / 'proc' / '123'
        proc.mkdir(parents=True)
        self.stat = proc / 'stat'
        self.stat.write_text('123 (worker task) S 1 123 123 ' + '0 ' * 15 + '777')
        Path(self.prefix + 'c1').write_text('123')
        Path(self.prefix + 'c1.start').write_text('777')
        Path(self.prefix + 'c1.v1').write_text('1')
        conf = self.d / 'conf'
        conf.write_bytes(b'S|c1|1|manual|||1|Test\n')
        changes = dict(SCENE_RUN=self.prefix, SCENES_CONF=conf.as_posix(),
                       SCENES_LOG=(self.d/'log').as_posix(), ALARM_SD=(self.d/'daemon').as_posix(),
                       SCENE_PID_SH=server.SCENE_PID_SH.replace(server.SCENE_RUN, self.prefix))
        patches = patch.multiple(server, **changes)
        patches.start(); self.addCleanup(patches.stop)
        d = self.d
        class Adb:
            def shell(self, command, **kwargs):
                command = command.replace('/proc/', (d/'proc').as_posix() + '/')
                stub = 'kill() { if [ "$1" = -0 ]; then [ "$2" = 123 ]; else echo "$*"; fi; }; '
                r = subprocess.run([shell,'-c',stub+command],capture_output=True,text=True,timeout=5)
                self.stdout = r.stdout
                return r.stdout,r.stderr,r.returncode
        self.adb = Adb()
        self.p = server.Panel(self.adb)

    def test_c_id_visible_without_service_files(self):
        self.assertEqual(self.p.scenes()['running'], ['c1'])

    def test_stale_or_zombie_pid_is_not_running_or_signalled(self):
        self.p.ring_reset = lambda: None
        for row in ['123 (worker task) S 1 123 123 ' + '0 '*15 + '778',
                    '123 (worker task) Z 1 123 123 ' + '0 '*15 + '777']:
            self.stat.write_text(row)
            self.assertEqual(self.p.scenes()['running'], [])
            with unittest.mock.patch.object(self.p, 'scenes', return_value={}):
                self.p.scene_stop('c1')
            self.assertNotIn('-TERM', self.adb.stdout)

    def test_stop_targets_own_group(self):
        self.p.ring_reset = lambda: None
        self.p.scenes = lambda: {}
        self.p.scene_stop('c1')
        self.assertIn('-TERM -123', self.adb.stdout)
