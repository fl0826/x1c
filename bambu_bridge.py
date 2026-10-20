"""X1C 局域网桥接核心。

只做一件事：把 Bambu Lab 打印机的局域网控制通道封装成 Python 对象，
让上层（LLM / Agent）可以"查询状态"和"发起打印"，而不必了解 MQTT 细节。

协议要点（Bambu Lab X1 系列，固件 01.05+ 的局域网模式）：
  * MQTT over TLS，端口 8883
    用户名 bblp，密码 = 打印机屏幕上的"访问码"(Access Code)
    打印机用自签名证书，必须关闭主机名校验（注意：这是被动的，不降低实际安全性，
    因为仍走 TLS 加密；风险在于无法防中间人，所以只在可信局域网内使用）
  * 订阅 device/{serial}/report   接收状态推送（JSON）
  * 发布 device/{serial}/request  下发指令（JSON）
  * 下发打印前必须先推送一条 pushall 请求拿到完整状态，
    否则打印机可能忽略指令。
  * 文件通道：FTPS 隐式 TLS，端口 990，同样 bblp / 访问码，
    上传到根目录，再用 MQTT 的 print.project_file 指令触发打印。

安全设计（重要）：
  这是能物理驱动一台机器的代码，因此：
   - 默认 dry_run=True，任何会真正动打印机的指令只打印不发送；
   - 真正执行需要显式传 allow_real_actions=True；
   - 白名单指令集，未知指令一律拒绝；
   - 全部收发报文可选落盘，便于事后审计。
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# 把 vendor 目录加进 sys.path，便于在没有 pip 的环境里直接跑
_HERE = Path(__file__).resolve().parent
_VENDOR = _HERE / "vendor"
if _VENDOR.is_dir() and str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

try:
    import paho.mqtt.client as mqtt
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "缺少 paho-mqtt。请运行: python fetch_wheel.py paho-mqtt"
    ) from exc

LOG = logging.getLogger("x1c")


def _read_dotenv(path: Path) -> dict[str, str]:
    """读取极简格式的 .env（KEY=VALUE，# 开头为注释）。

    不引入 python-dotenv 依赖：本项目的原则是零外部依赖，
    而且 .env 的格式简单到不需要库。
    """
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        values[key.strip()] = val.strip().strip('"').strip("'")
    return values


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """把 patch 递归合并进 base。

    打印机推送的是增量状态，字典要逐层合并；
    列表则整体替换（例如 ams 的 tray 列表每次都是完整数组）。
    """
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base

# 允许下发的指令白名单。任何不在此列的指令直接拒绝，避免误伤设备。
# 键名是报文最外层的对象名（print / system / ...），不是 "print.project_file"
# 这种点号写法 —— 点号写法会被打印机当成未知字段静默丢弃。
ALLOWED_COMMANDS = {
    "pushall",            # 请求全量状态（只读）
    "print",              # 打印相关（开打），内部再按 command 区分
}

# 只读指令：只让打印机把状态再说一遍，不可能改变设备状态。
# 这类指令即使在 dry-run 下也要真发出去，否则查状态会拿不到数据；
# 而且不该打印 DRY-RUN 提示，那是给"会动打印机"的指令用的。
READ_ONLY_COMMANDS = {"pushall"}

# 打印指令里 url 的前缀。文件必须先在打印机的 SD 卡根目录，
# 这条写法是实机验证过的（见 README 的"自动打印"一节）。
PRINT_URL_PREFIX = "file:///sdcard/"


def build_print_payload(
    filename: str,
    *,
    plate: int = 1,
    bed_type: str = "textured_plate",
    bed_leveling: bool = True,
    flow_cali: bool = True,
    vibration_cali: bool = True,
    use_ams: bool = False,
    ams_mapping: list[int] | None = None,
    timelapse: bool = False,
) -> dict[str, Any]:
    """构造触发打印的报文（print.project_file）。

    实机验证过的要点：
      * 最外层必须是 {"print": {...}}，写成 {"print.project_file": {...}}
        会被当成未知字段丢弃（返回 rc=0 但打印机毫无反应）。
      * url 用 file:///sdcard/<文件名>，文件要已在 SD 卡根目录；
        写成 ftp://<IP>/<名> 打印机会提示"不支持文件路径或者名称"。
      * 料在 AMS 里必须 use_ams=True + ams_mapping=[槽号]；
        use_ams=False 会让打印机按外挂料架取料，启动后立刻报
        "检测到禁用AMS时仍从AMS加载料" 并停止。
      * 校准开关默认与屏幕上手动打印一致（调平 + 挤出补偿 + 震动补偿）。
        关掉它们会少做"校准挤出补偿"这一步，行为和手动打印不一致。
    """
    base_name = Path(filename).name
    return {
        "print": {
            "sequence_id": str(int(time.time()) % 100000),
            "command": "project_file",
            "param": f"Metadata/plate_{plate}.gcode",
            "subtask_id": "0",
            "subtask_name": base_name,
            "bed_type": bed_type,
            "url": f"{PRINT_URL_PREFIX}{base_name}",
            "timelapse": timelapse,
            "bed_leveling": bed_leveling,
            "flow_cali": flow_cali,
            "vibration_cali": vibration_cali,
            "layer_inspect": False,
            "use_ams": use_ams,
            "ams_mapping": ams_mapping if ams_mapping is not None else [],
            "profile_id": "0",
            "project_id": "0",
            "task_id": "0",
        }
    }


def fmt_temp(actual: Any, target: Any = None) -> str:
    """格式化温度：目标温度未知时不要显示成 'None°C'。"""
    if actual is None:
        return "—"
    try:
        text = f"{float(actual):.1f}°C"
    except (TypeError, ValueError):
        return str(actual)
    if target is None:
        return text
    try:
        return f"{text} / {float(target):.1f}°C"
    except (TypeError, ValueError):
        return text


@dataclass
class PrinterConfig:
    """一台打印机的连接参数。"""

    ip: str
    serial: str
    access_code: str
    port: int = 8883
    ftps_port: int = 990
    # 打印机是自签名证书，用 SNI 会校验失败，所以默认关闭校验
    tls_insecure: bool = True
    timeout: float = 10.0

    @classmethod
    def from_env(cls) -> "PrinterConfig":
        """读取连接参数。

        取值顺序：环境变量 → 同目录下的 .env 文件。
        环境变量优先，方便临时覆盖（例如换一台打印机试试）。
        访问码不写进代码，也不提交到仓库（.env 已在 .gitignore 里）。
        """
        file_values = _read_dotenv(_HERE / ".env")

        def value(key: str, default: str | None = None) -> str | None:
            return os.environ.get(key) or file_values.get(key) or default

        missing = [
            key for key in ("X1C_IP", "X1C_SERIAL", "X1C_ACCESS_CODE")
            if not value(key)
        ]
        if missing:
            raise SystemExit(
                "缺少打印机连接参数: " + ", ".join(missing) + "\n\n"
                "两种配置方式（任选其一）：\n"
                f"  1) 在 {_HERE / '.env'} 里填写：\n"
                "         X1C_IP=172.16.20.29\n"
                "         X1C_SERIAL=00M09A360200842\n"
                "         X1C_ACCESS_CODE=你的访问码\n"
                "  2) 或临时设环境变量（PowerShell）：\n"
                "         $env:X1C_IP='172.16.20.29'\n\n"
                "参数位置：打印机屏幕 → 设置 → 网络（IP、访问码）、设置 → 设备（序列号）"
            )
        return cls(
            ip=str(value("X1C_IP")),
            serial=str(value("X1C_SERIAL")),
            access_code=str(value("X1C_ACCESS_CODE")),
            port=int(value("X1C_PORT", "8883") or 8883),
            ftps_port=int(value("X1C_FTPS_PORT", "990") or 990),
        )


@dataclass
class PrinterState:
    """从 report 话题里挑出来的关键状态，扁平化方便喂给 LLM。"""

    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def meta_version(self) -> int | None:
        meta = self.raw.get("_mock")
        if isinstance(meta, dict):
            value = meta.get("version")
            if isinstance(value, int):
                return value
        return None

    @property
    def print_info(self) -> dict[str, Any]:
        return self.raw.get("print", {}) or {}

    @property
    def gcode_state(self) -> str | None:
        return self.print_info.get("gcode_state")

    @property
    def progress_percent(self) -> int | None:
        return self.print_info.get("mc_percent")

    @property
    def remaining_minutes(self) -> int | None:
        return self.print_info.get("mc_remaining_time")

    @property
    def layer_num(self) -> int | None:
        return self.print_info.get("layer_num")

    @property
    def total_layers(self) -> int | None:
        return self.print_info.get("total_layer_num")

    @property
    def nozzle_temp(self) -> float | None:
        # 打印机实际用的字段名是 nozzle_temper（不是 nozzle_temperature），
        # 这是实机核对后修正的；两个都兼容，避免不同固件差异。
        return self.print_info.get("nozzle_temper") or self.print_info.get(
            "nozzle_temperature"
        )

    @property
    def nozzle_target(self) -> float | None:
        return self.print_info.get("nozzle_target_temper") or self.print_info.get(
            "nozzle_target_temperature"
        )

    @property
    def bed_temp(self) -> float | None:
        return self.print_info.get("bed_temper") or self.print_info.get(
            "bed_temperature"
        )

    @property
    def bed_target(self) -> float | None:
        return self.print_info.get("bed_target_temper") or self.print_info.get(
            "bed_target_temperature"
        )

    @property
    def chamber_temp(self) -> float | None:
        return self.print_info.get("chamber_temper") or self.print_info.get(
            "chamber_temperature"
        )

    @property
    def current_file(self) -> str | None:
        return self.print_info.get("subtask_name") or self.print_info.get("gcode_file")

    @property
    def hms_errors(self) -> list[Any]:
        return self.print_info.get("hms", []) or []


# 状态码转中文，方便直接念给用户听
GCODE_STATE_ZH = {
    "IDLE": "空闲",
    "RUNNING": "打印中",
    "PAUSE": "已暂停",
    "FINISH": "已完成",
    "FAILED": "失败",
    "PREPARE": "准备中",
    "SLICING": "切片中",
    "INIT": "初始化",
}


class X1CBridge:
    """与一台 X1C 的局域网会话。"""

    def __init__(
        self,
        config: PrinterConfig,
        *,
        allow_real_actions: bool = False,
        log_dir: Path | None = None,
        poll_interval: float = 30.0,
    ) -> None:
        self.cfg = config
        self.allow_real_actions = allow_real_actions
        # 主动轮询间隔。不能只依赖打印机推送：
        # 实测（含本机 TLS 栈）会出现"服务端已写出、客户端 select 迟迟不可读"的
        # 半双工停滞，客户端只有自己发出数据后才会收到积压报文。
        # 因此定期发一条 pushall，既保持状态新鲜，也顺便解开停滞。
        # 可用环境变量 X1C_POLL_INTERVAL 覆盖（测试里设小值）。
        env_interval = os.environ.get("X1C_POLL_INTERVAL")
        self.poll_interval = float(env_interval) if env_interval else poll_interval
        self._poller: threading.Thread | None = None
        self._poll_stop = threading.Event()
        self.report_topic = f"device/{config.serial}/report"
        self.request_topic = f"device/{config.serial}/request"
        self.state = PrinterState()
        self.connected = threading.Event()
        # 打印机拒绝连接时的 CONNACK 返回码（已还原为 MQTT 原始语义）
        self.connack_code: int | None = None
        # 订阅确认（SUBACK）单独等待。只等 CONNACK 是不够的：
        # 那时订阅还没生效，打印机不会推任何状态，调用方会拿到空状态。
        self.subscribed = threading.Event()
        # 每收到一条 report 就自增。判断"状态是否刷新过"要用它，
        # 而不是比较状态内容 —— 内容恰好没变时会误判。
        self.state_seq = 0
        self.state_event = threading.Event()
        # 收到（不论能否解析）的报文条数，用于区分"没收到"和"收到但没解析"
        self.messages_received = 0
        self.last_topic: str | None = None
        self.last_error: str | None = None
        # 已见过的最大状态版本号。用于丢弃过期报文 ——
        # 订阅瞬间打印机会先推一帧旧快照，如果不比较版本号，
        # 这帧旧快照会把更新的状态覆盖掉（会导致误判打印机空闲）。
        self.last_seen_version: int | None = None
        # 最近一次"取新鲜状态"的决策轨迹，排障用
        self.last_fresh_debug: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._log_dir = log_dir
        self._log_fp = None
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"x1c-bridge-{os.getpid()}",
        )
        self._client.username_pw_set("bblp", config.access_code)
        if config.tls_insecure:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            self._client.tls_set_context(ctx)
        else:
            self._client.tls_set()
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self._client.on_subscribe = self._on_subscribe

    # ---------- 生命周期 ----------

    def connect(
        self,
        wait: bool = True,
        require_subscribe: bool = True,
        retries: int = 3,
    ) -> bool:
        """连接并（默认）等到订阅确认。

        必须等 SUBACK：只连上但订阅未生效时，打印机会一条状态都不推，
        上层会安静地拿到空状态，排查起来非常费时。

        retries：TCP 建连失败时重试次数。本机所在网络（BAAI-Guest）
        会间歇性丢弃到打印机的 TCP 连接（TimeoutError），
        不重试的话指令还没发出就失败了 —— 表现为"命令像没到一样"。
        """
        last_error: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                LOG.info("连接 %s:%s ... (第 %d/%d 次)",
                         self.cfg.ip, self.cfg.port, attempt, retries)
                self._client.connect(self.cfg.ip, self.cfg.port, keepalive=30)
            except OSError as exc:
                last_error = exc
                LOG.warning("TCP 建连失败: %s", exc)
                if attempt < retries:
                    time.sleep(2.0 * attempt)
                    continue
                LOG.error("TCP 建连连续失败 %d 次，放弃", retries)
                return False

            self._client.loop_start()
            if not wait:
                return True
            if not self.connected.wait(self.cfg.timeout):
                if self.connack_code is not None:
                    LOG.error(
                        "打印机明确拒绝连接（CONNACK=%s）。请核对访问码与序列号。",
                        self.connack_code,
                    )
                    return False
                LOG.warning("连接超时（%.0fs），打印机未应答", self.cfg.timeout)
                self._stop_loop()
                if attempt < retries:
                    time.sleep(2.0 * attempt)
                    continue
                return False
            if require_subscribe and not self.subscribed.wait(self.cfg.timeout):
                LOG.warning("订阅确认超时（%.0fs），可能收不到状态推送", self.cfg.timeout)
                return False
            self._start_poller()
            return True
        if last_error:
            LOG.error("连接失败: %s", last_error)
        return False

    def _stop_loop(self) -> None:
        """停止后台循环。"""
        try:
            self._client.loop_stop()
        except Exception:  # noqa: BLE001
            pass
        self._start_poller()
        return True

    def _start_poller(self) -> None:
        """启动后台轮询。"""
        if self.poll_interval <= 0:
            return
        if self._poller and self._poller.is_alive():
            return
        self._poll_stop.clear()

        def run() -> None:
            while not self._poll_stop.wait(self.poll_interval):
                if not self.connected.is_set():
                    continue
                try:
                    self.request_status()
                except Exception:  # 轮询不能把主流程搞崩
                    LOG.debug("轮询请求状态失败", exc_info=True)

        self._poller = threading.Thread(
            target=run, name="x1c-state-poller", daemon=True
        )
        self._poller.start()

    def disconnect(self) -> None:
        self._poll_stop.set()
        poller, self._poller = self._poller, None
        if poller and poller.is_alive():
            poller.join(timeout=2.0)
        try:
            self._client.loop_stop()
            self._client.disconnect()
        finally:
            if self._log_fp:
                self._log_fp.close()
                self._log_fp = None

    def __enter__(self) -> "X1CBridge":
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.disconnect()

    # ---------- MQTT 回调 ----------

    def _on_connect(self, client, userdata, flags, rc, properties=None) -> None:
        # paho 2.x 传进来的是 ReasonCode 对象而不是 int，
        # 直接写 rc == 0 在部分版本上不成立，会导致"连上了却从不订阅"。
        rc_value = getattr(rc, "value", rc)
        if rc_value == 0:
            LOG.info("已连接，订阅 %s", self.report_topic)
            result, _mid = client.subscribe(self.report_topic, qos=0)
            if result != mqtt.MQTT_ERR_SUCCESS:
                LOG.error("订阅请求发送失败 rc=%s", result)
            self.connected.set()
            return

        self.connected.clear()
        # 重要：paho 2.x 在 MQTT v3 下会把 CONNACK 的返回码原样塞进 ReasonCode，
        # 于是真正的 5（未授权）会以 135（0x87，paho 自己的错误域）暴露出来。
        # 不还原就会把"访问码不对"误报成"连接失败"，排查会绕很远。
        normalized = rc_value & 0xFF if rc_value > 0xFF else rc_value
        self.connack_code = normalized
        hints = {
            1: "MQTT 协议版本不被支持",
            2: "client_id 被拒绝",
            3: "服务不可用（打印机可能正在重启）",
            4: "用户名或密码错误",
            5: "未授权：访问码不对或已过期",
        }
        LOG.error(
            "连接被打印机拒绝 rc=%s（%s）。请到打印机屏幕核对：设置 → 网络 → 访问码",
            normalized,
            hints.get(normalized, "未知原因"),
        )

    def _on_subscribe(self, client, userdata, mid, reason_codes, properties=None) -> None:
        LOG.info("订阅已确认 (mid=%s, codes=%s)", mid, reason_codes)
        self.subscribed.set()
        # 订阅生效后再要全量状态，否则这条请求会石沉大海
        self.request_status()

    def _on_disconnect(self, client, userdata, flags, rc, properties=None) -> None:
        LOG.warning("连接断开 rc=%s", rc)
        self.connected.clear()

    def _on_message(self, client, userdata, msg) -> None:
        self.messages_received += 1
        self.last_topic = msg.topic
        try:
            payload = json.loads(msg.payload.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            self.last_error = f"非 JSON 报文: {msg.payload[:120]!r}"
            LOG.debug("%s", self.last_error)
            return
        self._append_log("recv", payload)
        if not self._accept_version(payload):
            return
        # report 是增量的：每次只带变化字段，需要合并
        with self._lock:
            self._merge_state(payload)
            self.state_seq += 1
            snapshot = PrinterState(raw=json.loads(json.dumps(self.state.raw)))
        self.state_event.set()

    def _accept_version(self, payload: dict[str, Any]) -> bool:
        """版本比已见过的旧就丢弃（旧快照覆盖新状态是真实存在的竞态）。

        真实打印机不带版本号，此时永远接受（返回 True）。
        """
        version = PrinterState(raw=payload).meta_version
        if version is None:
            return True
        if self.last_seen_version is not None and version < self.last_seen_version:
            LOG.debug("丢弃过期报文 v%s < v%s", version, self.last_seen_version)
            return False
        self.last_seen_version = version
        return True

    def _merge_state(self, payload: dict[str, Any]) -> None:
        """report 报文是增量的，必须递归合并。

        注意 ams / vt_tray 这类是嵌套字典，若只做一层 update 会把
        ams 下已有的键整体冲掉（已由单元测试锁定该行为）。
        """
        _deep_merge(self.state.raw, payload)

    # ---------- 报文日志 ----------

    def _append_log(self, direction: str, payload: dict[str, Any]) -> None:
        if not self._log_dir:
            return
        if self._log_fp is None:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            path = self._log_dir / f"mqtt-{time.strftime('%Y%m%d')}.jsonl"
            self._log_fp = path.open("a", encoding="utf-8")
        record = {"ts": time.time(), "dir": direction, "payload": payload}
        self._log_fp.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._log_fp.flush()

    # ---------- 下发指令 ----------

    def send(self, command: str, **params: Any) -> bool:
        """下发一条简单指令（参数直接作为该指令的子字典）。默认 dry-run。"""
        if command not in ALLOWED_COMMANDS:
            raise ValueError(f"指令不在白名单内，拒绝下发: {command}")
        payload = {command: params} if params else {command: ""}
        return self._dispatch(payload, label=command)

    def send_command(self, payload: dict[str, Any], label: str = "command") -> bool:
        """下发一条完整报文（外层键已按协议分好组，如 {"print": {...}}）。"""
        if not isinstance(payload, dict) or len(payload) != 1:
            raise ValueError("报文的顶层只能有一个对象键，例如 {'print': {...}}")
        outer = next(iter(payload))
        if outer not in ALLOWED_COMMANDS:
            raise ValueError(f"指令不在白名单内，拒绝下发: {outer}")
        return self._dispatch(payload, label=label)


    def _dispatch(self, payload: dict[str, Any], label: str) -> bool:
        """真正决定发不发的地方。所有下发路径都必须经过这里。"""
        command = next(iter(payload))
        read_only = command in READ_ONLY_COMMANDS
        if not self.allow_real_actions and not read_only:
            LOG.warning("[DRY-RUN] 本应下发: %s", json.dumps(payload, ensure_ascii=False))
            print(f"[DRY-RUN] 未真实下发: {json.dumps(payload, ensure_ascii=False)}")
            return True
        if not self.connected.is_set():
            raise RuntimeError("尚未连接，无法下发指令")
        self._append_log("send", payload)
        info = self._client.publish(self.request_topic, json.dumps(payload), qos=1)
        info.wait_for_publish(timeout=self.cfg.timeout)
        LOG.info("已下发 %s (rc=%s)", label, info.rc)
        return info.rc == mqtt.MQTT_ERR_SUCCESS

    # ---------- 高层动作 ----------

    def request_status(self, wait_seconds: float = 0.0) -> PrinterState:
        self.send("pushall")
        if wait_seconds:
            time.sleep(wait_seconds)
        return self.state

    def wait_for_state(
        self,
        predicate: Callable[[PrinterState], bool],
        timeout: float = 60.0,
        interval: float = 1.0,
        require_data: bool = True,
    ) -> PrinterState | None:
        """轮询等待某个条件成立（例如等到 RUNNING）。

        注意 require_data：默认要求"至少收到过一次状态"才算数。
        否则打印机一条状态都没推时，空缓存可能碰巧让 predicate 成立，
        会把"还没数据"误报成"状态已就绪"，这类误判在真实设备上很难排查。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if (not require_data or self.state.print_info) and predicate(self.state):
                return self.state
            self.request_status()
            time.sleep(interval)
        return None
