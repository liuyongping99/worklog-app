import sys
from playwright.sync_api import sync_playwright
import base64, io

# valid 4x4 png
PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAG0lEQVR4nGNgQAYi7LxwxKDBJwVHDDai6nAEADm8A8EwpfHDAAAAAElFTkSuQmCC"
png_bytes = base64.b64decode(PNG_B64)

with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context()
    pg = ctx.new_page()
    pg.goto("http://127.0.0.1:5050/login", wait_until="networkidle")
    pg.select_option('select[name="staff_id"]', "6221")
    pg.click('button[type="submit"]')
    pg.wait_for_load_state("networkidle", timeout=10000)
    pg.goto("http://127.0.0.1:5050/loading-orders?start_date=2026-06-01&end_date=2026-12-31", wait_until="networkidle")
    pg.on("console", lambda m: print("CONSOLE:", m.type, m.text[:200]))
    pg.on("pageerror", lambda e: print("PAGEERROR:", str(e)[:200]))
    # find a 点数 button
    btn = pg.query_selector(".placement-add-btn")
    if not btn:
        print("NO_POINTS_BUTTON")
    else:
        rid = btn.get_attribute("data-record-id")
        print("found 点数 button record_id=", rid)
        # write png to temp
        import tempfile, os
        tf = os.path.join(tempfile.gettempdir(), "test_placement.png")
        with open(tf, "wb") as f: f.write(png_bytes)
        btn.click()
        pg.wait_for_timeout(1000)
        cls = pg.eval_on_selector("#imageModal", "el => el.className") if pg.query_selector("#imageModal") else "NO #imageModal"
        print("imageModal className after click:", cls)
        # file input inside modal
        fi = pg.query_selector(".img-upload-modal input[type=file]")
        if not fi:
            print("NO_FILE_INPUT")
        else:
            fi.set_input_files(tf)
            pg.wait_for_timeout(400)
            # confirm upload
            pg.click(".img-upload-modal .btn-confirm")
            # expect count modal to open (placementImageUploaded -> openPlacementCount)
            try:
                pg.wait_for_selector("#placementCountModal.show", timeout=8000)
                print("COUNT_MODAL_OPENED_OK")
            except Exception as e:
                print("COUNT_MODAL_NOT_OPEN", str(e)[:120])
            # verify placement block rendered in page
            blk = pg.query_selector(f'.placement-record-block[data-record-id="{rid}"]')
            print("placement block rendered:", blk is not None)
    b.close()
