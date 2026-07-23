"""OCR 引擎抽象层：Moonshot 云端 + PaddleOCR 本地双引擎。

提供统一的 recognize(image_bytes, filename) → {"success": bool, "items": [...]} 接口，
通过 get_ocr_engine(name) 工厂函数获取引擎实例（单例缓存）。
"""

import os
import sys
import io
import re
import json
import base64
import logging
from abc import ABC, abstractmethod
import openai  # 提到顶层,避免 except 子句引用 _openai 模块时 UnboundLocalError

# Windows DLL fix: torch's shm.dll needs its lib directory on the DLL search path.
# Must be called BEFORE any torch import (which happens transitively via PaddleOCR).
if sys.platform == 'win32':
    _torch_lib = os.path.join(os.path.dirname(sys.executable),
                              'Lib', 'site-packages', 'torch', 'lib')
    if os.path.isdir(_torch_lib):
        os.add_dll_directory(_torch_lib)
    os.add_dll_directory(os.path.dirname(sys.executable))

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════
# 知识库：从数据库加载产品名（按长度降序，优先匹配长名）
# ═══════════════════════════════════════════════════════════════════

_product_names_cache = None


def _load_product_names():
    """一次性加载所有产品名，按长度降序排列，确保"7P环保杂胶"优先于"杂胶"。"""
    global _product_names_cache
    if _product_names_cache is not None:
        return _product_names_cache
    try:
        from models._db import get_db
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT product_name FROM product "
            "WHERE product_name IS NOT NULL AND product_name != '' "
            "ORDER BY LENGTH(product_name) DESC"
        )
        _product_names_cache = [row[0] for row in cur.fetchall()]
        conn.close()
    except Exception:
        _product_names_cache = []
    return _product_names_cache


# ═══════════════════════════════════════════════════════════════════
# PaddleOCR 后处理：文本行 → 结构化 JSON
# ═══════════════════════════════════════════════════════════════════

# 单位映射表（OCR 可能输出各种变体）
_UNIT_MAP = {
    '支': '支', '个': '支', '只': '支',
    'y': 'y', 'Y': 'y', '码': 'y',
    'kg': 'kg', 'KG': 'kg', 'Kg': 'kg',
    '桶': '桶', '件': '件', '箱': '箱',
    '张': '张', '块': '块', '卷': '卷', '令': '令', '斤': '斤',
}

# 构建单位关键词正则（按长度降序避免短关键词抢匹）
_UNIT_KEYS = sorted(_UNIT_MAP.keys(), key=len, reverse=True)
_QTY_UNIT_RE = re.compile(
    r'(\d+(?:\.\d+)?)\s*(' + '|'.join(re.escape(k) for k in _UNIT_KEYS) + r')(?!\w)'
)

# 规格特征：含测量值、色号、材质
_SPEC_INDICATOR_RE = re.compile(
    r'\d+\.?\d*\s*[×xX\*]\s*\d+'          # 1.2×1.8m
    r'|\d+\.?\d*\s*[mM]\b'                 # 2m
    r'|\d+\s*[gG]\b'                       # 50g
    r'|\d+[#＃]\b'                          # 014#
    r'|[\d.]+(?:mm|cm|kg|Y|y)\b'           # 15kg, 100Y
    r'|\b[黑红蓝绿黄白杏米棕灰金银][色]?'    # 颜色
    r'|\b(?:软|硬|中|加面|双|单|消光|亮光|中性|粗|网)\b'  # 材质手感
)


def _extract_lines(ocr_result):
    """从 PaddleOCR 结果中提取文本行，返回 [(text, y_center, x_min), ...]。

    PaddleOCR 3.x 返回格式: [page_results]
    每个 page_result 是 [[[bbox], [text, confidence]], ...]
    """
    if not ocr_result or not ocr_result[0]:
        return []
    lines = []
    for item in ocr_result[0]:
        if not item or len(item) < 2:
            continue
        bbox = item[0]          # [[x0,y0],[x1,y1],[x2,y2],[x3,y3]]
        text = item[1][0]       # 识别的文本
        if not text or not text.strip():
            continue
        ys = [p[1] for p in bbox]
        xs = [p[0] for p in bbox]
        y_center = sum(ys) / len(ys)
        x_min = min(xs)
        lines.append((text.strip(), y_center, x_min))
    # 按 Y 坐标（上到下）、X 坐标（左到右）排序
    lines.sort(key=lambda t: (t[1], t[2]))
    return lines


