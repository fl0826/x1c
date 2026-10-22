# 3D 打印助手

一句话（可带照片）→ STL → 切片 → 上传 → 打印。

## 环境

```bash
conda activate fnn          # 只有转换需要
pip install -r requirements.txt
copy .env.example .env      # 填打印机 IP / 序列号 / 访问码
```

## 命令

```bash
python main.py upload  "切片.3mf"        # 只上传
python main.py print   "切片.3mf"        # 上传 + 打印
python main.py print   "切片.3mf" --ams 3   # 指定料槽 0-3
python main.py status                    # 查进度
python main.py generate "做个放两节5号电池的槽子"   # 一句话 → STL
```

- 切片必须手动，其余自动。
- `print` 默认 0 号槽；正在打印会拒绝；已上传（同名同大小同时间）会跳过上传。
- `generate` 先给方案，按回车确认后才写 STL；只写物品名也行（缺的尺寸用默认值）。

## 网页

```bat
start.bat        :: 网页 + 公网隧道（会打印 https 地址）
start-web.bat    :: 只要局域网
```

打开 http://127.0.0.1:8080，点「书房」那台打印机，控制台下选文件 → 选料槽 → 上传并打印。

切好片的 `.3mf` 放进 `outbox/`，页面 5 秒内自动出现。VR 要 HTTPS，用 `start.bat` 给的地址。

### 设计面板

页面左下角「设计」：写一句描述（可语音、可拍照）→ 生成方案 → 确认 → 点「在电脑上打开」拉起 Bambu Studio。

- 只写物品名即可（如「电池槽」），缺的尺寸用默认值
- 生成约 10~60 秒；确认前不会写文件
- STL 用「零件名 + 日期时间」命名，例如 `理线夹-20261022-152011.stl`
- 零件库：安装底板、钥匙挂钩、理线夹、垫圈、收纳盒、电池槽、杯垫、手机支架
- 语音/相机取决于浏览器，面板上会显示 `语音 ✅/❌ · 相机 ✅/❌`

⚠️ `/api/design` 没有鉴权，公网暴露时别人也能调用（会消耗 LLM 额度、写文件）。只在内网用，或先加 token。

## 转换

```bash
python convert_usdz_to_stl.py 扫描件.usdz     # 手机/Quest 扫描 → STL
```

自动处理单位（米→毫米）、Y 轴转 Z 轴、多边形三角化。

## 完整流程

```
1. 手机/Quest 扫描 → USDZ，或 CAD 导出 STL
2. python convert_usdz_to_stl.py x.usdz       （需要时）
3. Bambu Studio 打开 → 切片 → 文件 → 导出单盘切片文件
4. python main.py print 切片.gcode.3mf
```

## 配置

`.env`：打印机三行 + 大模型三行，注释里写了取值位置。

## 调用

```python
import api

api.design_model("做个理线夹，线6mm粗")        # → STL
api.upload_file(r"D:\桌面\切片.gcode.3mf")
api.start_print("切片.gcode.3mf", ams_slot=0)
api.get_status()
```

全部返回 dict，失败 `ok=False` 且带 `error`。

## 常见问题

| 现象 | 处理 |
|---|---|
| `缺少 trimesh` | `conda activate fnn` |
| `未配置 LLM_API_KEY` | `.env` 里填 |
| 连不上打印机 | 同一 WiFi、打印机开机 |
| `拒绝连接 (code=5)` | 访问码变了，去屏幕核对 |
| 屏幕报「禁用AMS时仍从AMS加载料」 | 加 `--ams 槽号` |
| 上传超时 | 自动重试 3 次；仍失败可设 `X1C_UPLOAD_TIMEOUT=300` |
| 页面显示「重连中」 | 打印时偶发，几秒自愈 |
| 状态显示「空闲（上次中断）」 | 上次任务被打断的状态残留，直接打印即可清除 |

## 测试

```bash
python -m unittest discover -s tests
```
