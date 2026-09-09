"""共享辅助：图片上传、文件存储、单位换算、备注校验等。

所有订单蓝图（出货/入库/装柜）共用的工具函数集中在这里。
"""
import os
import re
import uuid
import base64
from datetime import datetime
from flask import request
from rapidfuzz import fuzz


# 仓库根目录
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# =====================================================================
#  图片上传
# =====================================================================

def get_upload_dir(month_str=None):
    """返回上传目录，自动创建。

    Args:
        month_str: 月份字符串如 '2026-06'，默认当月

    Returns:
        (upload_dir, month_str) 二元组
    """
    if month_str is None:
        month_str = datetime.now().strftime('%Y-%m')
    upload_dir = os.path.join(BASE_DIR, 'upload', month_str)
    os.makedirs(upload_dir, exist_ok=True)
    return upload_dir, month_str


def save_base64_image(data_url, ext='png'):
    """从 data URL (data:image/...;base64,...) 解码并保存，返回绝对路径。"""
    header, b64 = data_url.split(',', 1)
    if 'png' in header:
        ext = 'png'
    elif 'jpeg' in header or 'jpg' in header:
        ext = 'jpg'
    elif 'gif' in header:
        ext = 'gif'
    elif 'webp' in header:
        ext = 'webp'
    upload_dir, _ = get_upload_dir()
    filename = f"{uuid.uuid4().hex}.{ext}"
    filepath = os.path.join(upload_dir, filename)
    with open(filepath, 'wb') as f:
        f.write(base64.b64decode(b64))
    return filepath


def save_uploaded_file(file_storage):
    """保存 Flask FileStorage 对象到 upload 目录，返回 (filepath, original_name)。"""
    original_name = file_storage.filename or ''
    if '.' in original_name:
        ext = original_name.rsplit('.', 1)[-1].lower()
    else:
        ext = 'png'
    if ext not in ['png', 'jpg', 'jpeg', 'gif', 'webp']:
        ext = 'png'
    upload_dir, _ = get_upload_dir()
    filename = f"{uuid.uuid4().hex}.{ext}"
    filepath = os.path.join(upload_dir, filename)
    file_storage.save(filepath)
    return filepath, original_name


def handle_order_image_upload():
    """统一的图片上传入口（出货/入库/装柜通用）：
    - JSON + base64 粘贴
    - multipart 文件上传
    返回 (json_response, http_status)
    """
    if request.is_json:
        data = request.get_json()
        image_data = data.get('image')
        if image_data and image_data.startswith('data:image'):
            filepath = save_base64_image(image_data)
            return {'filepath': filepath, 'original_name': ''}

    if 'image' in request.files:
        file = request.files['image']
        if file.filename:
            filepath, original_name = save_uploaded_file(file)
            return {'filepath': filepath, 'original_name': original_name}

    return None


# =====================================================================
#  商品单位提示（出货/入库通用）
# =====================================================================

def get_ypp(product_name, spec, units_cache=None):
    """从商品名 + 规格匹配 YPP（yards per piece），返回浮点码/支。

    Args:
        product_name: 商品名
        spec: 规格字符串
        units_cache: 可选的 units 列表（避免在循环中反复查 DB），
                     元素需有 product_name/spec_keyword/yards_perPiece/is_usingyardforcounting 字段
                     （为兼容 sqlite3.Row，访问用 []）

    Returns:
        float YPP（>0 表示启用码数提示），0 表示不适用
    """
    from models import ProductUnit  # 延迟导入避免循环依赖
    if units_cache is not None:
        matched = _match_unit_in_cache(product_name, spec, units_cache)
    else:
        matched = ProductUnit.get_match(product_name, spec)
    if matched and matched['is_usingyardforcounting']:
        return matched['yards_per_piece'] / 100.0
    return 0


