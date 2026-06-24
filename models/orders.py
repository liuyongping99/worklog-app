"""订单：出货 / 入库 / 装柜（三表 × 3 业务领域 = 9 个模型）"""
import os
from datetime import datetime
from ._db import get_db
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
        """更新单条明细。只更新 data 里传入的字段,缺省字段保持原值。"""
        conn = get_db()
        cursor = conn.cursor()
        fields = []
        values = []
        for key in ['product_name', 'specification', 'quantity', 'unit', 'remark']:
            if key in data:
                fields.append(f'{key} = ?')
                values.append(data[key])
        if not fields:
            conn.close()
            return
        values.append(record_id)
        cursor.execute(f'UPDATE shipping_records SET {", ".join(fields)} WHERE id = ?', values)
        conn.commit()
        conn.close()

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
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT order_pk, sort_order FROM shipping_records WHERE id = ?', (record_id,))
        cur = cursor.fetchone()
        if not cur:
            conn.close()
            return False
        order_pk, cur_sort = cur['order_pk'], cur['sort_order']
        cursor.execute(
            'SELECT id, sort_order FROM shipping_records WHERE order_pk = ? AND sort_order < ? ORDER BY sort_order DESC LIMIT 1',
            (order_pk, cur_sort)
        )
        prev = cursor.fetchone()
        if not prev:
            conn.close()
            return False
        cursor.execute('UPDATE shipping_records SET sort_order = -1 WHERE id = ?', (record_id,))
        cursor.execute('UPDATE shipping_records SET sort_order = ? WHERE id = ?', (cur_sort, prev['id']))
        cursor.execute('UPDATE shipping_records SET sort_order = ? WHERE id = ?', (prev['sort_order'], record_id))
        conn.commit()
        conn.close()
        return True

    @staticmethod
    def move_down(record_id: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT order_pk, sort_order FROM shipping_records WHERE id = ?', (record_id,))
        cur = cursor.fetchone()
        if not cur:
            conn.close()
            return False
        order_pk, cur_sort = cur['order_pk'], cur['sort_order']
        cursor.execute(
            'SELECT id, sort_order FROM shipping_records WHERE order_pk = ? AND sort_order > ? ORDER BY sort_order ASC LIMIT 1',
            (order_pk, cur_sort)
        )
        nxt = cursor.fetchone()
        if not nxt:
            conn.close()
            return False
        cursor.execute('UPDATE shipping_records SET sort_order = -1 WHERE id = ?', (record_id,))
        cursor.execute('UPDATE shipping_records SET sort_order = ? WHERE id = ?', (cur_sort, nxt['id']))
        cursor.execute('UPDATE shipping_records SET sort_order = ? WHERE id = ?', (nxt['sort_order'], record_id))
        conn.commit()
        conn.close()
        return True


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
    def set_note(order_id: int, note: str):
        """设置入库订单级备注（显示在商品信息行上方）"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('UPDATE inbound_orders SET order_note = ? WHERE id = ?', (note or '', order_id))
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
        conn = get_db()
        cursor = conn.cursor()
        fields = []
        values = []
        for key in ['product_name', 'specification', 'quantity', 'unit', 'remark']:
            if key in data:
                fields.append(f'{key} = ?')
                values.append(data[key])
        if not fields:
            conn.close()
            return
        values.append(record_id)
        cursor.execute(f'UPDATE inbound_records SET {", ".join(fields)} WHERE id = ?', values)
        conn.commit()
        conn.close()

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
    def create(order_pk: int, file_path: str, original_name: str = ''):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO inbound_images (order_pk, file_path, original_name, created_at) VALUES (?, ?, ?, ?)',
            (order_pk, file_path, original_name, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        )
        conn.commit()
        image_id = cursor.lastrowid
        conn.close()
        return image_id

    @staticmethod
    def get_by_order(order_pk: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM inbound_images WHERE order_pk = ? ORDER BY id ASC', (order_pk,))
        rows = cursor.fetchall()
        conn.close()
        result = []
        for row in rows:
            item = dict(row)
            item['relative_path'] = InboundImage.get_relative_path(item['file_path'])
            result.append(item)
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
            # 删除文件
            if os.path.exists(file_path):
                os.remove(file_path)
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
        # 删除文件
        for file_path in file_paths:
            if os.path.exists(file_path):
                os.remove(file_path)



class ShippingImage:
    @staticmethod
    def create(order_pk: int, file_path: str, original_name: str = ''):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO shipping_images (order_pk, file_path, original_name, created_at) VALUES (?, ?, ?, ?)',
            (order_pk, file_path, original_name, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        )
        conn.commit()
        image_id = cursor.lastrowid
        conn.close()
        return image_id

    @staticmethod
    def get_by_order(order_pk: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM shipping_images WHERE order_pk = ? ORDER BY id ASC', (order_pk,))
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
            if os.path.exists(file_path):
                os.remove(file_path)
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
            if os.path.exists(file_path):
                os.remove(file_path)


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
        """更新单条明细。只更新 data 里传入的字段,缺省字段保持原值。"""
        conn = get_db()
        cursor = conn.cursor()
        fields = []
        values = []
        for key in ['product_name', 'specification', 'quantity', 'unit', 'remark']:
            if key in data:
                fields.append(f'{key} = ?')
                values.append(data[key])
        if not fields:
            conn.close()
            return
        values.append(record_id)
        cursor.execute(f'UPDATE loading_order_records SET {", ".join(fields)} WHERE id = ?', values)
        conn.commit()
        conn.close()

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
                SELECT o.id as order_pk, o.date, o.customer, o.order_num, o.is_locked, o.order_note,
                       r.id as r_id, r.product_name, r.specification, r.quantity, r.unit, r.remark, r.created_at as r_created_at
                FROM loading_orders o
                LEFT JOIN loading_order_records r ON o.id = r.order_pk
                WHERE o.date >= ? AND o.date <= ?
                ORDER BY o.created_at DESC, r.sort_order, r.id
            ''', (start_date, end_date))
        else:
            cursor.execute('''
                SELECT o.id as order_pk, o.date, o.customer, o.order_num, o.is_locked, o.order_note,
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
    def create(order_pk: int, file_path: str, original_name: str = ''):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO loading_order_images (order_pk, file_path, original_name, created_at) VALUES (?, ?, ?, ?)',
            (order_pk, file_path, original_name, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        )
        conn.commit()
        image_id = cursor.lastrowid
        conn.close()
        return image_id

    @staticmethod
    def get_by_order(order_pk: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM loading_order_images WHERE order_pk = ? ORDER BY id ASC', (order_pk,))
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
            if os.path.exists(file_path):
                os.remove(file_path)
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


