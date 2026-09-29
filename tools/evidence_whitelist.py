# -*- coding: utf-8 -*-
"""验证：用「旧 C 盘绝对路径」去跑 _safe_delete_image_file 的白名单判定，是否会被拒绝。"""
import os

PROJ = r'D:\WORKLOG-APP'                    # 现在服务实际运行的项目根
UPLOAD_ROOT = os.path.realpath(os.path.join(PROJ, 'upload'))   # D:\WORKLOG-APP\upload

samples = [
    r'C:\Users\Administrator\worklog-app\upload\2026-05\e204c76264ae4fa7adc272989e8d232a.png',  # 迁移前的存量值
    r'D:\WORKLOG-APP\upload\2026-05\e204c76264ae4fa7adc272989e8d232a.png',                      # 迁移后的值
    'upload/2026-07/x_green_local_fuzzy.png',                                                    # 本来就相对的行
]

print('BASE_DIR     :', PROJ)
print('UPLOAD_ROOT  :', UPLOAD_ROOT)
print('-' * 78)
for p in samples:
    real = os.path.realpath(p)
    passed = real.startswith(UPLOAD_ROOT + os.sep)
    print('输入   : %s' % p)
    print('realpath: %s' % real)
    print('白名单  : %s   (os.path.exists=%s)' % ('PASS -> 允许删除' if passed else 'REJECT -> 拒绝删除', os.path.exists(p)))
    print('-' * 78)
