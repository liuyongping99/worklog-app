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


class DeepSeekResponseTruncated(RuntimeError):
    """DeepSeek 响应被截断(max_tokens 用尽)或 JSON 解析失败。

    2026-09-17 新增:让上层(ocr_pipeline.classify / shipping.ai_match)能
    区分「DeepSeek 真正格式错」与「max_tokens 不够 → fallback 本地 fuzzy」,
    而不是统一 fallback 成本地或抛通用 APIError。
    """
from PIL import Image

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
OCR_MATCH_PROMPT_VERSION = 'compare_rows_v3'

# 2026-08-18 v2.3: 业务字段规则纠错 (Direction 1)
# 字段前缀白名单 + 右列首字反查表:
#   字典:常见磅布类表单的字段名前缀(2-4 字);
#   反查:右列首字 -> 期望前缀 (用于 OCR 截断到首字时插回缺失字);
#   例子:右列以「名」开头 -> 应配前缀「品」->「品名」;
#         右列以「度」开头 -> 应配前缀「厚」->「厚度」;
#         右列以「感」开头 -> 应配前缀「手」->「手感」;
#         右列以「布」开头 -> 应配前缀「底」->「底布」。
_KNOWN_FIELD_PREFIXES = [
    '品名', '厚度', '手感', '底布', '成份', '规格', '包装',
    '尺寸', '颜色', '成份含量', '门幅', '克重', '材质', '面料', '里料',
    '规格型号', '纱织', '纱支', '纱织密度', '规格参数',
]
_KNOWN_FIELD_PREFIX_SET = set(_KNOWN_FIELD_PREFIXES)
# 右列首字 -> 期望单字前缀
_RIGHT_FIRST_TO_PREFIX = {
    '名': '品', '度': '厚', '感': '手', '布': '底',
    '格': '规', '份': '成', '装': '包', '寸': '尺',
    '色': '颜', '料': '面', '量': '成', '幅': '门',
    '重': '克', '织': '纱', '支': '纱', '号': '规',
}

# 2026-08-18 v2.6: 业务词典加载 (D4)
# 从出货记录统计高频品名, 用于 OCR 残缺输出补全
# (例: 2111 OCR 返回 "磅布", 词表最长匹配 "白磅布三文治")。
import json as _json
import os as _os
_PRODUCT_DICT_PATH = _os.path.join(
    _os.path.dirname(_os.path.abspath(__file__)),
    '..', '_product_dict.json')
_PRODUCT_NAME_FREQ = []  # [(name, count), ...] 按频次降序
try:
    with open(_PRODUCT_DICT_PATH, 'r', encoding='utf-8') as _f:
        _PRODUCT_NAME_FREQ = _json.load(_f)
    # 按 (频次降序, 名字长度降序) 排序 - 优先高频长词
    _PRODUCT_NAME_FREQ.sort(key=lambda x: (-x[1], -len(x[0])))
except Exception:
    pass

def _complete_product_value(value, min_freq=5):
    """D4: 按业务词典补全 OCR 残缺的品名 value (不含色字前缀)。

    色字 (黑/白/...) 不在本函数补全范围内 — 改由 D5 (背景色补全) 处理,
    避免出货频次最高的颜色覆盖真实出货颜色。
    例如 OCR 输出 "磅布", 本函数补全为 "磅布三文治", 色字由 D5 加上。

    Args:
        value: OCR 输出的品名文本。
        min_freq: 词典最低出货频次门槛。
    Returns:
        补全后的品名 (若词典无匹配则原样返回)。
    """
    if not value or not _PRODUCT_NAME_FREQ:
        return value
    v = value.strip()
    if len(v) < 2:
        return value
    COLOR_PREFIXES = ('黑', '白', '红', '蓝', '黄', '灰', '绿', '紫', '棕')
    BRAND_PREFIXES = ('7P', 'TP', '环保', '订做', '定做', '订制')

    def _strip_color(n):
        for c in COLOR_PREFIXES:
            if n.startswith(c):
                return n[1:]
        return n

    # 关键修复: value 也可能带色字 (如 OCR 出 "白磅布"), 比较时需同时剥离。
    # 用 stripped_v (v 的剥离色字版本) 匹配 stripped (name 的剥离色字版本)。
    stripped_v = _strip_color(v)
    if len(stripped_v) < 2:
        return value
    best = None
    best_score = (-1, -1, 0)  # (无品牌前缀, 右邻字符数, freq)
    for name, freq in _PRODUCT_NAME_FREQ:
        if freq < min_freq:
            continue
        # 跳过品牌前缀词条 (7P环保磅布三文治 等), 避免给完整核心词硬加品牌前缀
        if name.startswith(BRAND_PREFIXES):
            continue
        stripped_name = _strip_color(name)
        if len(stripped_name) <= len(stripped_v):
            continue  # 词条不长于 value, 不会扩展
        if stripped_v not in stripped_name:
            continue
        idx = stripped_name.find(stripped_v)
        right_len = len(stripped_name) - idx - len(stripped_v)
        # 评分: 无品牌前缀 (强制 1, 已被上面 skip 排除), 右邻最长, 频次高
        score = (
            1,  # 已经全部无品牌前缀
            right_len,
            freq,
        )
        if score > best_score:
            best = stripped_name
            best_score = score
    if best:
        return best
    return value


_COLOR_CHARS = ('黑', '白', '红', '蓝', '黄', '灰', '绿', '紫', '棕')


