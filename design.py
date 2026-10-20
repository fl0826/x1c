"""一句话（+ 可选照片）→ STL。

流程：模型只负责"选零件 + 填参数"，几何由 geometry/ 里的代码算，
生成后必须通过校验（封闭、壁厚、尺寸上限）才算成功。

模型需要联网查尺寸时用 web_lookup（Bing + 维基，免 key）；
信息不足（比如线径）必须调 ask_user 提问，不允许猜。
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from bambu_bridge import _read_dotenv
from geometry.parts import CATALOG, build

_HERE = Path(__file__).resolve().parent
OUTPUT_DIR = _HERE / "output"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120 Safari/537.36")

SYSTEM_PROMPT = """你是 3D 打印零件设计助手，帮用户把一个需求变成可打印的 STL。

规则（必须遵守）：
1. 只能使用零件库里的零件，不许自己发明几何或坐标。先调用 list_parts 看有哪些零件和参数。
2. 尺寸来源只能是这三种，按优先级：
   a) 公认标准规格（AA 电池 14.0×50.0mm、M3 螺丝、USB-C 8.25×2.4mm 等）——可直接用，说明依据
   b) 用户明确给出的数字 —— 直接用
   c) 联网查到的公开规格 —— 用 web_lookup 查，并在回答里说明来源
3. 照片只能识别"是什么东西"，不能用来量尺寸。不要从照片估算毫米数。
4. 缺少关键尺寸（线径、孔距、厚度等）时，调用 ask_user 提问，不要自己假设。
5. 参数必须落在零件允许的范围内，不确定就先查/先问。
6. 最后调用 build_part 生成并校验，通过后再简短总结（零件、关键尺寸、注意事项）。
7. 全程用中文，回答简短，不要长篇解释。"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_parts",
            "description": "列出所有可用零件、说明和参数名",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_lookup",
            "description": "联网查某个物体的公开尺寸规格（标准件、接口、常见物品）",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词，如 'USB-C 接口 尺寸 mm'"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": "信息不足时向用户提问（例如线径、孔距）。不要用它问能查到的东西",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "需要用户提供的字段名，如 ['线径_mm']",
                    },
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "build_part",
            "description": "按零件名和参数生成 STL 并校验",
            "parameters": {
                "type": "object",
                "properties": {
                    "part": {"type": "string", "description": "零件名，必须在 list_parts 的结果里"},
                    "params": {"type": "object", "description": "零件参数，键名与 list_parts 一致"},
                    "summary": {"type": "string", "description": "给用户看的一句话说明"},
                },
                "required": ["part", "params"],
            },
        },
    },
]


# ---------------------------------------------------------------- 配置


def llm_config() -> dict[str, str]:
    """从环境变量读，缺了再读同目录 .env。"""
    file_values = _read_dotenv(_HERE / ".env")

    def value(key: str, default: str = "") -> str:
        return os.environ.get(key) or file_values.get(key) or default

    return {
        "base_url": value("LLM_BASE_URL", "https://kuaipao.pro/v1").rstrip("/"),
        "api_key": value("LLM_API_KEY"),
        "model": value("LLM_MODEL", "qwen3.8-flash"),
    }


