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
from typing import Optional
import numpy as np
import openai  # 提到顶层,避免 except 子句引用 _openai 模块时 UnboundLocalError

from blueprints.ocr_log import (image_payload, items_outcome, log_ocr_call,
                                parsed_outcome, prompt_payload, text_outcome)

# Windows DLL fix: torch's shm.dll needs its lib directory on the DLL search path.
# Must be called BEFORE any torch import (which happens transitively via PaddleOCR).
if sys.platform == 'win32':
    _torch_lib = os.path.join(os.path.dirname(sys.executable),
                              'Lib', 'site-packages', 'torch', 'lib')
    if os.path.isdir(_torch_lib):
        os.add_dll_directory(_torch_lib)
    os.add_dll_directory(os.path.dirname(sys.executable))

logger = logging.getLogger('ocr.engine')

# 提示词版本号 —— 改 COMPARE_PROMPT 时同时 bump,事件日志按此版本切分分析
OCR_MATCH_PROMPT_VERSION = 'compare_rows_v2'

# ═══════════════════════════════════════════════════════════════════
# CLAHE 局部对比度增强(褶皱标签专用)
# ═══════════════════════════════════════════════════════════════════

# CLAHE 常量(褶皱标签专用)
_CLAHE_TILE_SIZE = 8
_CLAHE_CLIP_LIMIT = 2.0
_CLAHE_BINS = 256


# ═══════════════════════════════════════════════════════════════════
# 褶皱标签类别门控(双轨:子串 + 品类 code)
# ═══════════════════════════════════════════════════════════════════

# 轨 1:子串白名单(品名中包含任一即命中)
_WRINKLE_VARIANTS = frozenset({
    '白磅布三文治', '黑磅布三文治', 'B级 磅布三文治',
    '7P环保磅布三文治', '磅布三文治',
})

# 轨 2:品类 code 白名单(DeepSeekEngine._classify_product 返回值命中即算)
_WRINKLE_CATEGORY_CODES = frozenset({
    '0212', '021003', '021201', '021202', '021203',
})


def is_wrinkle_label_category(product_name: Optional[str]) -> bool:
    """双轨判定:品名是否属于"褶皱标签"类别(决定是否走 CLAHE 预处理)。

    轨 1:子串匹配 — 任一 _WRINKLE_VARIANTS 子串在 product_name 中出现。
    轨 2:品类 code — DeepSeekEngine._classify_product(product_name) 返回值
          在 _WRINKLE_CATEGORY_CODES 中。

    行为:
      - product_name 为空/None → False(不抛)
      - 任一轨异常(DB 挂/解析失败)→ 该轨视为未命中, 继续下一轨
      - 两轨都未命中 → False
      - 命中任一轨 → True
    """
    if not product_name:
        return False

    # 轨 1:子串匹配
    try:
        for v in _WRINKLE_VARIANTS:
            if v in product_name:
                return True
    except Exception:
        # 子串匹配本身不该抛, 防御性 catch 避免污染调用方
        pass

    # 轨 2:DeepSeekEngine._classify_product 查品类 code
    try:
        code = DeepSeekEngine._classify_product(product_name)
        if code is not None and code in _WRINKLE_CATEGORY_CODES:
            return True
    except Exception:
        # DB 异常 / 任何解析错误 → 视为未命中, 不阻塞主流程
        pass

    return False


