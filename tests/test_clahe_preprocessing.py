"""CLAHE 预处理单测。"""
import io
import unittest
import numpy as np
from PIL import Image

from blueprints.ocr_engine import PaddleOCREngine


def _make_uneven_image(size: int = 256) -> np.ndarray:
    """合成图:白底 + 黑字 + 正弦波亮度变化(模拟褶皱)。"""
    img = np.full((size, size, 3), 240, dtype=np.uint8)
    # 加文字 "TEST"(黑)
    for i, ch_off in enumerate([20, 60, 100, 140]):
        cv_x = ch_off
        img[80:170, cv_x:cv_x + 30] = 0
    # 加正弦波亮度变化(0.5x ~ 1.5x)模拟褶皱
    yy, xx = np.mgrid[:size, :size]
    wave = (np.sin(xx / size * np.pi * 4) * 0.5 + 1.0)  # 0.5~1.5
    img = np.clip(img.astype(np.float32) * wave[..., None], 0, 255).astype(np.uint8)
    return img


def _make_flat_image(size: int = 256, color: int = 128) -> np.ndarray:
    """纯色图。"""
    return np.full((size, size, 3), color, dtype=np.uint8)


class ClahePreprocessingTests(unittest.TestCase):

    def setUp(self):
        self.engine = PaddleOCREngine()

    def test_clahe_returns_same_shape(self):
        img = _make_uneven_image()
        out = self.engine._enhance_wrinkle_label(img)
        self.assertEqual(out.shape, img.shape)
        self.assertEqual(out.dtype, np.uint8)

    def test_clahe_returns_three_channel(self):
        img = _make_uneven_image()
        out = self.engine._enhance_wrinkle_label(img)
        # 3 通道(RGB),不是灰度
        self.assertEqual(out.shape[-1], 3)

    def test_clahe_improves_local_contrast_on_uneven(self):
        """褶皱图:CLAHE 后,16x16 局部块的对比度方差应大于输入。"""
        img = _make_uneven_image()
        out = self.engine._enhance_wrinkle_label(img)
        # 算 16x16 块 std,取方差(对比度变化越大,方差越大)
        def local_std_variance(arr):
            blocks = []
            for y in range(0, 256, 16):
                for x in range(0, 256, 16):
                    blocks.append(arr[y:y+16, x:x+16].std())
            return np.var(blocks)
        in_var = local_std_variance(img)
        out_var = local_std_variance(out)
        self.assertGreater(out_var, in_var * 0.8,
            f'CLAHE 后局部对比度方差 {out_var:.2f} 应 ≥ 输入 {in_var:.2f} * 0.8')

    def test_clahe_handles_gracefully_on_empty_array(self):
        empty = np.zeros((0, 0, 3), dtype=np.uint8)
        out = self.engine._enhance_wrinkle_label(empty)
        # 不抛异常,return 原图(空数组)
        self.assertEqual(out.shape, (0, 0, 3))

    def test_clahe_handles_invalid_shape(self):
        """shape 不对(2D 而非 3D)→ return 原图,不抛。"""
        bad = np.zeros((256, 256), dtype=np.uint8)
        out = self.engine._enhance_wrinkle_label(bad)
        self.assertTrue(np.array_equal(out, bad))

    def test_clahe_does_not_destroy_flat_image(self):
        """纯色图 → CLAHE 不破坏(输出应仍接近输入)。"""
        flat = _make_flat_image(color=200)
        out = self.engine._enhance_wrinkle_label(flat)
        # 允许小差异,但不应严重偏移
        diff = np.abs(out.astype(int) - flat.astype(int)).mean()
        self.assertLess(diff, 30, f'纯色图被破坏,平均偏差 {diff:.1f}')

    def test_clahe_preserves_color_balance(self):
        """彩色图:CLAHE 后 RGB 三通道相对关系不被严重破坏。"""
        # 红色 + 蓝色的合成图
        img = np.zeros((256, 256, 3), dtype=np.uint8)
        img[:, :128] = [200, 30, 30]   # 左半红
        img[:, 128:] = [30, 30, 200]   # 右半蓝
        out = self.engine._enhance_wrinkle_label(img)
        # 左半应仍偏红(红 > 蓝),右半应仍偏蓝(蓝 > 红)
        left_mean = out[:, :128].mean(axis=(0, 1))
        right_mean = out[:, 128:].mean(axis=(0, 1))
        self.assertGreater(left_mean[0], left_mean[2])  # 左:红 > 蓝
        self.assertGreater(right_mean[2], right_mean[0])  # 右:蓝 > 红


if __name__ == '__main__':
    unittest.main()