def _detect_bg_color_from_np(img_np):
    """D5 改进 (2026-08-19): 4 角采样判别塑料布色, 行为与 _helpers.detect_bg_color 对齐。

    原算法按 blue_diff (B-R) 阈值判别浅蓝/深蓝塑料, 但 blue_diff 对深蓝塑料
    也 ≥20 (深蓝 R≈40 B≈80, B-R=40), 导致深蓝塑料包裹的黑磅布三文治被误判为
    white, 进而污染 OCR 文本 (v2.6.1 _apply_bg_color_prefix 已移除)。

    新算法 (2026-08-19): 4 角采样 + 取 min, 阈值与 _helpers.detect_bg_color 一致:
        - min < 70  -> 'black'  (任一角为深色塑料即偏黑)
        - min > 100 -> 'white'  (4 角都浅才认白, 避免单角反光误判)
        - 70-100    -> None     (中性灰, 放弃推断)

    Args:
        img_np: numpy RGB 数组 (H, W, 3) uint8。
    Returns:
        'black', 'white' 或 None。
    """
    try:
        if img_np is None or img_np.ndim != 3 or img_np.shape[2] < 3:
            return None
        h, w = img_np.shape[:2]
        if h < 8 or w < 8:
            return None
        # 4 角采样: 边长 1/8 的矩形, 避开中心标签
        size_x = max(8, w // 8)
        size_y = max(8, h // 8)
        corners = [
            (0, 0, size_x, size_y),
            (w - size_x, 0, w, size_y),
            (0, h - size_y, size_x, h),
            (w - size_x, h - size_y, w, h),
        ]
        grays = []
        for x0, y0, x1, y1 in corners:
            tile = img_np[y0:y1, x0:x1]
            R = float(tile[..., 0].mean())
            G = float(tile[..., 1].mean())
            B = float(tile[..., 2].mean())
            grays.append(0.299 * R + 0.587 * G + 0.114 * B)
        # 取最暗角 (任一角深即偏 black)
        dark = min(grays)
        if dark < 70:
            return 'black'
        if dark > 100:
            return 'white'
        return None
    except Exception:
        return None


def _normalize_field_line(line_text):
    """v2.3: 基于右列首字推断字段名前缀的规范化。

    Examples:
      '品度：1.0mm'        -> '厚度：1.0mm'    (OCR 漏了'厚'前缀,反查修复)
      '口名：磅布三文治'    -> '品名：磅布三文治' (前 1 字相似修正)
      '主厚度：0.6mm'      -> '厚度：0.6mm'    (前 2 字 endswith 期望前缀)
      '手底布：磅布'       -> '底布：磅布'      (前 2 字 endswith 期望前缀)
      '厚手感：中性'        -> '手感：中性'      (前 2 字 endswith 期望前缀)
      '品名：磅布三文治'    -> 不改 (已对齐)
      '：加硬'             -> 不改 (无前缀,跳过)
    """
    import re as _re
    if not line_text:
        return line_text
    s = line_text.strip()
    # 提取前缀(0-3 个汉字,以第一个 ：或 : 切分)
    m = _re.match(r'^([\u4e00-\u9fff]{0,3})[：:](.*)$', s)
    if not m:
        return line_text
    pre, rest = m.group(1), m.group(2)
    rest_stripped = rest.lstrip('：:').strip()

    # Case 1: 前缀是字典已知全词 - 不动
    if pre in _KNOWN_FIELD_PREFIX_SET:
        return line_text

    # Case 2: 无前缀 - 不动
    if not pre:
        return line_text

    # Case 3+: 用右列首字反查
    right_first = rest_stripped[:1] if rest_stripped else ''
    expected = _RIGHT_FIRST_TO_PREFIX.get(right_first)

    if len(pre) == 1:
        # 单字前缀:校验与右列首字是否匹配
        if expected == pre:
            return line_text  # 已对齐
        if expected and rest_stripped:
            return expected + '：' + rest_stripped
    elif len(pre) >= 2:
        # 多字前缀(可能是「手底」「厚手」「主厚」等冗余);
        # 若以 expected 结尾,修剪前缀,只保留 expected。
        if expected and pre.endswith(expected) and rest_stripped:
            return expected + '：' + rest_stripped

    # Case 5: 都不匹配,RapidFuzz 兜底。
    # 同时尝试 fuzz.partial_ratio 应对 整词不似但子串重叠的场景
    # (例如「口名」vs「品名」仅 1/3 字匹配)。score_cutoff 放宽到 60。
    try:
        from rapidfuzz import process, fuzz
        best_ratio = process.extractOne(
            pre, _KNOWN_FIELD_PREFIXES, scorer=fuzz.ratio, score_cutoff=60)
        best_partial = process.extractOne(
            pre, _KNOWN_FIELD_PREFIXES, scorer=fuzz.partial_ratio, score_cutoff=60)
        best = best_ratio or best_partial
        if best:
            return best[0] + '：' + (rest_stripped or rest)
    except Exception:
        pass
    return line_text


# ═══════════════════════════════════════════════════════════════════
# 无表格线# 2026-08-18 v2.4: Direction 2 - 行位置 + 值模式 感知的字段名纠错
# 磅布三文治表单固定结构: 品名/厚度/手感/底布 (top→bottom)
# D1 在以下场景失明:
#   (a) 右列首字为数字(如「1.0mm」) -> D1 反查表无数字入口
#   (b) 完全无前缀 -> D1 Case 2 直接跳过
#   (c) 前缀与期期整词相似度低(「口名」 vs 「品名」 仅 1/3 字匹配, ratio<60)
# D2 用 (行位置 + 值模式) 二级信号纠错。
_FORM_ROW_PREFIXES = ['品名', '厚度', '手感', '底布']


def _value_matches_expected_row(value, row_name):
    """值是否匹配指定行的预期内容类型。

    Args:
        value: 字段冒号后的值(已 strip)。
        row_name: 期期的字段名(品名/厚度/手感/底布)。
    Returns:
        bool
    """
    import re as _re
    if not value:
        return False
    v = value.strip()
    if row_name == '手感':
        return v in ('加硬', '中性', '软性', '硬性', '加软', '硬', '软')
    if row_name == '厚度':
        return bool(_re.match(r'^\d+(\.\d+)?\s*m?m?$', v))
    if row_name in ('品名', '底布'):
        if '布' in v:
            return True
        for fab in ('棉', '涤', '麻', '丝', '绒', '无纺', '纺'):
            if fab in v:
                return True
        return False
    return False


def _normalize_row_by_position(lines, nrows=4):
    """Direction 2: 行位置 + 值模式 感知纠错。

    对 OCR 分行后的 nrows 行, 按行位置先验(磅布三文治固定顺序
    品名/厚度/手感/底布)配合值模式检测, 强制校正错误的字段名前缀。

    Args:
        lines: 行文本列表(每个含「：」或「:」分隔符)。
        nrows: 期期行数, 默认 4。
    Returns:
        校正后的行文本列表(同长度)。
    """
    out = list(lines)
    for i, ln in enumerate(out):
        if i >= len(_FORM_ROW_PREFIXES) or i >= nrows:
            break
        expected = _FORM_ROW_PREFIXES[i]
        if not ln:
            continue
        for sep in ('：', ':'):
            if sep in ln:
                pre, _, val = ln.partition(sep)
                pre = pre.strip()
                val = val.strip()
                # 1) 前缀已正确 -> 不动
                if pre == expected:
                    break
                # 2) 值非空且匹配期期行模式 -> 强制纠正
                if val and _value_matches_expected_row(val, expected):
                    out[i] = expected + '：' + val
                break
        else:
            # 行内无分隔符: 视为纯值行, 按位置补前缀
            ln_strip = ln.lstrip('：:').strip()
            if ln_strip and _value_matches_expected_row(ln_strip, expected):
                out[i] = expected + '：' + ln_strip
    return out


def _classify_line_value(val, prefix=''):
    """根据值(或前缀+值)分类到 4 行之一, 返回 品名/厚度/手感/底布/None。"""
    import re as _re
    if not val:
        return None
    v = val.strip()
    # 手感离散值
    if v in ('加硬', '中性', '软性', '硬性', '加软', '硬', '软'):
        return '手感'
    # 厚度: 数字+mm
    if _re.match(r'^\d+(\.\d+)?\s*m?m?$', v):
        return '厚度'
    # fabric: 含布或纺织字
    if '布' in v or any(fab in v for fab in ('棉', '涤', '麻', '丝', '绒')):
        if prefix == '品名':
            return '品名'
        if prefix == '底布':
            return '底布'
        return None  # ambiguous, 由 D3 按长度决定
    return None


def _reorder_by_value(lines, nrows=4):
    """D3: 按值分类后, 按 _FORM_ROW_PREFIXES 顺序输出 4 行。

    即使 OCR 错把厚度值放到了品名行, D3 仍按值的实际类型重排。
    品名/底布 ambiguous 时用长度消歧: 长的当品名, 短的当底布。
    """
    by_field = {'品名': [], '厚度': [], '手感': [], '底布': []}
    fabric_pool = []
    for ln in lines:
        pre, val = '', ''
        for sep in ('：', ':'):
            if sep in ln:
                pre, _, val = ln.partition(sep)
                pre = pre.strip()
                val = val.strip()
                break
        else:
            val = ln.lstrip('：:').strip()
        f = _classify_line_value(val, prefix=pre)
        if f and f in by_field:
            by_field[f].append(val)
        elif '布' in val or any(fab in val for fab in ('棉', '涤', '麻', '丝', '绒')):
            fabric_pool.append(val)
    # 品名/底布消歧
    all_fab = (by_field['品名'] + by_field['底布'] + fabric_pool)
    if len(all_fab) >= 2:
        all_fab_sorted = sorted(set(all_fab), key=lambda x: -len(x))
        # 已正确分类的保留; ambiguous 的按长度分配
        if not by_field['品名']:
            by_field['品名'] = [all_fab_sorted[0]]
        if not by_field['底布']:
            remaining = [v for v in all_fab_sorted[1:] if v not in by_field['品名']]
            by_field['底布'] = remaining[:1]
    out = []
    for f in _FORM_ROW_PREFIXES:
        items = by_field[f]
        if items:
            out.append(f + '：' + items[0])
        else:
            out.append('')
    return out


def _apply_business_dict(lines):
    """D4: 对每行应用业务词典补全。

    只对品名行 (前缀含「品名」) 的 value 调用 _complete_product_value。
    其他字段 (厚度/手感/底布) 都是离散或结构化值, 不走词典补全。
    """
    out = []
    for ln in lines:
        if not ln:
            out.append(ln)
            continue
        # 找分隔符
        pre, _, val = '', '', ''
        for sep in ('：', ':'):
            if sep in ln:
                pre, _, val = ln.partition(sep)
                break
        else:
            val = ln.lstrip('：:').strip()
        pre = pre.strip()
        if pre == '品名' and val:
            new_val = _complete_product_value(val)
            if new_val != val:
                out.append('品名：' + new_val)
                continue
        out.append(ln)
    return out


# 表单分行预处理(磅布三文治等 4 行×2 列表单标签)
# 背景:标签无表格线,DB 检测把同行文字合并、或漏检整行 → 漏字。
#       实测 CLAHE/降阈值对"无表格线"几乎无效,正确解法是重建行结构。
# 做法:检测文字上下边界后等分 N 行,逐行 OCR,再与整图 OCR 择优。
# ═══════════════════════════════════════════════════════════════════

# 表单固定行数(磅布三文治:品名/厚度/手感/底布 4 行)
_FORM_ROWS = 4
# 判定"有文字行"的投影阈值系数(相对投影最大值)
_FORM_TEXT_PROJ_COEF = 0.05


# ═══════════════════════════════════════════════════════════════════
# 标签退化类型分派(双轨:子串 + 品类 code)
# 不同退化机制需要不同预处理管线,故用 kind 字符串而非布尔开关。
# ═══════════════════════════════════════════════════════════════════

# 退化类型 kind 取值
KIND_FORM_NOLINES = 'form_nolines'   # 磅布三文治等:无表格线表单 → 分行 OCR
KIND_GLARE = 'glare'                 # 无纺布:透明膜反光 → 反光抑制
KIND_REDSTAMP = 'redstamp'           # 杂胶/纯胶:红章污染 → 红章掩膜+擦除

# 轨 1:子串白名单(品名包含任一即命中对应 kind)
_FORM_NOLINES_VARIANTS = frozenset({
    '白磅布三文治', '黑磅布三文治', 'B级 磅布三文治',
    '7P环保磅布三文治',
    # '磅布三文治' 已移除:它是其他变体的公共子串,
    # 会误中「环保磅布三文治」(有表格线标签,不走 form_nolines 分行 OCR)
})
# 命中本白名单的品名走 KIND_FORM_NOLINES → _extract_form_lines 分行 OCR。
# 已知已知盲点(塑膜反光漏厚度行):见 KIND_FORM_NOLINES 分支 glare 兜底注释
# + memory ocr-form-nolines-glare-fallback-2026-09-02。
_GLARE_VARIANTS = frozenset({'无纺布'})
# 【2026-09-02 修复】环保磅布三文治 加入 redstamp 白名单。
# 背景:订单 TD-2026-09-02-005(911/司机)image 3804 真机实测 — 环保磅布三文治
# 横版表格标签**常带检验合格红章**(肉眼可见红章盖「15P环保磅布三文治」+「0.6黑色」+
# 「中硬」三行的关键字段)。原 routing 把环保磅布三文治从 KIND_FORM_NOLINES 排除后
# 静默回 None,完全不走 redstamp 擦除 → 红章盖字段仍漏字。
# 同类损失历史:67 条中至少 7 条环保磅布三文治(2412/2413/2215/2234/2256 等)
# 因红章干扰 OCR 严重漏字。
# 修复:加到 _REDSTAMP_VARIANTS,走 commit 8412d03 已建的红章擦除 + 双 pass 取优。
# 表格线 + 默认 _ocr 整图路径不冲突(redstamp 走整图,不分行)。
# 注意:7P环保磅布三文治仍在 _FORM_NOLINES_VARIANTS,因轨 1 按声明顺序先匹配
# _FORM_NOLINES(_FORM_NOLINES_EXCLUDES 已不再包含 环保磅布三文治),子串「环保磅布
# 三文治」也能命中 form_nolines,但 7P 前缀会优先命中 _FORM_NOLINES_VARIANTS 第一项。
# 详见 memory: ocr-eco-pangburger-redstamp-routing-2026-09-02
_REDSTAMP_VARIANTS = frozenset({
    '杂胶', '纯胶', '环保磅布三文治',
    # 【2026-09-14】LB鱼鳞布系列 + LB特软 短名
    # 背景:订单 1001 明细 2805「LB特软 / 黑色-0.8」三张图 OCR 全部被红章污染:
    #   - 928-黑色 (红章 2020 + 0.8 串扰)
    #   - #08-黑色
    #   - 9750-8707 / 8-黑色 / 古格 (多行串扰,「古格」是红章字)
    # 历史损失:全库 11 条 LB特软 图片中 5 条被红章污染(yellow),剩下 6 条无红章的能干净出 green
    # '鱼鳞布' 覆盖 7P环保LB鱼鳞布/环保LB鱼鳞布/环保HA鱼鳞布/LB鱼鳞布 等全名变体
    # 'LB特软' 覆盖订单里的短名写法(不含「鱼鳞布」字面)
    '鱼鳞布', 'LB特软',
})

# 【2026-08-27 修复】已知有表格线标签, 显式排除 — 不会被任何轨命中 form_nolines。
# 背景:订单 TD-2026-08-27-006(879/阿桂)三条「环保磅布三文治」OCR 不全。
# 根因 1:白名单里「磅布三文治」是公共子串, 误中「环保磅布三文治」→ 已删。
# 根因 2:删完后「环保磅布三文治」与 7P 变体共享同一品类 code(0212/021003 等),
#         轨 2 仍命中 form_nolines。
# 【2026-09-02 更新】「环保磅布三文治」已从本集合移除,改走 KIND_REDSTAMP 路径
# (见 _REDSTAMP_VARIANTS 注释)。原因:其横版表格标签常带红章,form_nolines 路径
# 不擦除红章会漏字段;redstamp 走整图 _ocr,对表格线无副作用。
_FORM_NOLINES_EXCLUDES = frozenset({
    # '环保磅布三文治' 已移除 (2026-09-02):改走 KIND_REDSTAMP
})

# 轨 2:品类 code 白名单(DeepSeekEngine._classify_product 返回值命中即算)
# 目前仅 form_nolines 有稳定 code;glare/redstamp 以子串轨覆盖。
_WRINKLE_CATEGORY_CODES = frozenset({
    '0212', '021003', '021201', '021202', '021203',
})


def ocr_preprocess_kind(product_name: Optional[str]) -> Optional[str]:
    """双轨判定:品名属于哪类退化标签,返回对应预处理 kind。

    返回 'form_nolines' / 'glare' / 'redstamp' / None。

    轨 1:子串匹配(覆盖当前数据)——任一变体子串命中即返回其 kind。
    轨 2:品类 code(覆盖未来未知变体)——仅映射 form_nolines。

    防御:
      - product_name 空/None → None(不抛)
      - 任一轨异常(DB 挂/解析失败)→ 该轨视为未命中,继续下一轨
      - 两轨都未命中 → None
    """
    if not product_name:
        return None

    # 已知有表格线标签 — 显式排除所有 kind(2026-08-27)。
    # 用 startswith 精确匹配: 避免「环保磅布三文治」误中「7P环保磅布三文治」
    # (后者是真正无表格线标签, 应继续命中 form_nolines)。
    for excl in _FORM_NOLINES_EXCLUDES:
        if product_name.startswith(excl):
            return None

    # 轨 1:子串匹配
    for kind, variants in ((KIND_FORM_NOLINES, _FORM_NOLINES_VARIANTS),
                           (KIND_GLARE, _GLARE_VARIANTS),
                           (KIND_REDSTAMP, _REDSTAMP_VARIANTS)):
        try:
            for v in variants:
                if v in product_name:
                    return kind
        except Exception:
            # 子串匹配本身不该抛,防御性 catch 避免污染调用方
            pass

    # 轨 2:DeepSeekEngine._classify_product 查品类 code
    try:
        code = DeepSeekEngine._classify_product(product_name)
        if code is not None and code in _WRINKLE_CATEGORY_CODES:
            return KIND_FORM_NOLINES
    except Exception:
        # DB 异常 / 任何解析错误 → 视为未命中,不阻塞主流程
        pass

    return None


def is_wrinkle_label_category(product_name: Optional[str]) -> bool:
    """[已废弃] 兼容别名:等价于 ocr_preprocess_kind(...) == 'form_nolines'。"""
    return ocr_preprocess_kind(product_name) == KIND_FORM_NOLINES


def _text_roi(gray: np.ndarray, margin: int = 10) -> tuple:
    """据文字暗像素投影估计标签文字区域,返回 (y0, y1, x0, x1)。

    对无表格线表单,原图可能包含大面积背景(如整袋货物照片),导致
    `_form_row_bands` 按整张图切分而错过标签。本函数用固定亮度阈值
    (白底黑字标签的文字一般<80)把暗像素视为文字,再取水平和垂直投影
    超过峰值 5% 的范围作为 ROI。若 ROI 过小(<5%)或过大(>95%)则返回全图,
    避免误裁。异常时亦返回全图。
    """
    try:
        h, w = gray.shape
        # 固定阈值:黑字印刷字亮度一般显著低于标签白底,80 是稳健经验值
        text = (gray < 80).astype(np.uint8)
        proj_y = text.sum(axis=1)
        proj_x = text.sum(axis=0)
        thy = max(proj_y.max() * 0.05, 1)
        thx = max(proj_x.max() * 0.05, 1)
        ys = np.where(proj_y > thy)[0]
        xs = np.where(proj_x > thx)[0]
        if len(ys) == 0 or len(xs) == 0:
            return (0, h, 0, w)
        y0 = max(0, int(ys.min()) - margin)
        y1 = min(h, int(ys.max()) + 1 + margin)
        x0 = max(0, int(xs.min()) - margin)
        x1 = min(w, int(xs.max()) + 1 + margin)
        area_ratio = (y1 - y0) * (x1 - x0) / (h * w)
        if area_ratio < 0.05 or area_ratio > 0.95:
            return (0, h, 0, w)
        return (y0, y1, x0, x1)
    except Exception:
        return (0, gray.shape[0], 0, gray.shape[1])


def _form_row_bands(gray: np.ndarray, nrows: int = _FORM_ROWS):
    """2026-08-18 v2: 自适应行带切分 - 据文字行的真实 y 范围 + 间距自适应。

    v1 等分 nrows 段在小字右列紧贴场景下把'0.6mm'切成两段,导致行内字符丢失。
    v2 算法:
      1) 用 proj>0 检测实际文字行 y 范围 (run-length 合并 8px 内的小间断);
      2) 取 run 数 >= nrows → 第 i 个 band 中心 = 第 i 个 run 的中心,
         band 高度 = max(run 的相邻间距, 25) (-- 保证 OCR 不会切到字);
      3) < nrows:回退 v1 等分行为;
      4) > nrows:合并最近的相邻 run 直到 == nrows;
      5) 任意异常:回退 v1 等分。
    """
    try:
        h = gray.shape[0]
        if h == 0:
            return [(0, 0)]
        text = (gray < np.percentile(gray, 55)).astype(np.uint8)
        proj = text.sum(axis=1)
        # proj > 0 标记有文字像素的行
        in_text = proj > 0
        # 合并间距 <= 8px 的相邻 in_text 段为一行 (允许跨字符 whitespace)
        rows = []
        cur_a = None
        prev = -999
        gap_merge = 8
        for y, v in enumerate(in_text):
            if v:
                if cur_a is None:
                    cur_a = y
                if y - prev > gap_merge and prev >= 0:
                    rows.append((cur_a, prev))
                    cur_a = y
                prev = y
        if cur_a is not None:
            rows.append((cur_a, prev))

        if len(rows) < nrows:
            # 探测不到足够多行 -> v1 等分
            ys = np.where(proj > proj.max() * _FORM_TEXT_PROJ_COEF)[0]
            if len(ys) == 0:
                return [(0, h)]
            top = int(ys.min())
            bot = int(ys.max())
            bands = []
            for k in range(nrows):
                a = top + (bot - top) * k // nrows
                b = top + (bot - top) * (k + 1) // nrows
                bands.append((max(0, a - 3), min(h, b + 3)))
            return bands

        # 取前 nrows 个 row 行 (多余 run 合并到最近的)
        if len(rows) > nrows:
            # 合并方法:合并间距最小的相邻两行,直到 == nrows
            rows = list(rows)
            while len(rows) > nrows:
                gaps = [(rows[i + 1][0] - rows[i][1], i)
                        for i in range(len(rows) - 1)]
                gaps.sort()
                mi = gaps[0][1]
                rows = rows[:mi] + [(rows[mi][0], rows[mi + 1][1])] + rows[mi + 2:]

        centers = [(a + b) // 2 for (a, b) in rows]
        # band 高度:相邻 center 间距最小值的 1.4 (覆盖 + padding),下限 25px
        if len(centers) > 1:
            cents_sorted = sorted(centers)
            min_step = min(cents_sorted[i + 1] - cents_sorted[i]
                           for i in range(len(cents_sorted) - 1))
            band_h = max(int(min_step * 1.4), 25)
        else:
            band_h = max(int(h * 0.4), 25)

        bands = []
        for c in centers:
            bands.append((max(0, c - band_h // 2), min(h, c + band_h // 2)))
        return [(max(0, a - 3), min(h, b + 3)) for (a, b) in bands]
    except Exception:
        h = gray.shape[0]
        text = (gray < np.percentile(gray, 55)).astype(np.uint8)
        proj = text.sum(axis=1)
        ys = np.where(proj > proj.max() * _FORM_TEXT_PROJ_COEF)[0]
        if len(ys) == 0:
            return [(0, h)]
        top = int(ys.min())
        bot = int(ys.max())
        bands = []
        for k in range(nrows):
            a = top + (bot - top) * k // nrows
            b = top + (bot - top) * (k + 1) // nrows
            bands.append((max(0, a - 3), min(h, b + 3)))
        return bands


# ═══════════════════════════════════════════════════════════════════
# 退化类型预处理管线(numpy 手写,不引入新依赖)
#   form_nolines → 不在图级预处理(走 _extract_form_lines 分行 OCR)
#   glare        → 无纺布透明膜反光:多尺度 Retinex 抑制大尺度非均匀光照
#   redstamp     → 杂胶/纯胶红章污染:红域擦成白底,保留黑字
# ═══════════════════════════════════════════════════════════════════

def _gaussian_blur(img: np.ndarray, sigma: float) -> np.ndarray:
    """可分离高斯模糊(numpy 手写,避免引入 cv2/scipy)。

    img: (H, W) 或 (H, W, C) float64。返回同 shape。
    注意:用自实现 same-mode 切片(而非 np.convolve 的 mode='same',
    后者在核长 > 信号长时返回 max 长度,会撑破维度)。
    """
    if sigma <= 0:
        return img
    k = max(1, int(round(sigma * 3)))
    x = np.arange(-k, k + 1, dtype=np.float64)
    kernel = np.exp(-(x ** 2) / (2 * sigma ** 2))
    kernel /= kernel.sum()
    half = k  # kernel 长度 = 2k+1,中心偏移到 half

    def _conv1d(arr):
        # 沿行(水平)卷积,结果取与输入等长的一段
        return np.apply_along_axis(
            lambda row: np.convolve(row, kernel)[half:half + row.shape[0]],
            axis=1, arr=arr)

    def _conv1d_col(arr):
        # 转置后沿行卷积 = 沿原列卷积
        return np.apply_along_axis(
            lambda row: np.convolve(row, kernel)[half:half + row.shape[0]],
            axis=1, arr=arr.T).T

    if img.ndim == 2:
        out = _conv1d(img)
        out = _conv1d_col(out)
        return out
    out = img.copy()
    for c in range(img.shape[2]):
        ch = _conv1d(img[..., c])
        out[..., c] = _conv1d_col(ch)
    return out


def _suppress_glare(rgb: np.ndarray) -> np.ndarray:
    """无纺布透明膜反光:抑制大尺度非均匀光照(MSRCR 简化版)。

    做法:对亮度做多尺度 Retinex( log(I) - log(blur(I)) )得光照无关的反射分量,
    再按亮度比例重拼回彩色。高光区域被拉回,黑字保留。
    任何异常返回原图(设计硬约束:预处理不抛)。
    """
    try:
        arr = rgb.astype(np.float64)
        lum = (0.299 * arr[..., 0] + 0.587 * arr[..., 1] + 0.114 * arr[..., 2])
        lum = np.clip(lum, 1.0, 255.0)
        msr = np.zeros_like(lum)
        for sigma in (15.0, 80.0, 200.0):
            blur = np.clip(_gaussian_blur(lum, sigma), 1.0, 255.0)
            msr += np.log(lum) - np.log(blur)
        msr /= 3.0
        msr -= msr.min()
        if msr.max() > 0:
            msr = msr / msr.max() * 255.0
        lum_old = lum / 255.0
        lum_new = msr / 255.0
        # 原图按亮度比例映射到新亮度(保留色相,仅校正光照)
        scale = np.where(lum_old > 0.05, lum_new / lum_old, 1.0)
        scale = np.clip(scale, 0.2, 5.0)[:, :, None]
        return np.clip(arr * scale, 0, 255).astype(np.uint8)
    except Exception as e:
        logger.warning('反光抑制失败,用原图: %s', e)
        return rgb


def _suppress_red_stamp(rgb: np.ndarray) -> np.ndarray:
    """胶带/纯胶:红章污染 → 把红墨水(含淡红/粉红晕染)染成白底,保住黑字。

    2026-08-21 改进:
    TD-2026-08-21-002 案例:原阈值 S>60/V>100/R-G>30 太严,
    导致红章晕染区域(浅粉墨)未被抹去,“1.0mm” 被 PaddleOCR 误识为 “10mm”。
    现改为双通道:
      - 强红(红章主色):H 在 [0,15] 或 [165,180],S>50,V>80,R-G>15
      - 浅红/粉红(盖章晕染、淡墨):
          H 在 [0,25] 或 [155,180],S>35(排除纯灰),
          R-G>12 且 R>G+8,V>100(避免黑字被擦)
    黑字(R≈G≈B 都低,V<100)天然排除;
    白色/灰底(R≈G≈B,R-G<=12)天然排除。

    任何异常回原图。
    """
    try:
        import cv2
        hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)  # H:0-179, S:0-255, V:0-255
        H, S, V = hsv[..., 0], hsv[..., 1], hsv[..., 2]
        arr = rgb.astype(np.int16)
        r, g = arr[..., 0], arr[..., 1]
        # 强红通道:H/S/V/R-G 阈值同步放宽,覆盖暗红/低饱和红
        strong_red = (
            ((H <= 15) | (H >= 165))   # 红色色相范围(原 0-10 / 170-180)
            & (S > 50)                  # 饱和度(原 60)
            & (V > 100)                 # 亮度(原 100,保持以排除红章下暗红/黑字)
            & ((r - g) > 15)            # 红色差(原 30,放宽)
        )
        # 浅红/粉红通道:低饱和+明显红偏,排除米色背景
        faded_red = (
            ((H <= 15) | (H >= 165))   # 红章色相范围(对齐 strong_red,避免吃掉米色 H=20)
            & ((r - g) > 12)            # R-G>12(米色背景 R-G≈10,白底=0)
            & (r > g + 8)              # 双重保证
            & (V > 100)                 # 排除纯黑文字(R≈G≈B 都低)
        )
        red_mask = strong_red | faded_red
        out = rgb.copy()
        out[red_mask] = 255
        return out
    except Exception as e:
        logger.warning('去红处理失败中回原图: %s', e)
        return rgb

def _preprocess_for_kind(img_np: np.ndarray, kind: Optional[str]) -> np.ndarray:
    """按退化类型返回预处理后的 RGB 图。form_nolines 不在图级预处理(走分行 OCR)。

    异常由各函数内部捕获返回原图。
    """
    if kind == KIND_GLARE:
        return _suppress_glare(img_np)
    if kind == KIND_REDSTAMP:
        return _suppress_red_stamp(img_np)
    return img_np


def _sharpen_for_ocr(rgb: np.ndarray) -> np.ndarray:
    """轻量锐化预处理:为手写体数字增强对比度(2026-09-02)。

    背景:环保杂胶/杂胶/纯胶等橡胶类商品的标签常用「印刷体 + 手写马克笔数字」
    混排(工人手填厚度/颜色),PaddleOCR 对训练集外的手写数字识别率先天弱。
    不同 UnsharpMask 参数对同一手写数字的捕捉不同(实测:r=2 p=200 抓「4」,
    r=4 p=300 抓「1」),本函数提供 2 档预设,供 KIND_REDSTAMP 三遍 OCR 取优。

    异常返回原图 (设计硬约束)。
    """
    try:
        from PIL import Image
        sharp = Image.fromarray(rgb).filter(
            __import__('PIL.ImageFilter', fromlist=['UnsharpMask']).UnsharpMask(
                radius=2, percent=200, threshold=0))
        return np.array(sharp)
    except Exception as e:
        logger.warning('锐化失败,用原图: %s', e)
        return rgb


def _sharpen_strong_for_ocr(rgb: np.ndarray) -> np.ndarray:
    """强锐化变体,与 _sharpen_for_ocr 形成"不同字符"互补(2026-09-02)。

    实测同一张手写「1.4」:轻度(r2p200)抓「4」,中度(r4p300)抓「1」;
    KIND_REDSTAMP 三遍 OCR 合并时按 (line_count, total_chars, prefix_bonus) 评分取优,
    两个锐化版哪个总字符更多哪个胜出(通常能拿到至少 1 个数字)。
    """
    try:
        from PIL import Image
        sharp = Image.fromarray(rgb).filter(
            __import__('PIL.ImageFilter', fromlist=['UnsharpMask']).UnsharpMask(
                radius=4, percent=300, threshold=0))
        return np.array(sharp)
    except Exception as e:
        logger.warning('强锐化失败,用原图: %s', e)
        return rgb


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


_yard_product_names_cache = None


def _load_yard_product_names():
    """加载按码(y)计量的产品名(is_usingyardforcounting=1)。

    用于在 LLM STRUCT_PROMPT 里注入码基产品清单,让 LLM 输出直接和
    API/存储口径对齐:码基产品 quantity=码数、unit=y、remark=「N支」。
    避免 LLM 把"45支"填进 quantity、码数填进 remark 造成字段错位。
    """
    global _yard_product_names_cache
    if _yard_product_names_cache is not None:
        return _yard_product_names_cache
    try:
        from models._db import get_db
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT product_name FROM product_units "
            "WHERE is_usingyardforcounting=1 AND product_name IS NOT NULL AND product_name != '' "
            "ORDER BY LENGTH(product_name) DESC"
        )
        _yard_product_names_cache = [row[0] for row in cur.fetchall()]
        conn.close()
    except Exception:
        _yard_product_names_cache = []
    return _yard_product_names_cache


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


# ── 自由文本路径:商品表预过滤(2026-09-22 入库页「🤖 智能文本」)────────────
# 把整个 product 表 1145 行塞进 prompt 会拖慢响应 + reasoning 不稳。
# 替代方案:从用户输入里抽 2+ 字中文关键词,LIKE 模糊查 product 表,取
# 命中度最强的前 N 行(默认 15)塞进 prompt 让 LLM 就近匹配。
# 命中数 = 0 时不注入,LLM 自由生成(原行为)。
_CN_KEYWORD_RE = re.compile(r'[一-龥]{2,4}')  # 2~4 字中文短语,过滤单字噪声(旧版,留作历史)
_CN_KEYWORD_BASE_RE = re.compile(r'[一-龥]{2,}')  # ≥2 字中文段(2026-09-22 口语化改造)
# 口语虚词:扫中文段时跳过这些字头的(避免「那个」「点的」「面的」污染 LIKE 召回)
_SPOKEN_STOPWORDS = frozenset({
    '那个', '这个', '什么', '哪种', '这种', '那种', '一种', '一点',
    '一个', '几个', '一些', '一样', '面的', '点的', '一点的',
    '那个的', '这个的', '的', '了', '着', '过', '是',
})
_MAX_PRODUCT_CANDIDATES = 15  # 候选产品上限 — 控制 prompt 长度 ≤ ~500 chars


def _extract_spoken_keywords(text):
    """从口语化输入里抽取业务关键词(2~6 字),用于 LIKE 查 product 表。

    抽取规则(2026-09-22 改造,见上):
      - 扫所有 ≥2 字中文段,跳过口语虚词
      - 加末尾 2-3 字核心后缀(产品名通常是「修饰+核心」,如「黑皮糠纸」→「皮糠」+「皮糠纸」)
      - 加中间 2 字业务子串(跳过首 1-2 字修饰词)

    实测:7 个口语化用例从 0 召回到 15 命中,标准用例不受影响。
    """
    if not text:
        return []
    seen = set()
    for m in _CN_KEYWORD_BASE_RE.finditer(text):
        w = m.group()
        if w in _SPOKEN_STOPWORDS or len(w) < 2:
            continue
        seen.add(w)
        # 末尾 2-3 字核心后缀(「白杂胶」→「杂胶」)
        if len(w) >= 3:
            seen.add(w[-2:])
        if len(w) >= 4:
            seen.add(w[-3:])
        # 中间 2 字业务子串(跳过首 1-2 字修饰词「黑/白/那个」)
        if len(w) >= 4:
            for s in range(1, len(w) - 1):
                if len(w) - s >= 2:
                    seen.add(w[s:s + 2])
    return [kw for kw in seen if len(kw) >= 2]


def _select_relevant_products(text, max_n=_MAX_PRODUCT_CANDIDATES):
    """根据输入文本,从 product 表预过滤出最相关的 N 行。

    算法:
      1. 用 _extract_spoken_keywords 抽口语关键词(2026-09-22 改造)
      2. 对每个关键词,在 product.product_name / product.specification 上做 LIKE %kw%
      3. 命中行按"被多少个不同关键词命中"排序(命中多 > 命中少)
      4. 同命中数时按 product_name 长度 asc(短名优先,通常标准名)
      5. 取前 max_n 行,返回 [(product_name, specification), ...]

    性能:< 50ms。文本里没有任何中文关键词(如纯阿拉伯数字)时,返回 []。
    """
    keywords = _extract_spoken_keywords(text)
    if not keywords:
        return []
    try:
        from models._db import get_db as _get_db
        conn = _get_db()
        cur = conn.cursor()
        # 一次查全部,Python 端算命中数 — LIKE 通配是同一字段多次 OR,
        # SQLite 单条语句 + 命中数字典比多次 SQL COUNT 快得多。
        cur.execute(
            'SELECT product_name, specification FROM product '
            'WHERE product_name IS NOT NULL OR specification IS NOT NULL'
        )
        rows = cur.fetchall()
        conn.close()
    except Exception:
        # DB 不可用时降级为「不注入候选」,不影响主流程
        return []
    scored = {}  # (pn, spec) -> 命中关键词数
    for r in rows:
        pn = (r[0] or '').strip()
        sp = (r[1] or '').strip()
        if not pn and not sp:
            continue
        combined = pn + ' ' + sp
        hits = sum(1 for kw in keywords if kw in combined)
        if hits > 0:
            key = (pn, sp)
            if key not in scored or scored[key] < hits:
                scored[key] = hits
    # 排序:命中数 desc, 同命中数时按 pn 长度 asc(短名优先, 通常是标准名)
    ranked = sorted(scored.items(), key=lambda kv: (-kv[1], len(kv[0][0])))
    return [(k[0], k[1]) for k, _ in ranked[:max_n]]


def _fuzzy_align_to_products(items, candidates, name_threshold=95, spec_threshold=85):
    """LLM 输出后,用 RapidFuzz 把每行的 product_name/specification
    对照候选产品清单做近似匹配,贴近的强制替换为标准名/规格。

    阈值口径(2026-09-22 与用户确认):
      - product_name: 95 — 严格贴近才替换,避免「环保杂胶」被强行改成
        「7P环保杂胶」这种「业务语义偏差」型错配;LLM 输出保留更尊重
        用户原意(「环保」「7P」是不同业务类别,不能擅自加前缀)。
      - specification: 85 — 规格里有数字/字母/单位,小差异(空格/标点)
        经常是同款,贴近即可对齐;用户在前端可继续手动编辑。

    Returns: 改动后的 items 列表(原列表会被修改,无需深拷)
    """
    if not items or not candidates:
        return items
    try:
        from rapidfuzz import fuzz
    except ImportError:
        return items
    cand_names = [c[0] for c in candidates]
    cand_specs = [c[1] for c in candidates]
    for it in items:
        if not isinstance(it, dict):
            continue
        # product_name 对齐 — 严格阈值 95
        pn = (it.get('product_name') or '').strip()
        if pn:
            best = None
            best_score = 0
            for i, cand in enumerate(cand_names):
                if not cand:
                    continue
                # 只用 fuzz.ratio(全串比较),不用 partial_ratio —
                # partial 会把「环保杂胶」认成「7P环保杂胶」=100%,造成加前缀错配
                s = fuzz.ratio(pn, cand)
                if s > best_score:
                    best_score = s
                    best = cand
            if best and best_score >= name_threshold:
                it['product_name'] = best
        # specification 对齐 — 宽松阈值 85(规格短,空格/标点差异是噪声)
        sp = (it.get('specification') or '').strip()
        if sp:
            best = None
            best_score = 0
            for i, cand in enumerate(cand_specs):
                if not cand:
                    continue
                s = max(fuzz.ratio(sp, cand), fuzz.partial_ratio(sp, cand))
                if s > best_score:
                    best_score = s
                    best = cand
            if best and best_score >= spec_threshold:
                it['specification'] = best
    return items


def _compose_user_supplements(max_n: int = 50) -> str:
    """读 category_prompts 表 scope='text_recognize' 的全部活跃提示词,
    拼成一段要注入 prompt 末尾的「用户累积补充提示词」。

    设计:全局注入,不分关键词(详见 models/category_prompt.list_active_text_recognize_supplements)。
    用户每发现一个 LLM 错配 → 加一条约束 → 下次同类输入不再错。
    调用失败(DB 错误 / import 失败)返回空串,不影响主流程。

    Args:
        max_n: 最大注入条数,防止 prompt 爆涨(默认 50,够用 6+ 个月迭代)。
              超出按 id ASC 截断(老的先注入,业务语义固定优先)。

    Returns:
        拼好的 prompt 片段。无任何提示词时返回空串。
    """
    try:
        from models.category_prompt import CategoryPrompt
        items = CategoryPrompt.list_active_text_recognize_supplements(limit=max_n)
    except Exception:
        return ''
    items = [it for it in items if (it.get('prompt_text') or '').strip()]
    if not items:
        return ''
    lines = ['## 用户累积补充提示词(基于此前 bad case 修正,必须遵守)']
    for idx, it in enumerate(items, 1):
        txt = (it.get('prompt_text') or '').strip()
        if txt:
            lines.append(f'{idx}. {txt}')
    return '\n'.join(lines) + '\n\n'


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


def _group_into_rows(lines, y_threshold=None):
    """将同行的文本片段拼接。Y 坐标差距 < y_threshold 视为同行。

    Args:
        lines: list[(text, y_center, x_min)] 已排序的 OCR 文本行
        y_threshold: float | None — 显式数值 (>0) → 固定阈值 (与历史兼容,适合已知版面的回归测试)
            * None 或 0/负数 → 调用 _adaptive_y_threshold() 自适应推断.
              这是 2026-08-24 的默认行为:出货单/送货单的表格行距
              会随图片缩放/字号变化,固定 30 经常把相邻表格行合并.
    """
    if not lines:
        return []
    if y_threshold is None or y_threshold <= 0:
        y_threshold = _adaptive_y_threshold([l[1] for l in lines])
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


def _adaptive_y_threshold(ys):
    """根据相邻 Y 坐标的 gap 分布,自适应推断行分组阈值.

    适用场景:带表格线的出货单/送货单/装车单 ——
    文本行距可随图片缩放/字号变化,固定阈值 (原 30) 经常把相邻
    表格行误合并。算法利用「行内 vs 行间」两类 gap 在数值上的双峰分布
    找天然分界,比固定阈值稳定。

    算法:
        1. 把所有相邻 Y 坐标的差值排成有序序列 gaps (亚像素抖动已 round)。
        2. 找相邻 gaps 之间的最大跳跃 (max_jump):
           跳跃点左侧都是「行内 gap」(0~3 像素),
           跳跃点右侧都是「行间 gap」(>= 15 像素) —— 这是表格行天然的边界。
        3. 阈值 =「行间 gap 最小值」× 0.6 (留 40% 余量给图片缩放/抖动),
           clamp 到 [5, 20] 区间。
           2026-09-18 调整:原 [10, 35] 区间对 8-12px 紧凑表格行(典型装车单)
           会把相邻行误合并;放宽下界到 5 让短行距也能分开,收紧上界到 20
           避免行内波动被错判为跨行。
        4. 兜底:gap 分布均匀 (无明显跳跃) → 用中位数 × 0.6 推断;
           数据极少 (< 2 个 Y) → 返回 18。

    Args:
        ys: list[float] — 来自 _extract_lines() 的 y_center 序列。

    Returns:
        float — 自适应阈值(像素)。
    """
    if len(ys) < 2:
        return 18.0
    # round(1) 把同一行的微小抖动 (亚像素) 合并,避免噪声产生假 gap。
    ys_unique = sorted(set(round(float(y), 1) for y in ys))
    if len(ys_unique) < 2:
        return 18.0
    gaps = sorted(
        ys_unique[i + 1] - ys_unique[i] for i in range(len(ys_unique) - 1)
    )
    if not gaps:
        return 18.0
    if len(gaps) == 1:
        # 仅有 2 条 Y,无法定位「行内/行间」边界,按其 60% 推断。
        return max(5.0, min(gaps[0] * 0.6, 20.0))
    # 找最大跳跃 —— 行内 → 行间 的天然分界点。
    max_jump = 0.0
    jump_idx = -1
    for i in range(1, len(gaps)):
        jump = gaps[i] - gaps[i - 1]
        if jump > max_jump:
            max_jump = jump
            jump_idx = i
    if jump_idx > 0 and jump_idx < len(gaps):
        # 跳跃点之后的最小 gap =「行间下界」,这是最安全的阈值锚点。
        threshold = gaps[jump_idx] * 0.6
        return max(5.0, min(threshold, 20.0))
    # 兜底:gap 分布均匀 → 用中位数 60% 推断。
    threshold = gaps[len(gaps) // 2] * 0.6
    return max(8.0, min(threshold, 35.0))

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

    步骤:
      1. product_name 匹配汇总关键词(合计/总计/共X支/纯数字名)
      2. 清理 remark/specification 中被误填的汇总数据

    注意:不再做"中位数×3 离群值"检测(2026-09-03 拆除)。
    原因:码基/大批量出货单里,真实商品的 qty 跨度常 > 3x 中位数(如
    露华里 2400 码 vs 环保磅布三文治 300 码),该启发式假阳率太高。
    如果 LLM 给汇总行填了真实品名(步骤 1 漏),由用户在「AI 识别结果
    预览」弹窗里手动删,不应让安全网静默丢商品。

    Returns:
        (filtered_items, removed_count)
    """
    if not items:
        return items, 0

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

        # ── 检测 2: 无品名特征(空名或纯数字+单位) ──
        if not name or re.match(r'^[\d.,]+\s*[支yYkg件箱桶张块个]?$', name):
            removed += 1
            continue

        # ── 检测 3: remark 被填入了汇总数据 ──
        if _SUMMARY_IN_REMARK_RE.search(remark):
            # 把汇总数据从 remark 清掉,保留行
            it['remark'] = re.sub(
                r'^(合计|总计|小计|总数量|总金额|总共|共\d+|TOTAL|SUM)[：:\s]*\d*\s*[支yYkg件箱桶张块个]?\s*$',
                '', remark
            ).strip()
            # 清完后如果变空了就保持空
            if not it['remark']:
                it['remark'] = ''

        # ── 检测 4: specification 里只有"共X支"之类的汇总文本 ──
        if _SUMMARY_KEYWORDS_RE.search(spec):
            it['specification'] = ''
            # 不删除行,只清规格

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
    MAX_TOKENS = 24000  # 【2026-09-17】从 12000 加倍到 24000:实测 reasoning 长度对同图
    # 随机波动,12000 仍偶发 finish_reason='length'(09-17 11:03 单次 2 行跑了 69s
    # 才成功,reasoning 几乎榨干)。加倍给 24K token reasoning 余量。
    # 同步 TIMEOUT 60→120(见下)。
    TEMPERATURE = 1  # Kimi k2.6 锁死 temperature 必须为 1
    TIMEOUT = 120  # 【2026-09-17】reasoning 用满 24K token 在慢网络下可能跑 90s+,
    # 60s 必超时 ReadTimeout。从 60 翻倍到 120 留 2 倍冗余。

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
            # 注入码基产品清单,让 LLM 输出与 API/存储口径对齐(同 DeepSeek 引擎):
            # 码基产品 quantity=码数、unit=y、remark=「N支」。
            yard_names = _load_yard_product_names()
            yard_rule = ''
            if yard_names:
                yard_rule = (
                    '\n\n════════════════════════════════════════\n'
                    '【关键规则 — 码基产品字段对齐(必须遵守)】\n'
                    '════════════════════════════════════════\n'
                    '以下产品按码(y)计量,字段必须这样填:\n'
                    '  quantity = 码数(图片里的实际码长,数字,如 1642.5)\n'
                    '  unit = "y"\n'
                    '  remark = 支数(形如"45支")\n'
                    '即:若图片同时出现"45支"和码数(如 1642.5),码数进 quantity、unit 填 y、"45支"进 remark。\n'
                    '码基产品清单: ' + '、'.join(yard_names)
                )
            client = openai.OpenAI(api_key=self.API_KEY, base_url=self.BASE_URL)
            response = client.chat.completions.create(
                model=self.MODEL,
                messages=[{
                    'role': 'user',
                    'content': [
                        {'type': 'image_url', 'image_url': {'url': data_url}},
                        {'type': 'text', 'text': self.PROMPT + yard_rule}
                    ]
                }],
                max_tokens=self.MAX_TOKENS,
                temperature=self.TEMPERATURE,
                timeout=self.TIMEOUT,
                # 【2026-09-15】对齐 DeepSeek:强制 JSON 直出
                # 不加这个,Kimi k2.6 内部 reasoning 消耗 max_tokens 把 budget 烧光,
                # 即使只有 3 行商品也会 finish_reason='length' 截断(JSON 还没写完)
                # 见 DeepSeekEngine 注释(2026-09-02 历史 bug)。
                response_format={'type': 'json_object'},
            )
            raw_text = response.choices[0].message.content.strip()
            raw_text = self._clean_response(raw_text)
            finish_reason = response.choices[0].finish_reason
            # 【2026-09-17】finish_reason='length' 表示 max_tokens 用尽 → 响应被截断。
            # 真因是 Kimi k2.6 内部 chain-of-thought reasoning 把 budget 吃光,
            # 即使只有 1-2 行商品也会截断(reasoning 长度对同图随机波动)。
            # 不要告诉用户去拆单/删备注——那是无效操作,只会增加挫败感。
            if finish_reason == 'length':
                return {
                    'success': False,
                    'error': 'Moonshot 响应被截断(AI 推理超出预算)',
                    'hint': '真因是模型内部推理消耗 token,与订单行数/备注长度无关。'
                            '建议:1) 直接重试(下次推理可能更短);2) 切换到 PaddleOCR 或 DeepSeek 引擎;'
                            '3) 换张更清晰的图片(模糊图会迫使模型推理更久)'
                }
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
        # torch must be imported before paddleocr on Windows
        # (albumentations → torch DLL loading needs the path warm)
        # 提升到此处的两个 if 块之前 — 之前 `from paddleocr import PaddleOCR` 只在
        # `if self._ocr is None:` 块内,如果某个调用路径下 _ocr 已存在但 _wrinkle_ocr
        # 是 None(例如外部手动重置 _wrinkle_ocr 触发重加载),第二个块就会
        # NameError: name 'PaddleOCR' is not defined,并被外层 try/except 吞掉,
        # 静默退回到默认阈值。提升后两个分支都能拿到名字。
        import torch  # noqa: F401
        from paddleocr import PaddleOCR
        if self._ocr is None:
            # ── 「_ocr」= 订单截图/扫描件 OCR 默认实例 ──
            # 用途:智能添加里出货单/送货单/装车单的整图 OCR(电脑截图、扫描件,
            # 文字清晰、可能有表格线),走 extract_text(kind=None) 默认分支 + Y 聚类。
            # **不要**给实物商品标签用 — 实物标签模糊,需走下面 _wrinkle_ocr。
            self._ocr = PaddleOCR(
                lang='ch',
                use_angle_cls=True,   # 自动纠正旋转/倒置图片
                show_log=False,       # 抑制 PaddleOCR 调试输出
                det_db_thresh=0.2,    # 默认 0.3
                det_db_box_thresh=0.4,  # 默认 0.6 — 关键:调低才能框出小字厚度
                use_dilation=True,    # 连通断裂笔画,利好细小数字
            )
        # ── 「_wrinkle_ocr」= 实物商品标签 OCR 激进实例 ──
        # 用途:行级图 OCR(磅布三文治、杂胶等实物照片),文字可能反光/红章/模糊/字号极小,
        # 由 extract_text(kind='form_nolines' / 'glare' / 'redstamp') 触发。
        # 阈值比 _ocr 更激进(det_db_thresh=0.15 / det_db_box_thresh=0.30),保留弱检;
        # use_dilation 让文字区域更"胖",便于粘连字符切分(磅布三文治左列单字 vs 右列字段)。
        # **不要**给订单截图用 — 该图清晰印刷体,激进阈值会过度切分。
        # _wrinkle_ocr 加载失败时,extract_text 仍可路由回 _ocr(_ensure_model 用 try/except 保护)。
        if self._wrinkle_ocr is None:
            try:
                self._wrinkle_ocr = PaddleOCR(
                    lang='ch',
                    use_angle_cls=True,
                    show_log=False,
                    # 2026-08-18 v2 reverted: 0.20/0.40 在小字右列召回率回归,先保持 0.15/0.30。
                    det_db_thresh=0.15,
                    det_db_box_thresh=0.30,
                    use_dilation=True,
                )
            except Exception as e:
                logger.warning('褶皱 OCR 实例加载失败,回退到默认 _ocr: %s', e)
                self._wrinkle_ocr = self._ocr

    def _resize_if_needed(self, image_bytes):
        """解码为 RGB numpy 数组传给 PaddleOCR。

        2026-08-09 修复:原先直接把原始 bytes 交给 PaddleOCR.ocr(),
        实测小字「厚度：1.0mm」会被整块丢漏(字节解码路径的预处理对细字不友好)。
        改成解码成 numpy RGB 数组后,同样的检测参数即可稳定框出厚度数字。
        同时:超大图等比缩放到长边 ≤ MAX_DIMENSION(防内存爆炸)。
        """
        try:
            img = Image.open(io.BytesIO(image_bytes)).convert('RGB')
            w, h = img.size
            longest = max(w, h)
            if longest > self.MAX_DIMENSION:
                ratio = self.MAX_DIMENSION / longest
                img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
            return np.array(img)
        except Exception:
            # 解码失败就退回原始 bytes,PaddleOCR 自行解码(至多损失小字召回)
            return image_bytes

    def _extract_form_lines(self, image_np: np.ndarray, ocr, bg_color=None):
        """无表格线表单分行 OCR:逐带识别,再按几何坐标把同一行的左列单字
        与右列字段拼回成完整字段行。

        无表格线表单典型结构:左列竖排单字前缀(品/厚/手/底)+ 右列字段
        (名：磅布三文治 / 度：1.2mm / 感：加硬 / 布：磅布三文治)。逐带 OCR
        时左列常被识别成一整个竖向块(如「品厚」),与右列字段失联。本函数收集
        所有带坐标的 OCR 条目,把左列竖向块拆成单字后,按各自 y 坐标与右列字段
        配对前置,从而拼回「品名：磅布三文治」这类完整字段。最后与整图单次
        OCR 择优(字符更多者胜)。

        image_np: numpy RGB (H,W,3) uint8。
        ocr: PaddleOCR 实例(通常用低阈值的 _wrinkle_ocr)。
        返回 (lines, confs):lines 为自上而下、每行已拼回的字段文本列表,
                            confs 为对应置信度列表。异常时回退整图 OCR。
        """
        try:
            if image_np is None or image_np.size == 0 or image_np.ndim != 3:
                return [], []
            img_h, img_w = image_np.shape[:2]
            gray = (0.299 * image_np[..., 0] + 0.587 * image_np[..., 1]
                    + 0.114 * image_np[..., 2]).astype(np.uint8)

            # 先定位标签文字 ROI,避免整袋/整景大图导致分行切到背景。
            y0, y1, x0, x1 = _text_roi(gray, margin=10)
            # ROI 坐标统一加回到全局坐标,保证后续 _cluster_items_to_rows 可复用。
            # 阈值 0.95:只要 ROI 不是几乎全图,就使用 ROI 进行分行。
            if (y1 - y0) * (x1 - x0) < img_h * img_w * 0.95:
                roi_np = image_np[y0:y1, x0:x1]
                gray_roi = gray[y0:y1, x0:x1]
            else:
                roi_np = image_np
                gray_roi = gray
                x0 = y0 = 0

            # 用两套 band 粒度(4 带 + 6 带)分别 OCR,合并后按 y 聚类成 4 行,
            # 每行选最佳字段+最佳前缀拼回。4 带覆盖整行不易漏字段,6 带更细
            # 不易把文字切在边界上,合并可互补不同图的切分偏好。
            def _run_band_ocr(nrows_band):
                bands = _form_row_bands(gray_roi, nrows_band)
                band_items = []
                for a, b in bands:
                    res = ocr.ocr(roi_np[a:b], cls=True)
                    if not res or not res[0]:
                        continue
                    for item in res[0]:
                        if not item or len(item) < 2:
                            continue
                        box, payload = item[0], item[1]
                        txt = payload[0] if payload else ''
                        conf = payload[1] if len(payload) > 1 else 1.0
                        if not txt or not txt.strip():
                            continue
                        pts = np.asarray(box, dtype=np.float64)
                        xc = float(pts[:, 0].mean()) + x0
                        yc = float(pts[:, 1].mean()) + a + y0   # 转回全局 y
                        h_box = float(pts[:, 1].max() - pts[:, 1].min())
                        w_box = float(pts[:, 0].max() - pts[:, 0].min())
                        band_items.append((txt.strip(), float(conf), xc, yc, h_box, w_box))
                return band_items

            items = _run_band_ocr(_FORM_ROWS) + _run_band_ocr(_FORM_ROWS + 2)
            row_lines, row_confs = self._cluster_items_to_rows(
                items, _FORM_ROWS, gray_roi, roi_np.shape[1], roi_np.shape[0])

            # 对照:在 ROI 上做整图单次 OCR(同一低阈值实例),仅在分行拼回无产出时兜底
            full_res = ocr.ocr(roi_np, cls=True)
            full_lines, full_confs = [], []
            if full_res and full_res[0]:
                for item in full_res[0]:
                    if not item or len(item) < 2:
                        continue
                    payload = item[1]
                    txt = payload[0] if payload else ''
                    conf = payload[1] if len(payload) > 1 else 1.0
                    if txt and txt.strip():
                        full_lines.append(txt.strip())
                        full_confs.append(float(conf))

            # 2026-08-18 v2.3: 对每行应用字段规范化(方向 1)。
            # 这种规范化在 _extract_form_lines 末尾应用,不改 cluster 内部逻辑。
            row_lines = [_normalize_field_line(ln) for ln in row_lines]
            full_lines = [_normalize_field_line(ln) for ln in full_lines]

            # 2026-08-18 v2.4: Direction 2 - 行位置 + 值模式 感知纠错。
            # D1 以右列首字为唯一信号, 对数字开头/无前缀/低相似度场景失明。
            # D2 以「行位置(磅布三文治固定顺序品名/厚度/手感/底布) +
            # 值模式」二级信号纠错。
            row_lines = _normalize_row_by_position(row_lines, nrows=_FORM_ROWS)
            full_lines = _normalize_row_by_position(full_lines, nrows=_FORM_ROWS)

            # 2026-08-18 v2.5: Direction 3 - 值驱动行重排序。
            # 2111 这类图中 OCR 把厚度值错放到品名行 (值串行), D2 按行位置纠错无能为力。
            # D3 不依赖行位置, 而按值模式识别后按字段顺序输出。
            row_lines = _reorder_by_value(row_lines, nrows=_FORM_ROWS)
            full_lines = _reorder_by_value(full_lines, nrows=_FORM_ROWS)

            # 2026-08-18 v2.6: Direction 4 - 业务词典补全 (针对 OCR 残缺输出)。
            # 仅对品名行应用 (其他字段如厚度/手感/底布都是结构化数据, 不补全)。
            row_lines = _apply_business_dict(row_lines)
            full_lines = _apply_business_dict(full_lines)

            # 2026-08-19: 移除 v2.6.1 (基于图片背景色加色字前缀)。
            # 背景色由调用方 (ocr_pipeline.extract_ocr / 移动端上传端点) 单独检测,
            # 并通过 [标签背景: 黑色|白色] 后缀写到 ocr_text 末尾 + image.bg_color 列;
            # 不再污染 OCR 文本里的「品名」「底布」value.

            # 分行拼回是专为无表格线表单设计的修复(解决漏行/行合并),
            # 但切带边界仍可能截断字段。整图 OCR 负责补回这类字段。
            if row_lines and full_lines:
                return self._merge_form_candidates(
                    row_lines, row_confs, full_lines, full_confs)
            if row_lines:
                return row_lines, row_confs
            return full_lines, full_confs
        except Exception as e:
            logger.warning('表单分行 OCR 失败,回退整图: %s', e)
            try:
                full_res = ocr.ocr(image_np, cls=True)
                lines, confs = [], []
                if full_res and full_res[0]:
                    for item in full_res[0]:
                        if not item or len(item) < 2:
                            continue
                        payload = item[1]
                        txt = payload[0] if payload else ''
                        conf = payload[1] if len(payload) > 1 else 1.0
                        if txt and txt.strip():
                            lines.append(txt.strip())
                            confs.append(float(conf))
                return lines, confs
            except Exception:
                return [], []

    @staticmethod
    def _merge_form_candidates(row_lines, row_confs, full_lines, full_confs):
        """逐行合并分行与整图 OCR 结果，优先选择信息更完整的字段。"""
        def score(line, confidence):
            if not (line or '').strip():
                return -1.0
            value = line.strip()
            score = float(confidence or 0.0)
            if any(value.startswith(prefix + '：') or value.startswith(prefix + ':')
                   for prefix in _FORM_ROW_PREFIXES):
                score += 20.0
            if re.search(r'\b\d+(?:\.\d+)?\s*m?m\b', value, re.IGNORECASE):
                score += 8.0
            score += min(len(value), 30) * 0.05
            return score

        merged_lines = []
        merged_confs = []
        length = max(len(row_lines), len(full_lines))
        for index in range(length):
            candidates = []
            if index < len(row_lines):
                candidates.append((row_lines[index],
                                   row_confs[index] if index < len(row_confs) else 0.0))
            if index < len(full_lines):
                candidates.append((full_lines[index],
                                   full_confs[index] if index < len(full_confs) else 0.0))
            if not candidates:
                merged_lines.append('')
                merged_confs.append(1.0)
                continue
            best_line, best_conf = max(candidates, key=lambda item: score(*item))
            merged_lines.append(best_line)
            merged_confs.append(best_conf)
        return merged_lines, merged_confs

    def _cluster_items_to_rows(self, items, nrows, gray, img_w, img_h):
        """无表格线表单拼回:合并去重 + 按 y 聚类成 nrows 行 + 每行选最佳字段+前缀。

        多粒度 band OCR 后同一字段/前缀可能被多次识别,或左列单字被合并成
        竖向块(如「品厚」「厚手底」)。本函数:
          1) 拆分竖向多字块为单字;
          2) 对 y 接近且文本相同/包含的条目去重,保留更长/更置信者;
          3) 按 y 坐标找最大 gap 切分成 nrows 行;
          4) 每行内选字符最多的含冒号条目作为字段,选最短靠左的无冒号条目
             作为前缀,拼回完整字段行。
        无含冒号字段时退化为 _simple_rows。返回 (lines, confs)。
        """
        if not items:
            return [], []

        # 1) 拆分竖向合并块
        expanded = []
        for txt, conf, xc, yc, hb, wb in items:
            n = len(txt)
            if n >= 2 and hb > 1.8 * max(wb, 1.0):
                for i, ch in enumerate(txt):
                    yy = (yc - hb / 2.0) + (i + 0.5) * (hb / n)
                    expanded.append((ch, conf, xc, yy, hb / n, wb))
            else:
                expanded.append((txt, conf, xc, yc, hb, wb))

        # 2) 去重:按 y 接近 + 文本相同/包含
        expanded.sort(key=lambda t: t[3])
        deduped = []
        for it in expanded:
            txt, conf, xc, yc, hb, wb = it
            merged = False
            for i, d in enumerate(deduped):
                dtxt, dconf, dxc, dyc, dhb, dwb = d
                y_overlap = abs(yc - dyc) < max(hb, dhb) * 0.7
                text_same = (txt == dtxt or txt in dtxt or dtxt in txt)
                if y_overlap and text_same:
                    if len(txt) > len(dtxt) or (len(txt) == len(dtxt) and conf > dconf):
                        deduped[i] = it
                    merged = True
                    break
            if not merged:
                deduped.append(it)

        # 3) 按 y 最大 gap 切分 nrows 行
        if len(deduped) < nrows:
            return self._simple_rows(deduped)
        ys = [t[3] for t in deduped]
        gaps = [(ys[i + 1] - ys[i], i) for i in range(len(ys) - 1)]
        gaps.sort(reverse=True)
        split_indices = sorted([idx for _, idx in gaps[:nrows - 1]])
        split_ys = [(ys[idx] + ys[idx + 1]) / 2.0 for idx in split_indices]

        rows = [[] for _ in range(nrows)]
        for it in deduped:
            y = it[3]
            r = 0
            while r < nrows - 1 and y > split_ys[r]:
                r += 1
            rows[r].append(it)

        # 4) 每行选最佳字段 + 最佳前缀
        lines, confs = [], []
        for r in rows:
            if not r:
                continue
            fields = [t for t in r if ('：' in t[0] or ':' in t[0])]
            prefixes = [t for t in r if ('：' not in t[0] and ':' not in t[0])]
            if not fields:
                # 无字段:行内按 x 拼接兜底
                r.sort(key=lambda t: t[2])
                lines.append(''.join(t[0] for t in r))
                confs.append(sum(t[1] for t in r) / len(r))
                continue

            # 字段:字符最多(信息最完整)且置信度高
            fields.sort(key=lambda t: (-len(t[0]), -t[1]))
            field_txt, field_conf = fields[0][0], fields[0][1]

            prefix = ''
            if prefixes:
                # 前缀:短(<=3 字)、靠左、置信度高
                prefixes.sort(key=lambda t: (len(t[0]), t[2], -t[1]))
                pre = prefixes[0][0]
                if len(pre) <= 3:
                    prefix = pre
                    field_conf = (field_conf + prefixes[0][1]) / 2.0
            lines.append(prefix + field_txt)
            confs.append(field_conf)
        return lines, confs

    @staticmethod
    def _simple_rows(items):
        """非两栏退化:按 yc 聚类成行、行内按 x 从左到右拼接。"""
        if not items:
            return [], []
        items_sorted = sorted(items, key=lambda t: t[3])
        # 行距阈值取相邻 yc 差的中位数,避免把多行压成一行
        ys = [t[3] for t in items_sorted]
        gaps = [ys[i + 1] - ys[i] for i in range(len(ys) - 1)]
        row_gap = max(8.0, float(np.median(gaps)) * 0.6) if gaps else 20.0
        rows = []
        for it in items_sorted:
            txt, conf, xc, yc = it[0], it[1], it[2], it[3]
            if rows and yc <= rows[-1]['max_yc'] + row_gap:
                rows[-1]['items'].append(it)
                rows[-1]['max_yc'] = max(rows[-1]['max_yc'], yc)
            else:
                rows.append({'items': [it], 'max_yc': yc})
        lines, confs = [], []
        for r in rows:
            r['items'].sort(key=lambda t: t[2])
            line = ''.join(t[0] for t in r['items'])
            c = [t[1] for t in r['items']]
            lines.append(line)
            confs.append(sum(c) / len(c))
        return lines, confs

    @staticmethod
    def _collect(result):
        """从 PaddleOCR 的 ocr() 返回值收集合并行文本与置信度。

        返回 (lines, confs):lines 为各非空文本(已 strip),confs 为对应 float。
        """
        lines, confs = [], []
        if result and result[0]:
            for item in result[0]:
                if item and len(item) >= 2:
                    t = item[1][0]
                    if t and t.strip():
                        lines.append(t.strip())
                    try:
                        confs.append(float(item[1][1]))
                    except (TypeError, ValueError):
                        pass
        return lines, confs

    @staticmethod
    def _pick_better_ocr(orig_lines, orig_confs, pre_lines, pre_confs):
        """redstamp 两遍 OCR 取优:按 (行数, 字符总数, 已知字段前缀数) 综合打分。

        背景(2026-08-28):
        _suppress_red_stamp 的 faded_red 通道阈值过宽,误擦标签文字(如「软性」),
        导致擦除图 OCR 在 60% 样本里反而比原图 OCR 漏字更多。但擦除图偶尔能修对
        红章盖住的字(如 1.0 → 1.0),不能完全抛弃。

        综合打分 = (line_count, total_chars, known_prefix_bonus)
        高者胜。同分时原图胜(更保守,避免擦除图随机性引入噪声)。

        Args:
            orig_lines / orig_confs: 原图 OCR 结果。
            pre_lines / pre_confs: 红章擦除后 OCR 结果。

        Returns:
            (winner_lines, winner_confs): 取优后的结果。
        """
        def score(lines):
            if not lines:
                return (-1, -1, -1)
            n_lines = len(lines)
            total_chars = sum(len(l) for l in lines)
            # 已知业务字段前缀加权(品名/规格/颜色/厚度/手感/底布),鼓励提取完整结构
            known_prefixes = ('品名', '规格', '颜色', '厚度', '手感', '底布', '成份', '克重')
            prefix_bonus = sum(1 for l in lines
                              if any(p in l for p in known_prefixes))
            return (n_lines, total_chars, prefix_bonus)

        s_orig = score(orig_lines)
        s_pre = score(pre_lines)
        # 同分时原图胜(更保守)
        if s_pre > s_orig:
            return list(pre_lines), list(pre_confs)
        return list(orig_lines), list(orig_confs)

    @staticmethod
    def _pick_best_of_n(passes):
        """多 pass OCR 取优(2026-09-02):用于 KIND_REDSTAMP 三遍 OCR 合并。

        输入:passes = [(lines1, confs1), (lines2, confs2), (lines3, confs3), ...]
        打分 = (thickness_pattern, line_count, total_chars, prefix_bonus),高者胜。
        同分时索引小的胜(原图 > 红章擦除 > 锐化版,越靠后越激进/噪声越多)。

        背景:环保杂胶 / 杂胶 / 纯胶等橡胶类商品的标签含印刷体+手写体混排。
        PaddleOCR 对手写数字识别弱,不同锐化参数能捕捉不同字符(实测:r2p200 抓
        「4」,r4p300 抓「1」)。整体取优至少能拿到 1 个数字。

        【2026-09-14 改进】加 thickness_pattern_bonus(权重 1000):
          形如「数字.数字」(0.8/1.0/1.2 等厚度规格)的字符在原图常被串扰成
          「928 / #08 / 9750-8707」(红章污染),而擦除图/锐化图能恢复正确的
          0.8/1.0,但字符总数比污染版少 → 旧打分输给原图。
          实测订单 1001/record 2805 三张图: orig(928-黑色)13 字符胜 pre(0.8-黑色)10 字符。
          加 thickness_pattern_bonus 后: pre 命中「0.8」+1000 → pre 稳胜 orig。
        排序键优先级:thickness_pattern > 行数 > 字符数 > 前缀数。

        Args:
            passes: list of (lines, confs) tuples,按"保守→激进"顺序排列。
        Returns:
            (winner_lines, winner_confs): 最高分 pass 的结果。
        """
        # 厚度模式: 数字.数字(如 0.8 / 1.0 / 1.2 / 0.6 等)
        # 用 re.compile 缓存到闭包避免重复编译
        import re as _re_thick
        _thickness_pat = _re_thick.compile(r'\d+\.\d+')

        def score(lines):
            if not lines:
                return (-1, -1, -1, -1)
            n_lines = len(lines)
            total_chars = sum(len(l) for l in lines)
            known_prefixes = ('品名', '规格', '颜色', '厚度', '手感', '底布', '成份', '克重')
            prefix_bonus = sum(1 for l in lines
                              if any(p in l for p in known_prefixes))
            # 厚度模式计数:多行算多次(如「0.8黑色」+「1.0」算 2 次)
            thickness_pattern = sum(
                len(_thickness_pat.findall(l)) for l in lines)
            return (thickness_pattern, n_lines, total_chars, prefix_bonus)

        best_score = (-1, -1, -1, -1)
        best_idx = -1
        for idx, (lines, confs) in enumerate(passes):
            s = score(lines)
            if s > best_score:
                best_score = s
                best_idx = idx
        if best_idx < 0:
            return [], []
        return list(passes[best_idx][0]), list(passes[best_idx][1])

    def _resolve_kind(self, preprocess_kind, apply_wrinkle_enhance):
        """统一 kind 入参:优先 preprocess_kind;旧参数 apply_wrinkle_enhance=True
        等价于 'form_nolines'(向后兼容别名)。"""
        if preprocess_kind:
            return preprocess_kind
        if apply_wrinkle_enhance:
            return KIND_FORM_NOLINES
        return None

    @log_ocr_call('ocr.paddle', evt='extract_text',
                  failed_if=lambda r: not r,          # 失败时吞异常返回空串
                  payload=image_payload, outcome=text_outcome)
    def extract_text(self, image_bytes, preprocess_kind=None,
                     apply_wrinkle_enhance: bool = False):
        """只做 OCR 提取纯文本(换行拼接),供行级/整单匹配复用。

        Args:
            image_bytes: 图片字节流。
            preprocess_kind: 退化类型预处理开关,取值见 KIND_* 常量。
              'form_nolines' → 分行 OCR(逐行识别避免行合并/漏行)
              'glare'        → 反光抑制后 OCR(无纺布)
              'redstamp'     → 红章擦除后 OCR(杂胶/纯胶)
              None/其它      → 默认 _ocr(老路径,完全向后兼容)
            apply_wrinkle_enhance: [已废弃] 兼容别名,True 等价于
              preprocess_kind='form_nolines'。

        行为契约:
          - 默认 None,老调用方零感知。
          - _wrinkle_ocr 加载失败时,_ensure_model 已 try/except 回退到 _ocr。
          - 预处理异常由各 _suppress_* 内部捕获返回原图,不抛。
          - 异常路径与原版一致(吞错返回 '')。
        """
        kind = self._resolve_kind(preprocess_kind, apply_wrinkle_enhance)
        try:
            self._ensure_model()
            resized = self._resize_if_needed(image_bytes)
            if kind == KIND_FORM_NOLINES:
                # 无表格线表单:分行 OCR(逐行识别,避免行合并/漏行),结果再与
                # 整图 OCR 择优(字符更多者胜)。详见 _extract_form_lines。
                # _resize_if_needed 已返回 numpy RGB 数组;解码失败时才回退 bytes。
                if isinstance(resized, (bytes, bytearray)):
                    img_np = np.array(Image.open(io.BytesIO(resized)).convert('RGB'))
                else:
                    img_np = resized
                ocr = (self._wrinkle_ocr or self._ocr)
                # 一次性算 bg_color, 传入分行 OCR 内部用于色字补全 (D5)。
                bg_color = _detect_bg_color_from_np(img_np)
                lines, confs = self._extract_form_lines(img_np, ocr, bg_color=bg_color)
                # 【2026-09-02】form_nolines glare 兜底(必须与 extract_text_with_conf
                # 的 KIND_FORM_NOLINES 分支保持严格同构 — /re-ocr 按钮走后者)。
                # ─────────────────────────────────────────────────────────
                # 根因:黑磅布三文治/白磅布三文治/B级磅布三文治/7P环保磅布三文治
                # 常被塑膜包裹,塑膜反光干扰 PaddleOCR band 第 2 行(厚度)。
                # 案例:订单 TD-2026-09-02-002 / image 3788 / f888b1ab...jpg
                #  - 原图 form_nolines → [品名, (空), 手感, 底布] ← 漏「厚度:1.0mm」
                #  - glare 抑制图       → [(空), 厚度, 手感, 底布] ← 偶尔丢品名
                #  - 合并后            → [品名, 厚度:1.0mm, 手感, 底布] ← 完整
                # 同构修复:本路径与 commit 8412d03 (redstamp 双 pass 取优) 同构,
                # 但 redstamp 是整图取优,本路径必须逐行合并才能既保留原图清
                # 晰字段又补回 glare 找回的字段。
                # 详见 memory: ocr-form-nolines-glare-fallback-2026-09-02
                # 调优政策: ocr-failure-tuning-process
                # 同类隐患(67 条历史中 14 条曾因此 yellow/red):
                #   product_name IN ('黑磅布三文治', '白磅布三文治',
                #                     'B级 磅布三文治', '7P环保磅布三文治')
                # 防回归:tests/test_form_nolines_glare_fallback.py (6 项)
                # ─────────────────────────────────────────────────────────
                try:
                    glare_np = _suppress_glare(img_np)
                    if glare_np is not img_np:
                        glare_lines, glare_confs = self._extract_form_lines(
                            glare_np, ocr, bg_color=bg_color)
                        if glare_lines:
                            lines, confs = self._merge_form_candidates(
                                lines, confs, glare_lines, glare_confs)
                except Exception:
                    # 兜底路径异常 → 静默用原图结果,不影响主流程
                    pass
            elif kind in (KIND_GLARE, KIND_REDSTAMP):
                # 反光/红章:图级预处理(擦除退化)后走默认 _ocr。
                if isinstance(resized, (bytes, bytearray)):
                    img_np = np.array(Image.open(io.BytesIO(resized)).convert('RGB'))
                else:
                    img_np = resized
                ocr = self._ocr
                if kind == KIND_REDSTAMP:
                    # 【2026-08-28】红章擦除 60% 样本漏字(误擦「软性」等标签文字)。
                    # OCR 两次取优: 原图通常保留更多字段, 擦除图偶尔修对红章盖住的字。
                    # 【2026-09-02】加第 3 遍 OCR = 锐化版,捕捉「环保杂胶/杂胶/纯胶」
                    # 等橡胶类商品常见的「印刷体+手写体」混排 — 工人手填厚度数字时
                    # PaddleOCR 弱,不同锐化参数能抓不同字符(r2p200 → 「4」,r4p300 → 「1」)。
                    # 三遍 OCR 整体取优:按 (line_count, total_chars, prefix_bonus)
                    # 打分,同分时原图胜(更保守)。
                    # 详见 memory: ocr-handwritten-paddleocr-fallback-2026-09-02
                    # 【2026-09-14】打分首键加 thickness_pattern(数字.数字),让擦除图
                    # 修对的 0.8 厚度压过原图 928 错读;附行级修补把 orig 品名行拼回去
                    # (擦除图常把品名前缀 LB 一并擦掉,见 test_redstamp_thickness_pattern_pick
                    # + extract_text_with_conf 同款分支)。
                    orig_lines, orig_confs = self._collect(ocr.ocr(img_np, cls=True))
                    pre = _preprocess_for_kind(img_np, kind)
                    pre_lines, pre_confs = self._collect(ocr.ocr(pre, cls=True))
                    sharp = _sharpen_for_ocr(img_np)
                    sharp_lines, sharp_confs = self._collect(ocr.ocr(sharp, cls=True))
                    lines, confs = self._pick_best_of_n([
                        (orig_lines, orig_confs),
                        (pre_lines, pre_confs),
                        (sharp_lines, sharp_confs),
                    ])
                    # 行级修补:winner 含厚度但首行短于 orig → 从 orig 借首行
                    # 必须与 extract_text_with_conf 的 KIND_REDSTAMP 分支严格同构。
                    # 判定:winner 首行不是 orig 首行的子串(说明品名前缀 LB 等被擦除)。
                    if lines and orig_lines and lines is not orig_lines:
                        import re as _re_fix
                        winner_has_thickness = any(
                            _re_fix.search(r'\d+\.\d+', _l) for _l in lines)
                        orig_first = orig_lines[0]
                        winner_first = lines[0] if lines else ''
                        # winner 首行不是 orig 首行的子串 → 修补(借 orig 首行)
                        if (winner_has_thickness
                                and winner_first
                                and orig_first not in winner_first
                                and orig_first != winner_first):
                            # 把 orig 首行替换 winner 首行,保留 winner 其它行(含厚度)
                            lines = [orig_first] + [
                                _l for _l in lines if _l != winner_first]
                else:
                    pre = _preprocess_for_kind(img_np, kind)
                    lines, _confs = self._collect(ocr.ocr(pre, cls=True))
            else:
                ocr = self._ocr
                lines, _confs = self._collect(ocr.ocr(resized, cls=True))
            if not lines:
                return ''
            text = '\n'.join(lines)
            # 2026-08-09 修复:小字「厚度：1.0mm」常被识别成「度：1.0mm」(缺"厚"),
            # 这里补回"厚"字,让下游 AI 判别的厚度规则能稳定命中。
            import re as _re
            text = _re.sub(r'(?m)^\s*度\s*[:：]\s*(\d+\.?\d*)\s*mm?\s*$',
                           r'厚度：\1mm', text)
            return text
        except Exception as e:
            logger.exception('PaddleOCR extract_text failed: %s', e)
            return ''

    def extract_text_with_conf(self, image_bytes, preprocess_kind=None,
                               apply_wrinkle_enhance: bool = False):
        """与 extract_text 类似,但额外返回平均置信度 (0~1)。

        用途:移动端 Task 6,ocr 置信度低时(模糊图)在 reason 上追加提示,
        引导用户重拍。

        Args:
            image_bytes: 图片字节流。
            preprocess_kind: 退化类型预处理开关(同 extract_text)。
            apply_wrinkle_enhance: [已废弃] 兼容别名。

        Returns:
            (text, avg_conf): text 为拼接后的 OCR 纯文本(与 extract_text 行为一致
            —— 含「厚度」字补回);avg_conf 为所有非空文本行的平均置信度。
            失败或无文字时 avg_conf=1.0(不触发模糊提示)。
        """
        kind = self._resolve_kind(preprocess_kind, apply_wrinkle_enhance)
        try:
            self._ensure_model()
            resized = self._resize_if_needed(image_bytes)
            if kind == KIND_FORM_NOLINES:
                # 同 extract_text:无表格线表单走分行 OCR,并带置信度。
                if isinstance(resized, (bytes, bytearray)):
                    img_np = np.array(Image.open(io.BytesIO(resized)).convert('RGB'))
                else:
                    img_np = resized
                ocr = (self._wrinkle_ocr or self._ocr)
                bg_color = _detect_bg_color_from_np(img_np)
                lines, confs = self._extract_form_lines(img_np, ocr, bg_color=bg_color)
                # 【2026-09-02】form_nolines glare 兜底(出货页 ↻重 OCR 按钮路径)。
                # ─────────────────────────────────────────────────────────
                # 必须与 extract_text 的 KIND_FORM_NOLINES 分支严格同构 ——
                # /re-ocr 端点(出货页 ↻重 OCR 按钮)走 extract_text_with_conf,
                # 如果只修 extract_text 会绕过兜底(首次修复就漏了这里,见
                # test 6: test_extract_text_with_conf_also_has_glare_fallback)。
                # ─────────────────────────────────────────────────────────
                # 根因/案例/同类隐患/防回归:见 extract_text 同款注释块
                # 详见 memory: ocr-form-nolines-glare-fallback-2026-09-02
                # ─────────────────────────────────────────────────────────
                try:
                    glare_np = _suppress_glare(img_np)
                    if glare_np is not img_np:
                        glare_lines, glare_confs = self._extract_form_lines(
                            glare_np, ocr, bg_color=bg_color)
                        if glare_lines:
                            lines, confs = self._merge_form_candidates(
                                lines, confs, glare_lines, glare_confs)
                except Exception:
                    pass
            elif kind in (KIND_GLARE, KIND_REDSTAMP):
                if isinstance(resized, (bytes, bytearray)):
                    img_np = np.array(Image.open(io.BytesIO(resized)).convert('RGB'))
                else:
                    img_np = resized
                ocr = self._ocr
                if kind == KIND_REDSTAMP:
                    # 【2026-08-28】与 extract_text 一致:红章擦除 60% 漏字,
                    # OCR 两次取优(原图+擦除图)。
                    # 【2026-09-02】三遍 OCR(原图+擦除+锐化),与 extract_text 严格同构,
                    # 见 extract_text 的 KIND_REDSTAMP 注释 + memory
                    # ocr-handwritten-paddleocr-fallback-2026-09-02。
                    orig_lines, orig_confs = self._collect(ocr.ocr(img_np, cls=True))
                    pre = _preprocess_for_kind(img_np, kind)
                    pre_lines, pre_confs = self._collect(ocr.ocr(pre, cls=True))
                    sharp = _sharpen_for_ocr(img_np)
                    sharp_lines, sharp_confs = self._collect(ocr.ocr(sharp, cls=True))
                    lines, confs = self._pick_best_of_n([
                        (orig_lines, orig_confs),
                        (pre_lines, pre_confs),
                        (sharp_lines, sharp_confs),
                    ])
                    # 【2026-09-14】行级修补:擦除/锐化图常把品名前缀(LB 等)一并擦掉,
                    # 而原图保留了完整品名行;打分函数只择一 → winner 含厚度但缺品名。
                    # 修补规则:winner 含 数字.数字 厚度行 + 首行短于 orig 最长行 ≥3 字符
                    # → 用 orig 最长行(通常是品名)替换 winner 首行。
                    # 与 extract_text 的 KIND_REDSTAMP 分支严格同构(见对应注释)。
                    if lines and orig_lines and lines is not orig_lines:
                        import re as _re_fix
                        winner_has_thickness = any(
                            _re_fix.search(r'\d+\.\d+', _l) for _l in lines)
                        orig_first = orig_lines[0]
                        winner_first = lines[0] if lines else ''
                        # winner 首行不是 orig 首行子串 → 修补(借 orig 首行)
                        if (winner_has_thickness
                                and winner_first
                                and orig_first not in winner_first
                                and orig_first != winner_first):
                            lines = [orig_first] + [
                                _l for _l in lines if _l != winner_first]
                            # confs 同步:借用 orig 首行置信度,其余用 winner 原置信度
                            orig_first_conf = (orig_confs[0]
                                               if orig_confs else 0.95)
                            tail_confs = confs[1:] if len(confs) > 1 else []
                            confs = [orig_first_conf] + tail_confs
                            if len(confs) != len(lines):
                                confs = [0.95] * len(lines)
                else:
                    pre = _preprocess_for_kind(img_np, kind)
                    lines, confs = self._collect(ocr.ocr(pre, cls=True))
            else:
                ocr = self._ocr
                lines, confs = self._collect(ocr.ocr(resized, cls=True))
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


# 2026-08-09 改造:删掉本地硬编码的 CLASS_FALLBACK_KEYWORDS,
# 改用 models.category_prompt._load_level3_keywords() 从 DB 派生。
# 旧的分类关键字表是历史包袱,与 models 里的 FALLBACK_CATEGORY_KEYWORDS
# 已发生 drift;统一为 DB 派生后,两路分类器自动跟随 product_categories 表。




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
    MAX_TOKENS = 24000  # 【2026-09-17】从 12000 加倍到 24000,跟 MoonshotEngine 对齐。
    # reasoning token 长度对同图随机波动,12K 仍偶发 finish_reason='length',24K 留 2x 余量。
    # 同步 TIMEOUT 60→120(见下)。
    # 【2026-09-02】30s 太短 — 长 prompt(3000+ chars 注入自适应提示词)在高峰时段
    # 偶发响应超过 30s 触发 ReadTimeout。调到 60s 给长 prompt 留 2 倍冗余。
    # 【2026-09-17】MAX_TOKENS 翻倍后,reasoning 用满 24K token 在慢网络下可能跑 90s+,
    # 60s 必超时 ReadTimeout。从 60 翻倍到 120 留 2 倍冗余。
    TIMEOUT = 120

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
        '【不要遗漏 OCR 证据】\n'
        '  - OCR 文字是唯一输入，必须按版面和语义把所有可识别的商品/字段都保留下来，不能因为某一行缺少完整品名、规格或数量就整行删除。\n'
        '  - 标签的品名、厚度、手感、底布、规格、数量、单位、备注可能被分成多行或只识别出其中几行；要把属于同一标签/同一行的碎片合并后提取。\n'
        '  - 如果不确定一个片段应放入哪个字段，优先保留原文到 remark 或 specification，不得静默丢弃；不要为了凑字段而虚构品名、规格或数量。\n'
        '  - OCR 中存在多个候选结果时要合并互补信息，不要用较短的候选覆盖已经识别出的更长、更完整的候选。\n\n'
        '  - 订单表格按【OCR行】提供；每一行的文字按图片从左到右排列，表格空白单元格仍要保留位置，不能把上一行或下一行的同名列串过来。\n'
        '  - 单位、备注等单元格没有识别到文字时输出空字符串，不能根据上下文猜填；品名/规格/数量也必须按表头和行列位置分别提取。\n\n'
        '════════════════════════════════════════\n'
        '【关键规则 — 表格行严格按「行号」列对齐（防止品名/规格/数量串行）】\n'
        '════════════════════════════════════════\n'
        '订单单据每一行商品有唯一的"行号"（1、2、3…）。OCR 已经把每个文字块的 (x, y) 坐标保留下来，'
        '并按 Y 聚类成行，"OCR第N行：……" 中的 N 就是行号。\n'
        '必须严格按以下对齐规则（违反会导致品名+规格+数量串行，例如把第一行的"纯胶"标错到第二行的"3504"）：\n'
        '  1) **同行的字段才属于同一商品**。商品行结构："行号 + 商品全名 + 规格 + 单位 + 数量 + 备注"。\n'
        '     识别一行时只取同一 OCR 行内的文字，不能跨行拼接。\n'
        '  2) **行号列只读，不是商品字段**。"1"、"2"、"3" 是行号，绝对不能当 quantity 或 specification。\n'
        '  3) **数量与品名不能错配**。OCR 已按 Y 把同一行所有文字放在一起："纯胶 1.2黑软纯胶 码 3504 96支" 是完整一行。\n'
        '     如果 OCR 第 2 行是 "三文治 1.0黑双 码 2001 58支"，那 2001 是三文治的数量，不是纯胶的。\n'
        '  4) **校验兜底**：每条 items[i].quantity 必须与该行 OCR 中的数字字段一致。\n'
        '     如果 LLM 输出的 quantity 与该行 OCR 数字不匹配 → 立刻从 OCR 行重读修正。\n'
        '  5) **空白单元格不猜填**：某行 OCR 中"数量"位置无数字 → quantity 填 "1"，备注说明缺失，不要从其他行借数字。\n'
        '  6) **2026-09-18 强化：相邻行同数字禁止互换**。OCR 经常出现 2~4 行 "环保三文治 + 1.0黑双 / 0.8黑双 / 0.6黑双 + 194/242.5/727.5" 这种相邻行的数字排列；\n'
        '     **绝对不允许把相邻两行的数字互换**——例如把第 2 行的"1.0黑双" 配成第 3 行的数字 242.5。\n'
        '     数字归属该行的依据是它在该 OCR 行内**实际出现的字符串**,而不是它在全部 OCR 中的位置。\n'
        '  7) **2026-09-18 强化：双数字配对检查**。相邻两行的数字如果长度、形态相近（如 "194" vs "242.5" 都是 3 位 y 数），'
        '必须在 items 输出前**逐条**回答 "OCR 第 N 行的所有数字按出现顺序是: [列表]",然后按这个列表直接配对。\n'
        '     不要在脑子里"看着相近"自动交换。\n\n'
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
        '════════════════════════════════════════\n【关键规则 — 字面保真(LLM 防止改字)】\n════════════════════════════════════════\n商品名称、规格、单位、备注 必须 OCR 原文逐字保留，严禁把 OCR 中的字替换为同音/形近字。常见错例(必须避免):“磅布三文治” → 误改成 “磷布三文治”(磅≠磷)、“皮革” → 误改成 “皮格”(革≠格)、“中性” → 误改成 “中” 等。OCR 文字是怎么写的，JSON 里就怎么写，即使看起来 “不合理” 也要保留原文；后续若有疑问由人工核对，LLM 不要主动改字。\n\n只返回纯 JSON 对象，不要 markdown 代码块，不要解释文字。\n'
        '示例（注意示例中的 remark 都是有值的）：\n'
        '{"doc_number":"SO20260701","customer_name":"AC公司","items":['
        '{"product_name":"PVC桌布","specification":"1.2×1.8m","quantity":"50","unit":"支","remark":"急单"},'
        '{"product_name":"无纺布","specification":"2m","quantity":"30","unit":"kg","remark":"A-03仓位"},'
        '{"product_name":"7P环保杂胶","specification":"","quantity":"100","unit":"y","remark":"每箱25y"}'
        ']}\n'
        '如果没有可识别的商品行，返回 {"doc_number":"","customer_name":"","items":[]}\n\n'
        # 注意:"OCR 识别的文字如下：\n" 这个尾巴挪到 recognize() 里拼,
        # 以便在它前面插入动态的【码基产品字段对齐】规则(见 _load_yard_product_names)。
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
        '  - 规格里无加面/单面字样 + OCR 文字里有"加面"或"单面" → 默认视为多余信息，不判错\n'
        '    ★ 例外（2026-08-28 加严）：若 OCR 文字中"加面"嵌在品名词组内（如"18P加面杂胶"、"7P加面纯胶"、"加面三文治"、"双面杂胶"等"X加面Y"或"Y加面"形式），\n'
        '      "加面"是该商品的实际类型（双面 ≡ 加面），规格里必须有"加面"字样才算对得上；\n'
        '      若规格里无"加面"也未在备注/说明里标注加面 → 关键冲突，应判 red（让人工核对是规格漏写还是拿错货）。\n'
        '    ★ 对称条款（2026-08-28）："单面"是商品默认形态。OCR 文字中若"单面"嵌在品名词组内（如"18P单面杂胶中软"、"7P单面纯胶"），\n'
        '      表示实物就是单面型，规格里**不应**出现"加面"字样（单面 ≡ 非加面）；若规格含"加面" → 关键冲突 red。\n'
        '      （注：此规则亦已被上方"规格里有加面 + OCR 文字里有单面 → 加面不匹配 ✗"覆盖，此处显式重申以防 LLM 漏看。）\n'
        '\n'
        '▌规则 5：手感 / 软硬度匹配（软、中、硬）\n'
        '规格中常含手感词：软、中、硬。其中"中"是"中性"的简写，判定前一律先把"中"归一化为"中性"（中 ≡ 中性）。\n'
        'OCR 文字里若出现"手感"、"软"、"中性"、"中"、"硬"等字样，按以下规则与规格比对：\n'
        '  - 规格手感"软" + OCR 有"软" → 手感匹配 ✓\n'
        '  - 规格手感"软" + OCR 有"中性"/"中" → 手感不一致（规格要软，标签是中性）→ 按 yellow 处理，需人工核对\n'
        '  - 规格手感"中性"/"中" + OCR 有"中性"/"中" → 手感匹配 ✓\n'
        '  - 规格手感"硬" + OCR 有"硬" → 手感匹配 ✓\n'
        '  - 规格有手感词 + OCR 无任何手感词（连"手感"字样都没有）→ 手感缺失，按 yellow 处理\n'
        '  - 规格无手感词 + OCR 有手感词 → 视为多余信息，不判错\n'
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
        """品名 → 品类代码(level 3 category_code)。

        1. 先查 product 表(product_name → category_id → 向上取 level=3 父节点)
        2. 兜底(2026-08-09 改造):models.category_prompt._load_level3_keywords()
           从 product_categories.level=3 自动派生,共享 classify_record 的逻辑
        3. 都无匹配 → None

        2026-08-09 之前用的 CLASS_FALLBACK_KEYWORDS 已删除,与
        models.category_prompt.FALLBACK_CATEGORY_KEYWORDS 合并为单一 DB 派生来源。
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
            conn.close()
            # ── 策略 2: 复用 classify_record 的 DB 派生兜底 ──
            from models.category_prompt import classify_record
            cls = classify_record(product_name=product_name)
            return cls['category_code'] if cls else None
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

            # (b) DB 派生关键词(2026-08-09):复用 classify_record 的兜底,
            #     取该 category_code 对应品类名切出的 tokens
            from models.category_prompt import _load_level3_keywords
            for entry in _load_level3_keywords():
                if entry['category_code'] == category_code:
                    for kw in entry['tokens']:
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

    def compare_single_record(self, ocr_text, record, supplement_prompt: str = ''):
        """2026-07-30 新增:对单条 record 比对 OCR 文字,供行级图上传及 AI判别按钮调用。

        与 compare_rows 区别:prompt 只含 1 行(单 record),省 token、省时间。
        2026-07-31 改造:返回 prompt_text + raw_response 供前端「详情」按钮审计。
        2026-08-09 改造:接受 supplement_prompt(由调用方从 CategoryPrompt.compose_for_record 取),
                       非空时拼到 prompt 末尾「自适应提示词」段(与 compare_rows 一致)。

        返回 {'match_status', 'match_score', 'reason', 'prompt_text', 'raw_response'}

        调用方负责异常处理 — 失败应 fall back 到本地 RapidFuzz。
        """
        row_json = json.dumps(
            {'record_id': record.get('id', 0), 'product_name': record.get('product_name', ''),
             'specification': record.get('specification', '')},
            ensure_ascii=False)
        base_prompt = (self.COMPARE_PROMPT + '\n【OCR文字】\n' + ocr_text +
                       '\n【明细行】\n' + row_json)
        if supplement_prompt:
            prompt_text = (base_prompt +
                           '\n\n════════════════════════════════════════\n'
                           '【自适应提示词(同品类 / 同规格历史人工案例)】\n'
                           '════════════════════════════════════════\n' + supplement_prompt)
        else:
            prompt_text = base_prompt
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

        2026-09-17 改造:解析前显式检查 finish_reason='length',抛
        DeepSeekResponseTruncated(RuntimeError 子类),让上层
        ocr_pipeline.classify 把它当作"deepseek 失败 → fallback 本地 fuzzy",
        而不是含糊的 APIError('DeepSeek 返回非 JSON: ...')。该路径之前的
        APIError 调用缺 request= 参数,会再抛 TypeError(实测 2026-09-17
        log/202609/ocr-2026-09-17.log:155-158),走不到 fallback。

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
        except httpx.ReadTimeout as e:
            # 【2026-09-02】长 prompt(3000+ chars)在高峰时段偶发 ReadTimeout,
            # 重试 1 次给 DeepSeek 二次机会。仅 ReadTimeout 重试(连接错误重试
            # 容易把网络问题放大),重试间隔 1 秒。
            import time as _time
            try:
                with httpx.Client(timeout=self.TIMEOUT) as cli:
                    resp = cli.post(url, headers=headers, json=body)
                    resp.raise_for_status()
                    data = resp.json()
            except httpx.HTTPError as e2:
                raise openai.APIConnectionError(
                    message=f'无法连接 DeepSeek 服务(已重试 1 次): {type(e2).__name__}: {e2}',
                    request=e2.request) from e2
        except httpx.HTTPStatusError as e:
            # 401/403/429 等 SDK 风格的异常(确保 _classify_and_match_image 的 try/except 能 fallback)
            raise openai.APIStatusError(
                f'DeepSeek 服务异常({e.response.status_code}): {e.response.text[:200]}',
                request=e.request, response=e.response,
            )
        except httpx.HTTPError as e:
            # 网络/连接问题
            raise openai.APIConnectionError(
                message=f'无法连接 DeepSeek 服务: {type(e).__name__}: {e}',
                request=e.request) from e
        # ── finish_reason 必须在解析 JSON 前显式检查:length 表示 max_tokens 用尽,
        # JSON 还没写完,json.loads 会抛 JSONDecodeError 而非真正的"格式异常"。
        # 抛专用异常,让上层 fallback 路径能区分"截断"和"格式错"。
        finish_reason = ''
        try:
            finish_reason = (data.get('choices') or [{}])[0].get('finish_reason', '') or ''
        except (KeyError, IndexError, TypeError, AttributeError):
            finish_reason = ''
        if finish_reason == 'length':
            # 【2026-09-17】reasoning token 经常吃光 budget(JSON 还没写),
            # 截断时 raw 多半是半截 JSON 或空串 → 一律按截断处理。
            logger.warning(
                'DeepSeek 响应被截断(max_tokens=%s, multi=%s, prompt_chars=%s)',
                self.MAX_TOKENS, multi, len(prompt_text))
            raise DeepSeekResponseTruncated(
                f'DeepSeek 响应被截断(max_tokens={self.MAX_TOKENS}, '
                f'prompt_chars={len(prompt_text)}, multi={multi})')
        raw = ''
        try:
            raw = (data.get('choices') or [{}])[0].get('message', {}).get('content', '') or ''
            raw = raw.strip()
        except (KeyError, IndexError, TypeError, AttributeError) as e:
            # 响应结构异常(openai.APIError 必须传 request=,此处无原生 request 对象,
            # 改用我们的专用异常透传给上层)
            raise DeepSeekResponseTruncated(
                f'DeepSeek 响应结构异常: {str(e)[:200]}') from e
        raw = re.sub(r'^\s*```[a-zA-Z]*\s*\n?', '', raw)
        raw = re.sub(r'\n?\s*```\s*$', '', raw).strip()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            # 仍有可能 finish_reason='stop' 但 content 是坏 JSON(罕见,可能是模型返回了
            # 中文逗号 / 前言文本)。同样按 truncated 处理,让上层 fallback 本地 fuzzy。
            logger.warning('DeepSeek 返回非 JSON (finish_reason=%s): %s', finish_reason, raw[:200])
            raise DeepSeekResponseTruncated(
                f'DeepSeek 返回非 JSON(finish_reason={finish_reason}): {raw[:200]}') from e
        if multi:
            return parsed if isinstance(parsed, list) else parsed.get('results', [])
        return parsed

    def __init__(self):
        self._ocr_engine = PaddleOCREngine()

    def _ocr_image(self, image_bytes):
        """调用 PaddleOCR 提取纯文本行,并保留表格的行列顺序。

        2026-09-18 路径分离:本函数**只服务订单截图/扫描件** OCR(出货单/送货单/
        装车单 等有表格线单据,文字清晰、可能压缩/截图)。调用 PaddleOCR 默认
        实例 _ocr + Y 自适应聚类(默认 _adaptive_y_threshold clamp [5, 20])。

        **不要**走 `KIND_FORM_NOLINES` / `KIND_GLARE` / `KIND_REDSTAMP` 分支 ——
        那三条是给**实物商品标签**(模糊/反光/红章)OCR 用的(见 PaddleOCREngine.
        extract_text kind 参数)。订单截图是清晰印刷体,激进阈值会过度切分;实物标签
        的预处理(CLAHE/glare 抑制/redstamp 擦除)会损坏清晰截图的细节。

        历史回滚:曾改走 KIND_FORM_NOLINES(2026-09-18 初版修复),装车单是有表格线
        的多行单据,form_nolines 固定 4 行切分会把 5+ 行表格压成 4 段,反而更乱。
        回滚到默认 _ocr + Y 聚类,并把 clamp 从 [10, 35] 收紧到 [5, 20](装车单行距
        8-12 px 太密集,原下界 10 会合并相邻行)。
        """
        self._ocr_engine._ensure_model()
        resized = self._ocr_engine._resize_if_needed(image_bytes)
        # 用默认 _ocr 实例,不用 _wrinkle_ocr(那是给实物标签的激进实例)
        result = self._ocr_engine._ocr.ocr(resized, cls=True)
        lines, _ = self._ocr_engine._collect(result)
        return self._format_table_rows(result, lines)

    def _format_table_rows(self, result, default_lines):
        """有足够 OCR 文本时按 Y 聚类为行，保留从左到右的单元格顺序。"""
        if not result or not result[0] or len(result[0]) < 4:
            return '\n'.join(default_lines)
        lines = _extract_lines(result)
        if len(lines) < 4:
            return '\n'.join(default_lines)
        rows = _group_into_rows(lines)  # 2026-08-24: 自适应阈值,适配带表格线出货单
        if len(rows) < 2:
            return '\n'.join(default_lines)
        return '\n'.join(
            f'OCR第{index}行：{row}' for index, row in enumerate(rows, 1))

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

            # 注入码基产品清单,让 LLM 输出与 API/存储口径对齐:
            # 码基产品 quantity=码数、unit=y、remark=「N支」(避免 LLM 把支数填进 quantity)。
            yard_names = _load_yard_product_names()
            yard_rule = ''
            if yard_names:
                yard_rule = (
                    '\n════════════════════════════════════════\n'
                    '【关键规则 — 码基产品字段对齐(必须遵守)】\n'
                    '════════════════════════════════════════\n'
                    '以下产品按码(y)计量,字段必须这样填:\n'
                    '  quantity = 码数(图片里的实际码长,数字,如 1642.5)\n'
                    '  unit = "y"\n'
                    '  remark = 支数(形如"45支")\n'
                    '即:若图片同时出现"45支"和码数(如 1642.5),码数进 quantity、unit 填 y、"45支"进 remark。\n'
                    '码基产品清单: ' + '、'.join(yard_names) + '\n\n'
                )
            prompt = self.STRUCT_PROMPT + yard_rule + 'OCR 识别的文字如下：\n'
            client = openai.OpenAI(api_key=self.API_KEY, base_url=self.BASE_URL)
            response = client.chat.completions.create(
                model=self.MODEL,
                messages=[{
                    'role': 'user',
                    'content': prompt + ocr_text
                }],
                max_tokens=self.MAX_TOKENS,
                timeout=self.TIMEOUT,
                # 强制 JSON 输出:防止 DeepSeek 输出 markdown 围栏/前言导致 parse 失败
                # (历史 bug: 17 行订单含特殊字符时,模型用尽 reasoning token,返回空 content)
                response_format={'type': 'json_object'},
            )
            finish_reason = response.choices[0].finish_reason
            raw = response.choices[0].message.content.strip()
            # 【2026-09-17】finish_reason='length' 表示 max_tokens 用尽 → 响应被截断。
            # 真因是 DeepSeek v4-flash 内部 chain-of-thought reasoning 把 budget 吃光,
            # 即使只有 1-2 行商品也会截断(reasoning 长度对同图随机波动,09-16/09-17
            # 日志实证:同 7KB 图 30s 成功 / 43s 截断交替)。不要告诉用户去拆单/
            # 删备注——那是无效操作,只会增加挫败感。
            if finish_reason == 'length':
                # 【2026-09-17】DeepSeek 截断时自动 fallback 到 MiniMax 多模态引擎重试 1 次。
                # 截断真因是 DeepSeek v4-flash 内部 chain-of-thought reasoning 把 budget
                # 吃光(reasoning 长度对同图随机波动,见 09-16/09-17 日志)。换不同推理模型
                # 有合理概率成功。仅在 MiniMax API key 已配置 + 同图重试 1 次 — 避免
                # 对用户隐藏问题(成功路径会带 fallback_minimax=True 标记)。
                fallback = _try_minimax_fallback(image_bytes, filename)
                if fallback is not None:
                    return fallback
                return {
                    'success': False,
                    'error': 'DeepSeek 响应被截断(AI 推理超出预算)',
                    'hint': '真因是模型内部推理消耗 token,与订单行数/备注长度无关。'
                            '建议:1) 直接重试(下次推理可能更短);2) 切换到 Moonshot 或 PaddleOCR 引擎;'
                            '3) 换张更清晰的图片(模糊图会迫使模型推理更久)'
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
            # 2026-09-18:把 OCR 原文一起回前端,让人工核对(防 DeepSeek 串行错配)
            return {
                'success': True,
                'items': items,
                'doc_number': doc_number,
                'customer_name': customer_name,
                'ocr_text': ocr_text,
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

    # ── 自由文本 → 结构化明细行(2026-09-22 入库页新 tab)───────────────────────
    # 场景:用户粘贴/手敲「普通加面杂胶: 0.6白软加面 53支+30码」等自由格式,
    # 跳 PaddleOCR,直接用 DeepSeek 解析成 {product_name, specification,
    # quantity, unit, remark} 行结构。复用 STRUCT_PROMPT + 码基产品对齐规则 +
    # _filter_summary_items,输出 schema 与图片路径完全对齐,前端可共用同一套
    # #aiResultBody 编辑/添加流程。
    TEXT_STRUCT_PROMPT = (
        '你是一个商品信息提取助手。用户会用自由格式(可能含中文冒号/英文冒号/'
        '换行/数量+单位组合等)描述一批商品,你需要把它们解析成结构化的明细行。\n\n'
        '【常见输入形态(参考)】\n'
        '- 「普通加面杂胶: 0.6白软加面 53支+30码」 — 一段描述,品名和规格都包含在内\n'
        '- 「环保杂胶 1.2米 20支 / 软性纯胶 0.8mm 15支」 — 多行,分隔符多样\n'
        '- 「PVC桌布 1.2×1.8m 50支」 — 标准 品名/规格/数量 格式\n'
        '- 「0.6白软加面 53支+30码」 — 只有规格+数量\n'
        '- 「川盛皮纺: 普通加面杂胶 0.6白软加面 53支+30码 / 软性纯胶 0.8黑中软纯胶 80支」 — 抬头是客户,后面跟多个商品\n\n'
        '【数量 + 单位 + 备注的解析规则】\n'
        '- 「53支+30码」型:quantity=53,unit="支",remark="30码"(多余单位挪到备注)\n'
        '- 「53支 30码」型:同上,空格或+都视为分隔\n'
        '- 「30码」型(没支数):quantity=30,unit="y",remark=""\n'
        '- 「50支」型(没码数):quantity=50,unit="支",remark=""\n'
        '- 「100」型(纯数字):unit 默认为 "支",若数字明显是码(>30 且商品为杂胶/纯胶等码基)改 unit="y"\n\n'
        '【品名/规格拆分规则】\n'
        '- 如果品名整体带「:」或「：」,冒号前是品名族(如"普通加面杂胶"),冒号后是规格\n'
        '- 没冒号时,按空格拆 — 第一段通常是品名(如「环保杂胶」「软性纯胶」「PVC桌布」),'
        '中间带数字/字母/单位的(如「0.6白软加面」「1.2×1.8m」「B白300」)归为规格\n'
        '- 同义前缀(普通/定制/环保/加面/双面/单面)归到品名段\n\n'
        '返回 JSON 对象,包含以下字段:\n'
        '- doc_number: 单据编号 — 自由文本里若显式出现"订单号/单据编号/单号/编号/Order No"等,'
        '提取冒号后/空格后的内容;无则空字符串\n'
        '- customer_name: 客户名称 — 文本开头/标题位置的"川盛皮纺""ABC Co."等单位名;'
        '**只取名称本身,不要把"客户/Customer/客户单位"等字段标签算进去**。'
        '若不确定,空字符串。\n'
        '- items: 商品列表数组,每条商品:\n'
        '  - product_name: 商品名称(必须)\n'
        '  - specification: 规格型号(必须,无则空字符串)\n'
        '  - quantity: 数量(必须,字符串形式的数字如 "53" 或 "30")\n'
        '  - unit: 单位(必须,候选 "支"/"y"/"kg"/"桶"/"张"/"件"/"箱"/"卷"/"块"/"令"/"斤")\n'
        '  - remark: 备注(必须填写!剩余的码数、颜色、规格后缀等放进来,无则空字符串)\n\n'
        '════════════════════════════════════════\n'
        '【关键规则 — 严禁编造】\n'
        '════════════════════════════════════════\n'
        '- 文本里没有的商品不要瞎填,客户名和单据号不确定就空字符串\n'
        '- 数量必须是文本里出现过的数字,不要根据常识猜\n'
        '- 多个商品用换行/斜杠/分号/空格分隔时,按分隔符逐个提取\n'
        '- 只返回 JSON,不要任何额外解释或 markdown 围栏\n\n'
        '════════════════════════════════════════\n'
        '【数据一致性硬约束(2026-09-22 用户要求)】\n'
        '════════════════════════════════════════\n'
        '- **每个返回的 product_name + specification 组合,必须在下方「候选产品清单」里能找到贴近的项**。\n'
        '  如果找不到,改用最贴近的候选名(可以贴边但不脱离清单);\n'
        '  如果候选清单为空(没召回任何商品),才允许按文本自由生成。\n'
        '- 不要发明不存在的品名前缀(如「7P」「普通」「普通环保」等业务类别前缀,'
        '必须是商品表里真实存在的);不要发明不存在的规格(如「0.99米」「ABC-123」等)。\n'
        '- 口语化输入(如「白杂胶」「黑皮糠纸」「环保在胶」)→ 映射到候选里的标准名/规格,'
        '不要直接照抄口语原文。\n\n'
    )

    def recognize_text(self, text):
        """直接接收用户输入的自由文本,DeepSeek 解析为结构化明细行。

        输入: 自由文本字符串(可能含多行/多种分隔符)
        返回: 与 recognize() 一致的 dict 结构
            success=True:  {success, items, doc_number, customer_name, ocr_text: <原始文本>}
            success=False: {success=False, error, hint}
        """
        if not self.API_KEY or self.API_KEY.startswith('sk-your-'):
            return {
                'success': False,
                'error': 'DeepSeek API key 未配置',
                'hint': '请在 .env 中设置 DEEPSEEK_API_KEY 后重启应用'
            }
        text = (text or '').strip()
        if not text:
            return {'success': False, 'error': '输入文本为空', 'hint': '请先粘贴或输入商品描述'}
        # 截断极端输入(防 DeepSeek reasoning 把 budget 吃光,跟图片路径同样的根因)
        max_chars = 4000
        truncated = False
        if len(text) > max_chars:
            text = text[:max_chars]
            truncated = True

        try:
            # ── 候选产品预过滤(2026-09-22)─────────────────────────
            # 从输入文本抽中文关键词 → LIKE 查 product 表 → 取 top 15 行注入 prompt,
            # 让 LLM 就近匹配标准名/规格,避免自由发挥出"0.6白软加面"等近似错别字。
            # 候选清单为空时(纯数字/英文文本),L 退化为原「无候选」模式自由生成。
            candidates = _select_relevant_products(text)
            candidate_rule = ''
            if candidates:
                lines = ['  - ' + (pn or '(无)') + ' | ' + (sp or '(无)')
                         for pn, sp in candidates]
                candidate_rule = (
                    '\n════════════════════════════════════════\n'
                    f'【候选产品清单 — 共 {len(candidates)} 项 / 全文 1145 件】\n'
                    '════════════════════════════════════════\n'
                    '以下是从商品表按文本关键词预筛的标准品名/规格,**优先从这里挑选**,'
                    ' 避免拼写/标点/前后空格等近似错别字。如果某行商品不在清单里才允许自由生成。\n'
                    '格式:「品名 | 规格」\n'
                    + '\n'.join(lines) + '\n\n'
                )

            # 注入码基产品清单(与图片路径完全一致,避免 quantity/unit 颠倒)
            yard_names = _load_yard_product_names()
            yard_rule = ''
            if yard_names:
                yard_rule = (
                    '\n════════════════════════════════════════\n'
                    '【关键规则 — 码基产品字段对齐(必须遵守)】\n'
                    '════════════════════════════════════════\n'
                    '以下产品按码(y)计量,字段必须这样填:\n'
                    '  quantity = 码数(若文本里同时出现码数和支数,码数进 quantity)\n'
                    '  unit = "y"\n'
                    '  remark = 支数(形如"45支")\n'
                    '即:若文本同时出现"45支"和码数(如 1642.5),码数进 quantity、unit 填 y、"45支"进 remark。\n'
                    '码基产品清单: ' + '、'.join(yard_names) + '\n\n'
                )
            # ── 2026-09-22 用户累积的「口语→结构化」补充提示词(全局注入)───
            # 见 _compose_user_supplements() 的设计:用户加一条 → 后续别再犯。
            user_supplements = _compose_user_supplements()
            supplements = ''  # 占位 — 未来扩展点(动态生成的提示词),现阶段为空
            prompt = self.TEXT_STRUCT_PROMPT + candidate_rule + yard_rule + supplements + user_supplements + '用户输入的文本如下：\n' + text
            client = openai.OpenAI(api_key=self.API_KEY, base_url=self.BASE_URL)
            response = client.chat.completions.create(
                model=self.MODEL,
                messages=[{'role': 'user', 'content': prompt}],
                max_tokens=self.MAX_TOKENS,
                timeout=self.TIMEOUT,
                response_format={'type': 'json_object'},
            )
            finish_reason = response.choices[0].finish_reason
            raw = response.choices[0].message.content.strip()
            if finish_reason == 'length':
                return {
                    'success': False,
                    'error': 'DeepSeek 响应被截断(AI 推理超出预算)',
                    'hint': '真因是模型内部推理消耗 token,与文本长度无关。建议:1) 直接重试;'
                            '2) 把文本拆成两段分别解析。'
                }
            if not raw:
                return {
                    'success': False,
                    'error': 'DeepSeek 返回内容为空',
                    'hint': '可能原因:输入文本过长。建议重试或把文本拆短'
                }
            raw = re.sub(r'^\s*```[a-zA-Z]*\s*\n?', '', raw)
            raw = re.sub(r'\n?\s*```\s*$', '', raw)
            raw = raw.strip()
            logger.info("DeepSeek recognize_text raw (first 500 chars): %s", raw[:500])
            data = json.loads(raw)

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
                    'hint': '请重试或把文本改写得更结构化(如每行一个商品)'
                }
            # 复用图片路径的汇总行安全网(防止 LLM 把"合计/N行"当成商品行)
            items, _removed = _filter_summary_items(items)
            result = {
                'success': True,
                'items': items,
                'doc_number': doc_number,
                'customer_name': customer_name,
                'ocr_text': text,  # 跟图片路径保持同名,前端统一处理
            }
            if truncated:
                result['truncated'] = True
                result['hint'] = f'输入文本超过 {max_chars} 字符,已截断前 {max_chars} 字符解析'
            return result
        except ImportError:
            return {
                'success': False,
                'error': 'openai 包未安装',
                'hint': '请运行: pip install openai'
            }
        except openai.AuthenticationError:
            return {
                'success': False,
                'error': 'DeepSeek API key 失效(401)',
                'hint': '请检查 .env 中的 DEEPSEEK_API_KEY 是否正确'
            }
        except openai.APIConnectionError:
            return {
                'success': False,
                'error': '无法连接 DeepSeek 服务',
                'hint': '请检查网络连接'
            }
        except openai.RateLimitError:
            return {
                'success': False,
                'error': 'DeepSeek 调用频率过高(429)',
                'hint': '请稍候几秒后重试'
            }
        except openai.APIStatusError as e:
            return {
                'success': False,
                'error': f'DeepSeek 服务返回异常({e.status_code}){_api_error_detail(e)}',
                'hint': '请根据错误信息检查请求参数,或检查 DeepSeek 平台状态'
            }
        except json.JSONDecodeError:
            return {
                'success': False,
                'error': 'DeepSeek 返回格式无法解析',
                'hint': '请重试,或把文本改写成"品名 规格 数量 单位"的标准格式'
            }
        except Exception as e:
            logger.exception('DeepSeek recognize_text 失败: %s', e)
            return {
                'success': False,
                'error': f'DeepSeek 解析失败:{type(e).__name__}',
                'hint': '请重试'
            }


# ═══════════════════════════════════════════════════════════════════
# DeepSeek 截断 fallback 到 MiniMax 多模态(2026-09-17)
# ═══════════════════════════════════════════════════════════════════

def _try_minimax_fallback(image_bytes, filename):
    """DeepSeek 截断时调 MiniMax 多模态重试 1 次。

    返回 None 表示 fallback 不可用(无 key / MiniMax 也失败 / MiniMax 抛异常),
    调用方继续走 DeepSeek 原报错 — MiniMax 是静默 fallback,其错误不该泄漏给用户。
    返回 dict 表示 MiniMax 成功结果(已带 fallback_minimax=True 标记)。

    仅当 MINIMAX_API_KEY 已配置时才启用 — 跟现有 DEEPSEEK_API_KEY 一致走 .env。

    多模态路径:不传 PaddleOCR 文本,让 MiniMax-M3 直接看图。DeepSeek 已用
    PaddleOCR 抽过一轮文本,但多模态模型看原图通常更准(09-17 决策)。
    """
    minimax_key = os.environ.get('MINIMAX_API_KEY', '').strip()
    if not minimax_key or minimax_key.startswith('sk-your-'):
        logger.info(
            'DeepSeek 截断,但 MiniMax 未配置(MINIMAX_API_KEY 未设),跳过 fallback')
        return None
    try:
        logger.info('DeepSeek 截断,fallback 到 MiniMax 多模态(图片 + OCR 文本)')
        result = MiniMaxEngine().recognize(image_bytes, filename)
        if not result.get('success'):
            # MiniMax 也失败 → 不让 MiniMax 错误覆盖 DeepSeek 错误,返回 None
            # 让上层继续走 DeepSeek 的"AI 推理超出预算"提示
            logger.warning('MiniMax fallback 失败: %s', result.get('error', '?'))
            return None
        # 成功路径:加审计标记
        result['fallback_minimax'] = True
        result['original_engine'] = 'deepseek'
        logger.info(
            'MiniMax fallback 成功(items=%s, doc_number=%s)',
            len(result.get('items') or []),
            result.get('doc_number') or '')
        return result
    except Exception as e:
        # fallback 自身炸了不能影响主流程,只 log
        logger.exception('MiniMax fallback 异常: %s', e)
        return None


# ═══════════════════════════════════════════════════════════════════
# MiniMax 云端多模态引擎(2026-09-17,DeepSeek 截断 fallback + 可作主引擎)
# ═══════════════════════════════════════════════════════════════════

class MiniMaxEngine(BaseOCREngine):
    """MiniMax M3 多模态视觉引擎(国内 OpenAI 兼容 API)。

    用途:
    - 主路径:整单 / 入库 / 装柜 ai-recognize 选 MiniMax 作为识别引擎
    - fallback 路径:DeepSeek 截断时自动调 MiniMax 重试 1 次(见 _try_minimax_fallback)

    文档:https://platform.minimax.cn/docs/api-reference/text-chat-openai
    端点:https://api.minimaxi.com/v1(国内 OpenAI 兼容)
    模型:MiniMax-M3(支持 image_url 多模态输入,JPEG/PNG/GIF/WEBP,最大 10MB)

    配置:在 .env 写入 MINIMAX_API_KEY=sk-api-...,与 DEEPSEEK_API_KEY 一致不入 git。
    """

    API_KEY = os.environ.get('MINIMAX_API_KEY', '')
    BASE_URL = 'https://api.minimaxi.com/v1'
    MODEL = 'MiniMax-M3'
    MAX_TOKENS = 24000  # 与 DeepSeekEngine / MoonshotEngine 对齐(2026-09-17 加倍)
    TEMPERATURE = 1  # M3 推荐 1,见官方 quickstart
    TIMEOUT = 120  # 与 DeepSeekEngine 对齐(2026-09-17 加倍)

    # 复用 DeepSeekEngine 的 STRUCT_PROMPT — 提取规则完全相同,只是推理模型不同
    STRUCT_PROMPT = DeepSeekEngine.STRUCT_PROMPT

    def recognize(self, image_bytes, filename=''):
        if not self.API_KEY or self.API_KEY.startswith('sk-your-'):
            return {
                'success': False,
                'error': 'MiniMax API key 未配置',
                'hint': '请在 .env 中设置 MINIMAX_API_KEY 后重启应用'
            }

        # MiniMax 跟 Moonshot 一样支持多模态:image_url + text 一起发。
        # 不走 PaddleOCR(避免重复 OCR),让模型直接看图 + 我们的提示词。
        # PaddleOCR 的 OCR 文本(若 DeepSeek fallback 场景下传入)暂时丢弃 — 多模态
        # 模型直接看图准确率更高。DeepSeek fallback 调用方传进来的 ocr_text 参数
        # 仅作日志/debug 用。
        import base64
        b64 = base64.b64encode(image_bytes).decode('ascii')
        # data URL 前缀按文件扩展名猜,MiniMax 文档说支持 JPEG/PNG/GIF/WEBP
        ext = (filename.rsplit('.', 1)[-1] if '.' in filename else 'png').lower()
        mime = {'jpg': 'jpeg', 'jpeg': 'jpeg', 'png': 'png',
                'gif': 'gif', 'webp': 'webp'}.get(ext, 'png')
        data_url = f'data:image/{mime};base64,{b64}'

        # 注入码基产品清单(同 DeepSeekEngine.recognize)
        yard_names = _load_yard_product_names()
        yard_rule = ''
        if yard_names:
            yard_rule = (
                '\n════════════════════════════════════════\n'
                '【关键规则 — 码基产品字段对齐(必须遵守)】\n'
                '════════════════════════════════════════\n'
                '以下产品按码(y)计量,字段必须这样填:\n'
                '  quantity = 码数(图片里的实际码长,数字,如 1642.5)\n'
                '  unit = "y"\n'
                '  remark = 支数(形如"45支")\n'
                '即:若图片同时出现"45支"和码数(如 1642.5),码数进 quantity、unit 填 y、"45支"进 remark。\n'
                '码基产品清单: ' + '、'.join(yard_names) + '\n\n'
            )
        prompt = self.STRUCT_PROMPT + yard_rule

        try:
            client = openai.OpenAI(api_key=self.API_KEY, base_url=self.BASE_URL)
            response = client.chat.completions.create(
                model=self.MODEL,
                messages=[{
                    'role': 'user',
                    'content': [
                        {'type': 'image_url', 'image_url': {'url': data_url}},
                        {'type': 'text', 'text': prompt},
                    ],
                }],
                max_tokens=self.MAX_TOKENS,
                temperature=self.TEMPERATURE,
                timeout=self.TIMEOUT,
                response_format={'type': 'json_object'},
            )
            finish_reason = response.choices[0].finish_reason
            raw = (response.choices[0].message.content or '').strip()
            if finish_reason == 'length':
                return {
                    'success': False,
                    'error': 'MiniMax 响应被截断(AI 推理超出预算)',
                    'hint': '建议:1) 直接重试;2) 切换到 PaddleOCR 本地引擎'
                }
            if not raw:
                return {
                    'success': False,
                    'error': 'MiniMax 返回内容为空',
                    'hint': '可能原因:prompt 触发。建议重试或切换引擎'
                }
            raw = re.sub(r'^\s*```[a-zA-Z]*\s*\n?', '', raw)
            raw = re.sub(r'\n?\s*```\s*$', '', raw).strip()
            logger.info("MiniMax raw response (first 500 chars): %s", raw[:500])
            data = json.loads(raw)

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
                    'error': 'MiniMax 返回格式异常',
                    'hint': '请重试或切换到其他引擎'
                }
            items, _removed = _filter_summary_items(items)
            return {
                'success': True,
                'items': items,
                'doc_number': doc_number,
                'customer_name': customer_name,
            }
        except openai.APIConnectionError as e:
            return {
                'success': False,
                'error': f'MiniMax 连接失败:{type(e).__name__}',
                'hint': '检查网络 / API key 是否正确'
            }
        except openai.RateLimitError:
            return {
                'success': False,
                'error': 'MiniMax 调用频率过高（429）',
                'hint': '请稍候几秒后重试'
            }
        except openai.APIStatusError as e:
            return {
                'success': False,
                'error': f'MiniMax 服务返回异常（{getattr(e, "status_code", "?")}）',
                'hint': '请根据错误信息检查请求参数,或检查 MiniMax 平台状态'
            }
        except json.JSONDecodeError:
            return {
                'success': False,
                'error': 'MiniMax 返回格式无法解析',
                'hint': '请重试或切换到其他引擎'
            }
        except Exception as e:
            logger.exception('MiniMax 引擎失败: %s', e)
            return {
                'success': False,
                'error': f'MiniMax 引擎失败:{type(e).__name__}',
                'hint': '请重试或切换到其他引擎'
            }


# ═══════════════════════════════════════════════════════════════════
# 引擎工厂
# ═══════════════════════════════════════════════════════════════════

_engine_cache = {}
_VALID_ENGINES = ('moonshot', 'paddleocr', 'deepseek', 'minimax')

# 引擎 → 当前活跃模型(防回归参考表)
# 维护原则:DeepSeek/Moonshot/MiniMax 偶尔下线旧模型,出现 400 + "model not found" 时来这里对照,
# 到对应平台 docs 查最新清单后改对应引擎类的 MODEL 常量。
_ACTIVE_MODELS = {
    'moonshot': 'kimi-k2.6',
    'deepseek': 'deepseek-v4-flash',  # v4-pro 备选,价格 10x
    'paddleocr': '(本地模型,无需 token)',
    'minimax': 'MiniMax-M3',
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
            f'请使用 "moonshot"、"paddleocr"、"deepseek" 或 "minimax"'
        )

    if engine_name not in _engine_cache:
        if engine_name == 'moonshot':
            _engine_cache[engine_name] = MoonshotEngine()
        elif engine_name == 'deepseek':
            _engine_cache[engine_name] = DeepSeekEngine()
        elif engine_name == 'minimax':  # 2026-09-17 新增,DeepSeek 截断 fallback
            _engine_cache[engine_name] = MiniMaxEngine()
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
