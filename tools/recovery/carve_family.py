# -*- coding: utf-8 -*-
"""按【手机标签照尺寸族】定向雕刻 —— 不要求文件完整(尾部被覆盖也要)。

背景: 全量雕刻(要求 EOI 完整)只捞到 15 张该族, 但定向复扫显示空闲空间里有 7,115 张
      该尺寸族的 JPEG —— 它们的尾部(EOI)被 11:16 那次写入覆盖, 但 JPEG 头 + 前半段
      (标签文字所在)大概率还完好, 仍可 OCR 识别内容。
产出: E:\\carve_family\\  (每张一个文件 + index.json 记录尺寸/大小)
"""
import json
import os
import struct
import sys
import time
import pytsk3

VOL = '\\\\.\\D:'
OUT = r'E:\carve_family'
FAMILY = {(1280, 1707), (1707, 1280), (1702, 1276), (1200, 1600), (1280, 960),
          (960, 1280), (1080, 1920), (4032, 3024), (3024, 4032)}
SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def jpeg_dims(buf, start):
    i = start + 2
    n = len(buf)
    limit = min(n, start + 300000)
    while i + 9 < limit:
        if buf[i] != 0xFF:
            i += 1
            continue
        m = buf[i + 1]
        if m == 0xD8 or m == 0x01 or 0xD0 <= m <= 0xD7:
            i += 2
            continue
        if m in (0xDA, 0xD9):
            return None
        if i + 4 > limit:
            return None
        seg = struct.unpack_from('>H', buf, i + 2)[0]
        if seg < 2:
            return None
        if m in SOF:
            if i + 9 > limit:
                return None
            h, w = struct.unpack_from('>HH', buf, i + 5)
            if w <= 0 or h <= 0 or w > 20000 or h > 20000:
                return None
            return (w, h)
        i += 2 + seg
    return None


def main():
    os.makedirs(OUT, exist_ok=True)
    img = pytsk3.Img_Info(VOL)
    fs = pytsk3.FS_Info(img, offset=0)
    boot = fs.open_meta(7).read_random(0, 512)
    cluster_bytes = struct.unpack_from('<H', boot, 0x0B)[0] * boot[0x0D]

    bmf = fs.open_meta(6)
    bm_size = int(bmf.info.meta.size)
    bitmap = bytearray()
    off = 0
    while off < bm_size:
        d = bmf.read_random(off, min(4 << 20, bm_size - off))
        if not d:
            break
        bitmap += d
        off += len(d)
    print(f'空闲簇 {sum(bin(b).count("1") for b in bitmap):,}', flush=True)

    t0 = time.time()
    scanned = 0
    idx = []
    saved = 0
    buf = bytearray()
    c = 0
    total = len(bitmap) * 8
    RUN = 4096

    def flush(buf):
        nonlocal saved
        pos = []
        s = 0
        while True:
            p = buf.find(b'\xff\xd8\xff', s)
            if p == -1:
                break
            pos.append(p)
            s = p + 1
        for i, p in enumerate(pos):
            end = pos[i + 1] if i + 1 < len(pos) else len(buf)
            blob = bytes(buf[p:end])
            if len(blob) < 20000:
                continue
            d = jpeg_dims(buf, p)
            if not d or d not in FAMILY:
                continue
            fn = 'f%05d_%dx%d.jpg' % (saved, d[0], d[1])
            with open(os.path.join(OUT, fn), 'wb') as fh:
                fh.write(blob)
            idx.append({'file': fn, 'size': len(blob), 'w': d[0], 'h': d[1]})
            saved += 1

    while c < total:
        byte_i, bit_i = divmod(c, 8)
        if byte_i >= len(bitmap) or not ((bitmap[byte_i] >> bit_i) & 1):
            c += 1
            continue
        run = []
        while len(run) < RUN and c < total:
            byte_i, bit_i = divmod(c, 8)
            if byte_i >= len(bitmap) or not ((bitmap[byte_i] >> bit_i) & 1):
                break
            run.append(c)
            c += 1
        if not run:
            break
        try:
            data = img.read(run[0] * cluster_bytes, min(len(run) * cluster_bytes, 24 << 20))
        except Exception:
            break
        if not data:
            break
        buf += data
        scanned += len(data)
        if len(buf) > (64 << 20):
            flush(buf)
            buf = bytearray()
        if scanned % (8 << 30) < len(data):
            print(f'  已扫 {scanned/1024**3:.0f} GB, 落盘 {saved}, 用时 {time.time()-t0:.0f}s', flush=True)
    flush(buf)

    with open(os.path.join(OUT, 'index.json'), 'w', encoding='utf-8') as fh:
        json.dump(idx, fh)
    print(f'\n完成: 扫描 {scanned/1024**3:.1f} GB, 落盘 {saved} 张, 用时 {time.time()-t0:.0f}s')
    print('输出:', OUT)


if __name__ == '__main__':
    main()