def _group_into_rows(lines, y_threshold=30):
    """将同行的文本片段拼接。Y 坐标差距 < y_threshold 视为同行。"""
    if not lines:
        return []
    rows = []
    current_row = [lines[0]]
    current_y = lines[0][1]
    for item in lines[1:]:
        _, y, _ = item
        if abs(y - current_y) < y_threshold:
            current_row.append(item)
        else:
            current_row.sort(key=lambda t: t[2])
            rows.append(' '.join(t[0] for t in current_row if t[0]))
            current_row = [item]
            current_y = y
    # 最后一行
    current_row.sort(key=lambda t: t[2])
    rows.append(' '.join(t[0] for t in current_row if t[0]))
    return rows


def _parse_row(row_text):
    """解析单行文本：找数量+单位锚点 → 切三段。

    返回 None 表示该行没有可识别的数量+单位。
    """
    matches = list(_QTY_UNIT_RE.finditer(row_text))
    if not matches:
        return None
    # 取最后一个匹配（数量通常在行尾）
    last = matches[-1]
    quantity = last.group(1)
    unit_raw = last.group(2)
    unit = _UNIT_MAP.get(unit_raw, unit_raw)
    before = row_text[:last.start()].strip()
    after = row_text[last.end():].strip()
    # 清理 before 中的行号前缀
    before = re.sub(r'^[\d.、，,;；\s]+', '', before)
    # 清理 remark 尾部干扰
    after = after.strip('，,。.·-— ')
    return {'pre_text': before, 'quantity': quantity, 'unit': unit, 'remark': after}


def _split_name_spec(pre_text, product_names):
    """从 OCR 文本中拆出品名和规格。

    策略（优先级降序）：
    1. 精确匹配已知产品名（最长优先，已由 product_names 排序保证）
    2. 规格特征正则：找到第一个规格特征，之前为品名、之后为规格
    3. 兜底：整段当品名
    """
    if not pre_text:
        return '未知商品', ''
    pre_lower = pre_text.lower()

    # 策略 1: 子串匹配（最长优先）
    for name in product_names:
        name_lower = name.lower()
        if name_lower in pre_lower:
            spec = pre_lower.replace(name_lower, '', 1).strip()
            spec = spec.strip('- _/、，,·')
            return name, spec or ''

    # 策略 2: 规格特征
    spec_match = _SPEC_INDICATOR_RE.search(pre_text)
    if spec_match:
        name_part = pre_text[:spec_match.start()].strip()
        spec_part = pre_text[spec_match.start():].strip()
        return name_part or '未知商品', spec_part

    # 策略 3: 兜底
    return pre_text, ''


def parse_paddleocr_result(ocr_result):
    """PaddleOCR 原始结果 → 结构化商品列表。

    返回 [{"product_name": ..., "specification": ..., "quantity": ..., "unit": ..., "remark": ...}, ...]
    """
    product_names = _load_product_names()
    lines = _extract_lines(ocr_result)
    rows = _group_into_rows(lines)

    items = []
    for row_text in rows:
        if not row_text:
            continue
        parsed = _parse_row(row_text)
        if parsed is None:
            continue
        prod_name, spec = _split_name_spec(parsed['pre_text'], product_names)
        items.append({
            'product_name': prod_name,
            'specification': spec,
            'quantity': parsed['quantity'],
            'unit': parsed['unit'],
            'remark': parsed['remark']
        })
    return items


# ═══════════════════════════════════════════════════════════════════
# 共享后处理：汇总行二次过滤（安全网）
# ═══════════════════════════════════════════════════════════════════

# 汇总行关键词 — 匹配到任意一个即判定为汇总行
_SUMMARY_KEYWORDS_RE = re.compile(
    r'合计|总计|小计|总数量|总金额|总共|共\d|^共|^总|^TOTAL|^SUM|^Grand|^Subtotal|'
    r'累计|汇总|合\s*计|总\s*计|^\d+\s*[支yYkg件箱桶张块]\s*$'  # 纯"150支"格式
)

# 汇总行出现在 remark 中的模式（"共150支"被塞进备注）
_SUMMARY_IN_REMARK_RE = re.compile(
    r'^(合计|总计|小计|总数量|总金额|总共|共\d+|TOTAL|SUM)[：:\s]*\d*'
)


