# -*- coding: utf-8 -*-
"""实测：拷贝纸行「标签」按钮上传，验证走 copy-paper 端点 + 只建 1 条 shipping_images(source=copy_paper_label)。
捕获上传 POST 请求 URL 是关键证据。"""
import sqlite3, sys
from playwright.sync_api import sync_playwright

DB = r"c:\Users\Administrator\worklog-app\worklog.db"
TEST_IMG = r"c:\Users\Administrator\worklog-app\tests\fixtures\wrinkle_labels\1623c8389e8a4e61aa417294603f9f0e.jpg"
URL = "http://localhost:5050/shipping-records"

def db_cp_count():
    # 2026-09-09: 标签图迁入 shipping_images(source='copy_paper_label'),copy_paper_images 表已废弃
    c = sqlite3.connect(DB); n = c.execute("SELECT COUNT(*) FROM shipping_images WHERE source='copy_paper_label'").fetchone()[0]; c.close(); return n
def db_ship_latest():
    c = sqlite3.connect(DB)
    r = c.execute("SELECT id, record_pk, source, created_at FROM shipping_images ORDER BY id DESC LIMIT 3").fetchall()
    c.close(); return r

cp_before = db_cp_count()
print(f"[DB] shipping_images(copy_paper_label) 上传前: {cp_before} 条")
print(f"[DB] shipping_images 最近3条(上传前): {db_ship_latest()}")

upload_requests = []
with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page()
    pg.on("request", lambda req: upload_requests.append((req.method, req.url)) if req.method=="POST" else None)
    # 1. 登录
    pg.goto("http://localhost:5050/login", wait_until="domcontentloaded")
    pg.evaluate("document.getElementById('staff_id').value='6221'; document.querySelector('form').submit();")
    pg.wait_for_load_state("domcontentloaded")
    # 2. 出货页
    pg.goto(URL, wait_until="networkidle", timeout=15000)
    pg.wait_for_timeout(500)
    # 找拷贝纸行的标签按钮
    label_btn = pg.query_selector("button.copy-paper-label-btn")
    if not label_btn:
        print("[ERR] 没找到 copy-paper-label-btn — 拷贝纸行可能不在默认视图(只显示近2天)")
        b.close(); sys.exit(1)
    rec_id = label_btn.get_attribute("data-record-id")
    print(f"[页] 找到标签按钮, record_id={rec_id}")
    # 上传前该 order 的 order-images-area 图片数
    order_id = label_btn.get_attribute("data-order-id")
    before_imgs = pg.query_selector_all(f"div.order-images-area[data-order-id='{order_id}'] .img-item")
    print(f"[页] order {order_id} 上传前 order-images-area 图片数: {len(before_imgs)}")
    # 3. 点标签按钮 → 弹上传框
    label_btn.click()
    pg.wait_for_selector("#imageModal.show", timeout=3000)
    # 4. 塞测试图到 file input
    pg.set_input_files("#fileInput", TEST_IMG)
    pg.wait_for_selector("#previewActions button.btn-confirm:visible", timeout=3000)
    pg.wait_for_timeout(300)
    # 5. 点确认上传
    upload_requests.clear()  # 清掉登录的 POST
    pg.click("#previewActions button.btn-confirm")
    # 等 reload (location.reload)
    try:
        pg.wait_for_function("document.readyState === 'complete'", timeout=15000)
    except Exception as e:
        print(f"[warn] reload 等待: {e}")
    pg.wait_for_timeout(1000)
    print(f"[NET] 上传阶段 POST 请求: {upload_requests}")
    # 6. 上传后 order-images-area 图片数
    after_imgs = pg.query_selector_all(f"div.order-images-area[data-order-id='{order_id}'] .img-item")
    print(f"[页] order {order_id} 上传后 order-images-area 图片数: {len(after_imgs)}")
    b.close()

cp_after = db_cp_count()
print(f"[DB] shipping_images(copy_paper_label) 上传后: {cp_after} 条 (Δ={cp_after-cp_before})")
print(f"[DB] shipping_images 最近3条(上传后): {db_ship_latest()}")
# 判定
cp_posts = [u for m,u in upload_requests if "copy-paper-images" in u]
pl_posts = [u for m,u in upload_requests if "placement-images" in u]
print("\n=== 结论 ===")
if cp_posts and not pl_posts:
    print(f"✅ 走对了: POST copy-paper-images ({cp_posts[0]}) ; shipping_images(copy_paper_label) +{cp_after-cp_before}")
elif pl_posts and not cp_posts:
    print(f"❌ 走错了: POST placement-images ({pl_posts[0]}) ; 建了 shipping_images(source=placement)")
else:
    print(f"⚠️ 其他: cp={cp_posts} pl={pl_posts}")
