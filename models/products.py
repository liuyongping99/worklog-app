"""商品：单位 / 分类 / 产品"""
from datetime import datetime
from ._db import get_db


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
        conn.commit()
        conn.close()

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
    def create(product_code, product_name, category_id, specification='', model='', barcode='',
               cost_price=None, base_unit='', aux_unit='', conversion_rate='',
               stock_quantity=None, aux_quantity='', preset_price=None, status=1):
        conn = get_db()
        cursor = conn.cursor()
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
        cursor.execute(f'UPDATE product SET {", ".join(sets)} WHERE product_id = ?', vals)
        conn.commit()
        conn.close()

    @staticmethod
    def delete(product_id):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM product WHERE product_id = ?', (product_id,))
        conn.commit()
        conn.close()
