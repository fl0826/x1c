"""核心逻辑的回归测试（不需要网络、不碰打印机）。

运行:
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import unittest
import zipfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_ROOT), str(_ROOT / "vendor")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bambu_bridge import (  # noqa: E402
    ALLOWED_COMMANDS,
    PrinterConfig,
    PrinterState,
    X1CBridge,
    build_print_payload,
    fmt_temp,
)
from upload_and_print import is_sliced_3mf  # noqa: E402


def _tmp_dir(test_id: str) -> Path:
    """给每个测试一个独立的固定目录。

    不用 tempfile.mkdtemp：它创建的目录在受限沙箱里可能不可写。
    """
    name = test_id.split(".")[-1]
    path = _ROOT / ".test-tmp" / name
    path.mkdir(parents=True, exist_ok=True)
    return path


class TestFmtTemp(unittest.TestCase):
    """修过一个显示 bug：目标温度为空时会打出 '60.0°C / None°C'。"""

    def test_no_target_shows_only_actual(self):
        self.assertEqual(fmt_temp(60.0, None), "60.0°C")

    def test_with_target_shows_both(self):
        self.assertEqual(fmt_temp(140.0, 140.0), "140.0°C / 140.0°C")

    def test_missing_actual_shows_dash(self):
        self.assertEqual(fmt_temp(None, 140.0), "—")

    def test_int_is_formatted_as_float(self):
        self.assertEqual(fmt_temp(34, None), "34.0°C")


class TestPrinterState(unittest.TestCase):
    def _state(self, print_info: dict) -> PrinterState:
        return PrinterState(raw={"print": print_info})

    def test_summary_uses_real_field_names(self):
        """字段名是 *_temper，不是 *_temperature —— 写错就永远读不到温度。"""
        state = self._state(
            {
                "gcode_state": "RUNNING",
                "mc_percent": 9,
                "mc_remaining_time": 55,
                "layer_num": 3,
                "total_layer_num": 155,
                "nozzle_temper": 140.0,
                "nozzle_target_temper": 140.0,
                "bed_temper": 55.0,
                "bed_target_temper": 55.0,
                "chamber_temper": 34.0,
                "subtask_name": "model.gcode.3mf",
            }
        )
        self.assertEqual(state.gcode_state, "RUNNING")
        self.assertEqual(state.progress_percent, 9)
        self.assertEqual(state.nozzle_temp, 140.0)
        self.assertEqual(state.bed_target, 55.0)
        self.assertEqual(state.chamber_temp, 34.0)
        self.assertEqual(state.current_file, "model.gcode.3mf")
        self.assertEqual(state.remaining_minutes, 55)

    def test_idle_state_without_targets(self):
        """空闲时打印机不报目标温度，不能因此显示出 None。"""
        state = self._state({"gcode_state": "FINISH", "nozzle_temper": 30.0})
        self.assertEqual(fmt_temp(state.nozzle_temp, state.nozzle_target), "30.0°C")

    def test_empty_state_is_tolerated(self):
        state = self._state({})
        self.assertIsNone(state.gcode_state)
        self.assertIsNone(state.nozzle_temp)
        self.assertEqual(state.hms_errors, [])


class TestPrintPayload(unittest.TestCase):
    """自动打印的报文格式：这几点都是实机踩出来的，写错就静默失效。"""

    def test_outer_key_is_print_object_not_dotted(self):
        """曾经写成 {"print.project_file": {...}}，打印机当未知字段直接丢弃。"""
        payload = build_print_payload("a.3mf")
        self.assertEqual(list(payload), ["print"])
        self.assertNotIn("print.project_file", payload)

    def test_command_and_param(self):
        params = build_print_payload("a.gcode.3mf")["print"]
        self.assertEqual(params["command"], "project_file")
        self.assertEqual(params["param"], "Metadata/plate_1.gcode")
        self.assertEqual(params["subtask_name"], "a.gcode.3mf")

    def test_url_points_at_sdcard_root(self):
        """file:///sdcard/ 是验证过的写法；ftp://<IP>/<名> 会被拒绝。"""
        url = build_print_payload("a.3mf")["print"]["url"]
        self.assertEqual(url, "file:///sdcard/a.3mf")

    def test_calibration_flags_default_on(self):
        """默认要和屏幕手动打印一致：调平 + 挤出补偿 + 震动补偿都做。

        实测：关掉这些会让自动打印少掉"校准挤出补偿"这一步，
        和手动打印的行为不一致，排查问题时会造成误导。
        """
        params = build_print_payload("a.3mf")["print"]
        self.assertTrue(params["bed_leveling"])
        self.assertTrue(params["flow_cali"])
        self.assertTrue(params["vibration_cali"])

    def test_calibration_can_be_skipped(self):
        params = build_print_payload(
            "a.3mf", flow_cali=False, vibration_cali=False
        )["print"]
        self.assertFalse(params["flow_cali"])
        self.assertFalse(params["vibration_cali"])

    def test_external_spool_by_default(self):
        params = build_print_payload("a.3mf")["print"]
        self.assertFalse(params["use_ams"])
        self.assertEqual(params["ams_mapping"], [])

    def test_ams_slot_is_passed_through(self):
        """料在 AMS 里时必须 use_ams=true + ams_mapping=[槽号]。

        实测：料在 AMS 槽 0，却发 use_ams=false（外挂料架），
        打印机会在启动后立刻失败，屏幕报"检测到禁用AMS时仍从AMS加载料"。
        """
        params = build_print_payload("a.3mf", use_ams=True, ams_mapping=[0])["print"]
        self.assertTrue(params["use_ams"])
        self.assertEqual(params["ams_mapping"], [0])

    def test_ams_other_slot(self):
        params = build_print_payload("a.3mf", use_ams=True, ams_mapping=[3])["print"]
        self.assertEqual(params["ams_mapping"], [3])

    def test_plate_selects_gcode_path(self):
        params = build_print_payload("a.3mf", plate=2)["print"]
        self.assertEqual(params["param"], "Metadata/plate_2.gcode")

    def test_outer_key_is_whitelisted(self):
        self.assertIn("print", ALLOWED_COMMANDS)


