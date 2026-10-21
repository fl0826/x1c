"""对外接口 —— 供其他程序（网页、脚本、Agent）调用。

四个能力，相互独立，也可以组合：

    upload_file(r"D:\桌面\手腕.gcode.3mf")   # 上传
    start_print("手腕.gcode.3mf", ams_slot=0) # 开始打印
    get_status()                              # 查状态
    upload_and_print(path, ams_slot=0)        # 上传 + 打印

设计约定：
  * 一律返回 dict（可直接 json 序列化给网页用），不向调用方抛异常；
    失败时 ok=False 并带 error 说明。
  * 打印前一定先查状态：正在打印就拒绝，避免打断别人的活。
  * 关键参数集中在 .env（打印机凭据、LLM 配置）。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from bambu_bridge import (
    GCODE_STATE_ZH,
    PrinterConfig,
    X1CBridge,
    build_print_payload,
    fmt_temp,
)
from upload_and_print import (  # noqa: E402
    is_sliced_3mf,
    make_ftp,
    remote_mtime,
    remote_size,
    upload_with_retry,
)

# 打印机正在动的状态（这些状态下一律拒绝下发新任务）
ACTIVE_STATES = ("RUNNING", "PREPARE", "PAUSE", "SLICING")


def _config() -> PrinterConfig:
    return PrinterConfig.from_env()


def _probe(config: PrinterConfig, timeout: float = 20.0):
    """连一次、取一份状态、断开。返回 (状态, connack_code)。

    每次新建连接：打印一启动打印机会掐掉局域网连接，长连接会读到旧状态。
    """
    bridge = X1CBridge(config, allow_real_actions=False, log_dir=None)
    try:
        if not bridge.connect(retries=2):
            return None, bridge.connack_code
        bridge.subscribed.wait(12)
        return bridge.wait_for_state(lambda _s: True, timeout=timeout), None
    except Exception:  # noqa: BLE001
        return None, None
    finally:
        try:
            bridge.disconnect()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- 查状态


def get_status() -> dict[str, Any]:
    """读打印机当前状态。

    返回示例::

        {"ok": True, "state": "RUNNING", "state_zh": "打印中", "percent": 43,
         "layer": 48, "total_layers": 164, "remaining_minutes": 20,
         "file": "手腕.gcode.3mf", "nozzle": "199.0°C / 200.0°C",
         "bed": "55.0°C / 55.0°C", "chamber": "34.0°C",
         "idle": False, "errors": []}
    """
    try:
        config = _config()
    except SystemExit as exc:
        return {"ok": False, "error": str(exc)}

    state, connack = _probe(config)
    if state is None or not state.print_info:
        return {
            "ok": False,
            "error": _connect_error(connack, state is not None),
            "connack": connack,
        }

    code = state.gcode_state
    errors = state.hms_errors
    state_zh = GCODE_STATE_ZH.get(code or "", code)
    # 固件的 FAILED 会粘住：进度 0 且没有错误码时，机器其实已经空闲了
    if code == "FAILED" and not (state.progress_percent or 0) and not errors:
        state_zh = "空闲（上次中断）"
    return {
        "ok": True,
        "state": code,
        "state_zh": state_zh,
        "idle": code not in ACTIVE_STATES,
        "percent": state.progress_percent,
        "layer": state.layer_num,
        "total_layers": state.total_layers,
        "remaining_minutes": state.remaining_minutes,
        "file": state.current_file,
        "nozzle": fmt_temp(state.nozzle_temp, state.nozzle_target),
        "bed": fmt_temp(state.bed_temp, state.bed_target),
        "chamber": fmt_temp(state.chamber_temp),
        "errors": errors,
        "ams": _ams_slots(state.print_info),
        "printer": config.ip,
    }


def _ams_slots(print_info: dict) -> list[dict[str, Any]]:
    """把 AMS 各槽整理成前端好用的形式（槽位、材料、颜色、是否可用）。"""
    slots: list[dict[str, Any]] = []
    units = (print_info.get("ams") or {}).get("ams") or []
    for unit in units:
        for tray in unit.get("tray") or []:
            kind = tray.get("tray_type")
            slots.append({
                "slot": int(tray.get("id", 0)),
                "type": kind,
                "color": "#" + (tray.get("tray_color") or "")[:6] if tray.get("tray_color") else None,
                "remain": tray.get("remain"),
                "usable": bool(kind),
            })
    return sorted(slots, key=lambda item: item["slot"])


def _connect_error(connack, connected_but_empty: bool) -> str:
    if connected_but_empty:
        return "连上了但没收到状态数据（检查序列号是否正确）"
    if connack in (4, 5):
        return f"打印机拒绝连接 (code={connack})，访问码可能已变"
    if connack is not None:
        return f"打印机拒绝连接 (code={connack})"
    return "连不上打印机：检查是否同一网段、打印机是否开机"


# ---------------------------------------------------------------- 上传


def upload_file(local_path: str | Path, remote_dir: str = "/",
                progress=None) -> dict[str, Any]:
    """把一个切片好的 .3mf 上传到打印机。

    progress: 可选回调 progress(已发送字节, 总字节)，用于显示上传进度。

    返回::

        {"ok": True, "name": "手腕.gcode.3mf", "size": 643384}
    """
    path = Path(local_path)
    if not path.is_file():
        return {"ok": False, "error": f"文件不存在: {path}"}

    ok, reason = is_sliced_3mf(path)
    if not ok:
        return {
            "ok": False,
            "error": f"不能上传: {reason}",
            "hint": "Bambu Studio 打开 → 切片 → 文件 → 导出 → 导出单盘切片文件",
        }

    try:
        config = _config()
        name = upload_with_retry(config, path, remote_dir=remote_dir,
                                 progress=progress)
    except SystemExit as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"上传失败: {exc}"}

    size = remote_file_size(name, remote_dir)
    return {"ok": True, "name": name, "size": size, "detail": reason}


def remote_file_info(name: str, remote_dir: str = "/") -> dict[str, Any]:
    """查打印机上有没有这个文件。

    必须区分三种情况，否则会把"连不上"误报成"文件不存在"，
    然后去做一次注定失败的上传::

        {"connected": True,  "size": 643384}   # 有
        {"connected": True,  "size": None}     # 确实没有
        {"connected": False, "size": None, "error": "..."}   # 问不到
    """
    ftp = None
    try:
        config = _config()
        ftp = make_ftp(config, timeout=90)
        if remote_dir not in ("", "/"):
            ftp.cwd(remote_dir)
        return {
            "connected": True,
            "size": remote_size(ftp, name),
            "mtime": remote_mtime(ftp, name),
        }
    except Exception as exc:  # noqa: BLE001
        return {"connected": False, "size": None, "mtime": None,
                "error": f"连不上打印机: {exc}"}
    finally:
        if ftp is not None:
            try:
                ftp.quit()
            except Exception:  # noqa: BLE001
                try:
                    ftp.close()
                except Exception:  # noqa: BLE001
                    pass


def remote_file_size(name: str, remote_dir: str = "/") -> int | None:
    """文件大小；没有或问不到都返回 None。需要区分时用 remote_file_info。"""
    return remote_file_info(name, remote_dir).get("size")


# ---------------------------------------------------------------- 打印


def start_print(name: str, ams_slot: int | None = 0,
                wait_seconds: float = 70.0) -> dict[str, Any]:
    """让打印机开始打印**已经在打印机上**的文件。

    name      打印机上的文件名，例如 "手腕.gcode.3mf"
    ams_slot  AMS 槽位（0-3，默认 0）；传 None 表示走外挂料架

    打印前会先查状态：正在打印则拒绝，避免打断别人的活。
    返回::

        {"ok": True, "state": "RUNNING", "started": True,
         "file": "手腕.gcode.3mf", "ams_slot": 0}
    """
    try:
        config = _config()
    except SystemExit as exc:
        return {"ok": False, "error": str(exc)}

    # 1. 空闲检查
    state, connack = _probe(config)
    if state is None:
        return {"ok": False, "error": _connect_error(connack, False),
                "stage": "connect"}
    code = state.gcode_state
    if code in ACTIVE_STATES:
        return {
            "ok": False,
            "error": f"打印机正在打印（{GCODE_STATE_ZH.get(code, code)}），拒绝下发",
            "stage": "busy",
            "state": code,
            "file": state.current_file,
            "percent": state.progress_percent,
        }

    # 2. 文件必须已经在打印机上（连不上要单独说，不能当成"文件不存在"）
    info = remote_file_info(name)
    if not info.get("connected"):
        return {"ok": False, "error": info.get("error", "连不上打印机"),
                "stage": "connect"}
    if info.get("size") is None:
        return {
            "ok": False,
            "error": f"打印机上没有 {name}，请先调用 upload_file",
            "stage": "missing",
        }

    # 3. 下发
    payload = build_print_payload(
        name,
        use_ams=ams_slot is not None,
        ams_mapping=[ams_slot] if ams_slot is not None else None,
    )
    sender = X1CBridge(config, allow_real_actions=True, log_dir=None)
    try:
        if not sender.connect(retries=3):
            return {"ok": False, "error": "下发时连接失败", "stage": "send"}
        sender.subscribed.wait(12)
        sender.send_command(payload, label="print.project_file")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"下发失败: {exc}", "stage": "send"}
    finally:
        try:
            sender.disconnect()
        except Exception:  # noqa: BLE001
            pass

    # 4. 确认真的开始（读不到状态 ≠ 失败，见下方 reads 判断）
    started, reads = _wait_until_running(config, wait_seconds)
    result: dict[str, Any] = {
        "ok": True,
        "file": name,
        "ams_slot": ams_slot,
        "sent": True,
        "state": started,
        "started": bool(started),
        "status_reads": reads,
    }
    if not started:
        if reads == 0:
            result["note"] = ("指令已发出，但一次都没读到状态（网络抖动），"
                              "很可能已经在打印；请稍后用 get_status 确认")
        else:
            result["note"] = f"打印机没开始打印（读到 {reads} 次状态都不是打印中）"
    return result


def _wait_until_running(config: PrinterConfig,
                        seconds: float) -> tuple[str | None, int]:
    """等打印机进入打印状态，返回 (状态, 成功读到状态的次数)。"""
    reads = 0
    deadline = time.time() + seconds
    while time.time() < deadline:
        state, _connack = _probe(config, timeout=15.0)
        if state is not None:
            reads += 1
            if state.gcode_state in ACTIVE_STATES:
                return state.gcode_state, reads
        time.sleep(3)
    return None, reads


# ---------------------------------------------------------------- 组合


def upload_and_print(local_path: str | Path, ams_slot: int | None = 0) -> dict[str, Any]:
    """一步完成：上传 + 打印。

    返回::

        {"ok": True, "upload": {...}, "print": {...}}
    """
    uploaded = upload_file(local_path)
    if not uploaded.get("ok"):
        return {"ok": False, "stage": "upload", "upload": uploaded}
    printed = start_print(uploaded["name"], ams_slot=ams_slot)
    return {
        "ok": bool(printed.get("ok")) and printed.get("started", False),
        "upload": uploaded,
        "print": printed,
    }


# ---------------------------------------------------------------- 生成模型


def design_model(prompt: str, image: str | Path | None = None,
                 answers: dict | None = None) -> dict[str, Any]:
    """一句话（+可选照片）→ STL。

    prompt   需求描述，例如 "做个放两节5号电池的槽子"
    image    可选照片路径（只用来识别物体，不用来量尺寸）
    answers  上一轮 need_input 的答案，形如 {"线径_mm": 6}

    返回::

        {"ok": True, "stl": "output/battery_holder_183000.stl",
         "part": "battery_holder", "params": {...},
         "size_mm": [37.5, 55.0, 10.0], "message": "..."}

    缺信息时返回 ``{"ok": False, "need_input": {"question": ..., "fields": [...]}}``。
    """
    try:
        from design import design_model as _design
    except ImportError as exc:
        return {"ok": False, "error": f"模型生成模块不可用: {exc}"}

    try:
        return _design(prompt, image=image, answers=answers)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"生成失败: {exc}"}
