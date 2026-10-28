# -*- coding: utf-8 -*-
"""可行性验证: 从 D: 空闲空间雕刻候选图片, 看能不能捞回十月的原图。

流程:
  1. 读 $Bitmap → 空闲簇
  2. 扫描空闲空间, 找 JPEG(FFD8FF)/PNG 签名 → 按下一个签名或 EOF 切块
  3. 只保留【完整】的(JPEG 有 EOI / PNG 有 IEND)且宽度 >= 1000px 的(手机标签照特征)
  4. 落盘到 E:\\carve_sample\\ + 记录尺寸/大小索引

只读 D:, 只写 E:。
"""
import json
import os
import sys
import struct
import time
import pytsk3

VOL = '\\\\.\\D:'
OUT = r'E:\carve_full'
SAMPLE_GB = int(sys.argv[1]) if len(sys.argv) > 1 else 200   # 200 > 空闲空间总量 → 全扫
MIN_WIDTH = 1000


def jpeg_size(data, start):
    """从 JPEG 头解析宽高(找 SOF0/SOF2 段)。"""
    i = start + 2
    n = len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = struct.unpack_from('>H', data, i + 2)[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h, w = struct.unpack_from('>HH', data, i + 5)
            return w, h
        i += 2 + seg_len
    return None, None


def png_size(data, start):
    if len(data) < start + 24 or data[start + 12:start + 16] != b'IHDR':
        return None, None
    w, h = struct.unpack_from('>II', data, start + 16)
    return w, h


def main():
    os.makedirs(OUT, exist_ok=True)
    img = pytsk3.Img_Info(VOL)
    fs = pytsk3.FS_Info(img, offset=0)
    boot = fs.open_meta(7).read_random(0, 512)
    bps = struct.unpack_from('<H', boot, 0x0B)[0]
    spc = boot[0x0D]
    cluster_bytes = bps * spc

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
    print(f'空闲簇 {sum(bin(b).count("1") for b in bitmap):,} / 共 {len(bitmap)*8:,}', flush=True)

    sample_bytes = SAMPLE_GB * 1024 ** 3
    print(f'开始扫描 {SAMPLE_GB} GB 空闲空间...', flush=True)
    t0 = time.time()
    buf = bytearray()
    base_off = 0            # buf 在卷内的起始字节偏移(近似, 用于定位)
    idx = []
    saved = 0
    scanned = 0
    limit = sample_bytes

    def flush(buf):
        """从 buf 里切出候选图并落盘。"""
        nonlocal saved
        positions = []
        for magic in (b'\xff\xd8\xff', b'\x89PNG\r\n\x1a\n'):
            s = 0
            while True:
                p = buf.find(magic, s)
                if p == -1:
                    break
                positions.append(p)
                s = p + 1
        positions.sort()
        for i, p in enumerate(positions):
            end = positions[i + 1] if i + 1 < len(positions) else len(buf)
            blob = bytes(buf[p:end])
            if len(blob) < 5000:
                continue
            if blob.startswith(b'\xff\xd8\xff'):
                if b'\xff\xd9' not in blob[-6:]:
                    continue
                w, h = jpeg_size(blob, 0)
            else:
                if b'IEND' not in blob[-16:]:
                    continue
                w, h = png_size(blob, 0)
            if not w or w < MIN_WIDTH:
                continue
            ext = 'jpg' if blob.startswith(b'\xff\xd8\xff') else 'png'
            name = f'c{saved:06d}.{ext}'
            with open(os.path.join(OUT, name), 'wb') as fh:
                fh.write(blob)
            idx.append({'file': name, 'size': len(blob), 'w': w, 'h': h})
            saved += 1

    c = 0
    total = len(bitmap) * 8
    while c < total and scanned < limit:
        byte_i, bit_i = divmod(c, 8)
        if byte_i >= len(bitmap) or not ((bitmap[byte_i] >> bit_i) & 1):
            c += 1
            continue
        run = []
        while len(run) < 4096 and c < total:
            byte_i, bit_i = divmod(c, 8)
            if byte_i >= len(bitmap) or not ((bitmap[byte_i] >> bit_i) & 1):
                break
            run.append(c)
            c += 1
        if not run:
            break
        start = run[0] * cluster_bytes
        length = min(len(run) * cluster_bytes, 24 << 20)
        try:
            data = img.read(start, length)
        except Exception as e:
            print('读取失败:', str(e)[:80], flush=True)
            break
        if not data:
            break
        buf += data
        scanned += len(data)
        if len(buf) > (48 << 20):
            flush(buf)
            buf = bytearray()
        if scanned % (1 << 30) < len(data):
            print(f'  已扫 {scanned/1024**3:.2f} GB, 候选 {saved}, 用时 {time.time()-t0:.0f}s', flush=True)
    flush(buf)

    with open(os.path.join(OUT, 'index.json'), 'w', encoding='utf-8') as fh:
        json.dump(idx, fh)
    print(f'\n完成: 扫描 {scanned/1024**3:.2f} GB, 落盘候选 {saved} 张, 用时 {time.time()-t0:.0f}s')
    print('输出:', OUT)


if __name__ == '__main__':
    main()