class TestStartPrintPublishes(unittest.TestCase):
    """确认下发路径真的把报文发到了 request 主题（用假客户端捕获）。"""

    def _bridge(self, allow: bool) -> tuple[X1CBridge, list]:
        cfg = PrinterConfig(ip="127.0.0.1", serial="01P00A000000001",
                            access_code="12345678")
        bridge = X1CBridge(cfg, allow_real_actions=allow, log_dir=None)
        bridge.connected.set()
        captured: list = []

        class FakeInfo:
            rc = 0

            def wait_for_publish(self, timeout=None):
                return None

        def fake_publish(topic, payload, qos=0):
            captured.append((topic, json.loads(payload)))
            return FakeInfo()

        bridge._client.publish = fake_publish  # type: ignore[assignment]
        return bridge, captured

    def test_publishes_to_request_topic(self):
        bridge, captured = self._bridge(allow=True)
        bridge.send_command(build_print_payload("fengli.3mf"))
        self.assertEqual(len(captured), 1)
        topic, payload = captured[0]
        self.assertEqual(topic, "device/01P00A000000001/request")
        self.assertEqual(payload["print"]["command"], "project_file")
        self.assertEqual(payload["print"]["url"], "file:///sdcard/fengli.3mf")

    def test_dry_run_sends_nothing(self):
        bridge, captured = self._bridge(allow=False)
        bridge.send_command(build_print_payload("fengli.3mf"))
        self.assertEqual(captured, [], "预演模式绝不能真的发报文")

    def test_unknown_command_is_rejected(self):
        bridge, _ = self._bridge(allow=True)
        with self.assertRaises(ValueError):
            bridge.send_command({"system": {"command": "ledctrl"}})

    def test_payload_must_have_single_outer_key(self):
        bridge, _ = self._bridge(allow=True)
        with self.assertRaises(ValueError):
            bridge.send_command({"print": {}, "system": {}})


