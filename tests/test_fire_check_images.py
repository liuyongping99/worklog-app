"""消防检查记录图片 —— 上传 / 删除 / 级联清理 测试。

覆盖范围：
- POST /api/v1/fire-checks/<id>/images 未登录 → 401
- 记录不存在 → 404
- 一次上传多张 → 201，文件落盘 + DB 有行
- 伪装文件（文本改 .png 后缀）→ 400，且半成品文件被回滚清理
- GET /fire-checks 页面含图片 URL，且图片能被 /upload/ 取到
- DELETE 图片 → DB 行 + 物理文件一并清除
- 删除检查记录 → 其图片级联清除

⚠️ 上传目录隔离（重要）：
    生产 `upload/` 目录是**只增不删**的铁律区，测试绝不能往里写、更不能删。
    因此本测试把上传根目录（_helpers.BASE_DIR / _helpers.EBAK_UPLOAD_ROOT /
    models.fire_check.UPLOAD_ROOT / blueprints.upload.BASE_DIR）全部指向项目内的
    `tests/.tmp_fc_img_upload/`（静态可见的固定路径，不在生产 upload/ 下）。
    跑完把该目录里的测试产物逐个删掉，生产 upload/ 一个字节都不动。

    额外保险：删除物理文件走 `models.orders._safe_remove_file(path, upload_root)`，
    白名单按 `UPLOAD_ROOT` 判定 —— 补丁后即使代码有 bug 指向生产目录，也会被白名单挡下。
"""
import io
import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# 测试专用上传根（项目内固定路径，绝不是生产 upload/）
TMP_ROOT = os.path.join(PROJECT_ROOT, 'tests', '.tmp_fc_img_upload')
TMP_UPLOAD = os.path.join(TMP_ROOT, 'upload')
TMP_EBAK = os.path.join(TMP_ROOT, 'ebak')

from app import create_app
import blueprints._helpers as helpers
import blueprints.upload as upload_bp
import models.fire_check as fc_model
from models.fire_check import FireCheck, FireCheckImage
from models.tasks_flow import StaffDB
from models._db import get_db

from tests import fake_png_bytes as _MINIMAL_PNG  # Pillow 真生成，过 magic + verify 校验


def _png_part(name='test.png'):
    return (io.BytesIO(_MINIMAL_PNG()), name)


