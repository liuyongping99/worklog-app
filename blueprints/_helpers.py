"""共享辅助：图片上传、文件存储、单位换算、备注校验等。

所有订单蓝图（出货/入库/装柜）共用的工具函数集中在这里。
"""
import os
import re
import uuid
import base64
from datetime import datetime
from flask import request


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

    与 ProductUnit.get_match 和 JS 端 findInboundUnit 行为一致：两轮匹配，
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
            total_loose += len(re.findall(r'\d+(?:\.\d+)?[yY码]', remark))
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