def _filter_summary_items(items):
    """后处理安全网：过滤 LLM 未能跳过的汇总行。

    分两步：
    1. 标记明确的汇总行（product_name 匹配关键词，或只有数量+单位没有品名特征）
    2. 清理 remark 中被误填的汇总数据

    Returns:
        (filtered_items, removed_count)
    """
    if not items:
        return items, 0

    # 计算中位数数量，用于检测异常大的汇总值
    quantities = []
    for it in items:
        try:
            q = float(it.get('quantity', 0))
            if q > 0:
                quantities.append(q)
        except (ValueError, TypeError):
            pass

    if quantities:
        quantities.sort()
        n = len(quantities)
        median_qty = quantities[n // 2] if n > 0 else 0
        # 阈值：超过中位数的 3 倍 且 超过 50，可能是汇总行
        outlier_threshold = max(median_qty * 3, 50)
    else:
        outlier_threshold = float('inf')

    clean = []
    removed = 0

    for it in items:
        name = (it.get('product_name') or '').strip()
        spec = (it.get('specification') or '').strip()
        remark = (it.get('remark') or '').strip()
        qty_str = (it.get('quantity') or '0').strip()

        # ── 检测 1: product_name 匹配汇总关键词 ──
        if _SUMMARY_KEYWORDS_RE.search(name):
            removed += 1
            continue

        # ── 检测 2: 无品名特征（空名或纯数字+单位） ──
        if not name or re.match(r'^[\d.,]+\s*[支yYkg件箱桶张块个]?$', name):
            removed += 1
            continue

        # ── 检测 3: 数量异常大（可能是汇总值）─
        try:
            qty_val = float(qty_str)
        except (ValueError, TypeError):
            qty_val = 0

        if qty_val > outlier_threshold and len(clean) > 0:
            # 数量远大于中位数，且不是第一个商品 → 很可能是汇总行
            removed += 1
            continue

        # ── 检测 4: remark 被填入了汇总数据 ──
        if _SUMMARY_IN_REMARK_RE.search(remark):
            # 把汇总数据从 remark 清掉，保留行
            it['remark'] = re.sub(
                r'^(合计|总计|小计|总数量|总金额|总共|共\d+|TOTAL|SUM)[：:\s]*\d*\s*[支yYkg件箱桶张块个]?\s*$',
                '', remark
            ).strip()
            # 清完后如果变空了就保持空
            if not it['remark']:
                it['remark'] = ''

        # ── 检测 5: specification 里只有"共X支"之类的汇总文本 ──
        if _SUMMARY_KEYWORDS_RE.search(spec):
            it['specification'] = ''
            # 不删除行，只清规格

        clean.append(it)

    return clean, removed


# ═══════════════════════════════════════════════════════════════════
# 引擎接口
# ═══════════════════════════════════════════════════════════════════

class BaseOCREngine(ABC):
    """OCR 引擎基类。"""

    @abstractmethod
    def recognize(self, image_bytes, filename=''):
        """返回 {"success": bool, "items": [...], "error": str, "hint": str}"""
        ...


# ═══════════════════════════════════════════════════════════════════
# Moonshot 云端引擎（从 shipping.py 抽取）
# ═══════════════════════════════════════════════════════════════════

class MoonshotEngine(BaseOCREngine):
    """Moonshot Kimi k2.6 Vision API 引擎。"""

    API_KEY = os.environ.get('MOONSHOT_API_KEY', '')
    BASE_URL = 'https://api.moonshot.cn/v1'
    MODEL = 'kimi-k2.6'
    MAX_TOKENS = 4000
    TEMPERATURE = 1  # Kimi k2.6 锁死 temperature 必须为 1
    TIMEOUT = 60

    PROMPT = (
        '你是一个商品信息提取助手。请仔细看这张图片，识别其中的商品列表。\n'
        '图片中可能包含出货单、送货单、报价单、库存表、装车单等。\n'
        '请提取所有商品行，返回一个 JSON 对象，包含以下字段：\n'
        '- doc_number: 单据编号 — 仔细找图片里的"订单号/单据编号/装车单号/单号/编号/票号/流水号/出库单号/Order No/Doc No/PO#/"等标识,'
        '通常在单据顶部/标题/右上角,无则空字符串\n'
        '- customer_name: 客户名称 — 客户字段经常出现在单据抬头/顶部,前面常带"客户/Customer/收货单位/购货单位/客户名称/收货人/收件人/客户单位/收货方/购货方/抬头"等标签,'
        '也可能是一个独立的单位/公司名(没有显式标签,但出现在抬头第一行/左上角)。'
        '**务必把单据抬头的单位/公司名提取出来** — 即使没有明确标签, 只要在标题/抬头/左上角出现的公司名就算。'
        '**注意区分**: 商品行右侧/下方的"备注/购货单位"等不算客户, 抬头第一行/左上角的单位/公司名才是客户。'
        '提取后去掉"客户:"等标签前缀,只保留公司名本身。无则空字符串。\n'
        '- items: 商品列表数组，每条商品包含：\n'
        '  - product_name: 商品名称（必须）\n'
        '  - specification: 规格型号（必须，无则空字符串）\n'
        '  - quantity: 数量（必须）\n'
        '  - unit: 单位（必须）\n'
        '  - remark: 备注（必须填写！仔细看图片中该商品行右侧、下方、括号内是否有手写备注、'
        '批次号、仓位号、特殊要求等。确实没有才填空字符串）\n'
        '\n'
        '单位标准化：\n'
        '  "支"/"PCS"/"pc"/"个"/"只" → "支"\n'
        '  "码"/"y"/"Y"/"YDS" → "y"\n'
        '  "桶" → "桶"、"件" → "件"、"箱" → "箱"、"张" → "张"、"kg" → "kg"、"块" → "块"\n'
        '\n'
        '════════════════════════════════════════\n'
        '【关键规则 — 汇总行识别与跳过】\n'
        '════════════════════════════════════════\n'
        '以下特征的行是汇总/合计行，必须跳过，不能作为商品行，也不能把它的数字填入任何商品的 remark：\n'
        '  特征1: 包含"合计"/"总计"/"小计"/"共"/"总共"/"总数量"/"总金额"/"sum"/"total"等关键词\n'
        '  特征2: 出现在表格末尾（所有商品行之后），且只有数量没有具体品名\n'
        '  特征3: 格式类似"共150支"、"合计：320y"、"TOTAL: 500"——只有数字+单位，没有商品名和规格\n'
        '  特征4: 数量明显大于前面所有商品数量之和（异常大）\n'
        '满足上述任意一条 → 汇总行 → 跳过不提取。\n'
        '\n'
        '════════════════════════════════════════\n'
        '【关键规则 — 备注提取】\n'
        '════════════════════════════════════════\n'
        '备注是重要信息，请逐行仔细检查：\n'
        '  - 手写批注（如"急单"、"补货"、"样品"）\n'
        '  - 仓位号/货架号（如"A-03"、"2楼仓库"）\n'
        '  - 批次号/缸号（如"B20260701"、"3#缸"）\n'
        '  - 包装方式（如"每箱12支"、"散装"）\n'
        '  - 数量+单位之后的括号内容（如"50支（其中30支蓝色）"→ remark填"其中30支蓝色"）\n'
        '如果一个商品行在数量+单位之后还有其他文字，那就是备注，请务必提取。\n'
        '\n'
        '如果某行只有部分信息，quantity默认"1"\n'
        '只返回纯 JSON 对象，不要其他解释文字，不要 markdown 代码块。\n'
        '示例（注意 remark 都有值）：\n'
        '{"doc_number":"SO20260701","customer_name":"AC公司","items":['
        '{"product_name":"PVC桌布","specification":"1.2×1.8m","quantity":"50","unit":"支","remark":"急单"},'
        '{"product_name":"无纺布","specification":"2m","quantity":"30","unit":"kg","remark":"A-03仓位"},'
        '{"product_name":"7P环保杂胶","specification":"","quantity":"100","unit":"y","remark":"每箱25y"}'
        ']}\n'
        '如果图片中没有任何可识别的商品行，返回 {"doc_number":"","customer_name":"","items":[]}'
    )

    def _image_to_data_url(self, image_bytes, filename):
        """将图片字节转为 data:image/...;base64,... URL。"""
        ext = os.path.splitext(filename)[1].lower() if filename else '.jpg'
        mime_map = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                    '.gif': 'image/gif', '.webp': 'image/webp'}
        mime = mime_map.get(ext, 'image/jpeg')
        b64 = base64.b64encode(image_bytes).decode('utf-8')
        return f'data:{mime};base64,{b64}'

    @staticmethod
    def _clean_response(raw_text):
        """剥离 markdown 围栏，返回纯 JSON 文本。"""
        raw_text = re.sub(r'^\s*```[a-zA-Z]*\s*\n?', '', raw_text)
        raw_text = re.sub(r'\n?\s*```\s*$', '', raw_text)
        return raw_text.strip()

    def recognize(self, image_bytes, filename=''):
        if not self.API_KEY or self.API_KEY.startswith('sk-your-'):
            return {
                'success': False,
                'error': 'AI 识别功能未配置',
                'hint': '请在 .env 中设置 MOONSHOT_API_KEY'
                       '（申请地址 https://platform.moonshot.cn）后重启应用'
            }

        try:
            import openai
        except ImportError:
            return {
                'success': False,
                'error': 'openai 包未安装',
                'hint': '请运行: pip install openai'
            }

        data_url = self._image_to_data_url(image_bytes, filename)

        try:
            client = openai.OpenAI(api_key=self.API_KEY, base_url=self.BASE_URL)
            response = client.chat.completions.create(
                model=self.MODEL,
                messages=[{
                    'role': 'user',
                    'content': [
                        {'type': 'image_url', 'image_url': {'url': data_url}},
                        {'type': 'text', 'text': self.PROMPT}
                    ]
                }],
                max_tokens=self.MAX_TOKENS,
                temperature=self.TEMPERATURE,
                timeout=self.TIMEOUT
            )
            raw_text = response.choices[0].message.content.strip()
            raw_text = self._clean_response(raw_text)
            data = json.loads(raw_text)

            # 兼容新旧格式：新格式 {"doc_number":"...", "items":[...]}，旧格式 [...]
            if isinstance(data, list):
                items = data
                doc_number = ''
                customer_name = ''
            elif isinstance(data, dict):
                items = data.get('items', [])
                doc_number = data.get('doc_number', '') or ''
                customer_name = data.get('customer_name', '') or ''
            else:
                items = None
                doc_number = ''
                customer_name = ''

            if not isinstance(items, list):
                return {
                    'success': False,
                    'error': 'AI 返回的数据格式无法解析',
                    'hint': '图片可能不够清晰，建议手动输入或换张图片重试'
                }
            items, _removed = _filter_summary_items(items)
            return {
                'success': True,
                'items': items,
                'doc_number': doc_number,
                'customer_name': customer_name,
            }

        except openai.AuthenticationError:
            return {
                'success': False,
                'error': 'AI 识别 API key 失效（401）',
                'hint': '请到 Moonshot 控制台（https://platform.moonshot.cn）重新生成 API key，'
                        '更新 .env 里的 MOONSHOT_API_KEY 后重启应用'
            }
        except openai.RateLimitError:
            return {
                'success': False,
                'error': 'AI 调用频率过高（429）',
                'hint': '请稍候几秒后重试'
            }
        except openai.APIConnectionError:
            return {
                'success': False,
                'error': '无法连接 Moonshot 服务',
                'hint': '请检查网络连接，或稍后重试'
            }
        except openai.APIStatusError as e:
            return {
                'success': False,
                'error': f'AI 服务返回异常（{e.status_code}）',
                'hint': '请稍后重试，或检查 Moonshot 平台状态'
            }
        except json.JSONDecodeError:
            return {
                'success': False,
                'error': 'AI 返回的数据格式无法解析',
                'hint': '图片可能不规范，建议手动输入或换张图片'
            }
        except Exception as e:
            logger.exception('Moonshot 识别未预期错误: %s', e)
            return {
                'success': False,
                'error': f'识别失败：{type(e).__name__}',
                'hint': '请稍后重试；如反复出现请联系管理员查看应用日志'
            }


