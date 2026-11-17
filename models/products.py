"""商品：单位 / 分类 / 产品"""
import sqlite3
from datetime import datetime
from ._db import get_db


class ProductConflictError(Exception):
    """「品名 + 规格」与既有 SKU 冲突(uk_product_name_spec 唯一索引)。

    2026-11-16 起 product 表有该唯一索引:
      同一 (product_name, COALESCE(specification,'')) 只能存在一行。
    空格**不参与**归一 —— 'A1.5米 200g' 与 'A1.5米200g' 允许并存为两个 SKU。
    """

    def __init__(self, message, existing=None):
        super().__init__(message)
        self.message = message
        self.existing = existing


def _normalize_product_name(name):
    """商品名归一化:去掉普通空格与全角空格。

    对齐 find_by_name_and_spec 的兜底口径 —— 应对「无纺布 A白」(DB)
    vs「无纺布A白」(订单) 这种只差空格的漂移。
    """
    return (name or '').replace(' ', '').replace('\u3000', '')


class ProductUnit:
    """商品辅助单位:每支多少码 + (可选)按重量计价的单位白名单。
       复合唯一键 (product_name, spec_keyword),同名商品可有多条规格规则。
       两条独立轨道(2026-10-14):
         - is_usingyardforcounting=1 → 启用 YPP(按码卖)校验,行数=yards_per_piece/100
         - is_usingweightforcounting=1 → 启用「按重量」单位白名单校验,allowed_units
           存 JSON 数组如 '["kg","斤"]' 或逗号分隔 'kg,斤'
    """

    @staticmethod
    def create(product_name: str, yards_per_piece: int,
               is_usingyardforcounting: bool = False, spec_keyword: str = None,
               is_usingweightforcounting: bool = False, allowed_units: str = ''):
        """yards_per_piece: 实际值×100,如 7.5码 → 750。
        spec_keyword 为 NULL 时表示默认行。
        allowed_units: 形如 '["kg","斤"]' 或 'kg,斤',留空表示不启用按重量校验。
        """
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO product_units "
            "(product_name, spec_keyword, yards_per_piece, is_usingyardforcounting, "
            " is_usingweightforcounting, allowed_units) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (product_name, spec_keyword, yards_per_piece,
             1 if is_usingyardforcounting else 0,
             1 if is_usingweightforcounting else 0,
             allowed_units or '')
        )
        conn.commit()
        conn.close()

    @staticmethod
    def get_all():
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM product_units ORDER BY product_name ASC, spec_keyword ASC")
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    @staticmethod
    def get_by_name(product_name: str):
        """获取默认（无规格匹配）的单位配置"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM product_units WHERE product_name = ? AND spec_keyword IS NULL",
            (product_name,)
        )
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    @staticmethod
    def get_match(product_name: str, spec: str = ''):
        """按规格匹配：先找 spec 包含 spec_keyword 的规则，未命中则取默认行"""
        conn = get_db()
        cursor = conn.cursor()
        if spec:
            cursor.execute(
                "SELECT * FROM product_units WHERE product_name = ? AND spec_keyword IS NOT NULL ORDER BY id",
                (product_name,)
            )
            for row in cursor.fetchall():
                kw = row['spec_keyword']
                if kw and kw in spec:
                    conn.close()
                    return dict(row)
        # fallback 到默认行
        cursor.execute(
            "SELECT * FROM product_units WHERE product_name = ? AND spec_keyword IS NULL",
            (product_name,)
        )
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    @staticmethod
    def delete(product_name: str, spec_keyword: str = None):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM product_units WHERE product_name = ? AND spec_keyword IS ?",
            (product_name, spec_keyword)
        )
        conn.commit()
        conn.close()

    @staticmethod
    def update(product_name: str, yards_per_piece: int,
               is_usingyardforcounting: bool = None, spec_keyword: str = None,
               is_usingweightforcounting: bool = None, allowed_units: str = None):
        conn = get_db()
        cursor = conn.cursor()
        # 拼接 SET 子句(只更新传入的字段,允许只调 partial update)
        sets = ['yards_per_piece = ?']
        vals = [yards_per_piece]
        if is_usingyardforcounting is not None:
            sets.append('is_usingyardforcounting = ?')
            vals.append(1 if is_usingyardforcounting else 0)
        if is_usingweightforcounting is not None:
            sets.append('is_usingweightforcounting = ?')
            vals.append(1 if is_usingweightforcounting else 0)
        if allowed_units is not None:
            sets.append('allowed_units = ?')
            vals.append(allowed_units)
        vals.extend([product_name, spec_keyword])
        cursor.execute(
            f"UPDATE product_units SET {', '.join(sets)} "
            "WHERE product_name = ? AND spec_keyword IS ?",
            vals
        )
        conn.commit()
        conn.close()


class ProductCategory:
    @staticmethod
    def get_all():
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM product_categories ORDER BY level, sort_order, id")
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]

    @staticmethod
    def get_by_id(cat_id):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM product_categories WHERE id = ?", (cat_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    @staticmethod
    def get_children(parent_id):
        conn = get_db()
        cursor = conn.cursor()
        if parent_id is None:
            cursor.execute("SELECT * FROM product_categories WHERE parent_id IS NULL ORDER BY sort_order, id")
        else:
            cursor.execute("SELECT * FROM product_categories WHERE parent_id = ? ORDER BY sort_order, id", (parent_id,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]

    @staticmethod
    def get_tree():
        """返回嵌套树结构，根节点包含 children 列表。
        每个节点下的 children 按 category_code 升序排列。"""
        all_rows = ProductCategory.get_all()
        node_map = {}
        for r in all_rows:
            r['children'] = []
            node_map[r['id']] = r
        roots = []
        for r in all_rows:
            pid = r['parent_id']
            if pid is not None and pid in node_map:
                node_map[pid]['children'].append(r)
            elif pid is None:
                roots.append(r)

        # 递归按 category_code 排序
        def sort_children(node):
            node['children'].sort(key=lambda c: c['category_code'])
            for child in node['children']:
                sort_children(child)

        for root in roots:
            sort_children(root)

        return roots

    @staticmethod
    def descendant_ids(category_id, include_self=True):
        """某分类**及其所有后代**的 id 列表。

        库存页 / 产品页的分类筛选:点一个父分类 → 应该包含它下面所有子分类
        (主流进销存的通用做法)。否则用户点「白坯布」却看不到它的三级子类,
        会以为货丢了。

        分类不存在 → 返回空列表(调用方据此判定「无效分类」)。
        ⚠️ 防御环:分类树被改坏(parent_id 指回祖先)时不会死循环。
        """
        if category_id is None:
            return []
        try:
            cid = int(category_id)
        except (TypeError, ValueError):
            return []
        out = [cid] if include_self else []
        seen = {cid}
        frontier = [cid]
        while frontier:
            pid = frontier.pop()
            for c in ProductCategory.get_children(pid):
                child_id = int(c['id'])
                if child_id in seen:
                    continue
                seen.add(child_id)
                out.append(child_id)
                frontier.append(child_id)
        return out

    @staticmethod
    def get_level_options():
        """返回可用层级选项"""
        return [
            (1, '第1层 - 根'),
            (2, '第2层 - 大类'),
            (3, '第3层 - 中类'),
            (4, '第4层 - 小类'),
        ]

    @staticmethod
    def create(category_code, category_name, parent_id, level, sort_order=0):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO product_categories (category_code, category_name, parent_id, level, sort_order) VALUES (?, ?, ?, ?, ?)",
            (category_code, category_name, parent_id, level, sort_order)
        )
        cat_id = cursor.lastrowid
        conn.commit()
        conn.close()
        # ⭐2026-11-17 阶段 2:返回新建的 id(原来返回 None,调用方拿不到自己的分类 id,
        #   建子分类时只能再查一遍)。现有调用方都忽略返回值,不受影响。
        return cat_id

    @staticmethod
    def update(cat_id, **kwargs):
        allowed = ['category_code', 'category_name', 'parent_id', 'level', 'sort_order', 'status']
        sets = []
        vals = []
        for k in allowed:
            if k in kwargs:
                sets.append(f"{k} = ?")
                vals.append(kwargs[k])
        if not sets:
            return
        sets.append("updated_at = datetime('now','localtime')")
        vals.append(cat_id)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(f"UPDATE product_categories SET {', '.join(sets)} WHERE id = ?", vals)
        conn.commit()
        conn.close()

    @staticmethod
    def delete(cat_id):
        """删除分类及其所有子孙。先 BFS 收集 ID,再一次性批量 DELETE,
        避免递归时每层都新建一个 SQLite 连接。"""
        conn = get_db()
        cursor = conn.cursor()
        # BFS 收集所有子孙 ID(从 cat_id 开始)
        to_delete = [cat_id]
        frontier = [cat_id]
        while frontier:
            placeholders = ','.join('?' * len(frontier))
            cursor.execute(
                f"SELECT id FROM product_categories WHERE parent_id IN ({placeholders})",
                frontier
            )
            children = [r[0] for r in cursor.fetchall()]
            to_delete.extend(children)
            frontier = children
        # 一次性删除
        placeholders = ','.join('?' * len(to_delete))
        cursor.execute(
            f"DELETE FROM product_categories WHERE id IN ({placeholders})",
            to_delete
        )
        conn.commit()
        conn.close()


class Product:
    """产品管理"""
    @staticmethod
    def get_all():
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT p.*, c.category_name FROM product p LEFT JOIN product_categories c ON p.category_id = c.id ORDER BY p.product_code')
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]

    @staticmethod
    def get_by_id(product_id):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT p.*, c.category_name FROM product p LEFT JOIN product_categories c ON p.category_id = c.id WHERE p.product_id = ?', (product_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    @staticmethod
    def get_by_category(category_id):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT p.*, c.category_name FROM product p LEFT JOIN product_categories c ON p.category_id = c.id WHERE p.category_id = ? ORDER BY p.product_code',
            (category_id,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]

    @staticmethod
    def get_code_lookup():
        """一次性载入 product 表,返回商品编号查找字典(供列表页批量回填编号)。

        背景:入库/出货/装柜明细行要在「规格」后显示商品编号,若逐行调
        find_by_name_and_spec 会一条明细开一次连接。这里改为一次查询 + 内存匹配。

        返回:
            {
              'exact': {(商品名, 规格): 商品编号},      # 精确命中
              'norm':  {(去空格商品名, 规格): 商品编号},  # 名称去空格后命中
            }
        匹配口径与 find_by_name_and_spec 一致(精确 → 同名默认行 → 去空格同名)。
        """
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT product_code, product_name, specification FROM product ORDER BY product_id')
        rows = cursor.fetchall()
        conn.close()
        exact = {}
        norm = {}
        for row in rows:
            code = row['product_code'] or ''
            if not code:
                continue
            name = row['product_name'] or ''
            spec = row['specification'] or ''
            exact.setdefault((name, spec), code)
            norm.setdefault((_normalize_product_name(name), spec), code)
        return {'exact': exact, 'norm': norm}

    @staticmethod
    def match_code(product_name, specification='', lookup=None):
        """按 (商品名, 规格) 查商品编号,查不到返回空串。

        口径与 find_by_name_and_spec 一致:
          1. 商品名 + 规格 精确命中
          2. 同名 + 空规格 的默认行
          3. 名称去空格后 + 规格
          4. 名称去空格后 + 空规格默认行

        刻意不做「同名任一行」兜底 —— 规格对不上时给编号会误导用户
        (例:「白磅布三文治 0.6软」在 product 表里只有 0.4中性 / 0.6中性 / 0.8中性…)。

        lookup 传入 Product.get_code_lookup() 的结果可复用同一份快照(批量场景)。
        """
        if not product_name:
            return ''
        if lookup is None:
            lookup = Product.get_code_lookup()
        exact = lookup.get('exact') or {}
        norm = lookup.get('norm') or {}
        spec = (specification or '').strip()
        code = exact.get((product_name, spec))
        if code:
            return code
        if spec:
            code = exact.get((product_name, ''))
            if code:
                return code
            code = norm.get((_normalize_product_name(product_name), spec))
            if code:
                return code
            code = norm.get((_normalize_product_name(product_name), ''))
            if code:
                return code
        return ''

    @staticmethod
    def find_by_name_and_spec(product_name, specification=''):
        """按 (产品名, 规格) 找商品。

        匹配顺序(与 product_units / piece_conversions 双轮匹配口径一致):
          1. product_name = ? AND specification = ? 精确命中
          2. product_name = ? AND specification IS NULL/''  默认行兜底
          3. 名称「去空格」匹配 — 应对历史数据 product 表「无纺布 A白」vs 订单「无纺布A白」漂移
        任一命中即返回;无命中返回 None。

        用于按重量品类校验(record.product_name + record.specification
        → product.weight_category_id → product_weight_categories 行)。
        """
        if not product_name:
            return None
        conn = get_db()
        cursor = conn.cursor()
        spec = (specification or '').strip()
        # 精确:同名 + 同规格
        cursor.execute(
            'SELECT * FROM product WHERE product_name = ? AND specification = ?',
            (product_name, spec)
        )
        row = cursor.fetchone()
        if row:
            conn.close()
            return dict(row)
        # 默认行:同名 + 空规格
        if spec:
            cursor.execute(
                'SELECT * FROM product WHERE product_name = ? '
                "AND (specification IS NULL OR specification = '') LIMIT 1",
                (product_name,)
            )
            row = cursor.fetchone()
        if row:
            conn.close()
            return dict(row)
        # 去空格兜底(同义不同形)
        # 注意:无论 pn 是否含空格都跑 ——
        # 应对 (pn='无纺布A白' vs DB='无纺布 A白') 这种 pn 没空格但 DB 有空格的漂移
        norm_pn = product_name.replace(' ', '').replace('　', '')
        cursor.execute(
            "SELECT * FROM product WHERE REPLACE(REPLACE(product_name, ' ', ''), '　', '') = ? "
            "AND specification = ? LIMIT 1",
            (norm_pn, spec)
        )
        row = cursor.fetchone()
        if not row and spec:
            cursor.execute(
                "SELECT * FROM product WHERE REPLACE(REPLACE(product_name, ' ', ''), '　', '') = ? "
                "AND (specification IS NULL OR specification = '') LIMIT 1",
                (norm_pn,)
            )
            row = cursor.fetchone()
        if row:
            conn.close()
            return dict(row)
        # 最后兜底:取同名任一行
        cursor.execute(
            'SELECT * FROM product WHERE product_name = ? ORDER BY product_id LIMIT 1',
            (product_name,)
        )
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    @staticmethod
    def search_unbound(q='', limit=20):
        """模糊搜未绑定重量类别的商品候选;空 q 时返回 weight_category_id IS NULL 的前 limit 行。"""
        conn = get_db()
        cursor = conn.cursor()
        q = (q or '').strip()
        if q:
            cursor.execute(
                "SELECT product_id, product_code, product_name, specification, base_unit "
                "FROM product WHERE weight_category_id IS NULL "
                "AND (product_name LIKE ? OR specification LIKE ?) "
                "ORDER BY product_name LIMIT ?",
                (f'%{q}%', f'%{q}%', limit)
            )
        else:
            cursor.execute(
                "SELECT product_id, product_code, product_name, specification, base_unit "
                "FROM product WHERE weight_category_id IS NULL "
                "ORDER BY product_name LIMIT ?",
                (limit,)
            )
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]

    @staticmethod
    def find_by_name_spec(product_name, specification, exclude_id=None):
        """按 (product_name, COALESCE(specification,'')) 找冲突行。

        ⚠️ 键与数据库唯一索引 `uk_product_name_spec` **完全一致** ——
        空格原样保留,NULL 规格归成 ''(SQLite 里 NULL 互不相同,直接比会漏)。
        """
        conn = get_db()
        cursor = conn.cursor()
        sql = ("SELECT product_id, product_code, product_name, specification, model "
               "FROM product WHERE product_name = ? AND COALESCE(specification, '') = ?")
        params = [product_name, specification or '']
        if exclude_id:
            sql += ' AND product_id != ?'
            params.append(exclude_id)
        row = cursor.execute(sql, params).fetchone()
        conn.close()
        return dict(row) if row else None

    @staticmethod
    def _raise_friendly_integrity(exc, product_name, specification, exclude_id=None):
        """把 sqlite3.IntegrityError 翻成用户看得懂的话;不认识的原样抛出。

        ⚠️ SQLite 的报错文案有两种形态:
           · 普通列唯一索引 → 'UNIQUE constraint failed: product.product_code'(报列名)
           · 表达式唯一索引 → 'UNIQUE constraint failed: index \'uk_product_name_spec\''(报索引名)
           两种都要认。
        """
        msg = str(exc)
        if 'uk_product_name_spec' in msg or 'product.product_name' in msg:
            dup = Product.find_by_name_spec(product_name, specification, exclude_id=exclude_id)
            spec_txt = specification if (specification or '').strip() else '(空规格)'
            if dup:
                text = (f'「{product_name} / {spec_txt}」已存在(编号 {dup["product_code"]})。'
                        f'同一品名+规格只能有一个商品 —— 请把区分信息写进规格,或换一个商品编号。')
            else:
                text = (f'「{product_name} / {spec_txt}」与既有商品冲突。'
                        f'同一品名+规格只能有一个商品 —— 请把区分信息写进规格,或换一个商品编号。')
            raise ProductConflictError(text, existing=dup)
        if 'uk_product_code' in msg or 'product.product_code' in msg:
            raise ProductConflictError('商品编号已存在(product_code 唯一)。请换一个编号。')
        raise exc

    @staticmethod
    def create(product_code, product_name, category_id, specification='', model='', barcode='',
               cost_price=None, base_unit='', aux_unit='', conversion_rate='',
               stock_quantity=None, aux_quantity='', preset_price=None, status=1):
        conn = get_db()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO product (product_code, product_name, category_id, specification, model, barcode,
                                     cost_price, base_unit, aux_unit, conversion_rate,
                                     stock_quantity, aux_quantity, preset_price, status,
                                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        datetime('now','localtime'), datetime('now','localtime'))
            ''', (product_code, product_name, category_id, specification, model, barcode,
                  cost_price, base_unit, aux_unit, conversion_rate,
                  stock_quantity, aux_quantity, preset_price, status))
            conn.commit()
        except sqlite3.IntegrityError as e:
            conn.rollback()
            Product._raise_friendly_integrity(e, product_name, specification)
            raise
        product_id = cursor.lastrowid
        conn.close()
        return product_id

    @staticmethod
    def update(product_id, data):
        allowed = ['product_code', 'product_name', 'category_id', 'specification', 'model',
                   'barcode', 'cost_price', 'base_unit', 'aux_unit', 'conversion_rate',
                   'stock_quantity', 'aux_quantity', 'preset_price', 'status']
        sets = []
        vals = []
        for k in allowed:
            if k in data:
                sets.append(f'{k} = ?')
                vals.append(data[k])
        if not sets:
            return
        sets.append("updated_at = datetime('now','localtime')")
        vals.append(product_id)
        conn = get_db()
        cursor = conn.cursor()
        try:
            cursor.execute(f'UPDATE product SET {", ".join(sets)} WHERE product_id = ?', vals)
            conn.commit()
        except sqlite3.IntegrityError as e:
            conn.rollback()
            name = data.get('product_name')
            spec = data.get('specification')
            if name is None or spec is None:
                # 没传这两个字段 → 从库里取现值来定位冲突
                cur = get_db().cursor()
                row = cur.execute('SELECT product_name, specification FROM product '
                                  'WHERE product_id = ?', (product_id,)).fetchone()
                name = name if name is not None else (row['product_name'] if row else '')
                spec = spec if spec is not None else (row['specification'] if row else '')
            Product._raise_friendly_integrity(e, name, spec, exclude_id=product_id)
            raise
        conn.close()

    @staticmethod
    def delete(product_id):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM product WHERE product_id = ?', (product_id,))
        conn.commit()
        conn.close()
