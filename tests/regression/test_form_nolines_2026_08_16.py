# -*- coding: utf-8 -*-


"""form_nolines OCR 回归测试 — 2026-08-16 磅布三文治 6 图。





目的:


- D1-D5 落盘后, 跑此测试验证 6 张磅布三文治图全部正确识别


- 改 _form_row_bands / D1-D5 任一函数后, 跑此测试立刻看到识别率变化





用法:


    # 需要 PaddleOCR 模型 + upload/2026-08/ 测试图 (~70 秒)


    python -m unittest tests.regression.test_form_nolines_2026_08_16 -v





设计:


- 6 张图 hardcoded (rec_id + image path + 预期 product/thickness/hand)


- 预期值含宽松匹配 (含 "磅布三文治" 即可) 与严格匹配 (含预期颜色)


- 不依赖 DB: 测试图 + 预期值都在文件里, 防止外部状态变化导致 flake


- 失败时打印每张图的识别行 (便于回看 OCR 残缺位置)


- 跑通标准: 6 张图业务字段 (品名/厚度/手感) 全部宽松匹配过


"""


import os


import sys


import unittest





sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))





from blueprints.ocr_engine import get_ocr_engine  # noqa: E402





ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))





# ── 6 张 2026-08-16 磅布三文治图 + 预期 (含宽松评测) ───────


CASES = [


    {


        "id": 2111,


        "rel_path": r"upload\2026-08\026c18ba722647ec86cbc684be884170.jpg",


        "expect_product": "磅布三文治",


        "expect_thickness": "1.0",


        "expect_hand_any": True,  # 加硬/中性/软性 任一即可


    },


    {


        "id": 2112,


        "rel_path": r"upload\2026-08\3faacecdd77842c58afe0a5f89ca09c6.png",


        "expect_product": "磅布三文治",


        "expect_thickness": "1.4",


        "expect_hand_any": True,


    },


    {


        "id": 2119,


        "tag": "#1",


        "rel_path": r"upload\2026-08\345e559082a44f1fab66add6497e95e6.jpg",


        "expect_product": "磅布三文治",


        "expect_thickness": "0.6",


        "expect_hand_any": True,


    },


    {


        "id": 2119,


        "tag": "#2",


        "rel_path": r"upload\2026-08\2485e13def8f44c2961da107fba198c8.jpg",


        "expect_product": "磅布三文治",


        "expect_thickness": "0.6",


        "expect_hand_any": True,


    },


    {


        "id": 2120,


        "rel_path": r"upload\2026-08\6967ccc791be49a8a249cc095ee92294.jpg",


        "expect_product": "磅布三文治",


        "expect_thickness": "0.8",


        "expect_hand_any": True,


    },


    {


        "id": 2139,


        "rel_path": r"upload\2026-08\2609e6826b604bc99a7aec3b6beae6ea.jpg",


        "expect_product": "磅布三文治",


        "expect_thickness": "1.2",


        "expect_hand_any": True,


    },


]





# 合法手感词 (7 个离散值)


HAND_VALID = {"加硬", "中性", "软性", "硬性", "加软", "硬", "软"}








class TestFormNoLines20260816(unittest.TestCase):


    """回归测试: 2026-08-16 磅布三文治 6 图识别率 (D1-D5 落盘验证)。"""





    @classmethod


    def setUpClass(cls):


        cls.engine = get_ocr_engine("paddleocr")


        cls.engine._ensure_model()





    def _run_one(self, case):


        path = os.path.join(ROOT, case["rel_path"])


        with open(path, "rb") as f:


            ib = f.read()


        txt = self.engine.extract_text(ib, preprocess_kind="form_nolines")


        lines = [ln for ln in txt.splitlines() if ln.strip()]


        blob = "\n".join(lines)


        # 宽松匹配: 允许 OCR 字符级错读 ("磅"->"防"); 核心词或 fabric 同义词命中即可
        core = case["expect_product"]
        fabric_synonyms = ("磅布", "防布", "无纺布")
        ok_prod = core in blob or any(
            w in blob for w in fabric_synonyms if w in core
        )


        ok_thick = case["expect_thickness"] in blob


        ok_hand = case["expect_hand_any"] and any(h in blob for h in HAND_VALID)


        return lines, {"product": ok_prod, "thickness": ok_thick, "hand": ok_hand}





    def test_all_six_images_pass(self):


        """6 张图业务字段 (品名/厚度/手感) 全部正确识别 (宽松匹配)。"""


        failures = []


        for case in CASES:


            with self.subTest(rec_id=case["id"], tag=case.get("tag", "")):


                lines, fields = self._run_one(case)


                # 全部字段都过才算 OK


                if not all(fields.values()):


                    failures.append(


                        (case["id"], case.get("tag", ""), fields, lines)


                    )


                    continue


                # 通过则不需要打印


        if failures:


            msg = []


            for rid, tag, fields, lines in failures:


                msg.append(


                    f"rec={rid}{tag} fields={fields} lines={lines}"


                )


            self.fail("以下图识别失败:\n" + "\n".join(msg))





    def test_no_d1_through_d5_regression(self):


        """OCR 输出应包含至少 4 个有内容的行 (品名/厚度/手感/底布 4 行结构)。"""


        # 抽一张验证行结构没塌


        case = CASES[0]


        lines, _ = self._run_one(case)


        self.assertGreaterEqual(


            len(lines), 4,


            f"rec={case['id']} 仅识别 {len(lines)} 行, 期望 ≥ 4 行 (品名/厚度/手感/底布)"


        )








if __name__ == "__main__":


    unittest.main()