class TestIsSliced3mf(unittest.TestCase):
    """只看后缀不够：工程文件也是 .3mf，但里面没有 gcode。"""

    def setUp(self) -> None:
        # 不用 tempfile.mkdtemp：它建的目录在某类沙箱里不可写。
        # 固定子目录既稳定又方便失败后翻查产物。
        self.tmp = _tmp_dir(self.id())

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make(self, name: str, entries: dict[str, str]) -> Path:
        path = self.tmp / name
        with zipfile.ZipFile(path, "w") as zf:
            for entry, content in entries.items():
                zf.writestr(entry, content)
        return path

    def test_sliced_file_is_accepted(self):
        path = self._make(
            "sliced.gcode.3mf",
            {"Metadata/plate_1.gcode": "; gcode\n", "3D/3dmodel.model": "<x/>"},
        )
        ok, reason = is_sliced_3mf(path)
        self.assertTrue(ok, reason)
        self.assertIn("plate_1.gcode", reason)

    def test_project_file_is_rejected(self):
        path = self._make("project.3mf", {"3D/3dmodel.model": "<x/>"})
        ok, reason = is_sliced_3mf(path)
        self.assertFalse(ok)
        self.assertIn("没有 gcode", reason)

    def test_corrupt_zip_is_rejected(self):
        path = self.tmp / "broken.3mf"
        path.write_bytes(b"not a zip at all")
        ok, reason = is_sliced_3mf(path)
        self.assertFalse(ok)
        self.assertIn("打不开", reason)

    def test_other_suffix_is_rejected(self):
        path = self.tmp / "model.stl"
        path.write_bytes(b"solid x")
        ok, reason = is_sliced_3mf(path)
        self.assertFalse(ok)
        self.assertIn("不是 .3mf", reason)


def _has_convert_deps() -> bool:
    try:
        import trimesh  # noqa: F401
        from pxr import Usd, UsdGeom  # noqa: F401
    except ImportError:
        return False
    return True


