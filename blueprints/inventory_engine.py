# -*- coding: utf-8 -*-
"""库存折算 / SKU 匹配引擎 —— 纯函数,无 Flask / 无 DB 依赖。

这是整个库存系统的算法核心(B1 批次产出)。被三方复用:
  · tools/stock_readiness_check.py —— 过账就绪度体检(口径基线)
  · blueprints/stock.py (B3/B4)    —— 库存查询 / 入库过账
  · tests/test_inventory_engine.py —— 单测

设计原则(踩过的坑,别改):
  1. **宁可漏配,不可错配**。匹配不唯一 → 跳过待人工,绝不「猜一行」。
     实测:「同名任一行」兜底里 113/130 行是猜的,候选 SKU 数 1~96 不等,
     错配比不匹配隐蔽得多 —— 库存错了没人会发现。
  2. **折算规则来源有优先级**:规格级 product_units > 品名级 product.aux_unit
     > 件数换算 piece_conversions。规格级最精确(同品名多规格 YPP 不同)。
  3. **0 视为「无规则」**。product_units 里有 yards_per_piece=0 的占位行,
     当 0 系数入账会把库存清零。
  4. **7P 只在品名开头剥离**。product 表命名约定是不带 7P(见下方命名说明)。

命名说明(2026-11-16 用户确认):
  · 商品明细里 **7P 被「环保」字样代替** —— 因为 15P/21P 也是环保产品,
    用 7P 当系列标记会歧义。所以新入库的品名写「环保XXX」,不写「7P环保XXX」。
  · 旧的「7P环保杂胶」之类**保留原样不动**,入库侧两种写法混用,
    所以匹配时要把 7P 前缀当可选项剥掉。

匹配级联(2026-11-16 定稿):
  L0  product_code 精确         ← 订单明细新增列,唯一稳定标识
                                   (根治 15P/21P 同名歧义)
  L1  (品名, 规格) 精确
  L2  品名去空格 + 规格去空格   ← 修 '绿精灵特规' vs '绿精灵 特规' 这类
  L3  去 '7P' 前缀 → 重跑 L1/L2
  L4  规格归一(去空格 / '#'→'-' / '0.14'→'014')→ 同名同族内唯一解才接受
  L4b 规格前缀(规格被写缩写:'0.8中' vs '0.8中性')→ 同族内唯一解才接受
  L4c 品名唯一解                ← 仅当入库没填规格,且全表该品名只有 1 行
  L5  无解 → 跳过待人工

  ⚠️ **同名同规格多 SKU → 该键作废,不取第一条**(2026-11-16 加)
     订单明细表**没有 product_code 列**(用户决定:不加,继续靠品名+规格匹配),
     所以「同一个 (品名,规格) 指向多个 SKU」时,引擎**不能挑一个** ——
     挑了就是静默错配,库存错了要等盘点才发现。
     历史实测曾有 3 组这样的重复(已于 2026-11-16 由用户清理为 0 组:
     规格串行 → 改回本来的规格;规格漏填 → 从「型号」抄进「规格」)。

     ⭐ **数据库层已经加了第二道防线**(2026-11-16):
        `uk_product_name_spec` = UNIQUE(product_name, COALESCE(specification,''))
        → 精确重复 / 规格留空重复 建档时就被拒,不用等到引擎才发现。
        ⚠️ 但**空格不参与**该唯一键(业务要求空格原样保留):
           'A1.5米 200g' 与 'A1.5米200g' 允许并存为两个 SKU ——
           这种情况引擎靠「L1 原始精确优先 + 归一键歧义作废」保证不串货
           (字面一致的各命中各的那行;写法交叉的落待人工,宁可漏配不可错配)。

     将来 15P/21P 建档后若出现重复 → 引擎这条规则仍要保留,
     它是「不加 product_code」这个决策的**最后一道防线**,别删。
     作废后这些行走 L4/L4b 唯一解,解不了就进「待人工」,由人处理。
"""
import re

# ── 单位别名 → 标准单位 ────────────────────────────────────────────────
# product.base_unit 用「码」,入库单写「y」;两者都要归一。
UNIT_ALIAS = {
    'y': '码', 'Y': '码', '码': '码',
    '支': '支', '张': '张', '桶': '桶', 'kg': 'kg', 'KG': 'kg',
    '斤': '斤', '块': '块', '令': '令', '件': '件', '卷': '卷',
    '箱': '箱', '米': '米', '扎': '扎', '吨': '吨', '包': '包', '卷筒': '卷',
}

# 明确不是「货物计量单位」的基本单位(费用行 / 目录图误解析)。
# ⚠️ 不含 '扎' —— 扎是纸业真实单位(1扎=10张),只是缺换算规则,
#    缺规则属于 no_conversion,不是 bad_base_unit。
NON_COUNT_BASE_UNITS = ('元', '色', '床')

# 折算失败的原因码(过账时写进 stock_posting_skips)
REASON_NO_PRODUCT = 'no_product'          # 匹配不到 SKU
REASON_NO_CONVERSION = 'no_conversion'    # 匹配到了但缺换算规则
REASON_BAD_BASE_UNIT = 'bad_base_unit'    # 基本单位不是计量单位 / 为空
REASON_BAD_QTY = 'bad_qty'                # 数量非法(非数字 / ≤0)