def _match_unit_in_cache(product_name, spec, units_cache):
    """在预加载的 units 列表里找匹配项。先找 spec_keyword 匹配的，未命中则取默认行。

    与 ProductUnit.get_match 和 JS 端 ProductRowUtils.findUnit 行为一致：两轮匹配，
    避免默认行（无 spec_keyword）排在前面时抢在精确匹配之前返回。
    """
    spec_lower = (spec or '').lower()
    # 第一轮：优先匹配有 spec_keyword 且关键字在 spec 中的行
    if spec_lower:
        for u in units_cache:
            if u['product_name'] != product_name:
                continue
            kw = u['spec_keyword']
            if kw and kw.lower() in spec_lower:
                return u
    # 第二轮：兜底取没有 spec_keyword 的默认行
    for u in units_cache:
        if u['product_name'] != product_name:
            continue
        if not u['spec_keyword']:
            return u
    return None


def calc_hint(quantity_str, ypp, unit=None, remark=None):
    """根据数量和 YPP 算支数提示，如 "33支" 或 "33支+0.5码"。

    Args:
        quantity_str: 数量字符串（如 "50"）
        ypp: 码/支（>0 才计算）
        unit: 单位（可选）。当单位为"支"时直接返回数量作为支数提示
        remark: 备注（可选）。当无 YPP 配置时，从备注中提取支数作为兜底

    Returns:
        str 提示，空串表示不适用
    """
    try:
        qty = float(quantity_str)
    except (ValueError, TypeError):
        return ''
    # 优先级 0:备注里有 *Yy(per_piece) → 用 per_piece 算(与 check_remark 同构)
    # 见 :182-229 check_remark 的 per_piece 抽取规则
    if remark:
        m_mul1 = re.search(r'(\d+(?:\.\d+)?)\s*[yY码]\s*\*\s*(\d+)支', remark)
        if m_mul1:
            per_piece = float(m_mul1.group(1))
        else:
            m_mul2 = re.search(r'(\d+)支\s*\*\s*(\d+(?:\.\d+)?)\s*[yY码]', remark)
            if m_mul2:
                per_piece = float(m_mul2.group(2))
            else:
                per_piece = None
        if per_piece is not None and per_piece > 0:
            pieces = int(qty / per_piece)
            remainder = qty - (pieces * per_piece)
            if remainder < 0.01:
                return f'{pieces}支'
            return f'{pieces}支+{round(remainder, 2)}码'
    # 单位已经是支：直接以数量作为支数提示（无需 YPP 配置）
    if unit == '支':
        if qty == int(qty):
            return f'{int(qty)}支'
        return f'{qty}支'
    # 单位是码（或其他）：需要 YPP 配置才能换算
    if ypp > 0:
        pieces = int(qty / ypp)
        remainder = qty - (pieces * ypp)
        if remainder < 0.01:
            return f'{pieces}支'
        return f'{pieces}支+{round(remainder, 2)}码'
    # 兜底：从备注中提取支数（如 kg 计重的无纺布备注 "3支"）
    if remark:
        m = re.search(r'(\d+)支', remark)
        if m:
            return f'{m.group(1)}支'
    return ''


def check_remark(remark, quantity_str, ypp):
    """校验备注里 "X支" + 散码是否与 quantity 一致。

    备注语义：
      - "X支*Yy"  → 乘法：X 支，每支 Y 码，期望 = X × Y
      - "X支+Yy"  → 加法：X 支 + Y 码散码，期望 = X × ypp + Y
      - 两种可混合，如 "3支*48.5y+2y" → 期望 = 3 × 48.5 + 2

    Returns:
        '' (一致) / 'info' (轻微不一致，单支) / 'warn' (明显不一致)
    """
    if not remark or ypp <= 0:
        return ''
    m_pieces = re.search(r'(\d+)支', remark)
    if not m_pieces:
        return ''
    pieces = int(m_pieces.group(1))

    # 1) 抓 * 形式（两种顺序都支持）
    per_piece = None
    m_mul1 = re.search(r'(\d+(?:\.\d+)?)\s*[yY码]\s*\*\s*(\d+)支', remark)
    if m_mul1:
        per_piece = float(m_mul1.group(1))
    else:
        m_mul2 = re.search(r'(\d+)支\s*\*\s*(\d+(?:\.\d+)?)\s*[yY码]', remark)
        if m_mul2:
            per_piece = float(m_mul2.group(2))

    # 2) 把 * 形式整段抠掉，剩下 [+Yy / 裸 Yy] 才算散码
    remark_no_mul = re.sub(
        r'(\d+(?:\.\d+)?)\s*[yY码]\s*\*\s*(\d+)支', '', remark
    )
    remark_no_mul = re.sub(
        r'(\d+)支\s*\*\s*(\d+(?:\.\d+)?)\s*[yY码]', '', remark_no_mul
    )
    loose = sum(
        float(m) for m in re.findall(r'(\d+(?:\.\d+)?)[yY码]', remark_no_mul)
    )

    yards_per_piece = per_piece if per_piece is not None else ypp
    expected = pieces * yards_per_piece + loose
    try:
        actual = float(quantity_str)
    except (ValueError, TypeError):
        return ''
    if abs(expected - actual) <= 0.01:
        return ''
    return 'info' if pieces == 1 else 'warn'


