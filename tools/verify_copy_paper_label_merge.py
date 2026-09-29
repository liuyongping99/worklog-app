# -*- coding: utf-8 -*-
"""2026-09-09 验证:拷贝纸/日本纸标签图迁入 shipping_images(source='copy_paper_label') 后
PC 出货页 + 移动端详情页渲染是否正常(只读,不写业务数据)。"""
import sqlite3
from playwright.sync_api import sync_playwright

DB = r"c:\Users\Administrator\worklog-app\worklog.db"
URL = "http://localhost:5050/shipping-records"


def db_labels():
    c = sqlite3.connect(DB)
    rows = c.execute(
        "SELECT id, order_pk, record_pk, source FROM shipping_images "
        "WHERE source='copy_paper_label' ORDER BY id").fetchall()
    c.close()
    return rows


print("[DB] shipping_images(copy_paper_label):", db_labels())

errors = []
with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page()
    pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    pg.goto("http://localhost:5050/login", wait_until="domcontentloaded")
    pg.evaluate("document.getElementById('staff_id').value='6221'; document.querySelector('form').submit();")
    pg.wait_for_load_state("domcontentloaded")
    pg.goto(URL, wait_until="networkidle", timeout=20000)
    pg.wait_for_timeout(800)

    labels = pg.query_selector_all(".img-item-copy-paper-label")
    print(f"[PC] .img-item-copy-paper-label 数量: {len(labels)}")
    for el in labels:
        img = el.query_selector("img")
        print("     record_pk=%s image_id=%s src=%s" % (
            el.get_attribute("data-record-pk"),
            el.get_attribute("data-image-id"),
            img.get_attribute("src") if img else None))
        # 不应出现 OCR/AI 按钮
        print("     OCR按钮:", el.query_selector_all(".re-ocr-btn"),
              " AI按钮:", el.query_selector_all(".ai-judge-btn"))

    # 拷贝纸行:标签按钮红框 + 无普通图按钮
    trs = pg.query_selector_all("tr[data-record-id]")
    cp_rows = [t for t in trs if t.query_selector(".copy-paper-label-btn")]
    print(f"[PC] 拷贝纸行数: {len(cp_rows)}")
    for t in cp_rows:
        btn = t.query_selector(".copy-paper-label-btn")
        print("     record=%s 标签按钮class=%s 普通图按钮=%d 点数按钮=%d" % (
            t.get_attribute("data-record-id"),
            btn.get_attribute("class"),
            len(t.query_selector_all(".record-image-btn")),
            len(t.query_selector_all(".copy-paper-count-btn"))))

    # AI 比对列:拷贝纸行应为空
    for t in cp_rows:
        rid = t.get_attribute("data-record-id")
        cells = t.query_selector_all("td.match-col, .match-cell, td[data-match-col]")
        txt = " | ".join((c.inner_text() or "").strip() for c in cells)
        print(f"[PC] record {rid} AI比对列内容: {txt!r}")

    pg.screenshot(path=r"c:\Users\Administrator\worklog-app\tools\_shot_pc.png", full_page=False)

    # 移动端
    pg.goto("http://localhost:5050/m/shipping-today/order/955", wait_until="networkidle", timeout=20000)
    pg.wait_for_timeout(500)
    thumbs = pg.query_selector_all(".m-copy-paper-thumb img")
    print(f"[M] 标签缩略图数量: {len(thumbs)}")
    for im in thumbs:
        print("     src=", im.get_attribute("src"))
    pg.screenshot(path=r"c:\Users\Administrator\worklog-app\tools\_shot_m.png", full_page=False)
    b.close()

print("[JS] 控制台错误:", errors if errors else "无")
