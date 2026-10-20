"""把切片文件传到打印机（FTPS 990）。

Bambu 的文件通道规矩：
  * FTPS 隐式 TLS（连上就是 TLS，不需要 AUTH TLS），端口 990
  * 用户名固定 bblp，密码是访问码
  * 打印机只认已经切好片的 .3mf（内含 Metadata/plate_N.gcode），
    .stl 传上去也打不了 —— 切片必须在 Bambu Studio 侧完成
  * 启动打印这一步没有做自动化，上传后在打印机屏幕上选文件开始

用法：
  python upload_and_print.py model.3mf
  python upload_and_print.py model.3mf --remote-dir /
"""

from __future__ import annotations

import ftplib
import logging
import os
import ssl
import sys
import time
import zipfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from bambu_bridge import PrinterConfig  # noqa: E402

LOG = logging.getLogger("x1c.ftps")


class ImplicitFTPTLS(ftplib.FTP_TLS):
    """Bambu 的 FTPS 有两个标准库不支持的特性，都要自己处理：

      1. **隐式 TLS**：连上立刻握手（标准库 FTP_TLS 只做显式 AUTH TLS）
      2. **数据连接必须复用控制连接的 TLS 会话**：vsFTPd 会校验，
         否则数据连接被重置，报
         `522 SSL connection failed: session reuse required`
         或上传中途 ConnectionResetError。
    """

    def connect(self, host: str = "", port: int = 0, timeout: float = -999):  # type: ignore[override]
        self.host = host or self.host
        self.port = port or self.port
        if timeout != -999:
            self.timeout = timeout
        self.sock = self._create_connection_socket()
        # 关键差异：不做 AUTH TLS，直接握手
        self.sock = self.context.wrap_socket(self.sock, server_hostname=self.host)
        self.af = self.sock.family
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome

    def ntransfercmd(self, cmd, rest=None):  # type: ignore[override]
        """让数据连接复用控制连接的 TLS 会话，否则服务器会拒绝。"""
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            session = None
            try:
                session = self.sock.session
            except AttributeError:
                session = None
            try:
                if session is not None:
                    conn = self.context.wrap_socket(
                        conn, server_hostname=self.host, session=session
                    )
                else:
                    conn = self.context.wrap_socket(conn, server_hostname=self.host)
            except (ssl.SSLError, TypeError):
                # 某些 Python 版本不支持 session 参数，退化为普通握手
                conn = self.context.wrap_socket(conn, server_hostname=self.host)
        return conn, size

    def _create_connection_socket(self):
        import socket

        return socket.create_connection((self.host, self.port), self.timeout)


# 上传参数。这两个值按实测调过，换网络可能还要再调，所以留了环境变量覆盖：
#
#   慢网络实测（172.16.16.x，TCP 建连 0.2~5 秒）：
#     1024 字节块 / 45 秒  → 登录就花 32 秒，上传途中超时
#     8192 字节块 / 60 秒  → 登录 4 秒，上传 10 秒，约 7 KB/s（最快）
#     32768 字节块 / 120 秒 → 约 4 KB/s（块太大反而慢）
#
# 所以默认用 8192 字节块 + 120 秒超时。超时给得宽是因为这条链路的
# 单次操作耗时波动极大（4~32 秒都见过），卡在临界值上就会随机失败。
UPLOAD_BLOCK_SIZE = int(os.environ.get("X1C_UPLOAD_BLOCK", "8192"))
UPLOAD_TIMEOUT = float(os.environ.get("X1C_UPLOAD_TIMEOUT", "120"))

# 这些异常都是"网络抖了一下"导致的，值得重试而不是直接失败
RETRYABLE_ERRORS = (
    TimeoutError,
    ConnectionError,
    ssl.SSLError,
    ftplib.error_temp,
)


def make_ftp(config: PrinterConfig, timeout: float | None = None) -> ImplicitFTPTLS:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ftp = ImplicitFTPTLS(context=ctx, timeout=timeout or UPLOAD_TIMEOUT)
    ftp.connect(config.ip, config.ftps_port)
    ftp.login("bblp", config.access_code)
    ftp.prot_p()  # 数据连接也加密，否则会被拒绝
    ftp.set_pasv(True)
    LOG.info("FTPS 登录成功: %s:%s", config.ip, config.ftps_port)
    return ftp


def remote_size(ftp: ImplicitFTPTLS, name: str) -> int | None:
    try:
        return ftp.size(name)
    except ftplib.error_perm:
        return None


