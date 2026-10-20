# 3D 打印助手

一句话（可带照片）→ 模型 → 切片 → 上传 → 打印 → 查进度。

## 环境

```bash
conda activate fnn
```

只有 `convert` 需要 `trimesh` + `usd-core`；其余命令不需要。

## 命令

```bash
python main.py design  "做个放两节5号电池的槽子"        # 一句话 → STL
python main.py design  "照着照片做个支架" -i 照片.jpg    # 带照片
python main.py convert 扫描件.usdz                      # USDZ → STL
python main.py upload  切片.3mf                         # 仅上传
python main.py print   切片.3mf                         # 上传（如需要）+ 开打
python main.py print   切片.3mf --ams 3                 # 指定料槽 0-3
python main.py status                                   # 查看进度
```

加 `-v` 看详细日志。

- `design` 只生成 STL，切片仍需手动。缺关键尺寸时会追问你。
- `-i` 后面跟照片路径，可省略（纯文字也能用）。
- `print` 默认取 AMS **0 号槽**，用 `--ams` 改成 0-3 里的其他槽。
- `print` 会先查状态：正在打印就拒绝；文件已上传就跳过上传。

## 完整流程

1. `python main.py design "做个放两节5号电池的槽子"`
2. Bambu Studio 打开生成的 STL → 切片 → 文件 → 导出 → **导出单盘切片文件**
3. `python main.py print 切片.gcode.3mf`
4. `python main.py status`

只有第 2 步（切片）要手动。

## 配置

`.env`：

```env
# 打印机
X1C_IP=打印机IP
X1C_SERIAL=序列号
X1C_ACCESS_CODE=访问码

# 大模型（design 用，OpenAI 兼容接口）
LLM_BASE_URL=https://kuaipao.pro/v1
LLM_API_KEY=sk-...
LLM_MODEL=qwen3.8-flash
```

IP 和访问码在打印机「设置 → 网络」，序列号在「设置 → 设备」。

## 在别的程序里调用

```python
import api

api.design_model("做个理线夹，线6mm粗")        # → STL
api.upload_file(r"D:\桌面\切片.gcode.3mf")
api.start_print("切片.gcode.3mf", ams_slot=0)
api.get_status()
api.upload_and_print(r"D:\桌面\切片.gcode.3mf")  # 一步到位
```

全部返回 dict（可直接 JSON 给网页用），失败时 `ok=False` 并带 `error`。

## 常见问题

| 现象 | 处理 |
|---|---|
| `缺少依赖: trimesh` | 先 `conda activate fnn` |
| `未配置 LLM_API_KEY` | 在 `.env` 里填 `LLM_API_KEY` |
| design 追问尺寸 | 照片不能量尺寸，按提示给实测值或标准规格 |
| 连不上打印机 | 确认电脑和打印机在同一个 WiFi、打印机已开机 |
| `拒绝连接 (code=5)` | 访问码失效，去屏幕核对后改 `.env` |
| 启动后立刻停，屏幕报「检测到禁用AMS时仍从AMS加载料」 | 料在 AMS 里，用 `--ams 槽号` 指定 |
| 屏幕报「Z轴回零失败」 | 打印机机械问题，手动校准一次；与本工具无关 |
| 上传超时 | 会自动重试 3 次。仍失败就放宽：`$env:X1C_UPLOAD_TIMEOUT='300'` |
| `print` 提示正在打印 | 等它打完；`FAILED` 是终态，会直接继续 |
| 打印中查不到状态 | 网络差时会发生；换网络通常就好 |

## 测试

```bash
conda activate fnn
python -m unittest discover -s tests
```
