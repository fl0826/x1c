"""3D 打印助手 —— 命令行入口。

真正的能力在 api.py，这里只负责解析参数、把结果打印成人能看的文字。
想在其他程序里调用，直接 import api。

用法:
    python main.py design  "做个放两节5号电池的槽子"        # 一句话 → STL
    python main.py design  "照着照片做个支架" -i 照片.jpg    # 带照片
    python main.py convert 扫描件.usdz          # USDZ → STL
    python main.py upload  切片.3mf             # 上传到打印机
    python main.py print   切片.3mf             # 上传（如需要）+ 开打
    python main.py print   切片.3mf --ams 0     # 指定 AMS 槽位  四个槽位 0123
    python main.py status                       # 查看打印进度

料在 AMS 里时默认取 0 号槽（0-3 用 --ams 指定）。

design 需要 .env 里配置 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL。
convert 需要额外依赖: conda activate fnn（trimesh + usd-core）。
"""

import logging
import sys
from pathlib import Path

# Windows 控制台默认不是 UTF-8，直接 print 中文/符号会崩
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

# 默认只显示错误；-v 才看底层重连、TLS 等细节
logging.basicConfig(
    level=logging.INFO if any(a in ("-v", "--verbose") for a in sys.argv[1:])
    else logging.ERROR,
    format="%(levelname)-7s %(name)s: %(message)s",
)


# ---------------------------------------------------------------- 各命令


def cmd_convert() -> int:
    """USDZ → STL"""
    args = [a for a in sys.argv[2:] if not a.startswith("-")]
    if not args:
        print("用法: python main.py convert 扫描件.usdz [-o 输出.stl]")
        return 1

    from convert_usdz_to_stl import convert_usdz_to_stl

    path = Path(args[0])
    output = Path(args[1]) if len(args) > 1 else None

    if not path.exists():
        print(f"✗ 文件不存在: {path}")
        return 1
    if path.suffix.lower() != ".usdz":
        print(f"✗ 只支持 .usdz（当前: {path.suffix or '无扩展名'}）")
        return 1

    return 0 if convert_usdz_to_stl(path, output) else 1


def cmd_upload() -> int:
    """上传切片文件"""
    args = [a for a in sys.argv[2:] if not a.startswith("-")]
    if not args:
        print("用法: python main.py upload 切片.3mf")
        return 1

    import api

    result = api.upload_file(args[0])
    if not result.get("ok"):
        print(f"✗ {result.get('error')}")
        if result.get("hint"):
            print(f"  {result['hint']}")
        return 1

    print("=" * 60)
    print("✅ 上传成功")
    print("=" * 60)
    print(f"  文件: {result['name']}")
    print(f"  大小: {result.get('size')} 字节")
    print("\n下一步: python main.py print 同一个文件")
    return 0


def _parse_print_args(argv: list[str]) -> tuple[Path | None, int]:
    """解析 `print` 的参数：文件路径 + --ams 槽位（0-3，默认 0）。"""
    path: Path | None = None
    ams = 0
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--ams":
            if index + 1 >= len(argv):
                print("用法: --ams 0-3，例如 --ams 1")
                return None, 0
            try:
                ams = int(argv[index + 1])
            except ValueError:
                print(f"✗ --ams 需要 0-3 的数字，收到 {argv[index + 1]!r}")
                return None, 0
            if not 0 <= ams <= 3:
                print(f"✗ AMS 槽位只能是 0-3，收到 {ams}")
                return None, 0
            index += 2
            continue
        if not item.startswith("--"):
            path = Path(item)
        index += 1
    return path, ams


def cmd_print() -> int:
    """上传（如需要）+ 开始打印"""
    path, ams = _parse_print_args(sys.argv[2:])
    if path is None:
        print("用法: python main.py print 切片.3mf [--ams 0-3|none]")
        return 1
    if not path.exists():
        print(f"✗ 文件不存在: {path}")
        return 1

    import api

    name = path.name

    # 打印机上没有就先传上去；已有同一份文件就跳过
    info = api.remote_file_info(name)
    if not info.get("connected"):
        print(f"✗ {info.get('error')}")
        print("  确认打印机开机、和电脑在同一网段后再试")
        return 1
    if info.get("size") is None:
        print(f"打印机上没有 {name}，先上传 ...")
        uploaded = api.upload_file(path)
        if not uploaded.get("ok"):
            print(f"✗ {uploaded.get('error')}")
            if uploaded.get("hint"):
                print(f"  {uploaded['hint']}")
            return 1
        print(f"已上传: {uploaded.get('size')} 字节")
    else:
        print(f"打印机上已有: {name}（跳过上传）")

    result = api.start_print(name, ams_slot=ams)
    if not result.get("ok"):
        print(f"✗ {result.get('error')}")
        if result.get("stage") == "busy":
            print(f"  当前任务: {result.get('file')}  {result.get('percent')}%")
        return 1

    if result.get("started"):
        print(f"\n✅ 已开始打印（状态 {result['state']}）")
        print("   用 python main.py status 查看进度")
        return 0

    print(f"\n⚠ {result.get('note')}")
    # 读到过状态却始终没开打 → 是真失败；一次都没读到 → 网络问题，不算失败
    return 0 if result.get("status_reads") == 0 else 1