def find_ypp_mismatches(records, units_cache=None):
    """扫一批明细,找出所有 YPP 规则冲突项(数量与备注支数+码数不匹配)。

    判定规则与 check_remark 一致:
      - 没 YPP 配置(is_usingyardforcounting=0 或 yards_per_piece<=0)→ 跳过
      - 备注里没有 "X支" → 跳过(无 YPP 校验对象)
      - check_remark 返回 '' → 一致,跳过
      - 返回 'info'/'warn' → 列入结果

    Args:
        records: 明细列表,每条需含 product_name/specification/quantity/unit/remark
                 可选含 order_pk/date/customer 用于结果展示
        units_cache: 预加载的 product_units 列表(避免循环查 DB),
                     元素需含 product_name/spec_keyword/yards_per_piece/is_usingyardforcounting
                     None 时内部查一次 DB。

    Returns:
        list[dict] 不匹配项明细,按 (severity desc, date desc, record_id) 排序
        每条:
          record_id, order_id, date, customer,
          product_name, specification, quantity, unit, remark,
          pieces, per_piece_yards (或 None), loose_yards, ypp,
          expected (round 2), actual (float|None), diff (round 2|None),
          severity ('info' / 'warn')
    """
    if units_cache is None:
        from models import ProductUnit  # 延迟导入避免循环
        units_cache = ProductUnit.get_all() or []

    def _ypp(pn, sp):
        sl = (sp or '').lower()
        if sl:
            for u in units_cache:
                if u['product_name'] != pn:
                    continue
                kw = u['spec_keyword']
                if kw and kw.lower() in sl:
                    if u['is_usingyardforcounting'] and u['yards_per_piece'] > 0:
                        return u['yards_per_piece'] / 100.0
                    return 0
        for u in units_cache:
            if u['product_name'] != pn:
                continue
            if not u['spec_keyword']:
                if u['is_usingyardforcounting'] and u['yards_per_piece'] > 0:
                    return u['yards_per_piece'] / 100.0
                return 0
        return 0

    mismatches = []
    for r in records:
        pn = r.get('product_name') or ''
        sp = r.get('specification') or ''
        qt = r.get('quantity') or ''
        rk = r.get('remark') or ''

        # 备注里没有 "X支" → 无 YPP 校验对象,跳过
        if not re.search(r'\d+支', rk):
            continue

        ypp = _ypp(pn, sp)
        if ypp <= 0:
            continue

        severity = check_remark(rk, qt, ypp)
        if severity == '':
            continue

        # 拆解期望值的组成,便于前端展示
        m_pieces = re.search(r'(\d+)支', rk)
        pieces = int(m_pieces.group(1))
        per_piece = None
        m1 = re.search(r'(\d+(?:\.\d+)?)\s*[yY码]\s*\*\s*(\d+)支', rk)
        if m1:
            per_piece = float(m1.group(1))
        else:
            m2 = re.search(r'(\d+)支\s*\*\s*(\d+(?:\.\d+)?)\s*[yY码]', rk)
            if m2:
                per_piece = float(m2.group(2))
        no_mul = re.sub(r'(\d+(?:\.\d+)?)\s*[yY码]\s*\*\s*(\d+)支', '', rk)
        no_mul = re.sub(r'(\d+)支\s*\*\s*(\d+(?:\.\d+)?)\s*[yY码]', '', no_mul)
        loose = sum(float(x) for x in re.findall(r'(\d+(?:\.\d+)?)[yY码]', no_mul))
        ypp_eff = per_piece if per_piece is not None else ypp
        expected = pieces * ypp_eff + loose

        try:
            actual = float(qt)
        except (TypeError, ValueError):
            actual = None
        diff = (actual - expected) if actual is not None else None

        mismatches.append({
            'record_id': r.get('id'),
            'order_id': r.get('order_pk'),
            'date': r.get('date') or '',
            'customer': r.get('customer') or '',
            'product_name': pn,
            'specification': sp,
            'quantity': qt,
            'unit': r.get('unit') or '',
            'remark': rk,
            'pieces': pieces,
            'per_piece_yards': per_piece,
            'loose_yards': loose,
            'ypp': ypp,
            'expected': round(expected, 2),
            'actual': actual,
            'diff': round(diff, 2) if diff is not None else None,
            'severity': severity,
        })

    # warn 排前,info 排后;同 severity 内日期倒序,再按 record_id
    sev_rank = {'warn': 0, 'info': 1}

    def _sort_key(m):
        sev = sev_rank.get(m['severity'], 9)
        # 日期 'YYYY-MM-DD' → int 比较,无日期当 0 排最后
        d_int = int(m['date'].replace('-', '')) if m['date'] else 0
        return (sev, -d_int, m['record_id'] or 0)

    mismatches.sort(key=_sort_key)
    return mismatches


