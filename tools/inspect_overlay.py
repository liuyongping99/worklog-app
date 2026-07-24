"""用 playwright + 系统 Chrome 验证 overlay 实际渲染位置。

用法: python tools/inspect_overlay.py
输出: 截图到 /tmp/overlay_test.png + bbox 报告
"""
import os
import sys
import sqlite3
from playwright.sync_api import sync_playwright

CHROME_PATH = r'C:\Users\Administrator\AppData\Local\Google\Chrome\Application\chrome.exe'
BASE_URL = 'http://127.0.0.1:5050'


def get_staff_id():
    conn = sqlite3.connect('C:/Users/Administrator/worklog-app/worklog.db')
    return conn.execute('SELECT id FROM staff WHERE is_active=1 ORDER BY id DESC LIMIT 1').fetchone()[0]


def main():
    staff_id = get_staff_id()
    print(f'Using staff_id={staff_id}')

    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=CHROME_PATH,
            headless=True,
            args=['--no-sandbox', '--disable-setuid-sandbox'],
        )
        page = browser.new_page(viewport={'width': 1400, 'height': 1000})

        # Login
        page.goto(f'{BASE_URL}/login')
        page.wait_for_load_state('networkidle')
        page.select_option('select[name="staff_id"]', str(staff_id))
        page.click('button[type="submit"]')
        page.wait_for_load_state('networkidle')

        # Goto shipping-records
        page.goto(f'{BASE_URL}/shipping-records')
        page.wait_for_load_state('networkidle')

        # Find record-level images
        img_items = page.locator('.img-item-record')
        count = img_items.count()
        print(f'Found {count} img-item-record elements')

        if count == 0:
            print('ERROR: no record-level images on page')
            browser.close()
            return

        # Inspect the first 3
        for i in range(min(3, count)):
            print(f'\n=== img-item-record [{i}] ===')
            item = img_items.nth(i)

            photo = item.locator('.img-photo').first
            img = item.locator('img').first
            name_ol = item.locator('.img-overlay-name').first
            spec_ol = item.locator('.img-overlay-spec').first

            photo_box = photo.bounding_box()
            img_box = img.bounding_box() if img.count() > 0 else None
            name_box = name_ol.bounding_box() if name_ol.count() > 0 else None
            spec_box = spec_ol.bounding_box() if spec_ol.count() > 0 else None

            print(f'  img-photo box: {photo_box}')
            print(f'  img box: {img_box}')
            print(f'  overlay-name box: {name_box}')
            print(f'  overlay-spec box: {spec_box}')

            # Check overlap with img
            if name_box and img_box:
                name_overlap = (
                    name_box['y'] < img_box['y'] + img_box['height'] and
                    name_box['y'] + name_box['height'] > img_box['y'] and
                    name_box['x'] < img_box['x'] + img_box['width'] and
                    name_box['x'] + name_box['width'] > img_box['x']
                )
                print(f'  overlay-name OVERLAPS with img: {name_overlap}')

            if spec_box and img_box:
                spec_overlap = (
                    spec_box['y'] < img_box['y'] + img_box['height'] and
                    spec_box['y'] + spec_box['height'] > img_box['y'] and
                    spec_box['x'] < img_box['x'] + img_box['width'] and
                    spec_box['x'] + spec_box['width'] > img_box['x']
                )
                print(f'  overlay-spec OVERLAPS with img: {spec_overlap}')

            # Get computed CSS of overlay
            if name_ol.count() > 0:
                name_css = name_ol.evaluate('el => { const cs = getComputedStyle(el); return {position: cs.position, top: cs.top, left: cs.left, zIndex: cs.zIndex, color: cs.color, display: cs.display}; }')
                print(f'  overlay-name computed CSS: {name_css}')

            # Get computed CSS of img-photo
            if photo.count() > 0:
                photo_css = photo.evaluate('el => { const cs = getComputedStyle(el); return {position: cs.position, height: cs.height, width: cs.width, display: cs.display, overflow: cs.overflow}; }')
                print(f'  img-photo computed CSS: {photo_css}')

            # Get computed CSS of img
            if img.count() > 0:
                img_css = img.evaluate('el => { const cs = getComputedStyle(el); return {position: cs.position, height: cs.height, width: cs.width, top: cs.top}; }')
                print(f'  img computed CSS: {img_css}')

            # Walk up and check ancestors with .shipping-page class
            ancestors = item.evaluate('''el => {
                let result = [];
                let cur = el;
                while (cur && cur !== document.body) {
                    result.push({tag: cur.tagName, classes: cur.className});
                    cur = cur.parentElement;
                }
                return result;
            }''')
            print(f'  ancestors: {ancestors[:8]}')

        # Screenshot first image item
        img_items.first.scroll_into_view_if_needed()
        page.wait_for_timeout(300)
        img_items.first.screenshot(path='/tmp/overlay_test.png')
        print(f'\nScreenshot: /tmp/overlay_test.png')

        # Also screenshot full page for context
        page.screenshot(path='/tmp/overlay_fullpage.png', full_page=False)
        print(f'Full page screenshot: /tmp/overlay_fullpage.png')

        browser.close()


if __name__ == '__main__':
    main()