_NUM_EQ = re.compile(r'^([\d.]+)\s*([^=]+?)\s*=\s*([\d.]+)\s*(.+)$')
_NUM_ONLY = re.compile(r'^1\s*(.+)$')


# ── 基础归一函数 ────────────────────────────────────────────────────────
def norm_unit(u):
    """单位别名归一。未知单位原样返回。"""
    u = (u or '').strip()
    return UNIT_ALIAS.get(u, u)


def sq(x):
    """去空格(全角 + 半角)。"""
    return (x or '').replace(' ', '').replace('\u3000', '').strip()


def strip_7p(name):
    """剥掉开头的 '7P' 系列号 —— product 表命名约定是不带 7P。

    ⚠️ 只剥 '7P',**不剥 '15P'/'21P'** —— 那两个将来是独立商品,不能混。
    """
    return re.sub(r'^7P(?=环保|PP|PVC|)', '', name or '')


def norm_spec(s):
    """规格归一: 去空格 + '#'→'-' + '0.14'→'014'。"""
    s = sq(s).replace('#', '-')
    return re.sub(r'0\.(\d+)', r'0\1', s)


def parse_aux_rules(aux_unit):
    """解析 product.aux_unit → {辅单位: 系数(该辅单位 → base_unit)}。

    支持三种格式:
      '1支=46码'    → {'支': 46.0, '码': 1.0}
      '1吨=5818张'  → {'吨': 5818.0, '张': 1.0}
      '1码'         → {'码': 1.0}
    解析不出来 → {} (视为无规则)
    """
    au = (aux_unit or '').strip()
    if not au:
        return {}
    m = _NUM_EQ.match(au)
    if m:
        left_n, left_u, right_n, right_u = m.groups()
        try:
            ln, rn = float(left_n), float(right_n)
        except ValueError:
            return {}
        if ln <= 0:
            return {}
        return {left_u.strip(): rn / ln, right_u.strip(): 1.0}
    m2 = _NUM_ONLY.match(au)
    if m2:
        return {m2.group(1).strip(): 1.0}
    return {}


# ── 换算规则查找 ────────────────────────────────────────────────────────
def get_ypp(name, spec, unit_map):
    """规格级 YPP(支→码 折算系数)。

    unit_map: {product_name: [ {spec_keyword, yards_per_piece} ] }
    ⚠️ yards_per_piece == 0 视为「无规则」(占位行,见模块 docstring 原则 3)
    """
    rows = unit_map.get(name) or []
    if spec:
        for x in rows:
            kw = x.get('spec_keyword')
            if kw and kw in spec and x.get('yards_per_piece', 0) > 0:
                return x['yards_per_piece'] / 100.0
    for x in rows:
        if not x.get('spec_keyword') and x.get('yards_per_piece', 0) > 0:
            return x['yards_per_piece'] / 100.0
    return None


def get_piece(name, spec, piece_map):
    """件数换算规则。

    piece_map: {product_name: [ {spec_keyword, units_per_piece, target_unit} ] }
    """
    rows = piece_map.get(name) or []
    if spec:
        for x in rows:
            kw = x.get('spec_keyword')
            if kw and kw in spec:
                return x.get('units_per_piece'), x.get('target_unit')
    for x in rows:
        if not x.get('spec_keyword'):
            return x.get('units_per_piece'), x.get('target_unit')
    return None, None


# ── 折算 ────────────────────────────────────────────────────────────────
def convert_to_base_unit(qty, unit, product, unit_map=None, piece_map=None):
    """折算到 product.base_unit。

    返回 (ok: bool, factor: float|None, reason_code: str, reason_text: str)。
    ok=True 时 factor 表示「1 个入库单位 = 多少 base_unit」,
    入库数量折算后 = qty * factor。
    """
    unit_map = unit_map or {}
    piece_map = piece_map or {}
    base = norm_unit(product.get('base_unit'))
    u = norm_unit(unit)
    name = product.get('product_name') or ''
    spec = product.get('specification') or ''

    if not base:
        return False, None, REASON_BAD_BASE_UNIT, '商品未设基本单位'
    if base in NON_COUNT_BASE_UNITS:
        return False, None, REASON_BAD_BASE_UNIT, f'基本单位「{base}」不是货物计量单位'
    if u == base:
        return True, 1.0, '', ''

    # ① 规格级 YPP(最精确)
    if u == '支' and base == '码':
        ypp = get_ypp(name, spec, unit_map)
        if ypp:
            return True, ypp, '', ''

    # ② 品名级 aux_unit 换算关系
    f = parse_aux_rules(product.get('aux_unit')).get(u)
    if f:
        return True, f, '', ''

    # ③ 件数换算
    if u == '件':
        per, tgt = get_piece(name, spec, piece_map)
        if per is not None and tgt and norm_unit(tgt) == base:
            return True, float(per), '', ''
        if per is not None and tgt:
            return (False, None, REASON_NO_CONVERSION,
                    f'件数换算目标单位「{tgt}」≠ 基本单位「{base}」')
        return False, None, REASON_NO_CONVERSION, f'缺「件→{base}」件数换算规则'

    return False, None, REASON_NO_CONVERSION, f'无「{u}→{base}」换算规则'