def is_sliced_3mf(path: Path) -> tuple[bool, str]:
    """判断 .3mf 是不是「已经切好片」的。

    只看后缀不够：Bambu Studio 的工程文件也是 .3mf，但它里面没有
    Metadata/plate_N.gcode。传上去打印机也打不了，用户会白等一场。
    返回 (是否可打印, 原因)。
    """
    path = Path(path)
    if path.suffix.lower() != ".3mf":
        return False, f"不是 .3mf 文件（{path.suffix or '无扩展名'}）"
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
    except (zipfile.BadZipFile, OSError) as exc:
        return False, f"文件打不开（{exc}）"

    plates = [n for n in names if n.endswith(".gcode")]
    if plates:
        return True, f"已切片（含 {Path(plates[0]).name}）"
    return False, "这个 .3mf 里没有 gcode —— 是工程文件，还没切片"


def upload(
    config: PrinterConfig,
    local_file: Path,
    remote_name: str | None = None,
    remote_dir: str = "/",
) -> str:
    """上传文件，返回打印机侧的相对文件名。"""
    local_file = local_file.resolve()
    if not local_file.is_file():
        raise SystemExit(f"文件不存在: {local_file}")

    # 只有 .gcode / 切片好的 .3mf 有意义。工程文件传上去打印机打不了，
    # 与其让用户白跑一趟，不如在这里拦住并说清怎么解决。
    if local_file.suffix.lower() == ".3mf":
        ok, reason = is_sliced_3mf(local_file)
        if not ok:
            raise SystemExit(
                f"✗ 不能上传: {reason}\n"
                "  解决: 在 Bambu Studio 里打开它 → 切片 → "
                "文件 → 导出 → 导出单盘切片文件"
            )
    elif not local_file.name.lower().endswith(".gcode"):
        LOG.warning(
            "扩展名 %s 不是打印机原生格式；只有切片好的 .3mf 才能直接打印",
            local_file.suffix,
        )
    name = remote_name or local_file.name
    size = local_file.stat().st_size

    ftp = make_ftp(config)
    try:
        if remote_dir not in ("", "/"):
            try:
                ftp.cwd(remote_dir)
            except ftplib.error_perm as exc:
                # 进不去就说清楚，不要假装传到了指定目录
                LOG.warning(
                    "打印机上没有目录 %s（%s），改为上传到根目录 /", remote_dir, exc
                )
                try:
                    ftp.cwd("/")
                except ftplib.error_perm:
                    pass
        existing = remote_size(ftp, name)
        LOG.info(
            "上传 %s -> %s (%.1f MB)%s",
            local_file.name,
            name,
            size / 1024 / 1024,
            f"，打印机上已存在同尺寸文件" if existing == size else "",
        )
        t0 = time.time()
        with local_file.open("rb") as fh:
            ftp.storbinary(f"STOR {name}", fh, blocksize=UPLOAD_BLOCK_SIZE)
        # 传完校验大小：网络不稳时可能出现"看似成功但文件不全"
        actual = remote_size(ftp, name)
        if actual is not None and actual != size:
            raise RuntimeError(
                f"上传后大小不符：本地 {size} 字节，打印机上 {actual} 字节。"
                "文件可能不完整，请重试。"
            )
        LOG.info("上传完成并校验通过，用时 %.1fs", time.time() - t0)
    finally:
        try:
            ftp.quit()
        except Exception:
            ftp.close()
    return name


def upload_with_retry(
    config: PrinterConfig,
    local_file: Path,
    remote_dir: str = "/",
    attempts: int = 3,
    delay: float = 3.0,
) -> str:
    """上传失败自动重试。

    这条网络单次操作耗时波动很大（同样的登录见过 4 秒和 32 秒），
    偶发超时占多数。与其让用户重敲一遍命令，不如自己重试。
    每个异常都意味着整条 FTPS 会话作废，所以重试就是重连。
    """
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return upload(config, local_file, remote_dir=remote_dir)
        except SystemExit:
            raise
        except RETRYABLE_ERRORS as exc:
            last = exc
            if attempt >= attempts:
                break
            print(f"  第 {attempt}/{attempts} 次失败（{type(exc).__name__}: {exc}）")
            print(f"  网络不稳，{delay:.0f} 秒后重试...")
            time.sleep(delay)
            delay *= 1.5

    raise RuntimeError(
        f"重试 {attempts} 次仍失败（最后一次: {type(last).__name__}: {last}）。\n"
        "  网络较慢时可以试试放宽超时：\n"
        "     $env:X1C_UPLOAD_TIMEOUT='300'; python main.py upload 文件.3mf"
    ) from last