def summarize_remarks(records):
    """从一组明细里汇总备注支数、散码支数，以及辅助单位提示中的总支数。

    Args:
        records: 明细列表，每条需要有 'remark'、'unit_hint' 字段

    Returns:
        dict {
            'summary_pieces': int,        # 备注中 "X支" 的支数合计
            'summary_loose_pieces': int,   # 备注中散码出现次数
            'summary_unit_pieces': float,  # 辅助单位提示中提取的支数合计
        }
    """
    total_pieces = 0
    total_loose = 0
    total_unit_pieces = 0.0
    for item in records:
        remark = item.get('remark', '')
        if remark:
            m_pieces = re.search(r'(\d+)支', remark)
            if m_pieces:
                total_pieces += int(m_pieces.group(1))
            # 先抠掉 *Yy 形式(per_piece 码数,不是散码),再数剩下的 [Yy/码] 才是真散码
            # 与 check_remark 的 per_piece 提取规则同构,见 :182-229
            remark_no_mul = re.sub(
                r'(\d+(?:\.\d+)?)\s*[yY码]\s*\*\s*(\d+)支', '', remark
            )
            remark_no_mul = re.sub(
                r'(\d+)支\s*\*\s*(\d+(?:\.\d+)?)\s*[yY码]', '', remark_no_mul
            )
            total_loose += len(re.findall(r'\d+(?:\.\d+)?[yY码]', remark_no_mul))
        # 从辅助单位提示中提取支数（涵盖 unit=支 直接显示 + unit=y 换算后的结果）
        hint = item.get('unit_hint', '')
        if hint:
            m_hint = re.search(r'(\d+(?:\.\d+)?)支', hint)
            if m_hint:
                total_unit_pieces += float(m_hint.group(1))
    # 如果 total_unit_pieces 是整数，转为 int 显示
    if total_unit_pieces == int(total_unit_pieces):
        total_unit_pieces = int(total_unit_pieces)
    return {
        'summary_pieces': total_pieces,
        'summary_loose_pieces': total_loose,
        'summary_unit_pieces': total_unit_pieces,
    }