@unittest.skipUnless(_has_convert_deps(), "需要 trimesh + usd-core（conda activate fnn）")
class TestUsdzConvert(unittest.TestCase):
    """重点是单位换算：写错会让输出小 1000 倍，切片后是个看不见的薄片。"""

    def setUp(self) -> None:
        self.tmp = _tmp_dir(self.id())

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_usd(self, meters_per_unit: float, size_x: float, size_y: float,
                   size_z: float, up_axis: str = "Z") -> Path:
        """手写一个最小 USD（ASCII）文件。

        不用 pxr 创建：pxr 保存时要写系统临时文件，在受限环境里会失败。
        USDA 是纯文本，直接写更省事。
        """
        path = self.tmp / "cube.usda"
        points = [
            (0, 0, 0), (size_x, 0, 0), (size_x, size_y, 0), (0, size_y, 0),
            (0, 0, size_z), (size_x, 0, size_z),
            (size_x, size_y, size_z), (0, size_y, size_z),
        ]
        faces = [
            0, 1, 2, 0, 2, 3,
            4, 6, 5, 4, 7, 6,
            0, 4, 5, 0, 5, 1,
            1, 5, 6, 1, 6, 2,
            2, 6, 7, 2, 7, 3,
            3, 7, 4, 3, 4, 0,
        ]
        counts = [3] * (len(faces) // 3)
        path.write_text(
            "#usda 1.0\n(\n"
            f"    metersPerUnit = {meters_per_unit}\n"
            f'    upAxis = "{up_axis}"\n'
            ")\n\n"
            'def Mesh "Cube"\n{\n'
            f"    int[] faceVertexCounts = [{', '.join(str(c) for c in counts)}]\n"
            f"    int[] faceVertexIndices = [{', '.join(str(i) for i in faces)}]\n"
            "    point3f[] points = ["
            + ", ".join(f"({x}, {y}, {z})" for x, y, z in points)
            + "]\n}\n",
            encoding="utf-8",
        )
        return path

    def test_meters_per_unit_is_applied(self):
        from convert_usdz_to_stl import convert_usdz_to_stl

        # metersPerUnit=1.0 → 1 USD 单位 = 1000mm；边长 0.02 单位 = 20mm
        src = self._write_usd(meters_per_unit=1.0, size_x=0.02,
                              size_y=0.02, size_z=0.02)
        result = convert_usdz_to_stl(src, self.tmp / "out.stl")
        self.assertIsNotNone(result, "转换应成功")
        for extent in result["extents_mm"]:
            self.assertAlmostEqual(extent, 20.0, places=3)

    def test_millimeter_file_stays_the_same(self):
        from convert_usdz_to_stl import convert_usdz_to_stl

        # metersPerUnit=0.001 → 1 单位 = 1mm；边长 20 单位 = 20mm
        src = self._write_usd(meters_per_unit=0.001, size_x=20.0,
                              size_y=20.0, size_z=20.0)
        result = convert_usdz_to_stl(src, self.tmp / "out2.stl")
        self.assertIsNotNone(result)
        for extent in result["extents_mm"]:
            self.assertAlmostEqual(extent, 20.0, places=3)

    def test_y_up_is_rotated_to_z_up(self):
        from convert_usdz_to_stl import convert_usdz_to_stl

        # Y 向上的细长条：X=10, Y=40, Z=10（单位=1mm）
        src = self._write_usd(meters_per_unit=0.001, size_x=10.0,
                              size_y=40.0, size_z=10.0, up_axis="Y")
        result = convert_usdz_to_stl(src, self.tmp / "out3.stl")
        self.assertIsNotNone(result)
        extents = result["extents_mm"]
        self.assertAlmostEqual(extents[2], 40.0, places=3)
        self.assertAlmostEqual(extents[1], 10.0, places=3)

    def test_quad_faces_are_triangulated(self):
        """四边面要正确三角化（原实现只处理 3/4 边，>4 边会被丢弃）。"""
        from convert_usdz_to_stl import convert_usdz_to_stl

        path = self.tmp / "quad.usda"
        path.write_text(
            '#usda 1.0\n(\n    metersPerUnit = 0.001\n    upAxis = "Z"\n)\n'
            'def Mesh "Quad"\n{\n'
            "    int[] faceVertexCounts = [4]\n"
            "    int[] faceVertexIndices = [0, 1, 2, 3]\n"
            "    point3f[] points = [(0,0,0), (10,0,0), (10,10,0), (0,10,0)]\n"
            "}\n",
            encoding="utf-8",
        )
        result = convert_usdz_to_stl(path, self.tmp / "out4.stl")
        self.assertIsNotNone(result)
        self.assertEqual(result["faces"], 2, "一个四边面应三角化成 2 个三角形")

    def test_missing_file_returns_none(self):
        from convert_usdz_to_stl import convert_usdz_to_stl

        self.assertIsNone(convert_usdz_to_stl(self.tmp / "nope.usdz"))


class FakeState:
    """给 api 层用的假状态对象。"""

    def __init__(self, code: str = "FINISH", percent: int = 0,
                 current: str | None = None, layers: int = 0,
                 total: int = 0) -> None:
        self.gcode_state = code
        self.current_file = current
        self.progress_percent = percent
        self.layer_num = layers
        self.total_layers = total
        self.remaining_minutes = 0
        self.nozzle_temp = 30.0
        self.nozzle_target = None
        self.bed_temp = 25.0
        self.bed_target = None
        self.chamber_temp = None
        self.hms_errors = []
        self.print_info = {"gcode_state": code}


class FakeBridge:
    """捕获下发的报文，不联网。"""

    connack_code = None
    last_payload: dict | None = None

    def __init__(self, config, allow_real_actions=False, log_dir=None):
        self.allow_real_actions = allow_real_actions

    def connect(self, **kwargs):
        return True

    @property
    def subscribed(self):
        class _Subscribed:
            def wait(self, _timeout):
                return True

        return _Subscribed()

    def send_command(self, payload, label=None):
        FakeBridge.last_payload = payload
        FakeBridge.last_label = label
        return True

    def disconnect(self):
        pass


class TestStartPrint(unittest.TestCase):
    """api.start_print 的行为：AMS 映射、忙时保护、缺文件、状态读不到。"""

    def _run(self, *, ams_slot: int | None = 0, state_code: str = "FINISH",
             remote: int | None = 1024, connected: bool = True,
             wait_result: tuple = ("RUNNING", 1)) -> dict:
        import api

        FakeBridge.last_payload = None
        patches = [
            (api, "_config",
             lambda: api.PrinterConfig(ip="127.0.0.1", serial="S1",
                                       access_code="x")),
            (api, "_probe",
             lambda *a, **k: (FakeState(state_code), None)),
            (api, "remote_file_info",
             lambda *a, **k: {"connected": connected, "size": remote}),
            (api, "X1CBridge", FakeBridge),
            (api, "_wait_until_running", lambda *a, **k: wait_result),
        ]
        originals = [(obj, name, getattr(obj, name)) for obj, name, _v in patches]
        for obj, name, value in patches:
            setattr(obj, name, value)
        try:
            return api.start_print("a.3mf", ams_slot=ams_slot)
        finally:
            for obj, name, value in reversed(originals):
                setattr(obj, name, value)

    def test_default_slot_is_zero(self):
        """料一般在 0 号槽，默认就取 0。"""
        result = self._run()
        self.assertTrue(result["ok"])
        payload = FakeBridge.last_payload["print"]
        self.assertTrue(payload["use_ams"])
        self.assertEqual(payload["ams_mapping"], [0])

    def test_other_slot(self):
        self._run(ams_slot=3)
        self.assertEqual(FakeBridge.last_payload["print"]["ams_mapping"], [3])

    def test_external_spool(self):
        self._run(ams_slot=None)
        payload = FakeBridge.last_payload["print"]
        self.assertFalse(payload["use_ams"])
        self.assertEqual(payload["ams_mapping"], [])

    def test_busy_printer_is_refused(self):
        """正在打印时不下发，避免打断别人的活。"""
        result = self._run(state_code="RUNNING")
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "busy")
        self.assertIsNone(FakeBridge.last_payload, "忙时绝不能发报文")

    def test_unreachable_is_not_reported_as_missing(self):
        """连不上打印机时不能说"文件不存在"，否则会白做一次上传。"""
        result = self._run(connected=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "connect")
        self.assertIn("连不上", result["error"])
        self.assertIsNone(FakeBridge.last_payload)

    def test_missing_file_is_refused(self):
        result = self._run(remote=None)
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "missing")
        self.assertIsNone(FakeBridge.last_payload)

    def test_network_blind_is_not_reported_as_failure(self):
        """一次状态都没读到 → 网络问题，不能说"没开打"。"""
        result = self._run(wait_result=(None, 0))
        self.assertTrue(result["ok"], "指令发出即算成功")
        self.assertFalse(result["started"])
        self.assertIn("网络", result["note"])

    def test_confirmed_not_started(self):
        result = self._run(wait_result=(None, 4))
        self.assertTrue(result["ok"])
        self.assertFalse(result["started"])
        self.assertNotIn("网络", result["note"])

    def test_waiting_running_marks_started(self):
        result = self._run(wait_result=("RUNNING", 2))
        self.assertTrue(result["started"])
        self.assertEqual(result["state"], "RUNNING")


