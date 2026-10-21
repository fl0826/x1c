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
    """上传 3MF 文件到打印机"""
    if len(sys.argv) < 3:
        print("用法: python main.py upload 文件.3mf")
        return 1
    
    file_path = Path(sys.argv[2])
    if not file_path.exists():
        print(f"❌ 文件不存在: {file_path}")
        return 1
    
    if file_path.suffix.lower() != ".3mf":
        print(f"❌ 只支持 .3mf 文件")
        return 1
    
    from bambu_bridge import PrinterConfig
    from upload_and_print import upload
    
    try:
        print(f"\n上传: {file_path.name}")
        print("=" * 60)
        
        config = PrinterConfig.from_env()
        print(f"目标打印机: {config.ip}")
        
        remote_name = upload(config, file_path, remote_dir="/cache")
        
        print("\n" + "=" * 60)
        print("✅ 上传成功!")
        print("=" * 60)
        print(f"  文件: {remote_name}")
        print(f"  位置: /cache")
        print("\n下一步: 打印机屏幕选择文件开始打印")
        return 0
        
    except Exception as e:
        print(f"\n❌ 上传失败: {e}")
        return 1


def cmd_status():
    """查询打印机状态"""
    from bambu_bridge import X1CBridge, PrinterConfig, GCODE_STATE_ZH
    
    try:
        config = PrinterConfig.from_env()
        print(f"\n查询打印机: {config.ip}")
        print("=" * 60)
        
        bridge = X1CBridge(config, allow_real_actions=False)
        
        if not bridge.connect(wait=True, require_subscribe=True, retries=2):
            print("❌ 连接失败")
            if bridge.connack_code is not None:
                print(f"   打印机拒绝连接 (code={bridge.connack_code})")
                if bridge.connack_code in (4, 5):
                    print("   可能是访问码不对或已过期")
            else:
                print("   网络超时或打印机未响应")
            return 1
        
        # 等待状态推送
        print("等待状态数据...")
        import time
        time.sleep(2)
        
        state = bridge.state
        info = state.print_info
        
        if not info:
            print("❌ 未收到状态数据")
            print("   检查序列号是否正确")
            bridge.disconnect()
            return 1
        
        # 显示状态
        print("\n" + "=" * 60)
        print("打印机状态")
        print("=" * 60)
        
        gcode_state = state.gcode_state
        state_zh = GCODE_STATE_ZH.get(gcode_state or "", gcode_state)
        
        print(f"\n状态: {state_zh}")
        
        if gcode_state in ("IDLE", "FINISH", None):
            print("✅ 空闲 - 可以接受新任务")
        else:
            print(f"● 忙碌")
            print(f"  进度: {state.progress_percent}%")
            print(f"  层数: {state.layer_num}/{state.total_layers}")
            print(f"  剩余: {state.remaining_minutes} 分钟")
            print(f"  文件: {state.current_file}")
        
        print(f"\n温度:")
        print(f"  喷嘴: {state.nozzle_temp}°C / {state.nozzle_target}°C")
        print(f"  热床: {state.bed_temp}°C / {state.bed_target}°C")
        print(f"  腔体: {state.chamber_temp}°C")
        
        if state.hms_errors:
            print(f"\n⚠️  错误: {state.hms_errors}")
        
        bridge.disconnect()
        return 0
        
    except Exception as e:
        print(f"\n❌ 查询失败: {e}")
        return 1


def cmd_print():
    """上传并自动打印"""
    if len(sys.argv) < 3:
        print("用法: python main.py print 文件.3mf [--ams 槽位]")
        return 1
    
    file_path = Path(sys.argv[2])
    if not file_path.exists():
        print(f"❌ 文件不存在: {file_path}")
        return 1
    
    if file_path.suffix.lower() != ".3mf":
        print(f"❌ 只支持 .3mf 文件")
        return 1
    
    # 解析 AMS 槽位参数
    ams_slot = 0
    if "--ams" in sys.argv:
        try:
            idx = sys.argv.index("--ams")
            ams_slot = int(sys.argv[idx + 1])
        except (IndexError, ValueError):
            print("❌ --ams 参数格式错误，应为: --ams 0")
            return 1
    
    import api
    
    try:
        print(f"\n上传并打印: {file_path.name}")
        print("=" * 60)
        print(f"AMS 槽位: {ams_slot}")
        print()
        
        result = api.upload_and_print(file_path, ams_slot=ams_slot)
        
        if not result.get("ok"):
            print("\n❌ 失败!")
            print("=" * 60)
            
            if result.get("stage") == "upload":
                upload = result.get("upload", {})
                print(f"上传失败: {upload.get('error', '未知错误')}")
            else:
                upload = result.get("upload", {})
                print_result = result.get("print", {})
                print(f"✅ 上传成功: {upload.get('name')}")
                print(f"❌ 打印失败: {print_result.get('error', '未知错误')}")
                
                if print_result.get("stage") == "busy":
                    print(f"\n打印机正在打印: {print_result.get('file')}")
                    print(f"进度: {print_result.get('percent')}%")
                    print("\n请等待当前任务完成后再试")
            
            return 1
        
        upload = result.get("upload", {})
        print_result = result.get("print", {})
        
        print("\n" + "=" * 60)
        print("✅ 上传成功!")
        print("=" * 60)
        print(f"  文件: {upload.get('name')}")
        print(f"  大小: {upload.get('size', 0) / 1024 / 1024:.1f} MB")
        
        if print_result.get("started"):
            print("\n" + "=" * 60)
            print("✅ 打印已开始!")
            print("=" * 60)
            print(f"  状态: {print_result.get('state', 'RUNNING')}")
            print(f"\n查看进度: python main.py status")
        else:
            print("\n⚠️  打印指令已发送，但未确认启动")
            print(f"   {print_result.get('note', '')}")
            print(f"\n稍后查看: python main.py status")
        
        return 0
        
    except Exception as e:
        print(f"\n❌ 失败: {e}")
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