class FireCheckImagesTest(unittest.TestCase):
    def setUp(self):
        # ── 上传目录隔离：全部指向项目内 tests/.tmp_fc_img_upload/ ──
        os.makedirs(TMP_UPLOAD, exist_ok=True)
        self._orig = {
            'helpers_base': helpers.BASE_DIR,
            'ebak': helpers.EBAK_UPLOAD_ROOT,
            'model_root': fc_model.UPLOAD_ROOT,
            'upload_bp_base': upload_bp.BASE_DIR,
        }
        helpers.BASE_DIR = TMP_ROOT
        helpers.EBAK_UPLOAD_ROOT = TMP_EBAK
        fc_model.UPLOAD_ROOT = TMP_UPLOAD
        upload_bp.BASE_DIR = TMP_ROOT

        self.app = create_app()
        self.client = self.app.test_client()
        self.op = StaffDB.create(name='fc-img-tester', role='文员', is_active=1)
        self._login(self.op)
        self.rid = FireCheck.create(
            '2026-11-08', '灭火器压力正常', '成品仓', '应急灯不亮', '仅测试'
        )
        self.upload_dir, self.month = helpers.get_upload_dir()

    def tearDown(self):
        # 级联删图 + 删记录（模型层负责文件回收）
        FireCheck.delete(self.rid)
        db = get_db()
        db.execute('DELETE FROM fire_check_images')
        db.execute('DELETE FROM fire_checks')
        db.execute('DELETE FROM staff WHERE id = ?', (self.op['id'],))
        db.commit()
        db.close()
        # 还原上传目录指向
        helpers.BASE_DIR = self._orig['helpers_base']
        helpers.EBAK_UPLOAD_ROOT = self._orig['ebak']
        fc_model.UPLOAD_ROOT = self._orig['model_root']
        upload_bp.BASE_DIR = self._orig['upload_bp_base']
        # 清掉测试自己的产物：逐个删 tests/.tmp_fc_img_upload/ 下的文件，再删空目录
        self._purge_tmp_tree()

    @staticmethod
    def _purge_tmp_tree():
        """清理 tests/.tmp_fc_img_upload/ 下的测试产物（逐文件删，不递归删目录树）。"""
        for dirpath, _, filenames in os.walk(TMP_ROOT, topdown=False):
            for name in filenames:
                try:
                    os.remove(os.path.join(dirpath, name))
                except OSError:
                    pass
            try:
                os.rmdir(dirpath)
            except OSError:
                pass

    def _login(self, who):
        with self.client.session_transaction() as sess:
            sess['operator_id'] = who['id']

    def _logout(self):
        with self.client.session_transaction() as sess:
            sess.pop('operator_id', None)

    def _upload(self, files, record_id=None):
        rid = self.rid if record_id is None else record_id
        return self.client.post(
            f'/api/v1/fire-checks/{rid}/images',
            data={'image': files},
            content_type='multipart/form-data',
        )

    # ── 上传 ────────────────────────────────────────────────
    def test_upload_requires_login(self):
        """未登录 → 401"""
        self._logout()
        r = self._upload([_png_part()])
        self.assertEqual(r.status_code, 401, r.get_data(as_text=True))
        self.assertFalse(r.get_json()['success'])

    def test_upload_to_nonexistent_record_returns_404(self):
        """记录不存在 → 404"""
        r = self._upload([_png_part()], record_id=999999)
        self.assertEqual(r.status_code, 404, r.get_data(as_text=True))
        self.assertFalse(r.get_json()['success'])

    def test_upload_without_file_returns_400(self):
        """没选文件 → 400"""
        r = self.client.post(
            f'/api/v1/fire-checks/{self.rid}/images',
            data={},
            content_type='multipart/form-data',
        )
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))

    def test_upload_multiple_images(self):
        """一次上传多张 → 201，DB 有 2 行，文件落盘"""
        r = self._upload([_png_part('a.png'), _png_part('b.png')])
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        data = r.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(data['count'], 2)
        self.assertEqual(len(data['images']), 2)

        rows = FireCheckImage.get_by_record(self.rid)
        self.assertEqual(len(rows), 2)

        # DB 里存的是相对路径（month_dir + file_name），不是绝对盘符路径
        for row in rows:
            self.assertEqual(row['month_dir'], self.month)
            self.assertFalse(os.path.isabs(row['file_name']))
        # 返回的 URL 与 DB 行一一对应
        urls = {img['url'] for img in data['images']}
        self.assertEqual(urls, {f"/upload/{r['month_dir']}/{r['file_name']}" for r in rows})

        for row in rows:
            full = os.path.join(fc_model.UPLOAD_ROOT, row['month_dir'], row['file_name'])
            self.assertTrue(os.path.exists(full), f'上传后文件应存在: {full}')

    def test_upload_rejects_fake_image_and_rolls_back(self):
        """伪装文件（文本改 .png 后缀）→ 400，且不留下半成品文件"""
        before = set(os.listdir(self.upload_dir))
        r = self._upload([(io.BytesIO(b'not an image at all'), 'fake.png')])
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertFalse(r.get_json()['success'])
        # DB 没建行
        self.assertEqual(len(FireCheckImage.get_by_record(self.rid)), 0)
        # 落盘的半成品被回滚清理
        after = set(os.listdir(self.upload_dir))
        self.assertEqual(before, after, '校验失败的临时文件应被清理')

    def test_upload_rejects_non_image_extension(self):
        """非图片扩展名 → 400"""
        r = self._upload([(io.BytesIO(b'hello'), 'note.txt')])
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertEqual(len(FireCheckImage.get_by_record(self.rid)), 0)

    # ── 页面渲染 ────────────────────────────────────────────
    def test_page_renders_image_urls(self):
        """页面 HTML 里含上传后的图片 URL，且图片能被 /upload/ 取到"""
        r = self._upload([_png_part('c.png')])
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        url = r.get_json()['images'][0]['url']

        page = self.client.get('/fire-checks')
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn(url, html)
        self.assertIn('fc-img-add', html)      # 「＋添加图片」磁贴
        self.assertIn('fcOpenUpload', html)    # 打开添加图片弹框
        self.assertIn('fcPasteZone', html)     # 粘贴区
        self.assertIn('fcHandlePaste', html)   # 粘贴处理
        self.assertIn('showImgPreview', html)  # 点缩略图放大
        self.assertIn('fc-img-del', html)      # 删除按钮

        # 静态图片路由能取到（证明 URL 拼接与存储路径一致）
        got = self.client.get(url)
        self.assertEqual(got.status_code, 200, f'图片应能通过 {url} 访问')

    # ── 删除 ────────────────────────────────────────────────
    def test_delete_image_requires_login(self):
        """未登录 → 401"""
        r = self._upload([_png_part()])
        image_id = r.get_json()['images'][0]['id']
        self._logout()
        r = self.client.delete(f'/api/v1/fire-checks/images/{image_id}')
        self.assertEqual(r.status_code, 401, r.get_data(as_text=True))

    def test_delete_nonexistent_image_returns_404(self):
        """删不存在的图 → 404"""
        r = self.client.delete('/api/v1/fire-checks/images/999999')
        self.assertEqual(r.status_code, 404, r.get_data(as_text=True))

    def test_delete_image_removes_row_and_file(self):
        """删图 → DB 行 + 物理文件一并清除（且不影响同记录的其它图）"""
        r = self._upload([_png_part('d.png'), _png_part('d2.png')])
        imgs = r.get_json()['images']
        self.assertEqual(len(imgs), 2)
        victim, sibling = imgs[0], imgs[1]

        row = FireCheckImage.get_by_id(victim['id'])
        assert row is not None
        full = os.path.join(fc_model.UPLOAD_ROOT, row['month_dir'], row['file_name'])
        sibling_row = FireCheckImage.get_by_id(sibling['id'])
        assert sibling_row is not None
        sibling_full = os.path.join(
            fc_model.UPLOAD_ROOT, sibling_row['month_dir'], sibling_row['file_name'])

        self.assertTrue(os.path.exists(full))
        self.assertTrue(os.path.exists(sibling_full))

        r = self.client.delete(f"/api/v1/fire-checks/images/{victim['id']}")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(r.get_json()['success'])
        self.assertIsNone(FireCheckImage.get_by_id(victim['id']))
        self.assertFalse(os.path.exists(full), '删图后物理文件应被清除')
        # 只删被指定的那一张，同记录的其它图不受影响
        self.assertIsNotNone(FireCheckImage.get_by_id(sibling['id']))
        self.assertTrue(os.path.exists(sibling_full), '同记录的其它图不应被牵连删除')

    # ── 级联：删记录 → 图片跟着清 ────────────────────────────
    def test_delete_record_cascades_images(self):
        """删除检查记录 → 其图片（DB 行 + 物理文件）一并清除"""
        r = self._upload([_png_part('e.png'), _png_part('e2.png')])
        imgs = r.get_json()['images']
        paths = []
        for img in imgs:
            row = FireCheckImage.get_by_id(img['id'])
            assert row is not None
            paths.append(os.path.join(fc_model.UPLOAD_ROOT, row['month_dir'], row['file_name']))
        for p in paths:
            self.assertTrue(os.path.exists(p))

        FireCheck.delete(self.rid)

        self.assertEqual(len(FireCheckImage.get_by_record(self.rid)), 0)
        for p in paths:
            self.assertFalse(os.path.exists(p), '删记录后物理文件应被清除')

    # ── 改记录：不动图片 ────────────────────────────────────
    def test_edit_keeps_images_intact(self):
        """改记录不应动它的图片"""
        r = self._upload([_png_part('keep.png')])
        image_id = r.get_json()['images'][0]['id']
        self.client.post(f'/fire-checks/edit/{self.rid}', data={
            'date': '2026-11-08', 'location': '车间', 'check_items': '改了检查事项',
            'outstanding_items': '应急灯不亮', 'remark': '', 'status': '',
        })
        self.assertIsNotNone(FireCheckImage.get_by_id(image_id))
        self.assertEqual(len(FireCheckImage.get_by_record(self.rid)), 1)


if __name__ == '__main__':
    unittest.main()
