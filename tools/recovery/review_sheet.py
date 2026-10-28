# -*- coding: utf-8 -*-
"""给【人工复核清单】生成缩略图墙(带编号), 方便肉眼确认。"""
import csv
import json
import os

CARVE_DIR = r'E:\carve_family'
OUT_DIR = r'E:\carve_family\sheets'
COLS, ROWS = 4, 5
THUMB, PAD, LABEL_H = 300, 8, 44


def build(items, out_path):
    from PIL import Image, ImageDraw, ImageFile
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    W = COLS * (THUMB + PAD) + PAD
    H = ROWS * (THUMB + LABEL_H + PAD) + PAD
    sheet = Image.new('RGB', (W, H), (26, 28, 33))
    dr = ImageDraw.Draw(sheet)
    for i, it in enumerate(items):
        r, c = divmod(i, COLS)
        x = PAD + c * (THUMB + PAD)
        y = PAD + r * (THUMB + LABEL_H + PAD)
        try:
            with Image.open(os.path.join(CARVE_DIR, it['cand'])) as im:
                im = im.convert('RGB')
                w, h = im.size
                if max(w, h) > THUMB:
                    s = THUMB / max(w, h)
                    im = im.resize((max(1, int(w * s)), max(1, int(h * s))))
                sheet.paste(im, (x + (THUMB - im.size[0]) // 2, y))
        except Exception:
            dr.rectangle([x, y, x + THUMB, y + THUMB], outline=(220, 70, 70))
        dr.text((x + 3, y + THUMB + 4), f"#{i+1}  {os.path.basename(it['orig_path'])[-26:]}", fill=(255, 215, 110))
        dr.text((x + 3, y + THUMB + 18), f"ratio {it['ratio']}", fill=(150, 220, 150))
        dr.text((x + 3, y + THUMB + 31), (it.get('text') or '')[:34], fill=(210, 210, 210))
    sheet.save(out_path, quality=90)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    plan = json.load(open(os.path.join(CARVE_DIR, 'replan.json'), encoding='utf-8'))
    review = [p for p in plan if 0.25 <= p['ratio'] < 0.50]
    print('待复核:', len(review))
    if not review:
        return
    build(review, os.path.join(OUT_DIR, 'review_sheet.jpg'))
    print('缩略图墙:', os.path.join(OUT_DIR, 'review_sheet.jpg'))
    with open(os.path.join(OUT_DIR, 'review_index.csv'), 'w', newline='', encoding='utf-8-sig') as fh:
        w = csv.writer(fh)
        w.writerow(['编号', 'image_id', '原文件名', '原路径', 'ratio', '候选文件', '候选OCR', '参考OCR'])
        for i, p in enumerate(review):
            w.writerow([i + 1, p['image_id'], os.path.basename(p['orig_path'].replace('/', '\\')),
                        p['orig_path'], p['ratio'], p['cand'],
                        (p.get('text') or '')[:200], (p.get('ref_ocr') or '')[:200]])
    print('索引:', os.path.join(OUT_DIR, 'review_index.csv'))


if __name__ == '__main__':
    main()
