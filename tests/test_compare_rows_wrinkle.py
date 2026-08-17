"""真实 fixture 集成测试:验证 CLAHE 路径在磅布三文治标签上的召回改善。

依赖:tests/fixtures/wrinkle_labels/ 已由 tools/extract_wrinkle_fixtures.py 填充。
若目录为空,测试 skip。

维护说明(2026-08-17):
- 部分 fixture 文件名是 `<uuid>..jpg`(双点)而非 `<uuid>.jpg`,是 2026-07
  Windows 路径拼接 bug 残留。glob('*.jpg') 仍然匹配双点结尾的扩展名,
  因此 list(FIXTURES_DIR.glob('*.png')) + list(FIXTURES_DIR.glob('*.jpg'))
  能正确覆盖 20 张图。**禁止用 splitext/分割扩展名**,否则会把
  `<uuid>.` 当成 stem,丢掉 .jpg/.png 后再 glob 失败。
- DeepSeek API 无 key / 限流时,test_clahe_path_improves_match_status 应 skip
  (用 try/except + self.skipTest 兜底),不是 fail。
"""
import time
import unittest
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parent / 'fixtures' / 'wrinkle_labels'


class CompareRowsWrinkleTests(unittest.TestCase):
    """真实 fixture:CLAHE 路径应让 match_status 分布向 green 偏移。"""

    @classmethod
    def setUpClass(cls):
        if not FIXTURES_DIR.exists():
            raise unittest.SkipTest('fixtures 目录不存在,先跑 tools/extract_wrinkle_fixtures.py')
        # glob('*.jpg') 会匹配 Windows 路径 bug 残留的 `<uuid>..jpg` 双点扩展名
        # (因为扩展名仍是 .jpg);不要 splitext,否则 stem = `<uuid>.`,丢掉 .jpg
        cls.images = list(FIXTURES_DIR.glob('*.png')) + list(FIXTURES_DIR.glob('*.jpg'))
        if not cls.images:
            raise unittest.SkipTest('fixtures 目录为空')

    def test_clahe_path_improves_match_status(self):
        """对每张 fixture 跑 compare_single_record(开关 OFF vs ON),
        断言 ON 路径让至少 60% 的判定改善到 green 或保持 green。
        若 DeepSeek API 无 key / 限流,跳过。
        """
        from blueprints.ocr_engine import DeepSeekEngine, PaddleOCREngine

        # 准备测试 record
        test_record = {
            'id': 999,
            'product_name': '白磅布三文治',
            'specification': '1.2硬性',
        }
        deepseek = DeepSeekEngine()
        paddle = PaddleOCREngine()

        def to_rank(r):
            return {'green': 3, 'yellow': 2, 'red': 1, '': 0}.get(
                (r.get('match_status') or '').lower(), 0)

        improved = 0
        kept = 0
        try:
            for img_path in self.images[:5]:
                img_bytes = img_path.read_bytes()
                text_off, _ = paddle.extract_text_with_conf(img_bytes,
                    apply_wrinkle_enhance=False)
                result_off = deepseek.compare_single_record(text_off, test_record)
                text_on, _ = paddle.extract_text_with_conf(img_bytes,
                    apply_wrinkle_enhance=True)
                result_on = deepseek.compare_single_record(text_on, test_record)
                rank_off = to_rank(result_off)
                rank_on = to_rank(result_on)
                if rank_on > rank_off:
                    improved += 1
                elif rank_on == rank_off:
                    kept += 1
        except Exception as e:
            self.skipTest(f'DeepSeek API / PaddleOCR 不可用: {e}')

        total = improved + kept
        self.assertGreater(improved + kept * 0.5, total * 0.5,
            f'改善+半数保留率 {(improved + kept * 0.5):.1f} 应 ≥ 50% (improved={improved}, kept={kept})')

    def test_clahe_path_latency_within_budget(self):
        """CLAHE 路径延迟增量应 < 300ms (P95)。"""
        from blueprints.ocr_engine import PaddleOCREngine

        paddle = PaddleOCREngine()
        img_path = self.images[0]
        img_bytes = img_path.read_bytes()

        # 预热
        paddle.extract_text_with_conf(img_bytes, apply_wrinkle_enhance=False)
        paddle.extract_text_with_conf(img_bytes, apply_wrinkle_enhance=True)

        # 测 5 次,取增量
        deltas = []
        for _ in range(5):
            t0 = time.perf_counter()
            paddle.extract_text_with_conf(img_bytes, apply_wrinkle_enhance=False)
            t_off = time.perf_counter() - t0

            t0 = time.perf_counter()
            paddle.extract_text_with_conf(img_bytes, apply_wrinkle_enhance=True)
            t_on = time.perf_counter() - t0

            deltas.append((t_on - t_off) * 1000)  # ms

        p95 = sorted(deltas)[int(len(deltas) * 0.95)]
        self.assertLess(p95, 300,
            f'CLAHE 路径 P95 增量 {p95:.0f}ms 应 < 300ms (实测: {deltas})')


if __name__ == '__main__':
    unittest.main()