class TestGetStatus(unittest.TestCase):
    def test_maps_fields(self):
        import api

        state = FakeState("RUNNING", percent=43, current="手腕.gcode.3mf",
                          layers=48, total=164)
        patches = [
            (api, "_config",
             lambda: api.PrinterConfig(ip="1.2.3.4", serial="S", access_code="x")),
            (api, "_probe", lambda *a, **k: (state, None)),
        ]
        originals = [(obj, name, getattr(obj, name)) for obj, name, _v in patches]
        for obj, name, value in patches:
            setattr(obj, name, value)
        try:
            result = api.get_status()
        finally:
            for obj, name, value in reversed(originals):
                setattr(obj, name, value)

        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "RUNNING")
        self.assertEqual(result["percent"], 43)
        self.assertEqual(result["file"], "手腕.gcode.3mf")
        self.assertEqual(result["layer"], 48)
        self.assertFalse(result["idle"])

    def test_connect_failure_reports_reason(self):
        import api

        patches = [
            (api, "_config",
             lambda: api.PrinterConfig(ip="1.2.3.4", serial="S", access_code="x")),
            (api, "_probe", lambda *a, **k: (None, 5)),
        ]
        originals = [(obj, name, getattr(obj, name)) for obj, name, _v in patches]
        for obj, name, value in patches:
            setattr(obj, name, value)
        try:
            result = api.get_status()
        finally:
            for obj, name, value in reversed(originals):
                setattr(obj, name, value)

        self.assertFalse(result["ok"])
        self.assertIn("访问码", result["error"])