def _clahe_on_gray(gray: np.ndarray) -> np.ndarray:
    """对灰度图做 CLAHE(局部对比度受限自适应直方图均衡)。

    算法:8x8 网格,直方图裁剪系数 2.0,256 bin,双线性插值 tile 边界。
    输入输出 dtype:uint8,shape: (H, W)。

    任何异常由调用方 try/except 包住,本函数不抛。
    """
    h, w = gray.shape
    tile_h = h // _CLAHE_TILE_SIZE
    tile_w = w // _CLAHE_TILE_SIZE

    # Pad 到 tile_size 整数倍
    pad_h = _CLAHE_TILE_SIZE * tile_h - h
    pad_w = _CLAHE_TILE_SIZE * tile_w - w
    if pad_h < 0:
        pad_h = 0
    if pad_w < 0:
        pad_w = 0
    if pad_h > 0 or pad_w > 0:
        gray_p = np.pad(gray, ((0, pad_h), (0, pad_w)), mode='reflect')
    else:
        gray_p = gray

    # Reshape: (Ty, tile_h, Tx, tile_w) → (Ty, Tx, tile_h, tile_w)
    tiles = gray_p.reshape(_CLAHE_TILE_SIZE, tile_h, _CLAHE_TILE_SIZE, tile_w)
    tiles = tiles.transpose(0, 2, 1, 3)

    # 计算每 tile 的直方图
    hist = np.zeros((_CLAHE_TILE_SIZE, _CLAHE_TILE_SIZE, _CLAHE_BINS), dtype=np.int32)
    flat_tiles = tiles.reshape(_CLAHE_TILE_SIZE, _CLAHE_TILE_SIZE, -1)  # (Ty, Tx, pixels)
    for v in range(_CLAHE_BINS):
        hist[:, :, v] = (flat_tiles == v).sum(axis=-1)

    # 直方图裁剪 + 重分配
    pixels_per_tile = flat_tiles.shape[-1]
    clip_value = int(_CLAHE_CLIP_LIMIT * pixels_per_tile / _CLAHE_BINS)
    excess = np.maximum(hist - clip_value, 0).sum(axis=-1, keepdims=True)
    hist = np.minimum(hist, clip_value)
    hist += (excess // _CLAHE_BINS).astype(np.int32)

    # CDF
    cdf = hist.cumsum(axis=-1)
    cdf_max = cdf[:, :, -1:]
    cdf_max = np.where(cdf_max == 0, 1, cdf_max)  # 避免除零(空 tile)
    cdf = (cdf / cdf_max * (_CLAHE_BINS - 1)).astype(np.float32)

    # 把每个像素映射到 CDF 值
    # flat_tiles 当前 shape (Ty, Tx, pixels),值是 0..255
    # 用 take_along_axis 在最后一维做 lookup
    mapped = np.take_along_axis(cdf, flat_tiles.astype(np.int32), axis=-1)
    mapped = mapped.reshape(_CLAHE_TILE_SIZE, _CLAHE_TILE_SIZE, tile_h, tile_w)
    mapped = mapped.transpose(0, 2, 1, 3)  # (Ty, tile_h, Tx, tile_w)

    # Reshape 回全图
    result_p = mapped.reshape(_CLAHE_TILE_SIZE * tile_h, _CLAHE_TILE_SIZE * tile_w)

    # 裁掉 padding
    return result_p[:h, :w].astype(np.uint8)

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


def _api_error_detail(e):
    """从 OpenAI SDK 的 APIStatusError 提取服务端 body.message。

    不抛异常,失败返回空串。便于把"模型不存在/余额不足/参数非法"等
    具体原因透传到前端 error 字段,避免只看 400/401/422 还要查日志。

    兼容两种 body 结构:
      - OpenAI / Moonshot: {"error": {"message": "..."}}
      - DeepSeek:          {"message": "...", "type": "...", "code": "..."}
    """
    try:
        body = getattr(e, 'body', None) or {}
        if not isinstance(body, dict):
            return ''
        # 优先找嵌套结构 (OpenAI / Moonshot 风格)
        err = body.get('error')
        if isinstance(err, dict):
            msg = err.get('message')
            if isinstance(msg, str) and msg:
                return f' - {msg}'
        # 兜底:找顶层 message (DeepSeek 风格)
        msg = body.get('message')
        if isinstance(msg, str) and msg:
            return f' - {msg}'
    except Exception:
        pass
    return ''


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
    """Moonshot Kimi k2.6 Vision API 引擎。

    维护提示(2026-07-25):Moonshot 也会下线旧模型,如果未来出现 400 "model not found",
    先到 https://platform.moonshot.cn/docs 查看当前可用模型清单再改这里。
    """

    API_KEY = os.environ.get('MOONSHOT_API_KEY', '')
    BASE_URL = 'https://api.moonshot.cn/v1'
    MODEL = 'kimi-k2.6'  # Moonshot 当前视觉模型;下线时改这里
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

    @log_ocr_call('ocr.moonshot', evt='recognize',
                  failed_if=lambda r: not (r or {}).get('success'),
                  payload=image_payload, outcome=items_outcome)
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
                'error': f'AI 服务返回异常（{e.status_code}）{_api_error_detail(e)}',
                'hint': '请根据错误信息检查请求参数,或检查 Moonshot 平台状态'
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
        self._wrinkle_ocr = None

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
        # 褶皱标签专用 OCR 实例:更激进的检测阈值(det_db_thresh=0.15 / det_db_box_thresh=0.30)
        # 默认实例的阈值适合清晰印刷体;褶皱标签经 CLAHE 增强后,文字边缘弱,
        # 需降低阈值以避免漏检。use_dilation=True 让文字区域更"胖",便于粘连字符切分。
        # _wrinkle_ocr 加载失败时,extract_text 仍可路由回 _ocr(_ensure_model 用 try/except 保护)。
        if self._wrinkle_ocr is None:
            try:
                self._wrinkle_ocr = PaddleOCR(
                    lang='ch',
                    use_angle_cls=True,
                    show_log=False,
                    det_db_thresh=0.15,
                    det_db_box_thresh=0.30,
                    use_dilation=True,
                )
            except Exception as e:
                logger.warning('褶皱 OCR 实例加载失败,回退到默认 _ocr: %s', e)
                self._wrinkle_ocr = self._ocr

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

    def _enhance_wrinkle_label(self, image_np: np.ndarray) -> np.ndarray:
        """褶皱标签专用 CLAHE 预处理。numpy RGB (H,W,3) uint8 → RGB (H,W,3) uint8。

        任何异常 return 原图,不抛(防御性回退,见 spec 「错误处理」)。
        """
        try:
            if image_np is None or image_np.size == 0:
                return image_np
            if image_np.ndim != 3 or image_np.shape[-1] != 3:
                return image_np
            # 转 float32 算亮度,避免 uint8 下溢
            img_f = image_np.astype(np.float32)
            # 灰度化:ITU-R BT.601
            gray = 0.299 * img_f[..., 0] + 0.587 * img_f[..., 1] + 0.114 * img_f[..., 2]
            gray = np.clip(gray, 0, 255).astype(np.uint8)
            # CLAHE
            enhanced_gray = _clahe_on_gray(gray)
            # 用原始 RGB 通道按灰度缩放比例同步增强
            gray_f = gray.astype(np.float32)
            enhanced_f = enhanced_gray.astype(np.float32)
            # 比例:enhanced / gray(避开除零)
            ratio = np.where(gray_f > 1.0, enhanced_f / np.maximum(gray_f, 1.0), 1.0)
            result = np.clip(img_f * ratio[..., None], 0, 255).astype(np.uint8)
            return result
        except Exception as e:
            logger.warning('CLAHE 预处理失败,使用原图: %s', e)
            return image_np

    @log_ocr_call('ocr.paddle', evt='extract_text',
                  failed_if=lambda r: not r,          # 失败时吞异常返回空串
                  payload=image_payload, outcome=text_outcome)
    def extract_text(self, image_bytes, apply_wrinkle_enhance: bool = False):
        """只做 OCR 提取纯文本(换行拼接),供行级/整单匹配复用。

        Args:
            image_bytes: 图片字节流。
            apply_wrinkle_enhance: 是否走褶皱标签专用通道。
              True  → 先 CLAHE 增强 + 用 _wrinkle_ocr(更激进阈值)
              False → 默认 _ocr(老路径,完全向后兼容)

        行为契约:
          - apply_wrinkle_enhance 默认 False,老调用方零感知。
          - _wrinkle_ocr 加载失败时,_ensure_model 已 try/except 回退到 _ocr。
          - 异常路径与原版一致(吞错返回 '')。
        """
        try:
            self._ensure_model()
            resized = self._resize_if_needed(image_bytes)
            if apply_wrinkle_enhance:
                resized = self._enhance_wrinkle_label(resized)
            ocr = self._wrinkle_ocr if apply_wrinkle_enhance else self._ocr
            result = ocr.ocr(resized, cls=True)
            if not result or not result[0]:
                return ''
            lines = []
            for item in result[0]:
                if item and len(item) >= 2:
                    text = item[1][0]
                    if text and text.strip():
                        lines.append(text.strip())
            return '\n'.join(lines)
        except Exception as e:
            logger.exception('PaddleOCR extract_text failed: %s', e)
            return ''

    def extract_text_with_conf(self, image_bytes, apply_wrinkle_enhance: bool = False):
        """与 extract_text 类似,但额外返回平均置信度 (0~1)。

        用途:移动端 Task 6,ocr 置信度低时(模糊图)在 reason 上追加提示,
        引导用户重拍。

        Args:
            image_bytes: 图片字节流。
            apply_wrinkle_enhance: 是否走褶皱标签专用通道(同 extract_text)。

        Returns:
            (text, avg_conf): text 为拼接后的 OCR 纯文本(与 extract_text 行为一致
            —— 含「厚度」字补回);avg_conf 为所有非空文本行的平均置信度。
            失败或无文字时 avg_conf=1.0(不触发模糊提示)。
        """
        try:
            self._ensure_model()
            resized = self._resize_if_needed(image_bytes)
            if apply_wrinkle_enhance:
                resized = self._enhance_wrinkle_label(resized)
            ocr = self._wrinkle_ocr if apply_wrinkle_enhance else self._ocr
            result = ocr.ocr(resized, cls=True)
            if not result or not result[0]:
                return '', 1.0
            lines = []
            confs = []
            for item in result[0]:
                if item and len(item) >= 2:
                    text = item[1][0]
                    conf = item[1][1]
                    if text and text.strip():
                        lines.append(text.strip())
                    try:
                        confs.append(float(conf))
                    except (TypeError, ValueError):
                        pass
            text = '\n'.join(lines)
            # 2026-08-09 修复:小字「厚度：1.0mm」常被识别成「度：1.0mm」(缺"厚"),
            # 这里补回"厚"字,与 extract_text 行为保持一致。
            import re as _re
            text = _re.sub(r'(?m)^\s*度\s*[:：]\s*(\d+\.?\d*)\s*mm?\s*$',
                           r'厚度：\1mm', text)
            avg_conf = sum(confs) / len(confs) if confs else 1.0
            return text, avg_conf
        except Exception as e:
            logger.exception('PaddleOCR extract_text_with_conf failed: %s', e)
            return '', 1.0

    @log_ocr_call('ocr.paddle', evt='recognize',
                  failed_if=lambda r: not (r or {}).get('success'),
                  payload=image_payload, outcome=items_outcome)
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
# 品类关键词兜底表（_classify_product 在用）
# 格式: [(category_code, [keyword1, keyword2, ...]), ...]
# 匹配规则：按列表顺序，第一个 product_name 中含有关键词的行即命中
# 关键词去掉了 'A料'/'B料'（它们更可能是产品等级而非品类）、'LB'（可能不是独立品类）
# ═══════════════════════════════════════════════════════════════════
CLASS_FALLBACK_KEYWORDS = [
    ('0201', ['杂胶']),
    ('0301', ['纯胶']),
    ('0401', ['回力胶', 'EVA']),
    ('0204', ['无纺布']),
    ('0701', ['鱼鳞布', 'HA']),
    ('0208', ['潜水胶']),
    ('0601', ['PE板', 'PE']),
    ('0501', ['不织布']),
    ('0205', ['路华里']),
    ('0302', ['热熔胶']),
]


# ═══════════════════════════════════════════════════════════════════
# DeepSeek 结构化引擎（PaddleOCR 提取文字 + DeepSeek 转 JSON）
# ═══════════════════════════════════════════════════════════════════

class DeepSeekEngine(BaseOCREngine):
    """PaddleOCR 提取文字 → DeepSeek API 结构化 → JSON。

    兼具离线 OCR 的免费优势和云端 LLM 的语义理解能力，
    文字 token 极便宜（单次约 0.0001 元）。

    维护提示(2026-07-25):DeepSeek 已把 deepseek-chat 废弃,当前仅支持
    deepseek-v4-pro / deepseek-v4-flash。如果未来 v4 也下线,先到
    https://api-docs.deepseek.com 查看 error_codes 和模型清单再改 MODEL。
    """

    API_KEY = os.environ.get('DEEPSEEK_API_KEY', '')
    BASE_URL = 'https://api.deepseek.com/v1'
    MODEL = 'deepseek-v4-flash'  # 当前活跃;v4-flash 比 v4-pro 便宜约 10 倍,纯文本结构化够用
    MAX_TOKENS = 8000
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

    # ═══════════════════════════════════════════════════════════════════
    # ⚠️ 范围声明：本提示词仅用于明细行图片核对 (compare_rows)。
    #    STRUCT_PROMPT（用于整单 OCR → JSON 提取）保持独立，
    #    两者职责严格分开 —— AI 智能提取订单信息图片走 STRUCT_PROMPT，
    #    不受本提示词任何规则影响。
    # ═══════════════════════════════════════════════════════════════════

    COMPARE_PROMPT = (
        '你是出货单【明细行标签】核对助手。⚠️ 你的唯一职责：核对已录入明细行的标签图，'
        '不做整单提取，不修改任何品名/规格数据。\n'
        '下面给你两份数据：\n'
        '1) 从实物商品标签照片 OCR 出来的文字（可能顺序乱、有噪声、可能拍糊）。\n'
        '2) 该订单已录入的明细行列表（每行有 record_id、品名、规格）。\n'
        '\n'
        '请逐行判断：OCR 文字里是否出现了该明细行的商品（品名为主、规格为辅，'
        '规格因拍摄可能残缺，不必强求全中）。\n'
        '\n'
        '════════════════════════════════════════\n'
        '【关键匹配规则 — 请严格按这些规则判定】\n'
        '════════════════════════════════════════\n'
        '\n'
        '▌规则 1：环保属性 — "环保"等价词（覆盖所有品类）\n'
        '对于品名中含"环保"字样的商品(不只杂胶:纯胶/三文治/磅布三文治/HA猪皮纹/\n'
        'LB鱼鳞布/路华里等也属此范畴),标签上出现以下任意字样,都视为该商品名称\n'
        '中"环保"特征已被命中:\n'
        '  - 环保\n'
        '  - 7P / 15P / 18P / 21P\n'
        '（这些型号字样在该品类下均代表"环保"属性；只要 OCR 文字里出现上述任\n'
        ' 一字样，就可作为"品名含环保 X"的强证据,可判 green。）\n'
        '\n'
        '▌规则 2：颜色匹配\n'
        'OCR 文字中识别到的颜色（黑、白、红、蓝、绿、黄、棕、灰、米、杏、紫、粉、橙 等），\n'
        '需要与规格中的颜色匹配：\n'
        '  - 规格里有"黑色" + OCR 文字里有"黑"或"黑色" → 颜色匹配 ✓\n'
        '  - 规格里有"黑色" + OCR 文字里有"白色" → 颜色不一致（影响判定）\n'
        '  - 规格里有"黑色" + OCR 文字里无颜色词 → 颜色缺失（按 yellow 处理）\n'
        '  - 规格里无颜色 + OCR 文字里有颜色 → 视为多余信息，不判错\n'
        '\n'
        '▌规则 3：厚度匹配\n'
        'OCR 文字中识别到的厚度信息（通常带 mm 单位，如"3mm"、"1.5mm"），\n'
        '需要与规格中的厚度数值匹配：\n'
        '  - 规格里有"3mm" + OCR 文字里有"3mm"/"3.0mm" → 厚度匹配 ✓\n'
        '  - 规格里有"3mm" + OCR 文字里有"5mm" → 厚度不一致（影响判定）\n'
        '  - 规格里无厚度 + OCR 文字里有厚度 → 不判错\n'
        '\n'
        '▌规则 4：加面 / 单面 匹配\n'
        'OCR 文字中包含"加面"、"单面"、"双面"等字样时，按以下规则与规格比对：\n'
        '  - 规格里有"加面" + OCR 文字里有"加面"或"双面" → 加面匹配 ✓\n'
        '    （双面 ≡ 加面）\n'
        '  - 规格里有"加面" + OCR 文字里有"单面" → 加面不匹配 ✗\n'
        '    （关键冲突，应判 red）\n'
        '  - 规格里有"单面"或规格无加面要求 + OCR 文字里有"单面" → 匹配 ✓\n'
        '  - 规格里有"单面" + OCR 文字里有"加面" → 不匹配 ✗\n'
        '  - 规格里无加面/单面字样 + OCR 文字里有"加面"或"单面" → 视为多余信息，不判错\n'
        '\n'
        '════════════════════════════════════════\n'
        '【判定标准】\n'
        '════════════════════════════════════════\n'
        '  green（绿 ✓）：OCR 文字里明确出现该商品，品名+规格关键属性都对得上\n'
        '  yellow（黄 ⚠）：部分线索吻合，但有属性不确定（颜色/厚度缺失或无法识别），需人工核对\n'
        '  red（红 ✗）：OCR 文字里完全找不到该商品，或关键属性明显冲突\n'
        '    （如规格说"加面"标签说"单面"、规格"黑色"标签"白色"、厚度差很多）\n'
        '\n'
        '════════════════════════════════════════\n'
        '【输出格式】\n'
        '════════════════════════════════════════\n'
        '对每一行返回一个对象：\n'
        '{"record_id":<原样返回>,"match_status":"green|yellow|red","reason":"简述"}\n'
        '\n'
        'reason 必须中文，简要说明判定依据。示例：\n'
        '  - "品名 7P 环保 X 匹配（命中 7P 等价词），颜色黑对得上，厚度 3mm 吻合"\n'
        '  - "标签上出现单面，与规格加面冲突"\n'
        '  - "OCR 文字中未找到该商品信息"\n'
        '\n'
        '只返回 JSON 数组，不要 markdown 代码块、不要多余解释。\n'
    )

    def compare_rows(self, ocr_text, rows):
        """OCR 文字 vs 明细行列表 → 逐行 {record_id, match_status, reason}。

        Args:
            ocr_text: PaddleOCR 提取的纯文本（多行用 \\n 分隔）。
            rows: [{'record_id': int, 'product_name': str, 'specification': str}, ...]

        Returns:
            {'verdicts': list[dict], 'prompt': str} -
                verdicts 每项 {'record_id', 'match_status', 'reason'};
                prompt 是实际发给 DeepSeek 的完整 prompt 文本（事件日志审计用）。
        """
        rows_json = json.dumps(
            [{'record_id': r['record_id'], 'product_name': r.get('product_name', ''),
              'specification': r.get('specification', '')} for r in rows],
            ensure_ascii=False)
        base_prompt = self.COMPARE_PROMPT + '\n【OCR文字】\n' + ocr_text + '\n【明细行】\n' + rows_json
        # ── 2026-07-30 自适应提示词:如果 rows 里携带 _supplement(layer2 大类 / layer3 规格),
        # 把它们拼到 prompt 末尾,DeepSeek 能参照此前人工确认案例做更宽松的判断。 ──
        supplements = [r.get('_supplement') for r in rows if r.get('_supplement')]
        # ── 2026-08-04 自适应优化:分类 + few-shot 注入 ──
        # 取第一条记录的品名做分类(一个 order 内通常同品类),有 hv 案例就拼到 prompt
        category_code = None
        has_hv = False
        few_shot_block = ''
        if rows:
            first_name = rows[0].get('product_name', '')
            if first_name:
                category_code = self._classify_product(first_name)
                if category_code:
                    hv_cases = self._get_hv_cases(category_code)
                    if hv_cases:
                        has_hv = True
                        few_shot_block = self._build_few_shot(hv_cases)
        if supplements:
            layer_text = '\n\n'.join(supplements)
            prompt_text = (base_prompt + few_shot_block +
                           '\n\n════════════════════════════════════════\n'
                           '【自适应提示词(同品类 / 同规格历史人工案例)】\n'
                           '════════════════════════════════════════\n' + layer_text)
        else:
            prompt_text = base_prompt + few_shot_block
        verdicts = self._call_api_with_prompt(prompt_text, multi=True)
        # ── 后处理松弛:该品类有 hv → red 降级 yellow(放宽而非直接判红) ──
        verdicts = self._apply_few_shot_and_relaxation(verdicts, category_code, has_hv)
        return {'verdicts': verdicts, 'prompt': prompt_text}

    @staticmethod
    def _classify_product(product_name):
        """品名 → 品类代码（level 3 category_code）。

        1. 先查 product 表（product_name → category_id → 向上取 level=3 父节点）
        2. 兜底：CLASS_FALLBACK_KEYWORDS 子串匹配
        3. 都无匹配 → None
        """
        if not product_name:
            return None
        try:
            from models._db import get_db
            conn = get_db()
            cur = conn.cursor()
            # ── 策略 1: product 表精确匹配 ──
            cur.execute(
                'SELECT p.category_id FROM product p '
                'WHERE p.product_name = ? AND p.category_id IS NOT NULL LIMIT 1',
                (product_name,)
            )
            row = cur.fetchone()
            if row:
                cat_id = row[0]
                for _ in range(10):  # safety limit
                    cur.execute(
                        'SELECT parent_id, category_code, level '
                        'FROM product_categories WHERE id = ?',
                        (cat_id,)
                    )
                    cat_row = cur.fetchone()
                    if not cat_row:
                        break
                    parent, code, level = cat_row
                    if level == 3:
                        conn.close()
                        return code
                    cat_id = parent
            # ── 策略 2: 关键词兜底 ──
            pn_lower = product_name.lower()
            for code, keywords in CLASS_FALLBACK_KEYWORDS:
                for kw in keywords:
                    if kw.lower() in pn_lower:
                        conn.close()
                        return code
            conn.close()
        except Exception:
            pass
        return None

    @staticmethod
    def _get_hv_cases(category_code):
        """查该品类下人工确认过的记录，返回最新 3 条。

        匹配逻辑：找 shipping_records.product_name 归属该品类或含该品类关键词，
        再取 shipping_images.human_verified=1 的图片,按 id DESC 取最新 3 条
        不同 (product_name, specification) 组合。

        Args:
            category_code: str — 品类代码（如 "0201"）

        Returns:
            list[dict] — 每项 {'product_name': str, 'specification': str}
                        最多 3 条，无匹配 → 空列表
        """
        if not category_code:
            return []

        try:
            from models._db import get_db
            conn = get_db()
            cur = conn.cursor()

            # 构造 WHERE 条件:product 表命中该品类的品名 OR 关键词 LIKE
            conditions = []
            params = []

            # (a) product 表归属该品类的品名
            cur.execute(
                'SELECT DISTINCT p.product_name FROM product p '
                'JOIN product_categories pc ON p.category_id = pc.id '
                'WHERE pc.category_code = ?',
                (category_code,)
            )
            known_names = [r[0] for r in cur.fetchall()]
            if known_names:
                placeholders = ','.join(['?'] * len(known_names))
                conditions.append(f'sr.product_name IN ({placeholders})')
                params.extend(known_names)

            # (b) 关键词 LIKE(同品类的 fallback 关键词)
            for code, keywords in CLASS_FALLBACK_KEYWORDS:
                if code == category_code:
                    for kw in keywords:
                        conditions.append('sr.product_name LIKE ?')
                        params.append(f'%{kw}%')

            if not conditions:
                conn.close()
                return []

            where_clause = ' OR '.join(conditions)
            query = (
                'SELECT sr.product_name, sr.specification '
                'FROM shipping_images si '
                'JOIN shipping_records sr ON si.record_pk = sr.id '
                f'WHERE si.human_verified = 1 AND ({where_clause}) '
                'GROUP BY sr.product_name, sr.specification '
                'ORDER BY MAX(si.id) DESC '
                'LIMIT 3'
            )
            cur.execute(query, params)
            results = [
                {'product_name': r[0] or '', 'specification': r[1] or ''}
                for r in cur.fetchall()
            ]
            conn.close()
            return results
        except Exception:
            return []

    @staticmethod
    def _build_few_shot(cases):
        """将参考案例格式化为 COMPARE_PROMPT 注入块。

        Args:
            cases: list[dict] — _get_hv_cases 的返回值，每项含 product_name + specification

        Returns:
            str — 注入 text block，cases 为空则返回 ''
        """
        if not cases:
            return ''

        cases = cases[:3]  # safety cap
        lines = [
            '\n\n════════════════════════════════════════',
            '【参考案例：该品类此前已人工确认的匹配】',
            '',
            '以下标签照片此前经人工核查,确认与录入明细匹配。',
            '请在当前判断中参考这些案例的匹配标准,采用同等的宽松度:',
            '',
        ]
        for i, c in enumerate(cases, 1):
            pn = c.get('product_name', '')
            sp = c.get('specification', '')
            lines.append(
                f'{i}. 品名"{pn}" + 规格"{sp}"'
                f'  → 匹配 ✓（人工确认）'
            )
        lines.append('')
        lines.append('请用与上述案例一致的宽松度判断当前行。')
        return '\n'.join(lines)

    @staticmethod
    def _apply_few_shot_and_relaxation(results, category_code, has_hv):
        """后处理：当品类有 hv 记录时，将 red 降级为 yellow。

        设计意图:该品类此前有人工确认案例,说明 AI 之前的 red 判定过严。
        这次同样案例降级为 yellow,留给后续人工二次核对,而非直接判红。

        Args:
            results: list[dict] — DeepSeek 返回的原始结果
            category_code: str | None — 品类代码
            has_hv: bool — 该品类是否有 hv 案例

        Returns:
            list[dict] — 处理后的结果
        """
        if not has_hv:
            return results
        for item in results:
            if item.get('match_status') == 'red':
                item['match_status'] = 'yellow'
        return results

    def compare_single_record(self, ocr_text, record):
        """2026-07-30 新增:对单条 record 比对 OCR 文字,供行级图上传及 AI判别按钮调用。

        与 compare_rows 区别:prompt 只含 1 行(单 record),省 token、省时间。
        2026-07-31 改造:返回 prompt_text + raw_response 供前端「详情」按钮审计。

        返回 {'match_status', 'match_score', 'reason', 'prompt_text', 'raw_response'}

        调用方负责异常处理 — 失败应 fall back 到本地 RapidFuzz。
        """
        row_json = json.dumps(
            {'record_id': record.get('id', 0), 'product_name': record.get('product_name', ''),
             'specification': record.get('specification', '')},
            ensure_ascii=False)
        prompt_text = (self.COMPARE_PROMPT + '\n【OCR文字】\n' + ocr_text +
                       '\n【明细行】\n' + row_json)
        raw = ''
        try:
            result = self._call_api_with_prompt(prompt_text, multi=False)
            if isinstance(result, list):
                result = result[0] if result else {}
            # 尝试获取 raw_response(可能是 JSON 字符串,也可能不是)
            raw = json.dumps(result, ensure_ascii=False)
        except Exception:
            # _call_api_with_prompt 可能抛出异常(raw 保持为空)
            raise
        return {
            'match_status': (result.get('match_status') or '').lower(),
            'match_score': 95.0 if result.get('match_status') == 'green' else
                           70.0 if result.get('match_status') == 'yellow' else
                           30.0 if result.get('match_status') == 'red' else None,
            'reason': result.get('reason') or '',
            'prompt_text': prompt_text,
            'raw_response': raw,
        }

    @log_ocr_call('ocr.deepseek', evt='call_api',
                  payload=prompt_payload, outcome=parsed_outcome)
    def _call_api_with_prompt(self, prompt_text, multi: bool):
        """共用:对 prompt 调 DeepSeek,清洗 markdown,解析 JSON,统一返回格式。

        2026-07-30 改造:改用 httpx 直接打 — 避开 openai 库的 X-Stainless-* 头
        (这些 SDK 标识头会让 DeepSeek governor 模块误判为异常流量,返回
        'Authentication Fails (governor)'。curl 直接打 200,openai 库 401)。

        multi=True  → expect list[{record_id, match_status, reason}]
        multi=False → expect {match_status, reason} 或 [{...}](单 record 容错)
        """
        import httpx
        url = self.BASE_URL.rstrip('/') + '/chat/completions'
        headers = {
            'Authorization': 'Bearer ' + self.API_KEY,
            'Content-Type': 'application/json',
            # 不带 X-Stainless-* / User-Agent(裸 httpx 默认只有 python-httpx)
        }
        body = {
            'model': self.MODEL,
            'messages': [{'role': 'user', 'content': prompt_text}],
            'max_tokens': self.MAX_TOKENS,
        }
        try:
            with httpx.Client(timeout=self.TIMEOUT) as cli:
                resp = cli.post(url, headers=headers, json=body)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as e:
            # 401/403/429 等 SDK 风格的异常(确保 _classify_and_match_image 的 try/except 能 fallback)
            raise openai.APIStatusError(
                f'DeepSeek 服务异常({e.response.status_code}): {e.response.text[:200]}',
                request=e.request, response=e.response,
            )
        except httpx.HTTPError as e:
            # 网络/连接问题
            raise openai.APIConnectionError(
                f'无法连接 DeepSeek 服务: {type(e).__name__}: {e}') from e
        raw = ''
        try:
            raw = data['choices'][0]['message']['content'].strip()
        except (KeyError, IndexError, TypeError) as e:
            raise openai.APIError(f'DeepSeek 响应结构异常: {data}') from e
        raw = re.sub(r'^\s*```[a-zA-Z]*\s*\n?', '', raw)
        raw = re.sub(r'\n?\s*```\s*$', '', raw).strip()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            raise openai.APIError(f'DeepSeek 返回非 JSON: {raw[:200]}') from e
        if multi:
            return parsed if isinstance(parsed, list) else parsed.get('results', [])
        return parsed

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

    @log_ocr_call('ocr.deepseek', evt='recognize',
                  failed_if=lambda r: not (r or {}).get('success'),
                  payload=image_payload, outcome=items_outcome)
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
                timeout=self.TIMEOUT,
                # 强制 JSON 输出:防止 DeepSeek 输出 markdown 围栏/前言导致 parse 失败
                # (历史 bug: 17 行订单含特殊字符时,模型用尽 reasoning token,返回空 content)
                response_format={'type': 'json_object'},
            )
            finish_reason = response.choices[0].finish_reason
            raw = response.choices[0].message.content.strip()
            # finish_reason='length' 表示 max_tokens 用尽 → 响应被截断,通常是 reasoning
            # token 把 budget 吃完,JSON 还没写完。明确告知用户,而不是说「格式无法解析」
            if finish_reason == 'length':
                return {
                    'success': False,
                    'error': 'DeepSeek 响应被截断(订单行数过多或备注过长)',
                    'hint': '建议:1) 拆分订单图片(每张 ≤ 10 行);2) 简化商品行的备注文字;3) 重试一次'
                }
            # stop 但 content 为空:模型返回了空响应(罕见,可能是 prompt 触发)
            if not raw:
                return {
                    'success': False,
                    'error': 'DeepSeek 返回内容为空',
                    'hint': '可能原因:OCR 文字过长触发模型空响应。建议重试或切换到 Moonshot 引擎'
                }
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
                'error': f'DeepSeek 服务返回异常（{e.status_code}）{_api_error_detail(e)}',
                'hint': '请根据错误信息检查请求参数,或检查 DeepSeek 平台状态'
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

# 引擎 → 当前活跃模型(防回归参考表)
# 维护原则:DeepSeek/Moonshot 偶尔下线旧模型,出现 400 + "model not found" 时来这里对照,
# 到对应平台 docs 查最新清单后改对应引擎类的 MODEL 常量。
_ACTIVE_MODELS = {
    'moonshot': 'kimi-k2.6',
    'deepseek': 'deepseek-v4-flash',  # v4-pro 备选,价格 10x
    'paddleocr': '(本地模型,无需 token)',
}


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
