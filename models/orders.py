"""订单：出货 / 入库 / 装柜（三表 × 3 业务领域 = 9 个模型）"""
import os
import shutil
import subprocess
import json as _json
from datetime import datetime
from ._db import get_db


# mavis-trash 路径(在项目启动时解析一次,subprocess 调 .cmd 包装器)
_MAVIS_TRASH = shutil.which('mavis-trash')


def _safe_remove_file(file_path):
    """删除图片物理文件,走 mavis-trash(进回收站,可恢复)+ 路径白名单(防误删其他文件)。

    Args:
        file_path: 文件绝对路径(从 DB file_path 字段读取)

    Returns:
        bool - True 表示文件已送入回收站(或本来就不存在),False 表示越界被拒或 mavis-trash 不可用
    """
    if not file_path:
        return True
    # 路径白名单:先校验路径在 upload/ 下(必须在 exists 检查之前,防攻击者用不存在的路径绕过)
    try:
        real = os.path.realpath(file_path)
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        upload_root = os.path.realpath(os.path.join(base_dir, 'upload'))
        if not real.startswith(upload_root + os.sep):
            # 路径越界,拒绝(攻击者构造 ../etc/passwd 之类的路径,即使文件不存在也不能放行)
            return False
    except Exception:
        # realpath 解析失败,保险起见拒绝
        return False
    # 白名单通过后再看文件是否存在
    if not os.path.exists(file_path):
        return True
    if not _MAVIS_TRASH:
        # mavis-trash 未安装(理论上不会发生),退回 os.remove
        try:
            os.remove(file_path)
            return True
        except Exception:
            return False
    try:
        subprocess.run([_MAVIS_TRASH, file_path], check=False, timeout=10)
        return True
    except Exception:
        return False


# ── 明细「单条警告已核查」共享读写 ─────────────────────────────────────────
# 三套订单明细(shipping_records / inbound_records / loading_order_records)都有
# verified_warnings(JSON, NOT NULL DEFAULT '{}') 列,语义完全一致:
#   {rule_id: true} 表示该条校验警告被用户单独点掉,不再出现在主警告列表。
# 与整行 verified 字段并存:verified=1 = 整行所有警告都折叠。
# table 只由本模块内部传入固定字面量表名,不接受外部输入(避免 f-string 拼 SQL 的注入面)。
_VERIFY_WARNING_TABLES = ('shipping_records', 'inbound_records', 'loading_order_records')


def _verified_warnings_get(table: str, record_id: int) -> dict:
    """读某条明细的 verified_warnings(JSON),反序列化为 dict,失败返回空 dict。"""
    import json
    assert table in _VERIFY_WARNING_TABLES, f'非法表名: {table}'
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(f'SELECT verified_warnings FROM {table} WHERE id = ?', (record_id,))
    row = cursor.fetchone()
    conn.close()
    if not row or not row['verified_warnings']:
        return {}
    try:
        return json.loads(row['verified_warnings'])
    except (TypeError, ValueError):
        return {}


def _verified_warnings_set(table: str, record_id: int, rule_id: str, verified: bool) -> dict:
    """单条规则的核查切换:把 rule_id 设为 verified/未 verified,返回更新后的完整 dict。"""
    import json
    assert table in _VERIFY_WARNING_TABLES, f'非法表名: {table}'
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(f'SELECT verified_warnings FROM {table} WHERE id = ?', (record_id,))
        row = cursor.fetchone()
        if not row:
            return {}
        current = {}
        if row['verified_warnings']:
            try:
                current = json.loads(row['verified_warnings'])
            except (TypeError, ValueError):
                current = {}
        if verified:
            current[rule_id] = True
        else:
            current.pop(rule_id, None)
        cursor.execute(
            f'UPDATE {table} SET verified_warnings = ? WHERE id = ?',
            (json.dumps(current, ensure_ascii=False, sort_keys=True), record_id)
        )
        conn.commit()
        return current
    finally:
        conn.close()


def _unverify_clears_warnings(data: dict, fields: list, values: list):
    """update() 里的联动:verified 被切到 0/False 时,同时把 verified_warnings 清空。

    语义:"全部已核查"是顶层开关,取消它 → 用户应看到一份干净的警告列表,
    而不是残留着上次单条核查过的折叠项。
    """
    if data.get('verified') == 0 or data.get('verified') is False:
        fields.append('verified_warnings = ?')
        values.append('{}')


