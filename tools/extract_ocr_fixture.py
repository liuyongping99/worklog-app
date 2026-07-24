"""一次性脚本: 从 order 575 的 3 张 row-level 标签图提取 OCR 文字,保存到 fixture。

用法:
    python tools/extract_ocr_fixture.py

输出: tests/regression/fixtures/order_575_ocr.txt
     格式: 多张图的 OCR 文字按 record_id 顺序拼接,每张图之间用空行分隔,
     顶部加注 record_id 元数据方便人工对照。

注意:
- 此脚本需要 PaddleOCR + Windows DLL 修复(走 blueprints.ocr_engine 模块导入触发)
- 仅在 OCR 模型升级 / fixture 漂移时重跑,平时不动
- 跑完后请人工 review fixture 内容确认无误
"""
import os
import sys

# 必须先 import blueprints.ocr_engine 触发 Windows DLL 修复(shm.dll 路径)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blueprints.ocr_engine import PaddleOCREngine
from models._db import get_db

# order 575 的 3 张 row-level 标签图 record_id 列表(2026-07-24 确认)
TARGET_ORDER_PK = 575
TARGET_RECORD_IDS = [1508, 1509, 1510]  # 7P环保三文治 / 7P环保杂胶×2

FIXTURE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'tests', 'regression', 'fixtures', 'order_575_ocr.txt',
)


def main():
    os.makedirs(os.path.dirname(FIXTURE_PATH), exist_ok=True)

    conn = get_db()
    cur = conn.cursor()
    # file_path 在 shipping_images(每条 record 可多张,取第一张)
    placeholders = ','.join('?' for _ in TARGET_RECORD_IDS)
    cur.execute(
        f'SELECT sr.id, si.file_path, sr.product_name, sr.specification '
        f'FROM shipping_records sr '
        f'JOIN shipping_images si ON si.record_pk = sr.id '
        f'WHERE sr.id IN ({placeholders}) '
        f'ORDER BY sr.id',
        TARGET_RECORD_IDS,
    )
    rows = cur.fetchall()
    conn.close()

    # 按 TARGET_RECORD_IDS 顺序排
    rows_by_id = {r[0]: r for r in rows}
    ordered = [rows_by_id[rid] for rid in TARGET_RECORD_IDS if rid in rows_by_id]

    engine = PaddleOCREngine()
    sections = []
    for r in ordered:
        rid, file_path, product, spec = r
        # file_path 是绝对路径,直接读
        with open(file_path, 'rb') as f:
            text = engine.extract_text(f.read())
        header = f'# record_id={rid} product={product!r} spec={spec!r}'
        sections.append(f'{header}\n{text or "(空)"}')

    fixture_content = '\n\n'.join(sections) + '\n'

    with open(FIXTURE_PATH, 'w', encoding='utf-8') as f:
        f.write(fixture_content)

    print(f'[OK] Fixture 写入: {FIXTURE_PATH}')
    print(f'   共 {len(ordered)} 张图,合计 {sum(len(s.splitlines()) for s in sections)} 行 OCR 文字')
    print('--- 前 30 行预览 ---')
    for line in fixture_content.splitlines()[:30]:
        print(line)


if __name__ == '__main__':
    main()