def compute_placement_expected_zhi(remark, quantity_str, unit):
    """按 record 计算 placement 期望支数与是否有支目标。

    规则(2026-09-03 placement 期望值兜底;2026-09-09 扩展到令/张):
      1. 先按 remark 解析所有「X支」之和。
      2. 若 remark 无「X支」且 unit in ('支','令','张'):
           用 float(quantity_str) 兜底作为 expected_zhi, has_zhi=True。
           (quantity 解析失败或 ≤0 → 兜底失效, has_zhi=False。)
           — 拷贝纸(令)/日本纸(张)的 quantity 本身就是点数目标值。
      3. 否则沿用 remark 解析结果。

    Returns:
        (expected_zhi: float, has_zhi: bool)

    Note:
        - expected_zhi 始终以 float 返回(便于跨订单统一比较)
        - 整数 expected_zhi 仍以 33.0 形式返回(数值本身是整数)
        - 模板如需去小数点,用 `{{ '%g' % value }}` Jinja 格式符
        - 单位标签(支/令/张)由前端 placement_count.js 从 tr[data-count-unit] 读取,
          后端只管数值口径,不掺单位文字。
    """
    remark = remark or ''
    qty_str = (quantity_str or '').strip()

    # 1. 按原口径解析 remark 中的「X支」
    zhi_m = list(re.finditer(r'(\d+)\s*支', remark))
    remark_zhi = sum(int(x.group(1)) for x in zhi_m)
    has_remark_zhi = len(zhi_m) > 0

    # 2. 兜底:unit in ('支','令','张') 且备注无支数 → 用 quantity
    #    (拷贝纸=令 / 日本纸=张,quantity 即目标令/张数)
    if not has_remark_zhi and (unit or '').strip() in ('支', '令', '张'):
        try:
            qty_val = float(qty_str)
        except (ValueError, TypeError):
            qty_val = 0.0
        if qty_val > 0:
            return float(qty_val), True
        return 0.0, False

    # 3. 沿用 remark 解析结果
    return float(remark_zhi), has_remark_zhi


# =====================================================================
#  件数换算（件 → 张/只/令）
# =====================================================================

def extract_pieces_from_remark(remark):
    """从备注提取件数 + 散装张数。

    支持格式:
      "5件"             → pieces=5, loose=0
      "5件+20张"        → pieces=5, loose=20     # 5件按规格折算 + 20张散
      "5件*100张+20张"   → pieces=5, per_piece_override=100, loose=20  (乘形式+加形式)
                            (但当前用法多见 "5件" 配 piece_conversions 表, 暂不强支持 * 形式)

    Returns:
        dict {pieces, loose, per_piece_override} 或 None (无 "件" 字)
        per_piece_override: 当备注里用 * 指定每件张数时 (如 "5件*100张"), 用它替代
                          piece_conversions 表的 units_per_piece
    """
    if not remark:
        return None
    m = re.search(r'(\d+)件', remark)
    if not m:
        return None
    pieces = int(m.group(1))

    per_piece_override = None
    # 乘法: "5件*100张" 或 "100张*5件"
    m_mul1 = re.search(r'(\d+)件\s*\*\s*(\d+(?:\.\d+)?)\s*张', remark)
    m_mul2 = re.search(r'(\d+(?:\.\d+)?)\s*张\s*\*\s*(\d+)件', remark)
    if m_mul1:
        per_piece_override = float(m_mul1.group(2))
    elif m_mul2:
        per_piece_override = float(m_mul2.group(1))

    # 把 * 形式整段抠掉, 剩下的 [+Zz / 裸 Zz] 才是散装张数
    no_mul = re.sub(r'(\d+)件\s*\*\s*(\d+(?:\.\d+)?)\s*张', '', remark)
    no_mul = re.sub(r'(\d+(?:\.\d+)?)\s*张\s*\*\s*(\d+)件', '', no_mul)
    loose = sum(float(m) for m in re.findall(r'(\d+(?:\.\d+)?)张', no_mul))

    return {'pieces': pieces, 'loose': loose, 'per_piece_override': per_piece_override}


def _match_piece_in_cache(product_name, spec, cache):
    """在预加载的 piece_conversions 列表中两轮匹配。

    与 _match_unit_in_cache 行为一致：
    1. 优先匹配 spec_keyword 在 spec 中出现的行
    2. 兜底取 spec_keyword 为空的默认行
    """
    spec_lower = (spec or '').lower()
    if spec_lower:
        for pc in cache:
            if pc['product_name'] != product_name:
                continue
            kw = pc['spec_keyword']
            if kw and kw.lower() in spec_lower:
                return pc
    for pc in cache:
        if pc['product_name'] != product_name:
            continue
        if not pc['spec_keyword']:
            return pc
    return None