def _http_json(url: str, payload: dict, headers: dict,
               timeout: float = 120.0) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def chat(messages: list[dict], tools: list[dict] | None = None) -> dict:
    """调一次 chat completions，返回原始响应。"""
    config = llm_config()
    if not config["api_key"]:
        raise RuntimeError("未配置 LLM_API_KEY（写在 .env 里）")

    payload: dict[str, Any] = {
        "model": config["model"],
        "messages": messages,
        "temperature": 0.2,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    return _http_json(
        config["base_url"] + "/chat/completions",
        payload,
        {"Authorization": f"Bearer {config['api_key']}"},
    )


# ---------------------------------------------------------------- 联网查尺寸


def _fetch(url: str, timeout: float = 15.0) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


# 常见标准件直接映射到维基条目：搜索经常命中列表页或相近型号（AA 被搜成 AAA）
WIKI_ALIASES = {
    "aa电池": ("zh", "AA電池"),
    "5号电池": ("zh", "AA電池"),
    "aaa电池": ("zh", "AAA電池"),
    "7号电池": ("zh", "AAA電池"),
    "18650": ("zh", "18650型鋰電池"),
    "cr2032": ("zh", "鈕扣電池"),
    "aa battery": ("en", "AA battery"),
    "aaa battery": ("en", "AAA battery"),
    "usb-c": ("zh", "USB Type-C"),
    "usbc": ("zh", "USB Type-C"),
    "usb type-c": ("zh", "USB Type-C"),
}

SIZE_PATTERN = re.compile(r"\d+(\.\d+)?\s*(mm|毫米|cm|公分|millimet)")

# 搜索结果里必须出现尺寸，否则对"查规格"没用
UNIT_PATTERN = re.compile(r"\d+(\.\d+)?\s*(mm|毫米|cm|公分|millimet)")


def _size_snippet(text: str, window: int = 150) -> str | None:
    """从长正文里挑"尺寸最密集"的那一段。

    规格句通常一次给出多个毫米数（"直徑為10.5 mm，長度為44.5 mm"），
    所以按窗口内的尺寸数量挑，而不是取第一个匹配（容易抓到年份）。
    """
    matches = list(SIZE_PATTERN.finditer(text))
    if not matches:
        return None

    best_score = -1.0
    best_start = 0
    for match in matches:
        start = max(0, match.start() - window)
        end = min(len(text), match.end() + window)
        chunk = text[start:end]
        count = len(SIZE_PATTERN.findall(chunk))
        # 尺寸越集中越好；同样多的时候取靠前的
        score = count + (1.0 if count >= 2 else 0.0) - start / 1e9
        if score > best_score:
            best_score = score
            best_start = start

    end = min(len(text), best_start + window * 3)
    return text[best_start:end].replace("\n", " ").strip()


def _wiki_page(lang: str, title: str, want_size: bool = True) -> dict | None:
    """取维基条目。want_size=True 时优先返回含尺寸的那段正文。"""
    try:
        quote = urllib.parse.quote(title.replace(" ", "_"))
        data = json.loads(_fetch(
            f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{quote}"))
        intro = (data.get("extract") or "").strip()
        url = data.get("content_urls", {}).get("desktop", {}).get("page", "")
        text = intro
        if want_size:
            # 摘要通常不含尺寸，去正文里找
            full = json.loads(_fetch(
                f"https://{lang}.wikipedia.org/w/api.php?action=query"
                f"&prop=extracts&explaintext=1&format=json&titles={quote}"))
            pages = (full.get("query") or {}).get("pages") or {}
            body = next((p.get("extract") or "" for p in pages.values()), "")
            snippet = _size_snippet(body or intro)
            if snippet:
                text = snippet
        if not text:
            return None
        return {
            "source": f"维基百科/{lang}",
            "title": data.get("title", title),
            "text": text[:900],
            "url": url,
        }
    except Exception:  # noqa: BLE001
        return None


def _wikipedia(query: str) -> dict | None:
    """维基百科规格。优先用别名表精确定位，再退回搜索。"""
    key = query.lower().replace(" ", "")
    for alias, (lang, title) in WIKI_ALIASES.items():
        if alias.replace(" ", "") in key:
            page = _wiki_page(lang, title)
            if page:
                return page

    for lang in ("zh", "en"):
        try:
            url = (
                f"https://{lang}.wikipedia.org/w/api.php?action=query"
                "&generator=search&gsrlimit=4&prop=extracts"
                "&exintro=1&explaintext=1&format=json&gsrsearch="
                + urllib.parse.quote(query)
            )
            pages = (json.loads(_fetch(url)).get("query") or {}).get("pages") or {}
            candidates = []
            for page in pages.values():
                text = (page.get("extract") or "").strip()
                if not text:
                    continue
                # 必须带尺寸数字，否则多半是列表页/概述页
                if not SIZE_PATTERN.search(text):
                    continue
                candidates.append((page.get("title", ""), text))
            if not candidates:
                continue
            title, text = candidates[0]
            return {
                "source": f"维基百科/{lang}",
                "title": title,
                "text": text[:900],
                "url": f"https://{lang}.wikipedia.org/wiki/"
                       + urllib.parse.quote(title.replace(" ", "_")),
            }
        except Exception:  # noqa: BLE001
            continue
    return None


def _bing(query: str) -> list[dict]:
    """Bing 搜索结果前几条（免 key）。

    必须带地区参数（否则按出口 IP 返回日文页），
    并且只保留含尺寸数字的条目。
    """
    url = ("https://www.bing.com/search?q=" + urllib.parse.quote(query)
           + "&mkt=zh-CN&setlang=zh-hans&cc=cn")
    html = _fetch(url)
    results: list[dict] = []
    for block in re.findall(r'<li class="b_algo".*?</li>', html, re.S)[:8]:
        title = re.search(r"<h2[^>]*>(?:<a[^>]*>)?(.*?)</(?:a|h2)>", block, re.S)
        snippet = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        text = _clean(snippet.group(1)) if snippet else ""
        if not text or not UNIT_PATTERN.search(text):
            continue
        results.append({
            "title": _clean(title.group(1)) if title else "",
            "text": text,
        })
    return results[:3]


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def web_lookup(query: str) -> dict:
    """查尺寸规格：维基优先，再用 Bing 补。"""
    wiki = _wikipedia(query)
    bing = []
    try:
        bing = _bing(query)
    except Exception:  # noqa: BLE001
        pass

    parts: list[str] = []
    if wiki:
        parts.append(f"[{wiki['source']}] {wiki['title']}: {wiki['text']}")
    for item in bing:
        parts.append(f"[Bing] {item['title']}: {item['text']}")

    if not parts:
        return {"ok": False, "text": "没查到结果，请让用户提供尺寸"}
    return {
        "ok": True,
        "text": "\n\n".join(parts)[:2200],
        "sources": [wiki["url"]] if wiki else [],
    }


# ---------------------------------------------------------------- 生成


def _build(part: str, params: dict) -> dict:
    """生成 STL 并校验。"""
    if part not in CATALOG:
        return {
            "ok": False,
            "error": f"没有零件 {part!r}，可选: {', '.join(CATALOG)}",
        }

    spec = CATALOG[part]
    unknown = [key for key in params if key not in spec.params]
    if unknown:
        return {
            "ok": False,
            "error": f"{part} 不认识参数 {unknown}，可用: {list(spec.params)}",
        }

    try:
        mesh = build(part, **params)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"生成失败: {exc}"}

    if isinstance(mesh, tuple):   # 有些零件返回多个网格（如盒身+盒盖）
        mesh = mesh[0]

    problems = mesh.validate()
    size = [round(value, 1) for value in mesh.size()]
    if problems:
        return {"ok": False, "error": "校验未通过: " + "; ".join(problems),
                "size_mm": size}

    if max(size) > 256:
        return {"ok": False,
                "error": f"尺寸 {size} 超过 X1C 的 256mm 打印范围",
                "size_mm": size}

    OUTPUT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%H%M%S")
    path = OUTPUT_DIR / f"{part}_{stamp}.stl"
    mesh.to_stl_binary(path)
    return {
        "ok": True,
        "stl": str(path),
        "part": part,
        "params": params,
        "size_mm": size,
        "volume_cm3": round(mesh.volume() / 1000, 2),
    }


# ---------------------------------------------------------------- 编排


def _user_content(prompt: str, image: str | Path | None) -> Any:
    if not image:
        return prompt
    path = Path(image)
    mime = {".png": "image/png", ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(
        path.suffix.lower(), "image/jpeg")
    encoded = base64.b64encode(path.read_bytes()).decode()
    return [
        {"type": "text", "text": prompt},
        {"type": "image_url",
         "image_url": {"url": f"data:{mime};base64,{encoded}"}},
    ]


def design_model(prompt: str, image: str | Path | None = None,
                 answers: dict | None = None, max_rounds: int = 8) -> dict:
    """一句话（+可选照片）→ STL。

    缺信息时返回 {"ok": False, "need_input": {...}}，
    调用方把用户答案通过 answers 再传进来即可继续。
    """
    if not prompt.strip():
        return {"ok": False, "error": "需求不能为空"}

    text = prompt
    if answers:
        filled = "；".join(f"{key}={value}" for key, value in answers.items())
        text = f"{prompt}\n\n（用户补充：{filled}）"

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _user_content(text, image)},
    ]

    built: dict | None = None
    last_text = ""
    for _round in range(max_rounds):
        try:
            response = chat(messages, TOOLS)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"调用模型失败: {exc}"}

        message = (response.get("choices") or [{}])[0].get("message") or {}
        calls = message.get("tool_calls") or []
        messages.append(message)

        if not calls:
            last_text = (message.get("content") or "").strip()
            break

        for call in calls:
            name = call.get("function", {}).get("name")
            try:
                args = json.loads(call.get("function", {}).get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}

            if name == "list_parts":
                result: dict = {
                    "ok": True,
                    "parts": {
                        key: {"description": spec.description,
                              "params": spec.params}
                        for key, spec in CATALOG.items()
                    },
                }
            elif name == "web_lookup":
                result = web_lookup(str(args.get("query", "")))
            elif name == "ask_user":
                return {
                    "ok": False,
                    "need_input": {
                        "question": args.get("question", "还需要一些信息"),
                        "fields": args.get("fields") or [],
                    },
                }
            elif name == "build_part":
                result = _build(str(args.get("part", "")),
                                args.get("params") or {})
                if result.get("ok"):
                    built = result
                    built["summary"] = args.get("summary", "")
            else:
                result = {"ok": False, "error": f"未知工具 {name}"}

            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id", ""),
                "content": json.dumps(result, ensure_ascii=False),
            })

    if not built:
        return {
            "ok": False,
            "error": "模型没有生成可用的零件",
            "message": last_text,
        }

    built["ok"] = True
    built["message"] = last_text
    return built
