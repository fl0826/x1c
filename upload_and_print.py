"""FTPS 上传（供 api.py / main.py 调用）。

Bambu 的文件通道：
  * FTPS 隐式 TLS，端口 990，用户 bblp，密码是访问码
  * 只认已切片的 .3mf（内含 Metadata/plate_N.gcode），工程文件打不了
"""

from __future__ import annotations

import calendar
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
    """连上就握手（标准库只做显式 AUTH TLS），且数据连接复用会话。"""

    def connect(self, host: str = "", port: int = 0, timeout: float = -999):  # type: ignore[override]
        self.host = host or self.host
        self.port = port or self.port
        if timeout != -999:
            self.timeout = timeout
        self.sock = self._create_connection_socket()
        self.sock = self.context.wrap_socket(self.sock, server_hostname=self.host)
        self.af = self.sock.family
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome

    def ntransfercmd(self, cmd, rest=None):  # type: ignore[override]
        """数据连接复用控制连接的 TLS 会话，否则服务器会重置。"""
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            try:
                session = self.sock.session
            except AttributeError:
                session = None
            try:
                if session is not None:
                    conn = self.context.wrap_socket(
                        conn, server_hostname=self.host, session=session)
                else:
                    conn = self.context.wrap_socket(conn, server_hostname=self.host)
            except (ssl.SSLError, TypeError):
                conn = self.context.wrap_socket(conn, server_hostname=self.host)
        return conn, size

    def _create_connection_socket(self):
        import socket

        return socket.create_connection((self.host, self.port), self.timeout)


# 可调：网络差时把块调小、超时调大
UPLOAD_BLOCK_SIZE = int(os.environ.get("X1C_UPLOAD_BLOCK", "8192"))
UPLOAD_TIMEOUT = float(os.environ.get("X1C_UPLOAD_TIMEOUT", "120"))


def make_ftp(config: PrinterConfig, timeout: float | None = None) -> ImplicitFTPTLS:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ftp = ImplicitFTPTLS(context=ctx, timeout=timeout or UPLOAD_TIMEOUT)
    ftp.connect(config.ip, config.ftps_port)
    ftp.login("bblp", config.access_code)
    ftp.prot_p()
    ftp.set_pasv(True)
    LOG.info("FTPS 登录成功: %s:%s", config.ip, config.ftps_port)
    return ftp


def remote_size(ftp: ImplicitFTPTLS, name: str) -> int | None:
    try:
        return ftp.size(name)
    except ftplib.error_perm:
        return None


def remote_mtime(ftp: ImplicitFTPTLS, name: str) -> int | None:
    """打印机上文件的修改时间（Unix 秒）。取不到返回 None。"""
    try:
        reply = ftp.sendcmd(f"MDTM {name}")
    except (ftplib.Error, OSError):
        return None
    parts = reply.split()
    if len(parts) < 2 or len(parts[1]) < 14:
        return None
    try:
        stamp = time.strptime(parts[1][:14], "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return int(calendar.timegm(stamp))


def is_sliced_3mf(path: Path) -> tuple[bool, str]:
    """是不是「已切片」的 3mf（内含 plate_N.gcode）。"""
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


class _ProgressReader:
    """包一层文件对象，边读边回报进度。"""

    def __init__(self, handle, total: int, callback) -> None:
        self._handle = handle
        self._total = total
        self._callback = callback
        self._sent = 0
        self._last = 0.0

    def read(self, size: int = -1) -> bytes:
        data = self._handle.read(size)
        self._sent += len(data)
        now = time.time()
        if self._callback and (now - self._last >= 0.25 or self._sent >= self._total):
            self._last = now
            try:
                self._callback(self._sent, self._total)
            except Exception:  # noqa: BLE001
                pass
        return data

    def __getattr__(self, item):
        return getattr(self._handle, item)


def upload(
    config: PrinterConfig,
    local_file: Path,
    remote_name: str | None = None,
    remote_dir: str = "/",
    progress=None,
) -> str:
    """上传文件，返回打印机侧的文件名。"""
    local_file = local_file.resolve()
    if not local_file.is_file():
        raise SystemExit(f"文件不存在: {local_file}")
    if local_file.suffix.lower() == ".3mf":
        ok, reason = is_sliced_3mf(local_file)
        if not ok:
            raise SystemExit(
                f"不能上传: {reason}\n"
                "  解决: Bambu Studio 打开 → 切片 → 文件 → 导出 → 导出单盘切片文件"
            )

    name = remote_name or local_file.name
    size = local_file.stat().st_size

    ftp = make_ftp(config)
    try:
        if remote_dir not in ("", "/"):
            try:
                ftp.cwd(remote_dir)
            except ftplib.error_perm as exc:
                LOG.debug("进入 %s 失败（多半已在该目录）: %s", remote_dir, exc)
        LOG.info("上传 %s -> %s (%.1f MB)", local_file.name, name, size / 1024 / 1024)
        t0 = time.time()
        with local_file.open("rb") as fh:
            source = _ProgressReader(fh, size, progress) if progress else fh
            ftp.storbinary(f"STOR {name}", source, blocksize=UPLOAD_BLOCK_SIZE)
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
        except Exception:  # noqa: BLE001
            ftp.close()
    return name


def upload_with_retry(
    config: PrinterConfig,
    local_file: Path,
    remote_dir: str = "/",
    attempts: int = 3,
    delay: float = 3.0,
    progress=None,
) -> str:
    """上传失败自动重试（每次重试都是重连）。"""
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return upload(config, local_file, remote_dir=remote_dir,
                          progress=progress)
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt >= attempts:
                break
            print(f"  第 {attempt}/{attempts} 次失败（{type(exc).__name__}: {exc}）")
            print(f"  网络不稳，{delay:.0f} 秒后重试...")
            time.sleep(delay)
            delay *= 1.5

    raise RuntimeError(
        f"重试 {attempts} 次仍失败（最后一次: {type(last).__name__}: {last}）。\n"
        "  网络较慢时可以放宽超时：$env:X1C_UPLOAD_TIMEOUT='300'"
    ) from last