def get_piece_conversion(product_name, spec, cache=None):
    """查询件数换算规则。

    Args:
        product_name: 商品名
        spec: 规格字符串
        cache: 预加载的 piece_conversions 列表（避免循环查 DB），
               元素需有 product_name / spec_keyword / units_per_piece / target_unit 字段

    Returns:
        dict 或 None
    """
    from models import PieceConversion
    if cache is not None:
        return _match_piece_in_cache(product_name, spec, cache)
    return PieceConversion.get_match(product_name, spec)


def calc_piece_quantity(remark, conversion):
    """备注'X件' → 期望数量 X × units_per_piece (+ 散装张数)。

    Args:
        remark: 备注字符串
        conversion: get_piece_conversion 返回的 dict (含 units_per_piece)

    Returns:
        float 期望数量，或 None（备注无件数或无换算规则）
    """
    parsed = extract_pieces_from_remark(remark)
    if parsed is None or conversion is None:
        return None
    per_piece = parsed.get('per_piece_override') or conversion['units_per_piece']
    return parsed['pieces'] * per_piece + parsed['loose']


def check_piece_mismatch(remark, quantity_str, conversion):
    """校验备注件数 + 散装张数 vs 实际数量。

    规则（与 check_remark 对齐）:
        expected = 件数 × 每件张数 + 散装张数
        偏差 ≤ 0.01 → ''
        1 件偏差 → 'info' (粉)
        多件偏差 → 'warn' (红)

    Returns:
        '' / 'info' / 'warn'
    """
    parsed = extract_pieces_from_remark(remark)
    if parsed is None or conversion is None:
        return ''
    per_piece = parsed.get('per_piece_override') or conversion['units_per_piece']
    expected = parsed['pieces'] * per_piece + parsed['loose']
    try:
        actual = float(quantity_str)
    except (ValueError, TypeError):
        return ''
    if abs(expected - actual) <= 0.01:
        return ''
    return 'info' if parsed['pieces'] == 1 else 'warn'


# ── 图片内容安全校验 ─────────────────────────────────
# 三层防御:扩展名白名单 + 文件大小 + magic bytes + Pillow 真实解码
# 拒绝伪装文件(如 .exe 改后缀 .png、PNG header 后跟随机字节)

from PIL import Image, UnidentifiedImageError

_ALLOWED_IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'}
_MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10MB
# 各扩展名对应的 magic bytes 头部(只读前 12 字节就够区分)
_MAGIC_SIGNATURES = {
    '.png':  [b'\x89PNG\r\n\x1a\n'],
    '.jpg':  [b'\xff\xd8\xff'],
    '.jpeg': [b'\xff\xd8\xff'],
    '.gif':  [b'GIF87a', b'GIF89a'],
    '.webp': [b'RIFF'],  # WebP 文件头 12 字节为 RIFF....WEBP
    '.bmp':  [b'BM'],
}


def _check_magic(filepath, ext):
    """读前 12 字节,验证 magic bytes 与扩展名匹配。"""
    sigs = _MAGIC_SIGNATURES.get(ext)
    if not sigs:
        return  # 无 sig 时不拦(扩展名已白名单)
    try:
        with open(filepath, 'rb') as f:
            head = f.read(12)
    except OSError:
        raise ValueError("无法读取文件")
    if not any(head.startswith(s) for s in sigs):
        raise ValueError(f"文件内容不是有效的 {ext} 图片(magic bytes 不匹配)")


def _check_pillow(filepath):
    """Pillow 真实解码 → 截断/损坏的图会抛 UnidentifiedImageError。"""
    try:
        with Image.open(filepath) as im:
            im.verify()  # verify 不解码像素,只校验结构
    except (UnidentifiedImageError, OSError, SyntaxError) as e:
        raise ValueError(f"文件不是可识别的图片(Pillow 解码失败: {e})")


