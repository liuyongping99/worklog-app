"""端到端验证:拷贝纸标签图走 shipping_images(source='copy_paper_label')。

步骤:
  1. 登录(设置 staff_id session)
  2. POST 一张测试图到 /records/<rid>/copy-paper-images
  3. 断言 shipping_images 新增 1 条 source='copy_paper_label'
  4. DELETE /images/<id> (通用端点)
  5. 断言该条已删除
"""
import sqlite3, base64, io
from PIL import Image
from playwright.sync_api import sync_playwright

RID = 162  # 日本纸616/516, order 57, 未锁定
BASE = "http://localhost:5050"

# 造一张 2x2 红点 PNG
buf = io.BytesIO()
Image.new("RGB", (2, 2), (255, 0, 0)).save(buf, "PNG")
PNG_B64 = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def db_count():
    c = sqlite3.connect("worklog.db")
    n = c.execute("SELECT COUNT(*) FROM shipping_images WHERE source='copy_paper_label'").fetchone()[0]
    c.close()
    return n


with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page()
    pg.goto(f"{BASE}/login", wait_until="domcontentloaded")
    pg.evaluate("document.getElementById('staff_id').value='6221'; document.querySelector('form').submit();")
    pg.wait_for_timeout(500)

    before = db_count()
    print(f"[DB] copy_paper_label 上传前: {before}")

    # 1) 上传
    res = pg.evaluate(
        """async (b64) => {
            const r = await fetch('/api/v1/shipping-orders/records/%d/copy-paper-images', {
                method:'POST',
                headers:{'Content-Type':'application/json'},
                body: JSON.stringify({image: b64, source:'label'})
            });
            return await r.json();
        }""" % RID,
        PNG_B64,
    )
    print("[API] upload ->", res.get("success"), res.get("error") or "")
    assert res.get("success"), f"上传失败: {res}"
    new_id = res["image"]["id"]
    print(f"[API] 新图片 id={new_id}, source={res['image'].get('source')}")

    after_up = db_count()
    print(f"[DB] copy_paper_label 上传后: {after_up} (Δ={after_up-before})")
    assert after_up == before + 1, "应恰好新增 1 条 copy_paper_label"

    # 2) 删除(通用端点)
    res2 = pg.evaluate(
        """async (id) => {
            const r = await fetch('/api/v1/shipping-orders/images/'+id, {method:'DELETE'});
            return await r.json();
        }""",
        new_id,
    )
    print("[API] delete ->", res2.get("success"), res2.get("error") or "")
    assert res2.get("success"), f"删除失败: {res2}"

    after_del = db_count()
    print(f"[DB] copy_paper_label 删除后: {after_del}")
    assert after_del == before, "删除后应回到原数量"

    b.close()

print("\n✅ 端到端通过: 标签图存 shipping_images(source='copy_paper_label'),删除走通用端点")
