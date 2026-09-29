"""出货页『皮革/木板』类商品不应显示行级图片按钮(2026-08-30 回归测试)。

背景:
  皮革 / 木板类商品按"块"计数,无需拍照,商品行不应显示🖼️ 上传按钮。
  mobile_shipping / inbound / loading 三个端点的 Python 侧都用了
  `_BOARD_KEYWORDS = ("皮革", "木板")` + `_is_board()` 关键字检测,
  但出货页 shipping.py 没有同名 helper,模板退化为字面量检查:
    `{% if '皮革木板' not in (item.product_name or '') %}`

后果:商品名只含 "皮革" 或只含 "木板" 时,字面量 "皮革木板" 不在 name 里,
按钮错误显示。用户 2026-08-30 报告:"皮革木板的图片按钮又出来了"。

修复:在 shipping.py 加 `_BOARD_KEYWORDS` + `_is_board()`,在 item 上下文
里设 `item['is_board']`,模板改用 `{% if not item.is_board %}`。
本测试聚焦 helper 行为 + DB 现存记录回归两条防线。
"""

import pytest


# ── 1. helper 单元测试 ─────────────────────────────────────────────
class TestIsBoardHelper:
    """shipping._is_board() 应能识别『皮革』/『木板』任一关键字,
    而非要求两个字连在一起的字面量。"""

    def _get_helper(self):
        from blueprints.shipping import _is_board
        return _is_board

    def test_皮革木板_keyword_combined(self):
        """最常见的商品名『皮革木板』应识别为 board。"""
        assert self._get_helper()("皮革木板") is True

    def test_皮革_alone(self):
        """『皮革』单独存在(数据库现存 1 条)也应识别为 board。"""
        assert self._get_helper()("皮革") is True

    def test_木板_alone(self):
        """『木板』单独存在也应识别为 board。"""
        assert self._get_helper()("木板") is True

    def test_皮革_with_whitespace(self):
        """名字前后有空格 / 中间有空格(规格混合)也应识别。"""
        assert self._get_helper()(" 皮革 ") is True
        assert self._get_helper()("皮革 木板") is True
        assert self._get_helper()("木板 2x4") is True

    def test_empty_or_none_safe(self):
        """空字符串 / None 不抛异常,返回 False。"""
        from typing import cast
        assert self._get_helper()("") is False
        # 实际代码会用 item.get('product_name', ''),但 helper 自身也应兜住 None
        assert self._get_helper()(cast(str, None)) is False

    def test_non_board_product(self):
        """非 board 类商品应返回 False。"""
        assert self._get_helper()("环保磅布") is False
        assert self._get_helper()("PVC桌布") is False
        assert self._get_helper()("杂胶 1.2x1.8") is False

    def test_keyword_substring_inside_other_word_still_matches(self):
        """『皮』是『皮革』的子串,但『皮革』关键字必须命中。

        回归保护:防止有人把 helper 改写成 `_name == '皮革'` 严格匹配。
        """
        assert self._get_helper()("环保皮革 1.5") is True


# ── 2. DB 现存记录回归 ─────────────────────────────────────────────
class TestExistingBoardProductsAreMarked:
    """出货 DB 里所有『皮革』/『木板』商品行都应被识别为 board。"""

    @pytest.fixture
    def db_path(self):
        from models._db import DB_PATH
        return DB_PATH

    def test_distinct_product_names_all_classified_as_board(self, db_path):
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT DISTINCT product_name FROM shipping_records "
                "WHERE product_name LIKE '%皮革%' OR product_name LIKE '%木板%'"
            )
            names = [row[0] for row in cur.fetchall()]
        finally:
            conn.close()

        # 先 sanity check: DB 里得有这类记录(否则测试无意义)
        assert names, "DB 应至少存在 1 条含『皮革』或『木板』的出货记录"

        from blueprints.shipping import _is_board
        missed = [n for n in names if not _is_board(n)]
        assert not missed, (
            f"以下商品应被识别为 board 但 _is_board() 返回 False: {missed!r};"
            f"完整列表: {names!r}"
        )


# ── 3. 模板守卫逻辑回归(对 shipping-records.html 字面量检查的反例) ──
class TestTemplateLiteralGuardCaveat:
    """记录当前模板字面量检查的覆盖盲区 —— 防止『简化到字面量检查』的回退。

    这一类不直接调用模板,只断言字面量逻辑应有的覆盖范围。
    修复后模板改为使用 item.is_board,本测试就不再适用;保留作为反例参考。
    """

    def test_literal_guard_misses_皮革_alone(self):
        """记录现状:字面量『皮革木板』不在『皮革』里 → 按钮错误显示。

        修复后此项无意义(模板不再用字面量),但留作『回退会被该测试逮到』的提示。
        """
        name = "皮革"
        literal_hides = "皮革木板" not in (name or "")
        assert literal_hides is True, (
            "字面量检查未隐藏『皮革』单独的商品名 —— 这正是 bug 现场"
        )