class TestDesignHelpers(unittest.TestCase):
    """design.py 里容易写错的小逻辑。"""

    def test_size_snippet_prefers_dense_dimensions(self):
        """规格句一次给多个毫米数，要挑中它，而不是抓到年份。"""
        from design import _size_snippet

        text = ("AA电池由单电化电池构成。美国国家标准协会在1947年规范了尺寸。"
                "AA电池含端钮纽扣长49.2 - 50.5 mm，直径13.5 - 14.5 mm。"
                "碱性AA电池约重23 g。")
        snippet = _size_snippet(text)
        self.assertIsNotNone(snippet)
        self.assertIn("49.2", snippet)
        self.assertIn("14.5", snippet)

    def test_size_snippet_none_without_units(self):
        from design import _size_snippet

        self.assertIsNone(_size_snippet("1947 年规范了尺寸，共 23 克"))

    def test_clean_strips_tags(self):
        from design import _clean

        self.assertEqual(_clean("<b>直径</b>  14.0&nbsp;mm"), "直径 14.0&nbsp;mm")


class TestDesignBuild(unittest.TestCase):
    """几何校验要拦住不合理的零件，确认后才写文件。"""

    def test_unknown_part_is_rejected(self):
        from design import propose_part

        result = propose_part("no_such_part", {})
        self.assertFalse(result["ok"])
        self.assertIn("没有零件", result["error"])

    def test_unknown_param_is_rejected(self):
        from design import propose_part

        result = propose_part("battery_holder", {"nosuch": 1})
        self.assertFalse(result["ok"])
        self.assertIn("不认识参数", result["error"])

    def test_proposal_does_not_write_file(self):
        from design import OUTPUT_DIR, propose_part

        before = set(OUTPUT_DIR.glob("*.stl")) if OUTPUT_DIR.is_dir() else set()
        result = propose_part("spacer", {"outer_diameter": 12.0,
                                         "hole_diameter": 4.5, "height": 6.0})
        self.assertTrue(result["ok"], result.get("error"))
        self.assertTrue(result.get("staged"))
        after = set(OUTPUT_DIR.glob("*.stl")) if OUTPUT_DIR.is_dir() else set()
        self.assertEqual(before, after, "提议阶段不应写文件")

    def test_confirm_writes_stl(self):
        from design import propose_part, write_stl

        propose_part("spacer", {"outer_diameter": 12.0,
                                "hole_diameter": 4.5, "height": 6.0})
        result = write_stl()
        self.assertTrue(result["ok"], result.get("error"))
        stl = Path(result["stl"])
        self.assertTrue(stl.is_file())
        self.assertGreater(stl.stat().st_size, 0)
        self.assertEqual(len(result["size_mm"]), 3)
        stl.unlink(missing_ok=True)

    def test_oversized_part_is_rejected(self):
        from design import propose_part

        result = propose_part("mounting_plate",
                              {"width": 300.0, "depth": 40.0, "thickness": 3.0})
        self.assertFalse(result["ok"])
        self.assertTrue("256" in result["error"] or "校验" in result["error"])