def cmd_status() -> int:
    """查询打印机状态"""
    import api

    result = api.get_status()
    if not result.get("ok"):
        print(f"\n✗ {result.get('error')}")
        return 1

    print(f"\n打印机 {result.get('printer')}")
    print("=" * 60)
    print(f"状态: {result['state_zh']}")

    if result.get("idle"):
        print("✅ 空闲 —— 可以接受新任务")
    else:
        print("● 忙碌")
        print(f"  进度: {result.get('percent')}%")
        print(f"  层数: {result.get('layer')}/{result.get('total_layers')}")
        print(f"  剩余: {result.get('remaining_minutes')} 分钟")
        print(f"  文件: {result.get('file')}")

    print("\n温度:")
    print(f"  喷嘴: {result['nozzle']}")
    print(f"  热床: {result['bed']}")
    print(f"  腔体: {result['chamber']}")

    if result.get("errors"):
        print(f"\n⚠️  错误: {result['errors']}")
    return 0


def cmd_design() -> int:
    """一句话（+可选照片）→ STL"""
    argv = sys.argv[2:]
    image = None
    words: list[str] = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item in ("-i", "--image"):
            if index + 1 >= len(argv):
                print("用法: -i 后面跟图片路径")
                return 1
            image = argv[index + 1]
            index += 2
            continue
        words.append(item)
        index += 1

    prompt = " ".join(words).strip()
    if not prompt:
        print('用法: python main.py design "做个放两节5号电池的槽子" [-i 照片.jpg]')
        return 1

    import api

    result = api.design_model(prompt, image=image)

    # 缺关键尺寸 → 问一次，拿到答案再继续
    if not result.get("ok") and result.get("need_input"):
        need = result["need_input"]
        print(f"\n需要确认: {need['question']}")
        answers: dict = {}
        fields = need.get("fields") or []
        if not fields:
            fields = ["补充说明"]
        for field in fields:
            try:
                value = input(f"  {field}: ").strip()
            except EOFError:
                return 1
            if value:
                answers[field] = value
        if not answers:
            return 1
        result = api.design_model(prompt, image=image, answers=answers)

    if not result.get("ok"):
        print(f"✗ {result.get('error')}")
        if result.get("message"):
            print(f"  模型说明: {result['message']}")
        return 1

    print("\n" + "=" * 60)
    print("✅ 模型已生成")
    print("=" * 60)
    print(f"  零件: {result['part']}")
    print(f"  参数: {result['params']}")
    print(f"  尺寸: {' × '.join(str(v) for v in result['size_mm'])} mm")
    print(f"  体积: {result.get('volume_cm3')} cm³")
    print(f"  文件: {result['stl']}")
    if result.get("message"):
        print(f"\n{result['message']}")
    print("\n下一步：Bambu Studio 打开该 STL → 切片 → 文件 → 导出 → "
          "导出单盘切片文件")
    print("         然后 python main.py print 切片.gcode.3mf")
    return 0


COMMANDS = {
    "convert": cmd_convert,
    "design": cmd_design,
    "upload": cmd_upload,
    "print": cmd_print,
    "status": cmd_status,
}


def main() -> int:
    argv = [a for a in sys.argv[1:] if a not in ("-v", "--verbose")]
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 1 if not argv else 0

    command = argv[0]
    handler = COMMANDS.get(command)
    if handler is None:
        print(f"未知命令: {command}")
        print(f"可用命令: {', '.join(COMMANDS)}")
        return 1

    sys.argv = [sys.argv[0]] + argv
    return handler()


if __name__ == "__main__":
    sys.exit(main())
