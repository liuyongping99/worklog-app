"""行级图上传回调 + 删除回调的 JS 行为测试（jsdom 驱动）。

被测对象:`templates/_record_image_script.html` 里的两个函数:
- `recordImageUploaded` — 行级图上传成功后,把商品名/规格叠加到图上
- `bindDeleteImageButtons` — 删图成功后,行级图删完时把 🖼️ 按钮的 has-image 红框去掉

历史回归:
- recordImageUploaded 早期用 tds[1]/tds[2] 位置下标,跟 eco-col 错位,商品名显示成空、
  规格显示成商品名
- 删完所有行级图后 🖼️ 按钮的 .has-image 没清,看起来还在「已上传」状态

跑法: 用 Node + jsdom 抽真实函数 + 跑 DOM 行为。Node 不可用时 skip。
"""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


class RecordImageOverlayJsTests(unittest.TestCase):
    @unittest.skipUnless(
        Path('tests', 'record_image_overlay_check.js').exists()
        and shutil.which('node') is not None,
        '需要 node + tests/record_image_overlay_check.js 才能跑',
    )
    def test_upload_reads_product_and_spec_from_class_cells(self):
        """行级图上传后,图上叠加的商品名/规格应来自 .product-name-cell/.spec-cell,
        不是 td 位置下标(后者会跟 eco-col 错位)。"""
        proc = subprocess.run(
            ['node', 'tests/record_image_overlay_check.js'],
            capture_output=True, cwd=str(REPO_ROOT),
        )
        if proc.returncode != 0:
            self.fail(
                f"record_image_overlay_check.js 退出码 {proc.returncode}\n"
                f"STDOUT: {proc.stdout.decode('utf-8', errors='replace')}\n"
                f"STDERR: {proc.stderr.decode('utf-8', errors='replace')}"
            )
        out = proc.stdout.decode('utf-8', errors='replace')
        self.assertIn('✅ record_image_overlay_check 通过', out,
                      f'检查脚本未显示通过标记,实际输出:\n{out}')


if __name__ == '__main__':
    unittest.main()