class TestDesignModel(unittest.TestCase):
    """编排：模型只负责选零件填参数，几何由代码算。"""

    def _fake_chat(self, script: list[dict]):
        calls = {"n": 0}

        def fake(messages, tools=None):
            index = min(calls["n"], len(script) - 1)
            calls["n"] += 1
            return {"choices": [{"message": script[index]}]}

        return fake

    def _run(self, script: list[dict], **kwargs) -> dict:
        import design

        original = design.chat
        design.chat = self._fake_chat(script)
        try:
            return design.design_model("做个垫圈", **kwargs)
        finally:
            design.chat = original

    def _tool_call(self, name: str, args: dict, call_id: str = "c1") -> dict:
        import json as _json

        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": _json.dumps(args)},
            }],
        }

    def test_proposes_and_needs_confirm(self):
        result = self._run([
            self._tool_call("propose_part", {
                "part": "spacer",
                "params": {"outer_diameter": 12.0, "hole_diameter": 4.5,
                           "height": 6.0},
                "summary": "垫圈",
            }),
            {"role": "assistant", "content": "已生成垫圈"},
        ])
        self.assertFalse(result["ok"])
        plan = result["need_confirm"]
        self.assertEqual(plan["part"], "spacer")

    def test_ask_user_returns_need_input(self):
        result = self._run([
            self._tool_call("ask_user", {
                "question": "线的直径是多少？",
                "fields": ["线径_mm"],
            }),
        ])
        self.assertFalse(result["ok"])
        self.assertEqual(result["need_input"]["fields"], ["线径_mm"])
        self.assertIn("直径", result["need_input"]["question"])

    def test_invalid_params_are_reported_back(self):
        """参数不对时要把错误交回模型，最终仍失败则返回 ok=False。"""
        result = self._run([
            self._tool_call("propose_part", {"part": "no_such", "params": {}}),
            {"role": "assistant", "content": "抱歉，生成失败"},
        ])
        self.assertFalse(result["ok"])
        self.assertIn("没有给出", result["error"])


class TestNeedsUpload(unittest.TestCase):
    """打印机上那份要不要重传：大小 + 修改时间。"""

    def _file(self, name: str, size: int = 100, mtime: int = 1_700_000_000):
        import os

        folder = _tmp_dir(self.id())
        target = folder / name
        target.write_bytes(b"x" * size)
        os.utime(target, (mtime, mtime))
        return target

    def test_same_size_and_time_skips(self):
        import server

        local = self._file("a.3mf", 100, 1_700_000_000)
        info = {"size": 100, "mtime": 1_700_000_000 + 10}
        self.assertFalse(server._needs_upload(local, info))

    def test_size_differs_reuploads(self):
        import server

        local = self._file("b.3mf", 100, 1_700_000_000)
        info = {"size": 99, "mtime": 1_700_000_000}
        self.assertTrue(server._needs_upload(local, info))

    def test_mtime_drift_reuploads(self):
        """重新切片后字节数可能刚好相同，靠时间戳兜住。"""
        import server

        local = self._file("c.3mf", 100, 1_700_000_000)
        info = {"size": 100, "mtime": 1_700_000_000 + 3600}
        self.assertTrue(server._needs_upload(local, info))

    def test_unknown_remote_time_falls_back_to_size(self):
        import server

        local = self._file("d.3mf", 100, 1_700_000_000)
        info = {"size": 100, "mtime": None}
        self.assertFalse(server._needs_upload(local, info))


if __name__ == "__main__":
    unittest.main(verbosity=2)