# ═══════════════════════════════════════════════════════════════════
# PaddleOCR 本地引擎
# ═══════════════════════════════════════════════════════════════════

class PaddleOCREngine(BaseOCREngine):
    """PaddleOCR CPU 本地识别引擎。

    首次调用时惰性加载模型（～200MB），后续调用复用。
    """

    MAX_DIMENSION = 2000  # 长边超过此值会等比缩放（防止内存爆炸）

    def __init__(self):
        self._ocr = None

    def _ensure_model(self):
        if self._ocr is None:
            # torch must be imported before paddleocr on Windows
            # (albumentations → torch DLL loading needs the path warm)
            import torch  # noqa: F401
            from paddleocr import PaddleOCR
            self._ocr = PaddleOCR(
                lang='ch',
                use_angle_cls=True,   # 自动纠正旋转/倒置图片
                show_log=False,       # 抑制 PaddleOCR 调试输出
            )

    def _resize_if_needed(self, image_bytes):
        """如果图片过大，等比缩放到长边 ≤ MAX_DIMENSION。"""
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(image_bytes))
            w, h = img.size
            longest = max(w, h)
            if longest <= self.MAX_DIMENSION:
                return image_bytes
            ratio = self.MAX_DIMENSION / longest
            new_size = (int(w * ratio), int(h * ratio))
            img = img.resize(new_size, Image.LANCZOS)
            buf = io.BytesIO()
            fmt = img.format or 'JPEG'
            img.save(buf, format=fmt)
            return buf.getvalue()
        except Exception:
            # 缩放失败就直接用原图
            return image_bytes

    def extract_text(self, image_bytes):
        """只做 OCR 提取纯文本（换行拼接），供行级/整单匹配复用。"""
        self._ensure_model()
        resized = self._resize_if_needed(image_bytes)
        result = self._ocr.ocr(resized, cls=True)
        if not result or not result[0]:
            return ''
        lines = []
        for item in result[0]:
            if item and len(item) >= 2:
                text = item[1][0]
                if text and text.strip():
                    lines.append(text.strip())
        return '\n'.join(lines)

    def recognize(self, image_bytes, filename=''):
        try:
            self._ensure_model()
            resized = self._resize_if_needed(image_bytes)
            result = self._ocr.ocr(resized, cls=True)

            if not result or not result[0]:
                return {'success': True, 'items': []}

            items = parse_paddleocr_result(result)
            items, _removed = _filter_summary_items(items)
            return {'success': True, 'items': items}

        except ImportError:
            return {
                'success': False,
                'error': 'PaddleOCR 未安装',
                'hint': '请运行: pip install paddleocr'
            }
        except MemoryError:
            return {
                'success': False,
                'error': '图片过大导致内存不足',
                'hint': '请使用较小的图片（建议 < 5MB）或切换到 Moonshot 云端识别'
            }
        except Exception as e:
            logger.exception('PaddleOCR 识别失败: %s', e)
            return {
                'success': False,
                'error': f'本地识别失败：{type(e).__name__}',
                'hint': '请尝试使用 Moonshot 云端识别，或换一张更清晰的图片'
            }