def validate_image_content(filepath):
    """校验已落盘文件是否为合法图片(扩展名 + 大小 + magic + Pillow)。"""
    if not filepath or not os.path.exists(filepath):
        raise ValueError("文件不存在")
    ext = os.path.splitext(filepath)[1].lower()
    if ext not in _ALLOWED_IMAGE_EXTS:
        raise ValueError(f"不支持的图片格式: {ext}")
    size = os.path.getsize(filepath)
    if size == 0:
        raise ValueError("空文件")
    if size > _MAX_IMAGE_BYTES:
        raise ValueError(f"文件过大(>{_MAX_IMAGE_BYTES // 1024 // 1024}MB)")
    _check_magic(filepath, ext)
    _check_pillow(filepath)
    return True


def check_uploaded_image(file_storage):
    """校验上传的 FileStorage,返回扩展名(含 .)。

    三层防御:扩展名 + 大小 + magic bytes(读前 12 字节,不全量加载)。
    Pillow 真实解码留到落盘后的 validate_image_content 里做。
    """
    if not file_storage or not file_storage.filename:
        raise ValueError("未选择文件")
    ext = os.path.splitext(file_storage.filename)[1].lower()
    if ext not in _ALLOWED_IMAGE_EXTS:
        raise ValueError(f"不支持的图片格式: {ext}")
    # 大小(靠 seek+length,不读全文)
    file_storage.seek(0, os.SEEK_END)
    size = file_storage.tell()
    file_storage.seek(0)
    if size > _MAX_IMAGE_BYTES:
        raise ValueError(f"文件过大(>{_MAX_IMAGE_BYTES // 1024 // 1024}MB)")
    # magic bytes(读前 12 字节)
    head = file_storage.read(12)
    file_storage.seek(0)
    sigs = _MAGIC_SIGNATURES.get(ext, [])
    if sigs and not any(head.startswith(s) for s in sigs):
        raise ValueError(f"文件内容不是有效的 {ext} 图片(magic bytes 不匹配)")
    return ext


# =====================================================================
#  标签图片 OCR 匹配（方案 A：只打徽章，不改数据）
# =====================================================================

def _normalize_for_match(text):
    """归一化：全角→半角、码→y、去空白/标点、转小写。"""
    if not text:
        return ''
    out = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:            # 全角空格
            code = 0x20
        elif 0xFF01 <= code <= 0xFF5E:  # 全角 ASCII
            code -= 0xFEE0
        out.append(chr(code))
    s = ''.join(out).lower()
    s = s.replace('码', 'y')
    s = re.sub(r'[\s\-_/、，,。.·:：;；()（）\[\]【】#＃*]+', '', s)
    return s