# ── SKU 索引 ────────────────────────────────────────────────────────────
class ProductIndex:
    """product 表的内存索引。

    由 build_product_index(rows) 构造。rows 可以是 sqlite3.Row 或 dict,
    需要这些键:product_id / product_code / product_name / specification /
    base_unit / aux_unit
    """

    def __init__(self, rows):
        self.rows = []
        self.by_code = {}
        self.by_ns = {}          # (原始品名, 原始规格) -> row
        self.by_ns_sq = {}       # (去空格品名, 去空格规格) -> row
        self.by_name_sq = {}     # 去空格品名 -> [row]
        self.by_name_raw = {}    # 原始品名 -> [row]
        # 歧义收集:同一个键对应多个 SKU → 该键**作废**,不做「取第一条」
        self.ambiguous_code = {}
        self.ambiguous_ns = {}
        self.ambiguous_ns_sq = {}

        _codes = {}
        _ns = {}
        _ns_sq = {}
        for r in rows:
            row = dict(r)
            self.rows.append(row)
            code = row.get('product_code')
            if code:
                _codes.setdefault(code, []).append(row)
            n = row.get('product_name') or ''
            s = row.get('specification') or ''
            _ns.setdefault((n, s), []).append(row)
            _ns_sq.setdefault((sq(n), sq(s)), []).append(row)
            self.by_name_raw.setdefault(n, []).append(row)
            self.by_name_sq.setdefault(sq(n), []).append(row)

        self.by_code = {k: v[0] for k, v in _codes.items() if len(v) == 1}
        self.ambiguous_code = {k: [r['product_id'] for r in v] for k, v in _codes.items() if len(v) > 1}
        self.by_ns = {k: v[0] for k, v in _ns.items() if len(v) == 1}
        self.ambiguous_ns = {k: [r['product_id'] for r in v] for k, v in _ns.items() if len(v) > 1}
        self.by_ns_sq = {k: v[0] for k, v in _ns_sq.items() if len(v) == 1}
        self.ambiguous_ns_sq = {k: [r['product_id'] for r in v] for k, v in _ns_sq.items() if len(v) > 1}

    def family(self, name):
        """取同族候选行(品名归一后相同)。入库侧名字可能带 7P 前缀。"""
        out = []
        seen = set()
        for cand in (name, strip_7p(name)):
            for row in self.by_name_sq.get(sq(cand), []):
                if row['product_id'] not in seen:
                    seen.add(row['product_id'])
                    out.append(row)
        return out


def build_product_index(product_rows):
    """一次查库 → 内存索引。"""
    return ProductIndex(product_rows)


# ── 匹配 ────────────────────────────────────────────────────────────────
def _name_variants(name):
    """品名候选: 原名 / 去空格 / 去 7P / 去 7P+去空格。"""
    out = []
    for cand in (name, sq(name), strip_7p(name), sq(strip_7p(name))):
        if cand and cand not in out:
            out.append(cand)
    return out


def resolve_product(name, spec, code, index):
    """把入库明细行匹配到 product 表的 SKU。

    返回 (product_row 或 None, 命中口径标签)。
    """
    name = name or ''
    spec = spec or ''

    # L0 商品编号 —— 唯一稳定标识
    if code:
        p = index.by_code.get(code)
        if p:
            return p, 'L0-编号'

    variants = _name_variants(name)

    # L1 精确
    for nm in variants:
        p = index.by_ns.get((nm, spec))
        if p:
            return p, 'L1-精确'

    # L2 品名 + 规格都去空格
    for nm in variants:
        p = index.by_ns_sq.get((sq(nm), sq(spec)))
        if p:
            return p, 'L2-去空格'

    # L4 规格归一 → 同族唯一解
    target = norm_spec(spec)
    for nm in variants:
        hits = [r for r in index.by_name_sq.get(sq(nm), [])
                if norm_spec(r.get('specification') or '') == target]
        if len(hits) == 1:
            return hits[0], 'L4-规格归一唯一解'

    # L4b 规格前缀(缩写规格:'0.8中' vs '0.8中性')→ 同族唯一解
    # ⚠️ 必须唯一,否则跳过 —— 多个 '0.4*' 家族会同时命中,那是歧义不是缩写
    if target:
        for nm in variants:
            hits = [r for r in index.by_name_sq.get(sq(nm), [])
                    if norm_spec(r.get('specification') or '').startswith(target)
                    and norm_spec(r.get('specification') or '') != target]
            if len(hits) == 1:
                return hits[0], 'L4b-规格前缀唯一解'

    # L4c 品名唯一解 —— 仅当入库没填规格,且全表该品名只有 1 行
    if not spec:
        for nm in variants:
            hits = index.by_name_sq.get(sq(nm), [])
            if len(hits) == 1:
                return hits[0], 'L4c-品名唯一解'

    return None, 'L5-无解'