class ShippingOrder:
    @staticmethod
    def create(date: str, customer: str) -> int:
        """创建新订单，返回新订单的 id"""
        conn = get_db()
        cursor = conn.cursor()
        # 取下一个 order_num
        cursor.execute(
            'SELECT COALESCE(MAX(order_num), 0) + 1 FROM shipping_orders WHERE date = ? AND customer = ?',
            (date, customer)
        )
        order_num = cursor.fetchone()[0]
        cursor.execute(
            'INSERT INTO shipping_orders (date, customer, order_num, created_at, img_cols) VALUES (?, ?, ?, ?, 5)',
            (date, customer, order_num, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        )
        new_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return new_id

    @staticmethod
    def get_all():
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM shipping_orders ORDER BY date DESC, customer ASC, order_num ASC')
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    @staticmethod
    def count() -> int:
        """出货单总条数(全部,不限日期)。"""
        conn = get_db()
        cursor = conn.cursor()
        n = cursor.execute('SELECT COUNT(*) FROM shipping_orders').fetchone()[0]
        conn.close()
        return n

    @staticmethod
    def count_by_date(date: str) -> int:
        """指定日期的出货单条数。"""
        conn = get_db()
        cursor = conn.cursor()
        n = cursor.execute('SELECT COUNT(*) FROM shipping_orders WHERE date = ?', (date,)).fetchone()[0]
        conn.close()
        return n

    @staticmethod
    def get_by_id(order_id: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM shipping_orders WHERE id = ?', (order_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    @staticmethod
    def delete(order_id: int):
        """删除订单。有明细或图片时拒绝删除，必须先手动清空。"""
        conn = get_db()
        cursor = conn.cursor()
        # 检查子记录
        n_items = cursor.execute('SELECT COUNT(*) FROM shipping_records WHERE order_pk = ?', (order_id,)).fetchone()[0]
        n_imgs = cursor.execute('SELECT COUNT(*) FROM shipping_images WHERE order_pk = ?', (order_id,)).fetchone()[0]
        if n_items > 0 or n_imgs > 0:
            conn.close()
            return {'success': False, 'error': f'该订单下还有 {n_items} 条明细和 {n_imgs} 张图片，请先删除所有明细和图片后再删除订单'}
        cursor.execute('DELETE FROM shipping_orders WHERE id = ?', (order_id,))
        conn.commit()
        conn.close()
        return {'success': True}

    @staticmethod
    def lock(order_id: int):
        """锁定订单"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('UPDATE shipping_orders SET is_locked = 1 WHERE id = ?', (order_id,))
        conn.commit()
        conn.close()

    @staticmethod
    def unlock(order_id: int):
        """解锁订单"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('UPDATE shipping_orders SET is_locked = 0 WHERE id = ?', (order_id,))
        conn.commit()
        conn.close()

    @staticmethod
    def set_img_cols(order_id: int, cols: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('UPDATE shipping_orders SET img_cols = ? WHERE id = ?', (cols, order_id))
        conn.commit()
        conn.close()

    @staticmethod
    def set_note(order_id: int, note: str):
        """设置订单级备注（显示在商品信息行上方）"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('UPDATE shipping_orders SET order_note = ? WHERE id = ?', (note or '', order_id))
        conn.commit()
        conn.close()

    @staticmethod
    def set_doc_number(order_id: int, doc_number: str):
        """设置单据编号"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('UPDATE shipping_orders SET doc_number = ? WHERE id = ?', (doc_number or '', order_id))
        conn.commit()
        conn.close()

    @staticmethod
    def set_customer(order_id: int, customer: str):
        """修改出货订单客户名称（同 date+customer+order_num 唯一约束下重排 order_num）"""
        conn = get_db()
        cursor = conn.cursor()
        # 取出当前 date + order_num
        cursor.execute('SELECT date, order_num FROM shipping_orders WHERE id = ?', (order_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return {'success': False, 'error': '订单不存在'}
        old_date, old_order_num = row['date'], row['order_num']
        # 取新 customer 下当前最大 order_num
        cursor.execute(
            'SELECT COALESCE(MAX(order_num), 0) + 1 FROM shipping_orders WHERE date = ? AND customer = ?',
            (old_date, customer)
        )
        new_order_num = cursor.fetchone()[0]
        try:
            cursor.execute(
                'UPDATE shipping_orders SET customer = ?, order_num = ? WHERE id = ?',
                (customer, new_order_num, order_id)
            )
            conn.commit()
        except Exception as e:
            conn.rollback()
            conn.close()
            return {'success': False, 'error': str(e)}
        conn.close()
        return {'success': True, 'customer': customer, 'order_num': new_order_num}

    @staticmethod
    def set_date(order_id: int, new_date: str):
        """修改出货订单日期（同 date+customer+order_num 唯一约束下重排 order_num）

        new_date 格式：'YYYY-MM-DD'
        成功：{'success': True, 'date': new_date, 'order_num': new_num, 'customer': customer}
        失败：{'success': False, 'error': ...}
        """
        conn = get_db()
        cursor = conn.cursor()
        # 取出当前 customer + order_num
        cursor.execute('SELECT date, customer, order_num FROM shipping_orders WHERE id = ?', (order_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return {'success': False, 'error': '订单不存在'}
        old_date, customer, old_order_num = row['date'], row['customer'], row['order_num']

        # 如果日期没变，直接返回
        if new_date == old_date:
            conn.close()
            return {'success': True, 'date': old_date, 'order_num': old_order_num, 'customer': customer, 'unchanged': True}

        # 取新 date 下当前最大 order_num
        cursor.execute(
            'SELECT COALESCE(MAX(order_num), 0) + 1 FROM shipping_orders WHERE date = ? AND customer = ?',
            (new_date, customer)
        )
        new_order_num = cursor.fetchone()[0]
        try:
            cursor.execute(
                'UPDATE shipping_orders SET date = ?, order_num = ? WHERE id = ?',
                (new_date, new_order_num, order_id)
            )
            conn.commit()
        except Exception as e:
            conn.rollback()
            conn.close()
            return {'success': False, 'error': str(e)}
        conn.close()
        return {'success': True, 'date': new_date, 'order_num': new_order_num, 'customer': customer}


class ShippingRecord:
    @staticmethod
    def create(date: str, customer: str, product_name: str = '', specification: str = '', quantity: str = '', unit: str = '支', remark: str = '', order_pk: int = None) -> int:
        """添加商品明细，order_pk 不传时自动创建新订单，返回 record id"""
        if unit == '码':
            unit = 'y'
        conn = get_db()
        cursor = conn.cursor()
        if order_pk is None:
            order_pk = ShippingOrder.create(date, customer)
        # 取当前订单最大 sort_order + 1
        cursor.execute('SELECT COALESCE(MAX(sort_order), 0) + 1 FROM shipping_records WHERE order_pk = ?', (order_pk,))
        next_sort = cursor.fetchone()[0]
        cursor.execute(
            'INSERT INTO shipping_records (order_pk, product_name, specification, quantity, unit, remark, sort_order, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (order_pk, product_name, specification, quantity, unit, remark, next_sort, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        )
        record_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return record_id

    @staticmethod
    def create_many(order_pk: int, items: list) -> dict:
        """单事务批量插入明细，避免逐条独立提交导致中途失败留半单。

        items: [{'product_name','specification','quantity','unit','remark'}, ...]
        规则：品名或数量为空 的项跳过(计入 skipped)；'码'→'y' 归一化；
             任一 INSERT 抛异常则整批回滚。
        Returns: {'records': [dict...], 'skipped': int}
        """
        conn = get_db()
        cursor = conn.cursor()
        try:
            cursor.execute('SELECT COALESCE(MAX(sort_order), 0) FROM shipping_records WHERE order_pk = ?', (order_pk,))
            next_sort = cursor.fetchone()[0]
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            records = []
            skipped = 0
            for item in items:
                if not isinstance(item, dict):
                    skipped += 1
                    continue
                product_name = (str(item.get('product_name') or '')).strip()
                specification = (str(item.get('specification') or '')).strip()
                quantity = (str(item.get('quantity') if item.get('quantity') is not None else '')).strip()
                unit = (str(item.get('unit') or '支')).strip()
                if unit == '码':
                    unit = 'y'
                remark = (str(item.get('remark') or '')).strip()
                if not product_name or not quantity:
                    skipped += 1
                    continue
                next_sort += 1
                cursor.execute(
                    'INSERT INTO shipping_records (order_pk, product_name, specification, quantity, unit, remark, sort_order, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                    (order_pk, product_name, specification, quantity, unit, remark, next_sort, now)
                )
                records.append({
                    'id': cursor.lastrowid, 'product_name': product_name,
                    'specification': specification, 'quantity': quantity,
                    'unit': unit, 'remark': remark,
                })
            conn.commit()
            return {'records': records, 'skipped': skipped}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def get_all():
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT r.*, o.date, o.customer, o.order_num
            FROM shipping_records r
            JOIN shipping_orders o ON r.order_pk = o.id
            ORDER BY o.created_at DESC, o.customer ASC, o.order_num ASC, r.sort_order, r.id
        ''')
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    @staticmethod
    def get_groups(start_date=None, end_date=None):
        """返回所有订单（含空订单）及其明细，可按日期范围过滤"""
        conn = get_db()
        cursor = conn.cursor()
        if start_date and end_date:
            cursor.execute('SELECT * FROM shipping_orders WHERE date >= ? AND date <= ? ORDER BY created_at DESC', (start_date, end_date))
        else:
            cursor.execute('SELECT * FROM shipping_orders ORDER BY date DESC, customer ASC, order_num ASC')
        orders = [dict(row) for row in cursor.fetchall()]

        # 所有明细
        cursor.execute('SELECT * FROM shipping_records ORDER BY order_pk, sort_order, id')
        items = [dict(row) for row in cursor.fetchall()]

        # 按 order_pk 分组
        items_by_order = {}
        for item in items:
            pk = item['order_pk']
            if pk not in items_by_order:
                items_by_order[pk] = []
            items_by_order[pk].append(item)

        conn.close()

        # 组装结果
        for order in orders:
            order['records'] = items_by_order.get(order['id'], [])
        return orders

    @staticmethod
    def get_by_id(record_id: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM shipping_records WHERE id = ?", (record_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    @staticmethod
    def update(record_id: int, data: dict):
        """更新单条明细。只更新 data 里传入的字段,缺省字段保持原值。

        联动规则:当 verified 从 1 切到 0(取消"全部已核查")时,自动清空 verified_warnings JSON。
        否则用户看到的是「干净的警告列表」,符合"全部已核查是顶层开关"的语义。
        """
        conn = get_db()
        cursor = conn.cursor()
        fields = []
        values = []
        for key in ['product_name', 'specification', 'quantity', 'unit', 'remark', 'verified']:
            if key in data:
                fields.append(f'{key} = ?')
                values.append(data[key])
        if not fields:
            conn.close()
            return
        # 取消核查联动清空 per-rule 状态
        _unverify_clears_warnings(data, fields, values)
        values.append(record_id)
        cursor.execute(f'UPDATE shipping_records SET {", ".join(fields)} WHERE id = ?', values)
        conn.commit()
        conn.close()

    @staticmethod
    def get_verified_warnings(record_id: int) -> dict:
        """读某条明细的 verified_warnings(JSON),反序列化为 dict,失败返回空 dict。"""
        return _verified_warnings_get('shipping_records', record_id)

    @staticmethod
    def set_verified_warning(record_id: int, rule_id: str, verified: bool) -> dict:
        """单条规则的核查切换:把 rule_id 设为 verified/未 verified,返回更新后的完整 dict。

        设计:与全局 verified 字段并存 —— verified=1 仍代表"整行已核查,所有警告都折叠";
        verified_warnings 让用户可以单独点掉/恢复某条警告(目前用到 b_white_300g)。
        整体流程:addRowWarning 把 verified_warnings 传进去 → 警告渲染时根据它决定是否显示;
        用户点 ✓ 核查 / ✗ 取消核查 → POST 此端点 → 局部刷新警告行。
        """
        return _verified_warnings_set('shipping_records', record_id, rule_id, verified)

    @staticmethod
    def delete(record_id: int):
        """删除单条明细"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM shipping_records WHERE id = ?', (record_id,))
        conn.commit()
        conn.close()

    @staticmethod
    def delete_by_order(order_id: int):
        """删除指定订单的所有明细（保留订单外壳）"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM shipping_records WHERE order_pk = ?', (order_id,))
        conn.commit()
        conn.close()

    @staticmethod
    def move_up(record_id: int):
        """上移一条明细(交换相邻 sort_order)。三 UPDATE 必须原子,否则中途崩溃
        会留下 sort_order=-1 的孤儿值,渲染顺序错乱。"""
        conn = get_db()
        cursor = conn.cursor()
        try:
            cursor.execute('BEGIN IMMEDIATE')
            cursor.execute('SELECT order_pk, sort_order FROM shipping_records WHERE id = ?', (record_id,))
            cur = cursor.fetchone()
            if not cur:
                conn.rollback(); conn.close()
                return False
            order_pk, cur_sort = cur['order_pk'], cur['sort_order']
            cursor.execute(
                'SELECT id, sort_order FROM shipping_records WHERE order_pk = ? AND sort_order < ? ORDER BY sort_order DESC LIMIT 1',
                (order_pk, cur_sort)
            )
            prev = cursor.fetchone()
            if not prev:
                conn.rollback(); conn.close()
                return False
            cursor.execute('UPDATE shipping_records SET sort_order = -1 WHERE id = ?', (record_id,))
            cursor.execute('UPDATE shipping_records SET sort_order = ? WHERE id = ?', (cur_sort, prev['id']))
            cursor.execute('UPDATE shipping_records SET sort_order = ? WHERE id = ?', (prev['sort_order'], record_id))
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def move_down(record_id: int):
        """下移一条明细(交换相邻 sort_order)。三 UPDATE 必须原子,理由同 move_up。"""
        conn = get_db()
        cursor = conn.cursor()
        try:
            cursor.execute('BEGIN IMMEDIATE')
            cursor.execute('SELECT order_pk, sort_order FROM shipping_records WHERE id = ?', (record_id,))
            cur = cursor.fetchone()
            if not cur:
                conn.rollback(); conn.close()
                return False
            order_pk, cur_sort = cur['order_pk'], cur['sort_order']
            cursor.execute(
                'SELECT id, sort_order FROM shipping_records WHERE order_pk = ? AND sort_order > ? ORDER BY sort_order ASC LIMIT 1',
                (order_pk, cur_sort)
            )
            nxt = cursor.fetchone()
            if not nxt:
                conn.rollback(); conn.close()
                return False
            cursor.execute('UPDATE shipping_records SET sort_order = -1 WHERE id = ?', (record_id,))
            cursor.execute('UPDATE shipping_records SET sort_order = ? WHERE id = ?', (cur_sort, nxt['id']))
            cursor.execute('UPDATE shipping_records SET sort_order = ? WHERE id = ?', (nxt['sort_order'], record_id))
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


class InboundOrder:
    @staticmethod
    def create(date: str, supplier: str):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT MAX(order_num) FROM inbound_orders WHERE date = ? AND supplier = ?', (date, supplier))
        max_num = cursor.fetchone()[0] or 0
        order_num = max_num + 1
        created_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute(
            'INSERT INTO inbound_orders (date, supplier, order_num, created_at) VALUES (?, ?, ?, ?)',
            (date, supplier, order_num, created_at)
        )
        conn.commit()
        order_id = cursor.lastrowid
        conn.close()
        return order_id

    @staticmethod
    def count_by_date(date: str) -> int:
        """指定日期的入库单条数(用于「今天入库N单」展示)。"""
        conn = get_db()
        cursor = conn.cursor()
        n = cursor.execute('SELECT COUNT(*) FROM inbound_orders WHERE date = ?', (date,)).fetchone()[0]
        conn.close()
        return n

    @staticmethod
    def get_all(start_date=None, end_date=None):
        conn = get_db()
        cursor = conn.cursor()
        if start_date and end_date:
            cursor.execute('SELECT * FROM inbound_orders WHERE date >= ? AND date <= ? ORDER BY created_at DESC', (start_date, end_date))
        else:
            cursor.execute('SELECT * FROM inbound_orders ORDER BY created_at DESC')
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    @staticmethod
    def get_by_id(order_id: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM inbound_orders WHERE id = ?', (order_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    @staticmethod
    def delete(order_id: int):
        """删除订单。有明细或图片时拒绝删除，必须先手动清空。"""
        conn = get_db()
        cursor = conn.cursor()
        # 检查子记录
        n_items = cursor.execute('SELECT COUNT(*) FROM inbound_records WHERE order_pk = ?', (order_id,)).fetchone()[0]
        n_imgs = cursor.execute('SELECT COUNT(*) FROM inbound_images WHERE order_pk = ?', (order_id,)).fetchone()[0]
        if n_items > 0 or n_imgs > 0:
            conn.close()
            return {'success': False, 'error': f'该订单下还有 {n_items} 条明细和 {n_imgs} 张图片，请先删除所有明细和图片后再删除订单'}
        cursor.execute('DELETE FROM inbound_orders WHERE id = ?', (order_id,))
        conn.commit()
        conn.close()
        return {'success': True}



    @staticmethod
    def lock(order_id: int):
        """锁定订单"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('UPDATE inbound_orders SET is_locked = 1 WHERE id = ?', (order_id,))
        conn.commit()
        conn.close()

    @staticmethod
    def unlock(order_id: int):
        """解锁订单"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('UPDATE inbound_orders SET is_locked = 0 WHERE id = ?', (order_id,))
        conn.commit()
        conn.close()

    @staticmethod
    def set_img_cols(order_id: int, cols: int):
        """设置入库订单图片列数(每订单独立记忆,与出货/装柜对齐)"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('UPDATE inbound_orders SET img_cols = ? WHERE id = ?', (cols, order_id))
        conn.commit()
        conn.close()

    @staticmethod
    def set_note(order_id: int, note: str):
        """设置入库订单级备注（显示在商品信息行上方）"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('UPDATE inbound_orders SET order_note = ? WHERE id = ?', (note or '', order_id))
        conn.commit()
        conn.close()

    @staticmethod
    def set_doc_number(order_id: int, doc_number: str):
        """设置单据编号"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('UPDATE inbound_orders SET doc_number = ? WHERE id = ?', (doc_number or '', order_id))
        conn.commit()
        conn.close()

    @staticmethod
    def set_supplier(order_id: int, supplier: str):
        """修改入库订单供应商名称（重排 order_num）"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT date, order_num FROM inbound_orders WHERE id = ?', (order_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return {'success': False, 'error': '订单不存在'}
        old_date, _ = row['date'], row['order_num']
        cursor.execute(
            'SELECT COALESCE(MAX(order_num), 0) + 1 FROM inbound_orders WHERE date = ? AND supplier = ?',
            (old_date, supplier)
        )
        new_order_num = cursor.fetchone()[0]
        try:
            cursor.execute(
                'UPDATE inbound_orders SET supplier = ?, order_num = ? WHERE id = ?',
                (supplier, new_order_num, order_id)
            )
            conn.commit()
        except Exception as e:
            conn.rollback()
            conn.close()
            return {'success': False, 'error': str(e)}
        conn.close()
        return {'success': True, 'supplier': supplier, 'order_num': new_order_num}

class InboundRecord:
    @staticmethod
    def create(order_pk: int, product_name: str, specification: str, quantity: str, unit: str = '支', remark: str = ''):
        if unit == '码':
            unit = 'y'
        conn = get_db()
        cursor = conn.cursor()
        # 取当前订单最大 sort_order + 1
        cursor.execute('SELECT COALESCE(MAX(sort_order), 0) + 1 FROM inbound_records WHERE order_pk = ?', (order_pk,))
        next_sort = cursor.fetchone()[0]
        cursor.execute(
            'INSERT INTO inbound_records (order_pk, product_name, specification, quantity, unit, remark, sort_order, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (order_pk, product_name, specification, quantity, unit, remark, next_sort, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        )
        conn.commit()
        record_id = cursor.lastrowid
        conn.close()
        return record_id

    @staticmethod
    def get_groups(start_date=None, end_date=None):
        conn = get_db()
        cursor = conn.cursor()
        if start_date and end_date:
            cursor.execute('SELECT * FROM inbound_orders WHERE date >= ? AND date <= ? ORDER BY created_at DESC', (start_date, end_date))
        else:
            cursor.execute('SELECT * FROM inbound_orders ORDER BY created_at DESC')
        orders = [dict(row) for row in cursor.fetchall()]
        
        cursor.execute('SELECT * FROM inbound_records ORDER BY order_pk, sort_order, id')
        items = [dict(row) for row in cursor.fetchall()]
        
        # 获取图片
        cursor.execute('SELECT * FROM inbound_images ORDER BY id ASC')
        images = [dict(row) for row in cursor.fetchall()]
        # 为每个图片添加 relative_path
        for img in images:
            img['relative_path'] = InboundImage.get_relative_path(img['file_path'])
        
        items_by_order = {}
        for item in items:
            pk = item['order_pk']
            if pk not in items_by_order:
                items_by_order[pk] = []
            items_by_order[pk].append(item)
        
        images_by_order = {}
        for image in images:
            pk = image['order_pk']
            if pk not in images_by_order:
                images_by_order[pk] = []
            images_by_order[pk].append(image)
        
        conn.close()
        
        for order in orders:
            order['records'] = items_by_order.get(order['id'], [])
            order['images'] = images_by_order.get(order['id'], [])
        return orders

    @staticmethod
    def get_by_id(record_id: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM inbound_records WHERE id = ?', (record_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    @staticmethod
    def update(record_id: int, data: dict):
        """更新单条明细。只更新 data 里传入的字段,缺省字段保持原值。

        联动规则:verified 切到 0(取消"全部已核查")时,自动清空 verified_warnings JSON。
        """
        conn = get_db()
        cursor = conn.cursor()
        fields = []
        values = []
        for key in ['product_name', 'specification', 'quantity', 'unit', 'remark', 'verified']:
            if key in data:
                fields.append(f'{key} = ?')
                values.append(data[key])
        if not fields:
            conn.close()
            return
        _unverify_clears_warnings(data, fields, values)
        values.append(record_id)
        cursor.execute(f'UPDATE inbound_records SET {", ".join(fields)} WHERE id = ?', values)
        conn.commit()
        conn.close()

    @staticmethod
    def get_verified_warnings(record_id: int) -> dict:
        """读某条明细的 verified_warnings(JSON) → dict。"""
        return _verified_warnings_get('inbound_records', record_id)

    @staticmethod
    def set_verified_warning(record_id: int, rule_id: str, verified: bool) -> dict:
        """单条规则核查切换,返回更新后的完整 dict。语义同出货。"""
        return _verified_warnings_set('inbound_records', record_id, rule_id, verified)

    @staticmethod
    def delete(record_id: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM inbound_records WHERE id = ?', (record_id,))
        conn.commit()
        conn.close()

    @staticmethod
    def move_up(record_id: int):
        """上移：与同订单内上一条记录交换 sort_order"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT order_pk, sort_order FROM inbound_records WHERE id = ?', (record_id,))
        cur = cursor.fetchone()
        if not cur:
            conn.close()
            return False
        order_pk, cur_sort = cur['order_pk'], cur['sort_order']
        # 找上一条：同订单中 sort_order < 当前 的最大一条
        cursor.execute(
            'SELECT id, sort_order FROM inbound_records WHERE order_pk = ? AND sort_order < ? ORDER BY sort_order DESC LIMIT 1',
            (order_pk, cur_sort)
        )
        prev = cursor.fetchone()
        if not prev:
            conn.close()
            return False
        # 交换
        cursor.execute('UPDATE inbound_records SET sort_order = -1 WHERE id = ?', (record_id,))
        cursor.execute('UPDATE inbound_records SET sort_order = ? WHERE id = ?', (cur_sort, prev['id']))
        cursor.execute('UPDATE inbound_records SET sort_order = ? WHERE id = ?', (prev['sort_order'], record_id))
        conn.commit()
        conn.close()
        return True

    @staticmethod
    def move_down(record_id: int):
        """下移：与同订单内下一条记录交换 sort_order"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT order_pk, sort_order FROM inbound_records WHERE id = ?', (record_id,))
        cur = cursor.fetchone()
        if not cur:
            conn.close()
            return False
        order_pk, cur_sort = cur['order_pk'], cur['sort_order']
        # 找下一条：同订单中 sort_order > 当前 的最小一条
        cursor.execute(
            'SELECT id, sort_order FROM inbound_records WHERE order_pk = ? AND sort_order > ? ORDER BY sort_order ASC LIMIT 1',
            (order_pk, cur_sort)
        )
        nxt = cursor.fetchone()
        if not nxt:
            conn.close()
            return False
        # 交换
        cursor.execute('UPDATE inbound_records SET sort_order = -1 WHERE id = ?', (record_id,))
        cursor.execute('UPDATE inbound_records SET sort_order = ? WHERE id = ?', (cur_sort, nxt['id']))
        cursor.execute('UPDATE inbound_records SET sort_order = ? WHERE id = ?', (nxt['sort_order'], record_id))
        conn.commit()
        conn.close()
        return True


class InboundImage:
    @staticmethod
    def create(order_pk: int, file_path: str, original_name: str = '', source: str = 'upload', record_pk: int = None, sort_order: int = None, source_tag: str = None):
        """插入图片。
        - record_pk: None=订单级共享图, 非空=某条明细的专属图
        - sort_order: None 时由本方法在事务内计算 max+1(同事务累加); 显式传入则按用户值
        - source_tag: 整体图分类标签 ('备货照'|'装车照'|'归仓照'|None)
        """
        conn = get_db()
        cursor = conn.cursor()
        if sort_order is None:
            cursor.execute(
                'SELECT COALESCE(MAX(sort_order), -1) + 1 AS next FROM inbound_images WHERE order_pk = ?',
                (order_pk,)
            )
            sort_order = cursor.fetchone()['next']
        cursor.execute(
            'INSERT INTO inbound_images (order_pk, file_path, original_name, source, record_pk, sort_order, source_tag, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (order_pk, file_path, original_name, source, record_pk, sort_order, source_tag,
             datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        )
        conn.commit()
        image_id = cursor.lastrowid
        conn.close()
        return image_id

    @staticmethod
    def get_by_order(order_pk: int):
        """查订单所有图(含订单级 + 记录级),按 sort_order + id 排"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT * FROM inbound_images WHERE order_pk = ? ORDER BY sort_order ASC, id ASC',
            (order_pk,)
        )
        rows = cursor.fetchall()
        conn.close()
        result = []
        for row in rows:
            item = dict(row)
            item['relative_path'] = InboundImage.get_relative_path(item['file_path'])
            result.append(item)
        return result

    @staticmethod
    def get_by_record(record_pk: int):
        """查某条明细专属图片(订单级共享图不算),按 sort_order + id 排"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT * FROM inbound_images WHERE record_pk = ? ORDER BY sort_order ASC, id ASC',
            (record_pk,)
        )
        rows = cursor.fetchall()
        conn.close()
        result = []
        for row in rows:
            item = dict(row)
            item['relative_path'] = InboundImage.get_relative_path(item['file_path'])
            result.append(item)
        return result

    @staticmethod
    def get_combined_for_record(order_pk: int, record_pk: int):
        """合并视图:某 record 专属图 + 订单共享图,按 sort_order + id 统一排"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            '''SELECT * FROM inbound_images WHERE order_pk = ? AND record_pk IS NULL
               UNION ALL
               SELECT * FROM inbound_images WHERE order_pk = ? AND record_pk = ?
               ORDER BY sort_order ASC, id ASC''',
            (order_pk, order_pk, record_pk)
        )
        rows = cursor.fetchall()
        conn.close()
        result = []
        for row in rows:
            item = dict(row)
            item['relative_path'] = InboundImage.get_relative_path(item['file_path'])
            result.append(item)
        return result

    @staticmethod
    def get_all_by_orders(order_ids=None):
        """获取图片,按 order_pk 分组返回字典。

        Args:
            order_ids: 可选的订单 ID 列表(过滤范围),None 表示取全部图片。
                       页面通常只显示某个日期范围的订单,传入该范围的 order_id 列表
                       避免无谓加载历史图片元数据。
        返回的每张图都含 source_tag key(默认 None),与出货/装柜对齐。
        """
        conn = get_db()
        cursor = conn.cursor()
        if order_ids:
            if not order_ids:  # 空列表 → 直接返回空
                conn.close()
                return {}
            placeholders = ','.join('?' * len(order_ids))
            cursor.execute(
                f'SELECT * FROM inbound_images WHERE order_pk IN ({placeholders}) ORDER BY id ASC',
                list(order_ids)
            )
        else:
            cursor.execute('SELECT * FROM inbound_images ORDER BY id ASC')
        rows = cursor.fetchall()
        conn.close()
        result = {}
        for row in rows:
            item = dict(row)
            # 缺列补 None(老库迁移后 source_tag 默认 NULL,SELECT * 已含;但防御性兜底)
            item.setdefault('source_tag', None)
            item['relative_path'] = InboundImage.get_relative_path(item['file_path'])
            order_pk = item['order_pk']
            if order_pk not in result:
                result[order_pk] = []
            result[order_pk].append(item)
        return result

    @staticmethod
    def get_relative_path(file_path: str) -> str:
        """返回相对于 upload 目录的路径（用于 URL）"""
        # 去掉绝对路径前缀
        if 'upload\\' in file_path:
            path = file_path.split('upload\\')[-1]
        elif 'upload/' in file_path:
            path = file_path.split('upload/')[-1]
        else:
            path = file_path
        # 统一使用正斜杠（URL 格式）
        return path.replace('\\', '/')

    @staticmethod
    def get_by_id(image_id: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM inbound_images WHERE id = ?', (image_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    @staticmethod
    def set_match(image_id: int, status: str, score: float, reason: str = '',
               source: str = None):
        """写入标签匹配结果（match_status/match_score/reason）。

        Args:
            image_id: 图片 id
            status:   'green' | 'yellow' | 'red' | '' (空 = 清空,不打徽章)
            score:    0~100 置信度
            reason:   AI 中文判定依据(给前端 hover 提示用)
            source:   'local_fuzzy' (默认,本地 RapidFuzz) 或 'deepseek' (云端)
                      写入 inbound_images.match_source,前端据此用不同图标
        """
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'UPDATE inbound_images SET match_status = ?, match_score = ?, '
            'reason = ?, match_source = ? WHERE id = ?',
            (status, score, reason or None,
             source or 'local_fuzzy',  # 默认 local_fuzzy
             image_id)
        )
        conn.commit()
        conn.close()

    @staticmethod
    def set_human_verified(image_id: int, verified: bool = True):
        """人工覆盖 AI 比对结果(目前仅用于红牌的"确认通过")。

        写入后前端应把红牌徽章视为已确认(可隐藏确认按钮,或展示"已确认"标记)。
        """
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'UPDATE inbound_images SET human_verified = ? WHERE id = ?',
            (1 if verified else 0, image_id)
        )
        conn.commit()
        conn.close()

    @staticmethod
    def set_bg_color(image_id: int, bg: str):
        """写入自动识别的图片背景色('black'|'white'),供前端展示"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('UPDATE inbound_images SET bg_color = ? WHERE id = ?', (bg, image_id))
        conn.commit()
        conn.close()

    @staticmethod
    def delete(image_id: int):
        conn = get_db()
        cursor = conn.cursor()
        # 先获取文件路径
        cursor.execute('SELECT file_path FROM inbound_images WHERE id = ?', (image_id,))
        row = cursor.fetchone()
        if row:
            file_path = row['file_path']
            # 删除数据库记录
            cursor.execute('DELETE FROM inbound_images WHERE id = ?', (image_id,))
            conn.commit()
            conn.close()
            # 删除文件（走回收站 + 路径白名单）
            _safe_remove_file(file_path)
            return True
        conn.close()
        return False

    @staticmethod
    def delete_by_order(order_pk: int):
        """删除指定入库单的所有图片"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT file_path FROM inbound_images WHERE order_pk = ?', (order_pk,))
        rows = cursor.fetchall()
        file_paths = [row['file_path'] for row in rows]
        cursor.execute('DELETE FROM inbound_images WHERE order_pk = ?', (order_pk,))
        conn.commit()
        conn.close()
        # 删除文件（走回收站 + 路径白名单）
        for file_path in file_paths:
            _safe_remove_file(file_path)

    @staticmethod
    def delete_by_record(record_pk: int):
        """删除某条明细的所有专属图片（DB 行 + 物理文件）。

        删除单条明细时必须调用，否则 record 删掉后其行级图片变成孤儿：
        页面按现存 record 循环取图不再显示它们，而订单又因"仍有图片"删不掉。
        """
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT file_path FROM inbound_images WHERE record_pk = ?', (record_pk,))
        file_paths = [row['file_path'] for row in cursor.fetchall()]
        cursor.execute('DELETE FROM inbound_images WHERE record_pk = ?', (record_pk,))
        conn.commit()
        conn.close()
        for file_path in file_paths:
            _safe_remove_file(file_path)
        return file_paths



class ShippingImage:
    @staticmethod
    def create(order_pk: int, file_path: str, original_name: str = '', source: str = 'upload', record_pk: int = None, sort_order: int = None, source_tag: str = None):
        """插入图片。
        - record_pk: None=订单级共享图, 非空=某条明细的专属图
        - sort_order: None 时由本方法在事务内计算 max+1(同事务累加); 显式传入则按用户值
        - source_tag: 整体图分类标签 ('备货照'|'装车照'|'归仓照'|None)
        """
        conn = get_db()
        cursor = conn.cursor()
        if sort_order is None:
            cursor.execute(
                'SELECT COALESCE(MAX(sort_order), -1) + 1 AS next FROM shipping_images WHERE order_pk = ?',
                (order_pk,)
            )
            sort_order = cursor.fetchone()['next']
        cursor.execute(
            'INSERT INTO shipping_images (order_pk, file_path, original_name, source, record_pk, sort_order, source_tag, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (order_pk, file_path, original_name, source, record_pk, sort_order, source_tag,
             datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        )
        conn.commit()
        image_id = cursor.lastrowid
        conn.close()
        return image_id

    @staticmethod
    def get_by_order(order_pk: int):
        """查订单所有图(含订单级 + 记录级),按 sort_order + id 排"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT * FROM shipping_images WHERE order_pk = ? ORDER BY sort_order ASC, id ASC',
            (order_pk,)
        )
        rows = cursor.fetchall()
        conn.close()
        result = []
        for row in rows:
            item = dict(row)
            item['relative_path'] = ShippingImage.get_relative_path(item['file_path'])
            result.append(item)
        return result

    @staticmethod
    def get_by_record(record_pk: int):
        """查某条明细专属图片(订单级共享图不算),按 sort_order + id 排"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT * FROM shipping_images WHERE record_pk = ? ORDER BY sort_order ASC, id ASC',
            (record_pk,)
        )
        rows = cursor.fetchall()
        conn.close()
        result = []
        for row in rows:
            item = dict(row)
            item['relative_path'] = ShippingImage.get_relative_path(item['file_path'])
            result.append(item)
        return result

    @staticmethod
    def get_combined_for_record(order_pk: int, record_pk: int):
        """合并视图:某 record 专属图 + 订单共享图,按 sort_order + id 统一排"""
        conn = get_db()
        cursor = conn.cursor()
        # UNION ALL 让两表自然合并,再排序
        cursor.execute(
            '''SELECT * FROM shipping_images WHERE order_pk = ? AND record_pk IS NULL
               UNION ALL
               SELECT * FROM shipping_images WHERE order_pk = ? AND record_pk = ?
               ORDER BY sort_order ASC, id ASC''',
            (order_pk, order_pk, record_pk)
        )
        rows = cursor.fetchall()
        conn.close()
        result = []
        for row in rows:
            item = dict(row)
            item['relative_path'] = ShippingImage.get_relative_path(item['file_path'])
            result.append(item)
        return result

    @staticmethod
    def get_relative_path(file_path: str) -> str:
        if 'upload\\' in file_path:
            path = file_path.split('upload\\')[-1]
        elif 'upload/' in file_path:
            path = file_path.split('upload/')[-1]
        else:
            path = file_path
        return path.replace('\\', '/')

    @staticmethod
    def get_by_id(image_id: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM shipping_images WHERE id = ?', (image_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    @staticmethod
    def set_match(image_id: int, status: str, score: float, reason: str = '',
               source: str = None):
        """写入标签匹配结果（match_status/match_score/reason）。

        Args:
            image_id: 图片 id
            status:   'green' | 'yellow' | 'red' | '' (空 = 清空,不打徽章)
            score:    0~100 置信度
            reason:   AI 中文判定依据(给前端 hover 提示用)
            source:   'local_fuzzy' (默认,本地 RapidFuzz) 或 'deepseek' (云端)
                      写入 shipping_images.match_source,前端据此用不同图标
        """
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'UPDATE shipping_images SET match_status = ?, match_score = ?, '
            'reason = ?, match_source = ? WHERE id = ?',
            (status, score, reason or None,
             source or 'local_fuzzy',  # 默认 local_fuzzy
             image_id)
        )
        conn.commit()
        conn.close()

    @staticmethod
    def set_human_verified(image_id: int, verified: bool = True):
        """人工覆盖 AI 比对结果(目前仅用于红牌的"确认通过")。

        写入后前端应把红牌徽章视为已确认(可隐藏确认按钮,或展示"已确认"标记)。
        """
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'UPDATE shipping_images SET human_verified = ? WHERE id = ?',
            (1 if verified else 0, image_id)
        )
        conn.commit()
        conn.close()

    @staticmethod
    def set_bg_color(image_id: int, bg: str):
        """写入自动识别的图片背景色('black'|'white'),供前端展示"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('UPDATE shipping_images SET bg_color = ? WHERE id = ?', (bg, image_id))
        conn.commit()
        conn.close()

    @staticmethod
    def delete(image_id: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT file_path FROM shipping_images WHERE id = ?', (image_id,))
        row = cursor.fetchone()
        if row:
            file_path = row['file_path']
            cursor.execute('DELETE FROM shipping_images WHERE id = ?', (image_id,))
            conn.commit()
            conn.close()
            # 删除文件（走回收站 + 路径白名单）
            _safe_remove_file(file_path)
            return True
        conn.close()
        return False

    @staticmethod
    def delete_by_order(order_pk: int):
        """删除指定出货单的所有图片（DB 行 + 物理文件）"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT file_path FROM shipping_images WHERE order_pk = ?', (order_pk,))
        rows = cursor.fetchall()
        file_paths = [row['file_path'] for row in rows]
        cursor.execute('DELETE FROM shipping_images WHERE order_pk = ?', (order_pk,))
        conn.commit()
        conn.close()
        for file_path in file_paths:
            _safe_remove_file(file_path)

    @staticmethod
    def delete_by_record(record_pk: int):
        """删除某条明细的所有专属图片（DB 行 + 物理文件）。

        删除单条明细时必须调用，否则 record 删掉后其行级图片变成孤儿：
        页面按现存 record 循环取图不再显示它们，而订单又因"仍有图片"删不掉。
        """
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT id, file_path FROM shipping_images WHERE record_pk = ?', (record_pk,))
        rows = cursor.fetchall()
        ids = [r['id'] for r in rows]
        file_paths = [r['file_path'] for r in rows]
        # 连带清理摆放图计数点(若有)
        if ids:
            qmarks = ','.join('?' * len(ids))
            cursor.execute(f'DELETE FROM placement_marks WHERE image_id IN ({qmarks})', ids)
        cursor.execute('DELETE FROM shipping_images WHERE record_pk = ?', (record_pk,))
        conn.commit()
        conn.close()
        for file_path in file_paths:
            _safe_remove_file(file_path)
        return file_paths

    @staticmethod
    def delete_record_images_by_order(order_pk: int):
        """删除某订单下所有【行级】图片（record_pk 非空），保留订单级共享图。

        清空整单明细（delete_by_order 记录）时调用：明细都没了，其行级图片
        应一并清理，但订单外壳仍在，故订单级共享图（record_pk IS NULL）保留。
        """
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT id, file_path FROM shipping_images WHERE order_pk = ? AND record_pk IS NOT NULL',
            (order_pk,)
        )
        rows = cursor.fetchall()
        ids = [r['id'] for r in rows]
        file_paths = [r['file_path'] for r in rows]
        # 连带清理摆放图计数点(若有)
        if ids:
            qmarks = ','.join('?' * len(ids))
            cursor.execute(f'DELETE FROM placement_marks WHERE image_id IN ({qmarks})', ids)
        cursor.execute(
            'DELETE FROM shipping_images WHERE order_pk = ? AND record_pk IS NOT NULL',
            (order_pk,)
        )
        conn.commit()
        conn.close()
        for file_path in file_paths:
            _safe_remove_file(file_path)
        return file_paths


class PlacementImage:
    """摆放图 + 计数点(供"支"类商品清点数量)。

    摆放图复用 shipping_images 表,标记 source='placement',与 OCR/AI 比对图彻底隔离
    (不参与 OCR、不进整体图区、不进 match-col 聚合)。点击计数点存 placement_marks 表。
    """

    PLACEMENT_SOURCE = 'placement'

    @staticmethod
    def create(order_pk: int, record_pk: int, file_path: str, original_name: str = '', sort_order: int = None):
        return ShippingImage.create(
            order_pk=order_pk, file_path=file_path, original_name=original_name,
            source=PlacementImage.PLACEMENT_SOURCE, record_pk=record_pk, sort_order=sort_order,
        )

    @staticmethod
    def _parse_circles(raw):
        """把 DB 里的 circles 文本解析成列表;异常/null 返回空列表。"""
        if not raw:
            return []
        if isinstance(raw, (list, tuple)):
            return list(raw)
        try:
            val = _json.loads(raw)
            return val if isinstance(val, list) else []
        except Exception:
            return []

    @staticmethod
    def get_by_record(record_pk: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM shipping_images WHERE record_pk = ? AND source = 'placement' ORDER BY sort_order ASC, id ASC",
            (record_pk,))
        rows = cursor.fetchall()
        conn.close()
        result = []
        for row in rows:
            item = dict(row)
            item['relative_path'] = ShippingImage.get_relative_path(item['file_path'])
            item['marks'] = PlacementImage.get_marks(item['id'])
            item['n_marks'] = len(item['marks'])
            item['circles'] = PlacementImage._parse_circles(item.get('circles'))
            item['mark_scale'] = item.get('mark_scale') or 1
            item['loose_count'] = item.get('loose_count') or 0
            item['manual_count'] = item.get('manual_count')
            item['is_unload'] = bool(item.get('is_unload'))
            # 有符号支数:直接输入优先,否则点击计数点;卸载时取负(从总数扣减)
            eff = item['manual_count'] if item['manual_count'] is not None else item['n_marks']
            item['effective_zhi'] = eff
            item['signed_count'] = -eff if item['is_unload'] else eff
            result.append(item)
        return result

    @staticmethod
    def get_by_id(image_id: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM shipping_images WHERE id = ? AND source = 'placement'", (image_id,))
        row = cursor.fetchone()
        conn.close()
        if not row:
            return None
        item = dict(row)
        item['relative_path'] = ShippingImage.get_relative_path(item['file_path'])
        item['marks'] = PlacementImage.get_marks(item['id'])
        item['n_marks'] = len(item['marks'])
        item['circles'] = PlacementImage._parse_circles(item.get('circles'))
        item['mark_scale'] = item.get('mark_scale') or 1
        item['loose_count'] = item.get('loose_count') or 0
        item['manual_count'] = item.get('manual_count')
        item['is_unload'] = bool(item.get('is_unload'))
        eff = item['manual_count'] if item['manual_count'] is not None else item['n_marks']
        item['effective_zhi'] = eff
        item['signed_count'] = -eff if item['is_unload'] else eff
        return item

    @staticmethod
    def set_circles(image_id: int, circles):
        """写入检测到的圆柱端面(circles 为 list[dict],会被 json 序列化)。"""
        import json as _json
        conn = get_db()
        cursor = conn.cursor()
        raw = _json.dumps(circles, ensure_ascii=False) if circles else None
        cursor.execute('UPDATE shipping_images SET circles = ? WHERE id = ?', (raw, image_id))
        conn.commit()
        conn.close()

    @staticmethod
    def set_mark_scale(image_id: int, scale: float):
        """写入该摆放图计数数字的整体系缩放比例(弹框放大/缩小按钮)。clamp 到 0.3~4.0。"""
        try:
            scale = float(scale)
        except (TypeError, ValueError):
            scale = 1.0
        if scale <= 0:
            scale = 1.0
        scale = max(0.3, min(4.0, scale))
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('UPDATE shipping_images SET mark_scale = ? WHERE id = ?', (scale, image_id))
        conn.commit()
        conn.close()
        return scale

    @staticmethod
    def set_loose_count(image_id: int, count):
        """写入该摆放图的散码数量(点数弹框内「散码」按钮录入,支持小数 0.5/1.2)。负数按 0 处理。"""
        try:
            count = round(float(count), 1)
        except (TypeError, ValueError):
            count = 0
        if count < 0:
            count = 0
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('UPDATE shipping_images SET loose_count = ? WHERE id = ?', (count, image_id))
        conn.commit()
        conn.close()
        return count

    @staticmethod
    def set_manual_count(image_id: int, count):
        """写入该摆放图直接输入的支数(点数弹框内「输入支数」按钮录入)。

        - count 为 None / 空 → 置 NULL,回退到点击计数点(n_marks),保留已有 marks
        - count 为整数(含 0) → 写入该值,并清空点击计数点(直接输入与点击计数互斥,
          避免两种计数同时存在导致比对口径不一致)
        """
        conn = get_db()
        cursor = conn.cursor()
        if count is None or count == '':
            count = None
        else:
            try:
                count = int(round(float(count)))
            except (TypeError, ValueError):
                count = 0
            if count < 0:
                count = 0
        if count is None:
            cursor.execute('UPDATE shipping_images SET manual_count = NULL WHERE id = ?', (image_id,))
        else:
            cursor.execute('UPDATE shipping_images SET manual_count = ? WHERE id = ?', (count, image_id))
            cursor.execute('DELETE FROM placement_marks WHERE image_id = ?', (image_id,))
        conn.commit()
        conn.close()
        return count

    @staticmethod
    def set_unload(image_id: int, unload: bool):
        """设置该摆放图是否「卸载货物」。勾上时其清点支数以负数计入记录总数(从总数扣减)。

        返回值即写入后的 is_unload(0/1)。
        """
        flag = 1 if unload else 0
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('UPDATE shipping_images SET is_unload = ? WHERE id = ?', (flag, image_id))
        conn.commit()
        conn.close()
        return flag

    @staticmethod
    def delete(image_id: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT file_path FROM shipping_images WHERE id = ? AND source = ?',
                       (image_id, PlacementImage.PLACEMENT_SOURCE))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return False
        file_path = row['file_path']
        cursor.execute('DELETE FROM placement_marks WHERE image_id = ?', (image_id,))
        cursor.execute('DELETE FROM shipping_images WHERE id = ?', (image_id,))
        conn.commit()
        conn.close()
        _safe_remove_file(file_path)
        return True

    @staticmethod
    def add_mark(image_id: int, x_ratio: float, y_ratio: float, mark_r: float = 0.0) -> int:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT COALESCE(MAX(seq), 0) + 1 FROM placement_marks WHERE image_id = ?', (image_id,))
        seq = cursor.fetchone()[0]
        cursor.execute(
            'INSERT INTO placement_marks (image_id, seq, x_ratio, y_ratio, mark_r, created_at) VALUES (?, ?, ?, ?, ?, ?)',
            (image_id, seq, x_ratio, y_ratio, mark_r, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
        conn.close()
        return seq

    @staticmethod
    def get_marks(image_id: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM placement_marks WHERE image_id = ? ORDER BY seq ASC', (image_id,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]

    @staticmethod
    def delete_last_mark(image_id: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT MAX(seq) FROM placement_marks WHERE image_id = ?', (image_id,))
        max_seq = cursor.fetchone()[0]
        if max_seq is None:
            conn.close()
            return []
        cursor.execute('DELETE FROM placement_marks WHERE image_id = ? AND seq = ?', (image_id, max_seq))
        conn.commit()
        conn.close()
        return PlacementImage.get_marks(image_id)

    @staticmethod
    def delete_by_record(record_pk: int):
        """删除某明细的所有摆放图(连带计数点 + 物理文件)。"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT id, file_path FROM shipping_images WHERE record_pk = ? AND source = ?',
                       (record_pk, PlacementImage.PLACEMENT_SOURCE))
        rows = cursor.fetchall()
        for row in rows:
            cursor.execute('DELETE FROM placement_marks WHERE image_id = ?', (row['id'],))
            _safe_remove_file(row['file_path'])
        n = len(rows)
        if n:
            cursor.execute('DELETE FROM shipping_images WHERE record_pk = ? AND source = ?',
                           (record_pk, PlacementImage.PLACEMENT_SOURCE))
        conn.commit()
        conn.close()
        return n


class LoadingPlacementImage:
    """装柜摆放图 + 计数点(对齐出货页「交互式点数清点」)。

    复用 loading_order_images 表,标记 source='placement',与装柜 OCR/AI 比对图隔离。
    计数点存独立的 loading_placement_marks 表(避免与 shipping 主键冲突串图)。
    口径与出货 PlacementImage 完全一致:直接输入支数(manual_count)优先,无则回退点击
    计数点(n_marks);勾选「卸载货物」(is_unload)时取负,从记录总数扣减。
    """

    PLACEMENT_SOURCE = 'placement'

    @staticmethod
    def create(order_pk: int, record_pk: int, file_path: str, original_name: str = '', sort_order: int = None):
        conn = get_db()
        cursor = conn.cursor()
        if sort_order is None:
            cursor.execute(
                'SELECT COALESCE(MAX(sort_order), -1) + 1 AS next FROM loading_order_images '
                'WHERE order_pk = ? AND source = ?',
                (order_pk, LoadingPlacementImage.PLACEMENT_SOURCE))
            sort_order = cursor.fetchone()['next']
        cursor.execute(
            'INSERT INTO loading_order_images '
            '(order_pk, record_pk, file_path, original_name, source, sort_order, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?)',
            (order_pk, record_pk, file_path, original_name,
             LoadingPlacementImage.PLACEMENT_SOURCE, sort_order,
             datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
        image_id = cursor.lastrowid
        conn.close()
        return image_id

    @staticmethod
    def _parse_circles(raw):
        if not raw:
            return []
        if isinstance(raw, (list, tuple)):
            return list(raw)
        try:
            val = _json.loads(raw)
            return val if isinstance(val, list) else []
        except Exception:
            return []

    @staticmethod
    def get_by_record(record_pk: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM loading_order_images WHERE record_pk = ? AND source = 'placement' ORDER BY sort_order ASC, id ASC",
            (record_pk,))
        rows = cursor.fetchall()
        conn.close()
        result = []
        for row in rows:
            item = dict(row)
            item['relative_path'] = LoadingOrderImage.get_relative_path(item['file_path'])
            item['marks'] = LoadingPlacementImage.get_marks(item['id'])
            item['n_marks'] = len(item['marks'])
            item['circles'] = LoadingPlacementImage._parse_circles(item.get('circles'))
            item['mark_scale'] = item.get('mark_scale') or 1
            item['loose_count'] = item.get('loose_count') or 0
            item['manual_count'] = item.get('manual_count')
            item['is_unload'] = bool(item.get('is_unload'))
            eff = item['manual_count'] if item['manual_count'] is not None else item['n_marks']
            item['effective_zhi'] = eff
            item['signed_count'] = -eff if item['is_unload'] else eff
            result.append(item)
        return result

    @staticmethod
    def get_by_id(image_id: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM loading_order_images WHERE id = ? AND source = 'placement'", (image_id,))
        row = cursor.fetchone()
        conn.close()
        if not row:
            return None
        item = dict(row)
        item['relative_path'] = LoadingOrderImage.get_relative_path(item['file_path'])
        item['marks'] = LoadingPlacementImage.get_marks(item['id'])
        item['n_marks'] = len(item['marks'])
        item['circles'] = LoadingPlacementImage._parse_circles(item.get('circles'))
        item['mark_scale'] = item.get('mark_scale') or 1
        item['loose_count'] = item.get('loose_count') or 0
        item['manual_count'] = item.get('manual_count')
        item['is_unload'] = bool(item.get('is_unload'))
        eff = item['manual_count'] if item['manual_count'] is not None else item['n_marks']
        item['effective_zhi'] = eff
        item['signed_count'] = -eff if item['is_unload'] else eff
        return item

    @staticmethod
    def set_circles(image_id: int, circles):
        import json as _json2
        conn = get_db()
        cursor = conn.cursor()
        raw = _json2.dumps(circles, ensure_ascii=False) if circles else None
        cursor.execute('UPDATE loading_order_images SET circles = ? WHERE id = ?', (raw, image_id))
        conn.commit()
        conn.close()

    @staticmethod
    def set_mark_scale(image_id: int, scale: float):
        try:
            scale = float(scale)
        except (TypeError, ValueError):
            scale = 1.0
        if scale <= 0:
            scale = 1.0
        scale = max(0.3, min(4.0, scale))
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('UPDATE loading_order_images SET mark_scale = ? WHERE id = ?', (scale, image_id))
        conn.commit()
        conn.close()
        return scale

    @staticmethod
    def set_loose_count(image_id: int, count):
        try:
            count = round(float(count), 1)
        except (TypeError, ValueError):
            count = 0
        if count < 0:
            count = 0
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('UPDATE loading_order_images SET loose_count = ? WHERE id = ?', (count, image_id))
        conn.commit()
        conn.close()
        return count

    @staticmethod
    def set_manual_count(image_id: int, count):
        conn = get_db()
        cursor = conn.cursor()
        if count is None or count == '':
            count = None
        else:
            try:
                count = int(round(float(count)))
            except (TypeError, ValueError):
                count = 0
            if count < 0:
                count = 0
        if count is None:
            cursor.execute('UPDATE loading_order_images SET manual_count = NULL WHERE id = ?', (image_id,))
        else:
            cursor.execute('UPDATE loading_order_images SET manual_count = ? WHERE id = ?', (count, image_id))
            cursor.execute('DELETE FROM loading_placement_marks WHERE image_id = ?', (image_id,))
        conn.commit()
        conn.close()
        return count

    @staticmethod
    def set_unload(image_id: int, unload: bool):
        flag = 1 if unload else 0
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('UPDATE loading_order_images SET is_unload = ? WHERE id = ?', (flag, image_id))
        conn.commit()
        conn.close()
        return flag

    @staticmethod
    def delete(image_id: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT file_path FROM loading_order_images WHERE id = ? AND source = ?',
                       (image_id, LoadingPlacementImage.PLACEMENT_SOURCE))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return False
        file_path = row['file_path']
        cursor.execute('DELETE FROM loading_placement_marks WHERE image_id = ?', (image_id,))
        cursor.execute('DELETE FROM loading_order_images WHERE id = ?', (image_id,))
        conn.commit()
        conn.close()
        _safe_remove_file(file_path)
        return True

    @staticmethod
    def add_mark(image_id: int, x_ratio: float, y_ratio: float, mark_r: float = 0.0) -> int:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT COALESCE(MAX(seq), 0) + 1 FROM loading_placement_marks WHERE image_id = ?', (image_id,))
        seq = cursor.fetchone()[0]
        cursor.execute(
            'INSERT INTO loading_placement_marks (image_id, seq, x_ratio, y_ratio, mark_r, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (image_id, seq, x_ratio, y_ratio, mark_r, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
        conn.close()
        return seq

    @staticmethod
    def get_marks(image_id: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM loading_placement_marks WHERE image_id = ? ORDER BY seq ASC', (image_id,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]

    @staticmethod
    def delete_last_mark(image_id: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT MAX(seq) FROM loading_placement_marks WHERE image_id = ?', (image_id,))
        max_seq = cursor.fetchone()[0]
        if max_seq is None:
            conn.close()
            return []
        cursor.execute('DELETE FROM loading_placement_marks WHERE image_id = ? AND seq = ?', (image_id, max_seq))
        conn.commit()
        conn.close()
        return LoadingPlacementImage.get_marks(image_id)

    @staticmethod
    def delete_by_record(record_pk: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT id, file_path FROM loading_order_images WHERE record_pk = ? AND source = ?',
                       (record_pk, LoadingPlacementImage.PLACEMENT_SOURCE))
        rows = cursor.fetchall()
        for row in rows:
            cursor.execute('DELETE FROM loading_placement_marks WHERE image_id = ?', (row['id'],))
            _safe_remove_file(row['file_path'])
        n = len(rows)
        if n:
            cursor.execute('DELETE FROM loading_order_images WHERE record_pk = ? AND source = ?',
                           (record_pk, LoadingPlacementImage.PLACEMENT_SOURCE))
        conn.commit()
        conn.close()
        return n


class InboundPlacementImage:
    """inbound page placement image + click count marks (align shipping / loading).

    Reuses inbound_images table with source='placement', isolated from OCR / AI match.
    Count marks stored in inbound_placement_marks table (separate FK to inbound_images).
    API surface mirrors PlacementImage / LoadingPlacementImage so front-end can share
    placement_count.js via configurable API base.
    """

    PLACEMENT_SOURCE = 'placement'

    @staticmethod
    def create(order_pk: int, record_pk: int, file_path: str, original_name: str = '', sort_order: int = None):
        conn = get_db()
        cursor = conn.cursor()
        if sort_order is None:
            cursor.execute(
                'SELECT COALESCE(MAX(sort_order), -1) + 1 AS next FROM inbound_images '
                'WHERE order_pk = ? AND source = ?',
                (order_pk, InboundPlacementImage.PLACEMENT_SOURCE))
            sort_order = cursor.fetchone()['next']
        cursor.execute(
            'INSERT INTO inbound_images '
            '(order_pk, record_pk, file_path, original_name, source, sort_order, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?)',
            (order_pk, record_pk, file_path, original_name,
             InboundPlacementImage.PLACEMENT_SOURCE, sort_order,
             datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
        image_id = cursor.lastrowid
        conn.close()
        return image_id

    @staticmethod
    def _parse_circles(raw):
        if not raw:
            return []
        if isinstance(raw, (list, tuple)):
            return list(raw)
        try:
            val = _json.loads(raw)
            return val if isinstance(val, list) else []
        except Exception:
            return []

    @staticmethod
    def get_by_record(record_pk: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM inbound_images WHERE record_pk = ? AND source = 'placement' ORDER BY sort_order ASC, id ASC",
            (record_pk,))
        rows = cursor.fetchall()
        conn.close()
        result = []
        for row in rows:
            item = dict(row)
            item['relative_path'] = InboundImage.get_relative_path(item['file_path'])
            item['marks'] = InboundPlacementImage.get_marks(item['id'])
            item['n_marks'] = len(item['marks'])
            item['circles'] = InboundPlacementImage._parse_circles(item.get('circles'))
            item['mark_scale'] = item.get('mark_scale') or 1
            item['loose_count'] = item.get('loose_count') or 0
            item['manual_count'] = item.get('manual_count')
            item['is_unload'] = bool(item.get('is_unload'))
            eff = item['manual_count'] if item['manual_count'] is not None else item['n_marks']
            item['effective_zhi'] = eff
            item['signed_count'] = -eff if item['is_unload'] else eff
            result.append(item)
        return result

    @staticmethod
    def get_by_id(image_id: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM inbound_images WHERE id = ? AND source = 'placement'", (image_id,))
        row = cursor.fetchone()
        conn.close()
        if not row:
            return None
        item = dict(row)
        item['relative_path'] = InboundImage.get_relative_path(item['file_path'])
        item['marks'] = InboundPlacementImage.get_marks(item['id'])
        item['n_marks'] = len(item['marks'])
        item['circles'] = InboundPlacementImage._parse_circles(item.get('circles'))
        item['mark_scale'] = item.get('mark_scale') or 1
        item['loose_count'] = item.get('loose_count') or 0
        item['manual_count'] = item.get('manual_count')
        item['is_unload'] = bool(item.get('is_unload'))
        eff = item['manual_count'] if item['manual_count'] is not None else item['n_marks']
        item['effective_zhi'] = eff
        item['signed_count'] = -eff if item['is_unload'] else eff
        return item

    @staticmethod
    def set_circles(image_id: int, circles):
        raw = _json.dumps(circles, ensure_ascii=False) if circles else None
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('UPDATE inbound_images SET circles = ? WHERE id = ?', (raw, image_id))
        conn.commit()
        conn.close()

    @staticmethod
    def set_mark_scale(image_id: int, scale: float):
        try:
            scale = float(scale)
        except (TypeError, ValueError):
            scale = 1.0
        if scale <= 0:
            scale = 1.0
        scale = max(0.3, min(4.0, scale))
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('UPDATE inbound_images SET mark_scale = ? WHERE id = ?', (scale, image_id))
        conn.commit()
        conn.close()
        return scale

    @staticmethod
    def set_loose_count(image_id: int, count):
        try:
            count = round(float(count), 1)
        except (TypeError, ValueError):
            count = 0
        if count < 0:
            count = 0
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('UPDATE inbound_images SET loose_count = ? WHERE id = ?', (count, image_id))
        conn.commit()
        conn.close()
        return count

    @staticmethod
    def set_manual_count(image_id: int, count):
        conn = get_db()
        cursor = conn.cursor()
        if count is None or count == '':
            count = None
        else:
            try:
                count = int(round(float(count)))
            except (TypeError, ValueError):
                count = 0
            if count < 0:
                count = 0
        if count is None:
            cursor.execute('UPDATE inbound_images SET manual_count = NULL WHERE id = ?', (image_id,))
        else:
            cursor.execute('UPDATE inbound_images SET manual_count = ? WHERE id = ?', (count, image_id))
            cursor.execute('DELETE FROM inbound_placement_marks WHERE image_id = ?', (image_id,))
        conn.commit()
        conn.close()
        return count

    @staticmethod
    def set_unload(image_id: int, unload: bool):
        flag = 1 if unload else 0
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('UPDATE inbound_images SET is_unload = ? WHERE id = ?', (flag, image_id))
        conn.commit()
        conn.close()
        return flag

    @staticmethod
    def delete(image_id: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT file_path FROM inbound_images WHERE id = ? AND source = ?',
            (image_id, InboundPlacementImage.PLACEMENT_SOURCE))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return False
        file_path = row['file_path']
        cursor.execute('DELETE FROM inbound_placement_marks WHERE image_id = ?', (image_id,))
        cursor.execute('DELETE FROM inbound_images WHERE id = ?', (image_id,))
        conn.commit()
        conn.close()
        _safe_remove_file(file_path)
        return True

    @staticmethod
    def add_mark(image_id: int, x_ratio: float, y_ratio: float, mark_r: float = 0.0) -> int:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT COALESCE(MAX(seq), 0) + 1 FROM inbound_placement_marks WHERE image_id = ?', (image_id,))
        seq = cursor.fetchone()[0]
        cursor.execute(
            'INSERT INTO inbound_placement_marks (image_id, seq, x_ratio, y_ratio, mark_r, created_at) VALUES (?, ?, ?, ?, ?, ?)',
            (image_id, seq, x_ratio, y_ratio, mark_r, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
        conn.close()
        return seq

    @staticmethod
    def get_marks(image_id: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT id, seq, x_ratio, y_ratio, mark_r FROM inbound_placement_marks WHERE image_id = ? ORDER BY seq ASC',
            (image_id,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]

    @staticmethod
    def delete_last_mark(image_id: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT id FROM inbound_placement_marks WHERE image_id = ? ORDER BY seq DESC LIMIT 1',
            (image_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return []
        cursor.execute('DELETE FROM inbound_placement_marks WHERE id = ?', (row['id'],))
        conn.commit()
        conn.close()
        return InboundPlacementImage.get_marks(image_id)

    @staticmethod
    def delete_by_record(record_pk: int):
        """delete all placement images for a record (cleanup on record delete)."""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, file_path FROM inbound_images WHERE record_pk = ? AND source = 'placement'",
            (record_pk,))
        rows = cursor.fetchall()
        file_paths = [r['file_path'] for r in rows]
        ids = [r['id'] for r in rows]
        if ids:
            qmarks = ','.join('?' * len(ids))
            cursor.execute(f'DELETE FROM inbound_placement_marks WHERE image_id IN ({qmarks})', ids)
        cursor.execute(
            "DELETE FROM inbound_images WHERE record_pk = ? AND source = 'placement'",
            (record_pk,))
        conn.commit()
        conn.close()
        for fp in file_paths:
            _safe_remove_file(fp)
        return file_paths


class OcrMatchEvent:
    """出货 OCR/AI/人工核查三类事件的 append-only 日志。

    三类事件:
      - 'record_ocr':  行级图片上传后,本地 OCR + fuzzy 匹配结果
      - 'ai_match':    整单 ai-match 后,DeepSeek 比对结果(每个 record 一条)
      - 'human_verify':人工核查 manual-verify 时,该图的 AI 裁决 vs 人裁决
    """
    # 允许写入字段白名单(防 SQL 注入,只接受已知列)
    _ALLOWED_FIELDS = {
        'ocr_text', 'ocr_engine', 'product_name', 'specification',
        'prompt_payload', 'ai_match_status', 'ai_match_score',
        'ai_match_reason', 'ai_raw_response', 'ai_engine',
        'prompt_version', 'human_status', 'human_reason',
        'human_verified_by', 'created_at',
    }

    @staticmethod
    def create(event_type: str, record_id: int, order_id: int, image_id: int = None, **fields) -> int:
        """写一条事件。失败时 logger.error,不抛回。"""
        import logging
        from datetime import datetime as _dt
        from . import _db as _db_mod
        conn = None
        try:
            conn = _db_mod.get_db()
            cursor = conn.cursor()
            data = {
                'event_type': event_type,
                'record_id': record_id,
                'order_id': order_id,
                'image_id': image_id,
            }
            for k, v in fields.items():
                if k in OcrMatchEvent._ALLOWED_FIELDS:
                    data[k] = v
            # created_at 默认 = 现在
            data.setdefault('created_at', _dt.now().strftime('%Y-%m-%d %H:%M:%S'))
            cols = list(data.keys())
            placeholders = ','.join('?' for _ in cols)
            sql = f'INSERT INTO ocr_match_event ({",".join(cols)}) VALUES ({placeholders})'
            cursor.execute(sql, [data[k] for k in cols])
            eid = cursor.lastrowid
            conn.commit()
            conn.close()
            return eid
        except Exception:
            logging.getLogger(__name__).exception(
                'OcrMatchEvent.create 失败 (event=%s record=%s):', event_type, record_id,
            )
            try:
                if conn:
                    conn.close()
            except Exception:
                pass
            return None

    @staticmethod
    def get_by_record(record_id: int, event_type: str = None) -> list:
        """查某明细行的所有事件(老→新)。event_type 过滤可选。"""
        conn = get_db()
        cursor = conn.cursor()
        if event_type:
            cursor.execute(
                'SELECT * FROM ocr_match_event WHERE record_id = ? AND event_type = ? ORDER BY created_at ASC, id ASC',
                (record_id, event_type),
            )
        else:
            cursor.execute(
                'SELECT * FROM ocr_match_event WHERE record_id = ? ORDER BY created_at ASC, id ASC',
                (record_id,),
            )
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]

    @staticmethod
    def get_by_image(image_id: int, event_type: str = None) -> list:
        """查某图片的所有事件(老→新;同 record_ocr / human_verify 用)。

        ai_match 事件按 record 写,image_id=NULL,所以不在此查询范围。
        """
        conn = get_db()
        cursor = conn.cursor()
        if event_type:
            cursor.execute(
                'SELECT * FROM ocr_match_event WHERE image_id = ? AND event_type = ? '
                'ORDER BY created_at ASC, id ASC',
                (image_id, event_type),
            )
        else:
            cursor.execute(
                'SELECT * FROM ocr_match_event WHERE image_id = ? ORDER BY created_at ASC, id ASC',
                (image_id,),
            )
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]

    @staticmethod
    def get_latest_by_record(record_id: int, event_type: str) -> dict | None:
        """取某 record 最新一条某类型事件(按 created_at DESC, id DESC 兜底)。

        主要给 ai_match 用(image_id=NULL,不能按图查)。
        """
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT * FROM ocr_match_event WHERE record_id = ? AND event_type = ? '
            'ORDER BY created_at DESC, id DESC LIMIT 1',
            (record_id, event_type),
        )
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    @staticmethod
    def get_latest_by_image(image_id: int, event_type: str) -> dict | None:
        """取某图片最新一条某类型事件。record_ocr / human_verify 用。"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT * FROM ocr_match_event WHERE image_id = ? AND event_type = ? '
            'ORDER BY created_at DESC, id DESC LIMIT 1',
            (image_id, event_type),
        )
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    @staticmethod
    def get_by_order(order_id: int, event_type: str = None) -> list:
        """查某订单所有事件(老→新;先 record_id 内排序、再合并)。"""
        conn = get_db()
        cursor = conn.cursor()
        if event_type:
            cursor.execute(
                'SELECT * FROM ocr_match_event WHERE order_id = ? AND event_type = ? ORDER BY record_id ASC, created_at ASC, id ASC',
                (order_id, event_type),
            )
        else:
            cursor.execute(
                'SELECT * FROM ocr_match_event WHERE order_id = ? ORDER BY record_id ASC, created_at ASC, id ASC',
                (order_id,),
            )
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]

    @staticmethod
    def get_ai_human_delta(record_id: int) -> list:
        """取同 record 的所有事件;分析 AI vs 人裁决一致率时用。"""
        return OcrMatchEvent.get_by_record(record_id)


class LoadingOrder:
    """装柜订单主表"""
    @staticmethod
    def create(date: str, customer: str = '') -> int:
        """创建新订单，返回新订单的 id"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT COALESCE(MAX(order_num), 0) + 1 FROM loading_orders WHERE date = ? AND customer = ?',
            (date, customer)
        )
        order_num = cursor.fetchone()[0]
        cursor.execute(
            'INSERT INTO loading_orders (date, customer, order_num, created_at) VALUES (?, ?, ?, ?)',
            (date, customer, order_num, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        )
        new_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return new_id

    @staticmethod
    def get_all():
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM loading_orders ORDER BY date DESC, customer ASC, order_num ASC')
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    @staticmethod
    def get_by_id(order_id: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM loading_orders WHERE id = ?', (order_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    @staticmethod
    def delete(order_id: int):
        """删除订单。有明细或图片时拒绝删除，必须先手动清空。"""
        conn = get_db()
        cursor = conn.cursor()
        # 检查子记录
        n_items = cursor.execute('SELECT COUNT(*) FROM loading_order_records WHERE order_pk = ?', (order_id,)).fetchone()[0]
        n_imgs = cursor.execute('SELECT COUNT(*) FROM loading_order_images WHERE order_pk = ?', (order_id,)).fetchone()[0]
        if n_items > 0 or n_imgs > 0:
            conn.close()
            return {'success': False, 'error': f'该订单下还有 {n_items} 条明细和 {n_imgs} 张图片，请先删除所有明细和图片后再删除订单'}
        cursor.execute('DELETE FROM loading_orders WHERE id = ?', (order_id,))
        conn.commit()
        conn.close()
        return {'success': True}

    @staticmethod
    def lock(order_id: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('UPDATE loading_orders SET is_locked = 1 WHERE id = ?', (order_id,))
        conn.commit()
        conn.close()

    @staticmethod
    def unlock(order_id: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('UPDATE loading_orders SET is_locked = 0 WHERE id = ?', (order_id,))
        conn.commit()
        conn.close()

    @staticmethod
    def set_note(order_id: int, note: str):
        """设置装柜订单级备注（显示在商品信息行上方）"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('UPDATE loading_orders SET order_note = ? WHERE id = ?', (note or '', order_id))
        conn.commit()
        conn.close()

    @staticmethod
    def set_doc_number(order_id: int, doc_number: str):
        """设置单据编号"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('UPDATE loading_orders SET doc_number = ? WHERE id = ?', (doc_number or '', order_id))
        conn.commit()
        conn.close()

    @staticmethod
    def set_img_cols(order_id: int, cols: int):
        """设置装柜订单图片列数（每订单独立记忆，与 shipping_orders 对齐）"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('UPDATE loading_orders SET img_cols = ? WHERE id = ?', (cols, order_id))
        conn.commit()
        conn.close()

    @staticmethod
    def set_customer(order_id: int, customer: str):
        """修改装柜订单客户名称（重排 order_num）"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT date, order_num FROM loading_orders WHERE id = ?', (order_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return {'success': False, 'error': '订单不存在'}
        old_date, _ = row['date'], row['order_num']
        cursor.execute(
            'SELECT COALESCE(MAX(order_num), 0) + 1 FROM loading_orders WHERE date = ? AND customer = ?',
            (old_date, customer)
        )
        new_order_num = cursor.fetchone()[0]
        try:
            cursor.execute(
                'UPDATE loading_orders SET customer = ?, order_num = ? WHERE id = ?',
                (customer, new_order_num, order_id)
            )
            conn.commit()
        except Exception as e:
            conn.rollback()
            conn.close()
            return {'success': False, 'error': str(e)}
        conn.close()
        return {'success': True, 'customer': customer, 'order_num': new_order_num}

    @staticmethod
    def toggle_lock(order_id: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('UPDATE loading_orders SET is_locked = NOT is_locked WHERE id = ?', (order_id,))
        conn.commit()
        conn.close()


class LoadingOrderRecord:
    """装柜订单商品明细"""
    @staticmethod
    def create(date: str, customer: str = '', product_name: str = '', specification: str = '', quantity: str = '', unit: str = '支', remark: str = '', order_pk: int = None):
        """添加商品明细，order_pk 不传时自动创建新订单"""
        if unit == '码':
            unit = 'y'
        conn = get_db()
        cursor = conn.cursor()
        if order_pk is None:
            order_pk = LoadingOrder.create(date, customer)
        cursor.execute('SELECT COALESCE(MAX(sort_order), 0) + 1 FROM loading_order_records WHERE order_pk = ?', (order_pk,))
        next_sort = cursor.fetchone()[0]
        cursor.execute(
            'INSERT INTO loading_order_records (order_pk, product_name, specification, quantity, unit, remark, sort_order, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (order_pk, product_name, specification, quantity, unit, remark, next_sort, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        )
        conn.commit()
        record_id = cursor.lastrowid
        conn.close()
        return record_id

    @staticmethod
    def get_all():
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT r.*, o.date, o.customer, o.order_num, o.is_locked
            FROM loading_order_records r
            JOIN loading_orders o ON r.order_pk = o.id
            ORDER BY o.date DESC, o.customer ASC, o.order_num ASC, r.sort_order, r.id
        ''')
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    @staticmethod
    def get_by_order(order_pk: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM loading_order_records WHERE order_pk = ? ORDER BY sort_order, id', (order_pk,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    @staticmethod
    def get_by_id(record_id: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM loading_order_records WHERE id = ?', (record_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    @staticmethod
    def update(record_id: int, data: dict):
        """更新单条明细。只更新 data 里传入的字段,缺省字段保持原值。

        联动规则:verified 切到 0(取消"全部已核查")时,自动清空 verified_warnings JSON。
        """
        conn = get_db()
        cursor = conn.cursor()
        fields = []
        values = []
        for key in ['product_name', 'specification', 'quantity', 'unit', 'remark', 'verified']:
            if key in data:
                fields.append(f'{key} = ?')
                values.append(data[key])
        if not fields:
            conn.close()
            return
        _unverify_clears_warnings(data, fields, values)
        values.append(record_id)
        cursor.execute(f'UPDATE loading_order_records SET {", ".join(fields)} WHERE id = ?', values)
        conn.commit()
        conn.close()

    @staticmethod
    def get_verified_warnings(record_id: int) -> dict:
        """读某条明细的 verified_warnings(JSON) → dict。"""
        return _verified_warnings_get('loading_order_records', record_id)

    @staticmethod
    def set_verified_warning(record_id: int, rule_id: str, verified: bool) -> dict:
        """单条规则核查切换,返回更新后的完整 dict。语义同出货。"""
        return _verified_warnings_set('loading_order_records', record_id, rule_id, verified)

    @staticmethod
    def delete(record_id: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM loading_order_records WHERE id = ?', (record_id,))
        conn.commit()
        conn.close()

    @staticmethod
    def move_up(record_id: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT order_pk, sort_order FROM loading_order_records WHERE id = ?', (record_id,))
        cur = cursor.fetchone()
        if not cur:
            conn.close()
            return False
        order_pk, cur_sort = cur['order_pk'], cur['sort_order']
        cursor.execute(
            'SELECT id, sort_order FROM loading_order_records WHERE order_pk = ? AND sort_order < ? ORDER BY sort_order DESC LIMIT 1',
            (order_pk, cur_sort)
        )
        prev = cursor.fetchone()
        if not prev:
            conn.close()
            return False
        cursor.execute('UPDATE loading_order_records SET sort_order = -1 WHERE id = ?', (record_id,))
        cursor.execute('UPDATE loading_order_records SET sort_order = ? WHERE id = ?', (cur_sort, prev['id']))
        cursor.execute('UPDATE loading_order_records SET sort_order = ? WHERE id = ?', (prev['sort_order'], record_id))
        conn.commit()
        conn.close()
        return True

    @staticmethod
    def move_down(record_id: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT order_pk, sort_order FROM loading_order_records WHERE id = ?', (record_id,))
        cur = cursor.fetchone()
        if not cur:
            conn.close()
            return False
        order_pk, cur_sort = cur['order_pk'], cur['sort_order']
        cursor.execute(
            'SELECT id, sort_order FROM loading_order_records WHERE order_pk = ? AND sort_order > ? ORDER BY sort_order ASC LIMIT 1',
            (order_pk, cur_sort)
        )
        nxt = cursor.fetchone()
        if not nxt:
            conn.close()
            return False
        cursor.execute('UPDATE loading_order_records SET sort_order = -1 WHERE id = ?', (record_id,))
        cursor.execute('UPDATE loading_order_records SET sort_order = ? WHERE id = ?', (cur_sort, nxt['id']))
        cursor.execute('UPDATE loading_order_records SET sort_order = ? WHERE id = ?', (nxt['sort_order'], record_id))
        conn.commit()
        conn.close()
        return True

    @staticmethod
    def get_grouped(start_date=None, end_date=None):
        """按订单分组返回数据（包括空订单）"""
        conn = get_db()
        cursor = conn.cursor()
        # 先查询所有订单（日期范围内），再左连接明细
        if start_date and end_date:
            cursor.execute('''
                SELECT o.id as order_pk, o.date, o.customer, o.order_num, o.is_locked, o.order_note, o.doc_number,
                       o.img_cols as img_cols,
                       r.id as r_id, r.product_name, r.specification, r.quantity, r.unit, r.remark, r.created_at as r_created_at
                FROM loading_orders o
                LEFT JOIN loading_order_records r ON o.id = r.order_pk
                WHERE o.date >= ? AND o.date <= ?
                ORDER BY o.created_at DESC, r.sort_order, r.id
            ''', (start_date, end_date))
        else:
            cursor.execute('''
                SELECT o.id as order_pk, o.date, o.customer, o.order_num, o.is_locked, o.order_note, o.doc_number,
                       o.img_cols as img_cols,
                       r.id as r_id, r.product_name, r.specification, r.quantity, r.unit, r.remark, r.created_at as r_created_at
                FROM loading_orders o
                LEFT JOIN loading_order_records r ON o.id = r.order_pk
                ORDER BY o.created_at DESC, r.sort_order, r.id
            ''')
        rows = cursor.fetchall()
        conn.close()

        groups = {}
        for row in rows:
            row_dict = dict(row)
            key = (row_dict['date'], row_dict['customer'], row_dict['order_num'])
            if key not in groups:
                groups[key] = {
                    'order_pk': row_dict['order_pk'],
                    'date': row_dict['date'],
                    'customer': row_dict['customer'],
                    'order_num': row_dict['order_num'],
                    'is_locked': row_dict['is_locked'],
                    'order_note': row_dict.get('order_note', ''),
                    'doc_number': row_dict.get('doc_number', ''),
                    'img_cols': row_dict.get('img_cols') or 3,
                    'records': []
                }
            # 只添加有明细的记录
            if row_dict['r_id'] is not None:
                groups[key]['records'].append({
                    'id': row_dict['r_id'],
                    'product_name': row_dict['product_name'],
                    'specification': row_dict['specification'],
                    'quantity': row_dict['quantity'],
                    'unit': row_dict['unit'],
                    'remark': row_dict['remark'],
                    'created_at': row_dict['r_created_at']
                })
        
        # 按创建时间倒序返回
        return sorted(groups.values(), key=lambda x: x['order_pk'], reverse=True)


class LoadingOrderImage:
    """装柜订单图片"""
    @staticmethod
    def create(order_pk: int, file_path: str, original_name: str = '', source: str = 'upload', record_pk: int = None, sort_order: int = None):
        """插入图片。
        - record_pk: None=订单级共享图, 非空=某条明细的专属图
        - sort_order: None 时由本方法在事务内计算 max+1; 显式传入则按用户值
        """
        conn = get_db()
        cursor = conn.cursor()
        if sort_order is None:
            cursor.execute(
                'SELECT COALESCE(MAX(sort_order), -1) + 1 AS next FROM loading_order_images WHERE order_pk = ?',
                (order_pk,)
            )
            sort_order = cursor.fetchone()['next']
        cursor.execute(
            'INSERT INTO loading_order_images (order_pk, file_path, original_name, source, record_pk, sort_order, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?)',
            (order_pk, file_path, original_name, source, record_pk, sort_order,
             datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        )
        conn.commit()
        image_id = cursor.lastrowid
        conn.close()
        return image_id

    @staticmethod
    def get_by_order(order_pk: int):
        """查订单所有图(含订单级 + 记录级),按 sort_order + id 排"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT * FROM loading_order_images WHERE order_pk = ? ORDER BY sort_order ASC, id ASC',
            (order_pk,)
        )
        rows = cursor.fetchall()
        conn.close()
        result = []
        for row in rows:
            item = dict(row)
            item['relative_path'] = LoadingOrderImage.get_relative_path(item['file_path'])
            result.append(item)
        return result

    @staticmethod
    def get_by_record(record_pk: int):
        """查某条明细专属图片(订单级共享图不算),按 sort_order + id 排"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT * FROM loading_order_images WHERE record_pk = ? ORDER BY sort_order ASC, id ASC',
            (record_pk,)
        )
        rows = cursor.fetchall()
        conn.close()
        result = []
        for row in rows:
            item = dict(row)
            item['relative_path'] = LoadingOrderImage.get_relative_path(item['file_path'])
            result.append(item)
        return result

    @staticmethod
    def get_combined_for_record(order_pk: int, record_pk: int):
        """合并视图:某 record 专属图 + 订单共享图,按 sort_order + id 统一排"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            '''SELECT * FROM loading_order_images WHERE order_pk = ? AND record_pk IS NULL
               UNION ALL
               SELECT * FROM loading_order_images WHERE order_pk = ? AND record_pk = ?
               ORDER BY sort_order ASC, id ASC''',
            (order_pk, order_pk, record_pk)
        )
        rows = cursor.fetchall()
        conn.close()
        result = []
        for row in rows:
            item = dict(row)
            item['relative_path'] = LoadingOrderImage.get_relative_path(item['file_path'])
            result.append(item)
        return result

    @staticmethod
    def get_relative_path(file_path: str) -> str:
        if 'upload\\' in file_path:
            path = file_path.split('upload\\')[-1]
        elif 'upload/' in file_path:
            path = file_path.split('upload/')[-1]
        else:
            path = file_path
        return path.replace('\\', '/')

    @staticmethod
    def get_by_id(image_id: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM loading_order_images WHERE id = ?', (image_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    @staticmethod
    def delete(image_id: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT file_path FROM loading_order_images WHERE id = ?', (image_id,))
        row = cursor.fetchone()
        if row:
            file_path = row['file_path']
            cursor.execute('DELETE FROM loading_order_images WHERE id = ?', (image_id,))
            conn.commit()
            conn.close()
            # 删除文件（走回收站 + 路径白名单）
            _safe_remove_file(file_path)
            return True
        conn.close()
        return False

    @staticmethod
    def get_all_by_orders(order_ids=None):
        """获取图片,按 order_pk 分组返回字典。

        Args:
            order_ids: 可选的订单 ID 列表(过滤范围),None 表示取全部图片。
                       页面通常只显示某个日期范围的订单,传入该范围的 order_id 列表
                       避免无谓加载历史图片元数据。
        """
        conn = get_db()
        cursor = conn.cursor()
        if order_ids:
            if not order_ids:  # 空列表 → 直接返回空
                conn.close()
                return {}
            placeholders = ','.join('?' * len(order_ids))
            cursor.execute(
                f'SELECT * FROM loading_order_images WHERE order_pk IN ({placeholders}) ORDER BY id ASC',
                list(order_ids)
            )
        else:
            cursor.execute('SELECT * FROM loading_order_images ORDER BY id ASC')
        rows = cursor.fetchall()
        conn.close()
        result = {}
        for row in rows:
            item = dict(row)
            item['relative_path'] = LoadingOrderImage.get_relative_path(item['file_path'])
            order_pk = item['order_pk']
            if order_pk not in result:
                result[order_pk] = []
            result[order_pk].append(item)
        return result

    @staticmethod
    def set_match(image_id: int, status: str, score: float, reason: str = '',
               source: str = None):
        """写入标签匹配结果（match_status/match_score/reason）。

        Args:
            image_id: 图片 id
            status:   'green' | 'yellow' | 'red' | '' (空 = 清空,不打徽章)
            score:    0~100 置信度
            reason:   AI 中文判定依据(给前端 hover 提示用)
            source:   'local_fuzzy' (默认,本地 RapidFuzz) 或 'deepseek' (云端)
                      写入 loading_order_images.match_source,前端据此用不同图标
        """
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'UPDATE loading_order_images SET match_status = ?, match_score = ?, '
            'reason = ?, match_source = ? WHERE id = ?',
            (status, score, reason or None,
             source or 'local_fuzzy',  # 默认 local_fuzzy
             image_id)
        )
        conn.commit()
        conn.close()

    @staticmethod
    def set_human_verified(image_id: int, verified: bool = True):
        """人工覆盖 AI 比对结果(目前仅用于红牌的"确认通过")。

        写入后前端应把红牌徽章视为已确认(可隐藏确认按钮,或展示"已确认"标记)。
        """
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'UPDATE loading_order_images SET human_verified = ? WHERE id = ?',
            (1 if verified else 0, image_id)
        )
        conn.commit()
        conn.close()

    @staticmethod
    def set_bg_color(image_id: int, bg: str):
        """写入自动识别的图片背景色('black'|'white'),供前端展示"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('UPDATE loading_order_images SET bg_color = ? WHERE id = ?', (bg, image_id))
        conn.commit()
        conn.close()


class UnifiedSearch:
    """综合查找：跨出货/入库/装柜三个订单类型的明细搜索。"""

    @staticmethod
    def _build_where(table_alias, start_date, end_date, customer, product_name, specification):
        """为单个订单类型构建 WHERE 子句和参数列表。"""
        conditions = []
        params = []
        if start_date:
            conditions.append(f"{table_alias}.date >= ?")
            params.append(start_date)
        if end_date:
            conditions.append(f"{table_alias}.date <= ?")
            params.append(end_date)
        if customer:
            # 入库表用 supplier，出货/装柜用 customer
            col = 'supplier' if table_alias == 'io' else 'customer'
            conditions.append(f"{table_alias}.{col} LIKE ?")
            params.append(f"%{customer}%")
        if product_name:
            conditions.append("r.product_name LIKE ?")
            params.append(f"%{product_name}%")
        if specification:
            # 同时模糊匹配明细表 specification 和 product 表 model（型号）字段
            conditions.append("(r.specification LIKE ? OR p.model LIKE ?)")
            params.append(f"%{specification}%")
            params.append(f"%{specification}%")
        where_sql = "WHERE " + " AND ".join(conditions) if conditions else ""
        return where_sql, params

    @staticmethod
    def search(scope, start_date=None, end_date=None, customer=None, product_name=None, specification=None):
        """
        跨表搜索订单明细。

        Args:
            scope: list[str] — 包含 'shipping' / 'inbound' / 'loading' 中的一个或多个
            start_date, end_date: str 'YYYY-MM-DD'
            customer, product_name, specification: str — 支持 % 模糊匹配

        Returns:
            list[dict] — 每条记录包含统一字段：type, order_id, date, customer, order_num,
                         is_locked, record_id, product_name, specification, quantity, unit, remark, sort_order
        """
        conn = get_db()
        cursor = conn.cursor()
        results = []

        # ── 出货 ──
        if 'shipping' in scope:
            where_sql, params = UnifiedSearch._build_where(
                'so', start_date, end_date, customer, product_name, specification
            )
            cursor.execute(f'''
                SELECT 'shipping' as type, so.id as order_id, so.date, so.customer, so.order_num, so.is_locked,
                       r.id as record_id, r.product_name, r.specification, r.quantity, r.unit, r.remark, r.sort_order
                FROM shipping_records r
                JOIN shipping_orders so ON r.order_pk = so.id
                LEFT JOIN product p ON r.product_name = p.product_name AND r.specification = p.specification
                {where_sql}
                ORDER BY so.date DESC, so.customer ASC, so.order_num ASC, r.sort_order
            ''', params)
            results.extend([dict(row) for row in cursor.fetchall()])

        # ── 入库 ──
        if 'inbound' in scope:
            where_sql, params = UnifiedSearch._build_where(
                'io', start_date, end_date, customer, product_name, specification
            )
            cursor.execute(f'''
                SELECT 'inbound' as type, io.id as order_id, io.date, io.supplier as customer, io.order_num, io.is_locked,
                       r.id as record_id, r.product_name, r.specification, r.quantity, r.unit, r.remark, r.sort_order
                FROM inbound_records r
                JOIN inbound_orders io ON r.order_pk = io.id
                LEFT JOIN product p ON r.product_name = p.product_name AND r.specification = p.specification
                {where_sql}
                ORDER BY io.date DESC, io.supplier ASC, io.order_num ASC, r.sort_order
            ''', params)
            results.extend([dict(row) for row in cursor.fetchall()])

        # ── 装柜 ──
        if 'loading' in scope:
            where_sql, params = UnifiedSearch._build_where(
                'lo', start_date, end_date, customer, product_name, specification
            )
            cursor.execute(f'''
                SELECT 'loading' as type, lo.id as order_id, lo.date, lo.customer, lo.order_num, lo.is_locked,
                       r.id as record_id, r.product_name, r.specification, r.quantity, r.unit, r.remark, r.sort_order
                FROM loading_order_records r
                JOIN loading_orders lo ON r.order_pk = lo.id
                LEFT JOIN product p ON r.product_name = p.product_name AND r.specification = p.specification
                {where_sql}
                ORDER BY lo.date DESC, lo.customer ASC, lo.order_num ASC, r.sort_order
            ''', params)
            results.extend([dict(row) for row in cursor.fetchall()])

        conn.close()

        # 全局排序：日期倒序 → 类型 → 客户 → 订单号 → 明细排序
        results.sort(
            key=lambda x: (x['date'], x['type'], x['customer'], x['order_num'], x['sort_order']),
            reverse=True
        )
        return results


class CopyPaperImage:
    """拷贝纸/日本纸 行级图片 + 人工录入张数。

    与 ShippingImage / PlacementImage 隔离:
    - 不进 OCR pipeline
    - 不进 match-col / 整体图区
    - 仅用于人工参考 + 行级 total 比对
    """

    @staticmethod
    def create(record_pk: int, file_path: str, original_name: str, source: str):
        if source not in ('label', 'count'):
            raise ValueError(f"source must be 'label' or 'count', got {source!r}")
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO copy_paper_images (record_pk, file_path, original_name, source) "
            "VALUES (?, ?, ?, ?)",
            (record_pk, file_path, original_name or '', source))
        new_id = cur.lastrowid
        conn.commit()
        conn.close()
        return new_id

    @staticmethod
    def list_by_record(record_pk: int):
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM copy_paper_images WHERE record_pk = ? "
            "ORDER BY source ASC, created_at ASC, id ASC",
            (record_pk,))
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows

    @staticmethod
    def get_by_id(image_id: int):
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM copy_paper_images WHERE id = ?", (image_id,))
        row = cur.fetchone()
        conn.close()
        return dict(row) if row else None

    @staticmethod
    def update_count(image_id: int, sheet_count):
        if sheet_count is not None:
            try:
                sheet_count = int(sheet_count)
            except (TypeError, ValueError):
                raise ValueError(f"sheet_count must be int or None, got {sheet_count!r}")
            if sheet_count < 0:
                raise ValueError(f"sheet_count must be >= 0, got {sheet_count}")
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "UPDATE copy_paper_images SET sheet_count = ? WHERE id = ?",
            (sheet_count, image_id))
        changed = cur.rowcount > 0
        conn.commit()
        conn.close()
        return changed

    @staticmethod
    def delete(image_id: int) -> bool:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM copy_paper_images WHERE id = ?", (image_id,))
        changed = cur.rowcount > 0
        conn.commit()
        conn.close()
        return changed