def detect_bg_color(filepath):
    """检测商品标签图片的背景色(4 边角采样,取最暗角判断)。

    用于"黑磅布三文治""白磅布三文治"等标签上不写颜色的品类:
    从图片 4 角塑料布区域推断实物颜色,作为元数据供下游比对使用
    (ocr_pipeline 末尾 append "[标签背景: 黑色/白色]" 后缀,不直接污染 OCR 文本)。

    实现细节 (2026-08-19 改进):
      - 4 角采样:左上 / 右上 / 左下 / 右下,每角取边长 1/8 的矩形 (避开中心标签)
      - 用灰度图, 每角算 ImageStat 均值
      - 取 4 角的 min 作为整体判定输入 (任一角为深色即偏向 black)
      - 黑色塑料布 (min < 70)  -> 'black'   → 实物是"黑磅布三文治"
      - 白色塑料布 (min > 100) -> 'white'   → 实物是"白磅布三文治"
      - 中性灰 (70-100)        -> None      → 放弃推断避免误判

    阈值与原 4×4 网格算法保持一致 (70 / 100), 保证现有 image.bg_color 不漂移。

    Returns:
        'black', 'white', 或 None.
    """
    try:
        from PIL import Image, ImageStat
        img = Image.open(filepath).convert('L')
        w, h = img.size
        # 4 角采样区域大小:边长的 1/8 (小一些更"纯",但太小易采到边)
        size_x = max(8, w // 8)
        size_y = max(8, h // 8)
        corners = [
            (0, 0, size_x, size_y),                          # 左上
            (w - size_x, 0, w, size_y),                      # 右上
            (0, h - size_y, size_x, h),                      # 左下
            (w - size_x, h - size_y, w, h),                  # 右下
        ]
        samples = [ImageStat.Stat(img.crop(c)).mean[0] for c in corners]
        # 取最暗角 (4 角中, 最暗那角更代表深色塑料布; 任一角深即偏 black)
        dark = min(samples)
        if dark < 70:
            return 'black'
        if dark > 100:
            return 'white'
        return None
    except Exception:
        return None


def _char_tokens(s):
    """把连续字符串按字符切成空格分隔的 token 串，供 token_set_ratio 无视顺序比较。"""
    return ' '.join(list(s))


def match_label_to_row(ocr_text, product_name, specification=''):
    """标签 OCR 文本 vs 本行 品名+规格 的模糊匹配（品名为主 + 规格加分）。

    Returns:
        (status, score, reason): status ∈ {'green', 'yellow', 'red'},
                                score ∈ 0.0~100.0,
                                reason: 中文判定依据,前端 hover 提示用
                                        统一前缀"本地模糊匹配"与 DeepSeek 区分。
    """
    text = _normalize_for_match(ocr_text)
    name = _normalize_for_match(product_name)
    spec = _normalize_for_match(specification)
    if not text or not name:
        return ('red', 0.0, '本地模糊匹配失败: 标签或品名为空')

    # 品名：字符级 token_set_ratio（无视顺序）与 partial_ratio（容忍目标只是文本的一段）取大
    name_score = max(
        fuzz.token_set_ratio(_char_tokens(name), _char_tokens(text)),
        fuzz.partial_ratio(name, text),
    )
    score = float(name_score)

    # 规格加分：命中则往上抬（最多约 +15，封顶 100）
    spec_score = 0.0
    if spec:
        spec_score = fuzz.partial_ratio(spec, text)
        score = min(100.0, name_score + spec_score * 0.15)

    if score >= 85:
        status = 'green'
    elif score >= 60:
        status = 'yellow'
    else:
        status = 'red'

    # 构造中文 reason: "本地模糊匹配 92分 (品名命中, 规格命中)"
    score_str = f'{score:.0f}'
    name_hit = '命中' if name_score >= 60 else '未命中'
    parts = [f'品名{name_hit}']
    if spec:
        spec_hit = '命中' if spec_score >= 60 else '未命中'
        parts.append(f'规格{spec_hit}')
    reason = f'本地模糊匹配 {score_str}分 ({", ".join(parts)})'

    return (status, score, reason)


# =====================================================================
#  用户上传时的旋转（移动端拍照方向修正）
# =====================================================================
from io import BytesIO
from PIL import Image

_VALID_ROTATIONS = (0, 90, 180, 270)


def apply_user_rotation(filepath: str, rotate_deg) -> str:
    """按 rotate_deg 旋转已落盘图片；deg=0/None 时原样返回 filepath。非法值抛 ValueError。

    - 移动端拍照上传时常常方向不对（横屏拍竖屏单据），前端拿 EXIF 算出 rotate_deg 一起 POST。
    - 校验合法值集合 {0, 90, 180, 270}；非法值抛 ValueError，由调用方转 400。
    - 直接覆盖原文件，不改路径（保持 ShippingImage.file_path 不变）。
    - PIL 的 rotate 顺时针为正，这里取负号使用户视角的"右旋 90°"对应 PIL 顺时针 90°，
      这样前端传 90 表示"把图往右转 90°"，与常见 UX 一致。
    """
    try:
        deg = int(rotate_deg) if rotate_deg is not None else 0
    except (TypeError, ValueError):
        raise ValueError("方向参数非法")
    if deg not in _VALID_ROTATIONS:
        raise ValueError("方向参数非法")
    if deg == 0:
        return filepath
    img = Image.open(filepath)
    if img.mode != "RGB":
        img = img.convert("RGB")
    rotated = img.rotate(-deg, expand=True)
    rotated.save(filepath)
    return filepath