# ═══════════════════════════════════════════════════════════════════
# DeepSeek 结构化引擎（PaddleOCR 提取文字 + DeepSeek 转 JSON）
# ═══════════════════════════════════════════════════════════════════

class DeepSeekEngine(BaseOCREngine):
    """PaddleOCR 提取文字 → DeepSeek API 结构化 → JSON。

    兼具离线 OCR 的免费优势和云端 LLM 的语义理解能力，
    文字 token 极便宜（单次约 0.0001 元）。
    """

    API_KEY = os.environ.get('DEEPSEEK_API_KEY', '')
    BASE_URL = 'https://api.deepseek.com/v1'
    MODEL = 'deepseek-chat'
    MAX_TOKENS = 4000
    TIMEOUT = 30

    STRUCT_PROMPT = (
        '你是一个商品信息提取助手。以下是从出货单/送货单/装车单图片中 OCR 识别出的文字，'
        '可能包含表格行列或连续文字。\n\n'
        '请提取所有商品行，返回一个 JSON 对象，包含以下字段：\n'
        '- doc_number: 单据编号 — 仔细找"订单号/单据编号/装车单号/单号/编号/票号/流水号/出库单号/Order No/Doc No/PO#/"等标识,'
        '通常在单据顶部/标题/右上角,无则空字符串\n'
        '- customer_name: 客户名称 — **只提取公司/单位/店铺名称本身**, **不要把字段标签(客户/Customer/收货单位等)算进去**。'
        '常见格式: "客户: 川盛皮纺" → 提取 "川盛皮纺"; "Customer: ABC Co." → 提取 "ABC Co."; "客户单位: XYZ公司" → 提取 "XYZ公司"。'
        '字段标签通常出现在单据抬头/顶部/左上角; 也可能是独立的单位/公司名(没有显式标签,但出现在抬头第一行)。'
        '**注意区分**: 商品行右侧/下方的"备注/购货单位"等不算客户, 抬头第一行/左上角的单位/公司名才是客户。'
        '无则空字符串。\n'
        '- items: 商品列表数组，每条商品包含：\n'
        '  - product_name: 商品名称（必须）\n'
        '  - specification: 规格型号（必须，无则空字符串）\n'
        '  - quantity: 数量（必须，无则填"1"）\n'
        '  - unit: 单位（必须）\n'
        '  - remark: 备注（必须填写！仔细检查该行商品右侧、下方、括号内是否有手写备注、'
        '批次号、仓位号、特殊要求等文字。确实没有才填空字符串）\n\n'
        '单位标准化规则：\n'
        '  "支"/"PCS"/"pc"/"个"/"只" → "支"\n'
        '  "码"/"y"/"Y"/"YDS" → "y"\n'
        '  "桶" → "桶"、"件" → "件"、"箱" → "箱"、"张" → "张"、"kg" → "kg"\n\n'
        '════════════════════════════════════════\n'
        '【关键规则 — 汇总行识别与跳过】\n'
        '════════════════════════════════════════\n'
        '以下特征的行是汇总/合计行，必须跳过，不能作为商品行，也不能把它的数字填入任何商品的 remark：\n'
        '  特征1: 包含"合计"/"总计"/"小计"/"共"/"总共"/"总数量"/"总金额"/"sum"/"total"等关键词\n'
        '  特征2: 出现在所有商品行之后（OCR 文字的最后 1~2 行），且只有数量没有具体品名\n'
        '  特征3: 格式类似"共150支"、"合计：320y"、"TOTAL: 500"——只有数字+单位，没有商品名和规格\n'
        '  特征4: 数量明显大于前面所有商品数量之和（如单行200支，而前面商品都是10-50支）\n'
        '  特征5: 带有"件数"/"箱数"/"包数"等包装汇总词，且该数字不等于任何单个商品的数量\n'
        '满足上述任意一条特征 → 该行是汇总行 → 跳过，不提取。\n\n'
        '════════════════════════════════════════\n'
        '【关键规则 — 备注提取】\n'
        '════════════════════════════════════════\n'
        '备注是单据中的重要信息，常见形式：\n'
        '  - 手写批注（如"急单"、"补货"、"样品"）\n'
        '  - 仓位号/货架号（如"A-03"、"2楼仓库"）\n'
        '  - 批次号/缸号（如"B20260701"、"3#缸"）\n'
        '  - 包装方式（如"每箱12支"、"散装"）\n'
        '  - 特殊要求（如"客户自提"、"加急"、"注意防潮"）\n'
        '  - 在数量+单位之后的数字或短文本（如"50支（其中30支蓝色）"→ remark填"其中30支蓝色"）\n'
        '提取时务必逐行检查 OCR 文字中数量+单位之后的剩余文本，那就是备注！\n'
        '如果 OCR 文字中某行的末尾还有多余文字，请填入 remark，不要丢弃。\n\n'
        '只返回纯 JSON 对象，不要 markdown 代码块，不要解释文字。\n'
        '示例（注意示例中的 remark 都是有值的）：\n'
        '{"doc_number":"SO20260701","customer_name":"AC公司","items":['
        '{"product_name":"PVC桌布","specification":"1.2×1.8m","quantity":"50","unit":"支","remark":"急单"},'
        '{"product_name":"无纺布","specification":"2m","quantity":"30","unit":"kg","remark":"A-03仓位"},'
        '{"product_name":"7P环保杂胶","specification":"","quantity":"100","unit":"y","remark":"每箱25y"}'
        ']}\n'
        '如果没有可识别的商品行，返回 {"doc_number":"","customer_name":"","items":[]}\n\n'
        'OCR 识别的文字如下：\n'
    )

    def __init__(self):
        self._ocr_engine = PaddleOCREngine()

    def _ocr_image(self, image_bytes):
        """调用 PaddleOCR 提取纯文本行。"""
        self._ocr_engine._ensure_model()
        resized = self._ocr_engine._resize_if_needed(image_bytes)
        result = self._ocr_engine._ocr.ocr(resized, cls=True)
        if not result or not result[0]:
            return ''
        lines = []
        for item in result[0]:
            if item and len(item) >= 2:
                text = item[1][0]
                if text and text.strip():
                    lines.append(text.strip())
        return '\n'.join(lines)

    def recognize(self, image_bytes, filename=''):
        if not self.API_KEY or self.API_KEY.startswith('sk-your-'):
            return {
                'success': False,
                'error': 'DeepSeek API key 未配置',
                'hint': '请在 .env 中设置 DEEPSEEK_API_KEY 后重启应用'
            }

        try:
            ocr_text = self._ocr_image(image_bytes)
            if not ocr_text:
                return {'success': True, 'items': [], 'doc_number': ''}

            client = openai.OpenAI(api_key=self.API_KEY, base_url=self.BASE_URL)
            response = client.chat.completions.create(
                model=self.MODEL,
                messages=[{
                    'role': 'user',
                    'content': self.STRUCT_PROMPT + ocr_text
                }],
                max_tokens=self.MAX_TOKENS,
                timeout=self.TIMEOUT
            )
            raw = response.choices[0].message.content.strip()
            # 剥离 markdown 围栏
            raw = re.sub(r'^\s*```[a-zA-Z]*\s*\n?', '', raw)
            raw = re.sub(r'\n?\s*```\s*$', '', raw)
            raw = raw.strip()
            # 调试日志: 看 DeepSeek 实际返回什么
            logger.info("DeepSeek raw response (first 500 chars): %s", raw[:500])
            data = json.loads(raw)

            # 兼容新旧格式：新格式 {"doc_number":"...", "items":[...]}，旧格式 [...]
            if isinstance(data, list):
                items = data
                doc_number = ''
                customer_name = ''
            else:
                items = data.get('items', [])
                doc_number = data.get('doc_number', '') or ''
                customer_name = data.get('customer_name', '') or ''

            if not isinstance(items, list):
                return {
                    'success': False,
                    'error': 'DeepSeek 返回格式异常',
                    'hint': '请重试或切换到其他引擎'
                }
            items, _removed = _filter_summary_items(items)
            return {
                'success': True,
                'items': items,
                'doc_number': doc_number,
                'customer_name': customer_name,
            }

        except ImportError:
            return {
                'success': False,
                'error': 'openai 包未安装',
                'hint': '请运行: pip install openai'
            }
        except openai.AuthenticationError:
            return {
                'success': False,
                'error': 'DeepSeek API key 失效（401）',
                'hint': '请检查 .env 中的 DEEPSEEK_API_KEY 是否正确'
            }
        except openai.APIConnectionError:
            return {
                'success': False,
                'error': '无法连接 DeepSeek 服务',
                'hint': '请检查网络连接，或切换到 PaddleOCR 本地引擎'
            }
        except openai.RateLimitError:
            return {
                'success': False,
                'error': 'DeepSeek 调用频率过高（429）',
                'hint': '请稍候几秒后重试，或切换到 PaddleOCR 本地引擎'
            }
        except openai.APIStatusError as e:
            return {
                'success': False,
                'error': f'DeepSeek 服务返回异常（{e.status_code}）',
                'hint': '请稍后重试，或检查 DeepSeek 平台状态'
            }
        except json.JSONDecodeError:
            return {
                'success': False,
                'error': 'DeepSeek 返回格式无法解析',
                'hint': '请重试或切换到其他引擎'
            }
        except Exception as e:
            logger.exception('DeepSeek 引擎失败: %s', e)
            return {
                'success': False,
                'error': f'DeepSeek 引擎失败：{type(e).__name__}',
                'hint': '请重试或切换到其他引擎'
            }


