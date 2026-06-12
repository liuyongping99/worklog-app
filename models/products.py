"""商品：单位 / 分类 / 产品"""
from datetime import datetime
from ._db import get_db


class ProductUnit:
    """商品辅助单位：每支多少码。yards_per_piece 存储为实际值×100的整数
       复合唯一键 (product_name, spec_keyword)，同名商品可有多条规格规则
    """

    @staticmethod
    def create(product_name: str, yards_per_piece: int, is_usingyardforcounting: bool = False, spec_keyword: str = None):
        """yards_per_piece: 实际值×100，如 7.5码 → 750。spec_keyword 为 NULL 时表示默认"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO product_units (product_name, spec_keyword, yards_per_piece, is_usingyardforcounting) VALUES (?, ?, ?, ?)",
            (product_name, spec_keyword, yards_per_piece, 1 if is_usingyardforcounting else 0)
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
    def update(product_name: str, yards_per_piece: int, is_usingyardforcounting: bool = None, spec_keyword: str = None):
        conn = get_db()
        cursor = conn.cursor()
        if is_usingyardforcounting is not None:
            cursor.execute(
                "UPDATE product_units SET yards_per_piece = ?, is_usingyardforcounting = ? WHERE product_name = ? AND spec_keyword IS ?",
                (yards_per_piece, 1 if is_usingyardforcounting else 0, product_name, spec_keyword)
            )
        else:
            cursor.execute(
                "UPDATE product_units SET yards_per_piece = ? WHERE product_name = ? AND spec_keyword IS ?",
                (yards_per_piece, product_name, spec_keyword)
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
        cursor.execute("SELECT * FROM product_categories WHERE parent_id = ? ORDER BY sort_order, id", (parent_id,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]

    @staticmethod
    def get_tree():
        """返回嵌套树结构，根节点包含 children 列表"""
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
        sets.append("updated_at = CURRENT_TIMESTAMP")
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
    def create(product_code, product_name, category_id, specification='', model='', barcode='',
               cost_price=None, base_unit='', aux_unit='', conversion_rate='',
               stock_quantity=None, aux_quantity='', preset_price=None, status=1):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO product (product_code, product_name, category_id, specification, model, barcode,
                                 cost_price, base_unit, aux_unit, conversion_rate,
                                 stock_quantity, aux_quantity, preset_price, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'), datetime('now','localtime'))
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
