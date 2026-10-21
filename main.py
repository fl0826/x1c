"""3D打印助手 - 核心功能

功能：
1. 上传 3MF 文件到打印机
2. 上传并自动打印
3. 查询打印机状态
4. 大模型生成 3D 模型

用法：
    # 只上传
    python main.py upload 文件.3mf
    
    # 上传+打印
    python main.py print 文件.3mf
    python main.py print 文件.3mf --ams 1
    
    # 查询状态
    python main.py status
    
    # AI 生成（实验性）
    python main.py generate "做个手机支架"
"""

import sys
from pathlib import Path

# Windows 控制台编码（幂等：重复 import 也不会崩）
if sys.platform == 'win32':
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))


def cmd_upload():
    """上传 3MF 文件到打印机（根目录，和打印指令一致）"""
    if len(sys.argv) < 3:
        print("用法: python main.py upload 文件.3mf")
        return 1

    file_path = Path(sys.argv[2])
    if not file_path.exists():
        print(f"❌ 文件不存在: {file_path}")
        return 1
    if file_path.suffix.lower() != ".3mf":
        print("❌ 只支持 .3mf 文件")
        return 1

    import api

    print(f"\n上传: {file_path.name}")
    print("=" * 60)

    result = api.upload_file(file_path)
    if not result.get("ok"):
        print(f"\n❌ {result.get('error')}")
        if result.get("hint"):
            print(f"   {result['hint']}")
        return 1

    print("\n" + "=" * 60)
    print("✅ 上传成功")
    print("=" * 60)
    print(f"  文件: {result['name']}")
    print(f"  大小: {(result.get('size') or 0) / 1024 / 1024:.2f} MB")
    print("\n下一步: python main.py print 同名文件")
    return 0


def cmd_status():
    """查询打印机状态"""
    import api

    result = api.get_status()
    print(f"\n查询打印机: {result.get('printer', '?')}")
    print("=" * 60)

    if not result.get("ok"):
        print(f"\n❌ {result.get('error')}")
        return 1

    print(f"\n状态: {result['state_zh']}")
    print(f"文件: {result.get('file') or '—'}")
    print(f"进度: {result.get('percent')}%   "
          f"层: {result.get('layer')}/{result.get('total_layers')}   "
          f"剩余: {result.get('remaining_minutes')} 分")
    print(f"喷嘴: {result['nozzle']}")
    print(f"热床: {result['bed']}")
    print(f"腔体: {result['chamber']}")

    ams = result.get("ams") or []
    if ams:
        print("\nAMS:")
        for slot in ams:
            kind = slot.get("type") or "空"
            print(f"  槽 {slot['slot']}: {kind}")

    if result.get("errors"):
        print(f"\n⚠ 错误: {result['errors']}")
    return 0


def cmd_print():
    """上传并打印"""
    if len(sys.argv) < 3:
        print("用法: python main.py print 文件.3mf [--ams 槽位]")
        return 1

    file_path = Path(sys.argv[2])
    if not file_path.exists():
        print(f"❌ 文件不存在: {file_path}")
        return 1
    if file_path.suffix.lower() != ".3mf":
        print("❌ 只支持 .3mf 文件")
        return 1

    ams_slot = 0
    if "--ams" in sys.argv:
        try:
            ams_slot = int(sys.argv[sys.argv.index("--ams") + 1])
        except (IndexError, ValueError):
            print("❌ --ams 格式错误，应为: --ams 0")
            return 1

    import api

    print(f"\n上传并打印: {file_path.name}   (AMS 槽位 {ams_slot})")
    print("=" * 60)
    result = api.upload_and_print(file_path, ams_slot=ams_slot)

    upload = result.get("upload") or {}
    printed = result.get("print") or {}

    if not upload.get("ok"):
        print(f"❌ 上传失败: {upload.get('error')}")
        return 1
    print(f"✅ 已上传: {upload.get('name')} "
          f"({(upload.get('size') or 0) / 1024 / 1024:.2f} MB)")

    if printed.get("started"):
        print(f"✅ 已开始打印（状态 {printed.get('state')}）")
        print("   查看进度: python main.py status")
        return 0

    if printed.get("ok"):
        print(f"⚠ 指令已发出但未确认: {printed.get('note', '')}")
        print("   稍后用 python main.py status 确认")
        return 0

    print(f"❌ 未开始打印: {printed.get('error')}")
    if printed.get("stage") == "busy":
        print(f"   打印机正在打印: {printed.get('file')} "
              f"({printed.get('percent')}%)")
    return 1


def cmd_generate():
    """大模型生成 3D 模型"""
    if len(sys.argv) < 3:
        print("用法: python main.py generate \"描述\"")
        return 1
    
    prompt = sys.argv[2]
    
    import api
    
    try:
        print(f"\n生成需求: {prompt}")
        print("=" * 60)
        print("调用 AI 模型...")
        
        result = api.design_model(prompt)
        
        if not result.get("ok"):
            if "need_input" in result:
                need = result["need_input"]
                print(f"\n❓ {need.get('question', '需要更多信息')}")
                print(f"\n缺少参数: {', '.join(need.get('fields', []))}")
                return 1
            
            print(f"\n❌ 生成失败: {result.get('error', '未知错误')}")
            return 1
        
        print("\n" + "=" * 60)
        print("✅ 生成成功!")
        print("=" * 60)
        print(f"  零件: {result.get('part')}")
        print(f"  文件: {result.get('stl')}")
        print(f"  尺寸: {' × '.join(map(str, result.get('size_mm', [])))} mm")
        print(f"  体积: {result.get('volume_cm3', 0)} cm³")
        
        if result.get("message"):
            print(f"\n{result['message']}")
        
        print(f"\n下一步:")
        print(f"  1. 用 Bambu Studio 打开 {result.get('stl')}")
        print(f"  2. 切片并导出 .3mf")
        print(f"  3. python main.py print 文件.3mf")
        
        return 0
        
    except Exception as e:
        print(f"\n❌ 失败: {e}")
        return 1


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    
    command = sys.argv[1]
    
    if command == "upload":
        return cmd_upload()
    elif command == "print":
        return cmd_print()
    elif command == "status":
        return cmd_status()
    elif command == "generate":
        return cmd_generate()
    else:
        print(f"未知命令: {command}")
        print("\n可用命令: upload, print, status, generate")
        return 1


if __name__ == "__main__":
    sys.exit(main())