# ═══════════════════════════════════════════════════════════════════
# 引擎工厂
# ═══════════════════════════════════════════════════════════════════

_engine_cache = {}
_VALID_ENGINES = ('moonshot', 'paddleocr', 'deepseek')


def get_ocr_engine(engine_name=None):
    """获取 OCR 引擎实例（单例缓存）。

    Args:
        engine_name: 'moonshot' | 'paddleocr' | 'deepseek'，
                     None 时读 .env 的 OCR_BACKEND。

    Returns:
        BaseOCREngine 实例。
    """
    if engine_name is None:
        engine_name = os.environ.get('OCR_BACKEND', 'moonshot')
    engine_name = engine_name.lower().strip()

    if engine_name not in _VALID_ENGINES:
        raise ValueError(
            f'不支持的 OCR 引擎: {engine_name}，'
            f'请使用 "moonshot"、"paddleocr" 或 "deepseek"'
        )

    if engine_name not in _engine_cache:
        if engine_name == 'moonshot':
            _engine_cache[engine_name] = MoonshotEngine()
        elif engine_name == 'deepseek':
            _engine_cache[engine_name] = DeepSeekEngine()
        else:
            try:
                _engine_cache[engine_name] = PaddleOCREngine()
            except ImportError as e:
                raise RuntimeError(
                    'PaddleOCR 未安装。请运行: pip install paddleocr'
                ) from e
            except Exception as e:
                raise RuntimeError(f'PaddleOCR 初始化失败: {e}') from e

    return _engine_cache[engine_name]
