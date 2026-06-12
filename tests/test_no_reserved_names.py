"""防回归:禁止用 Windows 保留设备名做文件名/目录名。

为什么:CON / PRN / AUX / NUL / COM1-9 / LPT1-9 是 MS-DOS 时代的字符设备名,
被硬编码进 Win32 文件 API。如果 Python 模块名/模板名落到这些名字上:

- Win32 GetFileAttributesW 找不到文件 → import 失败、os.path.exists 返 False
- Flask render_template 找不到模板
- 文件实际能通过 bash/MSYS 创建(POSIX 层绕开 Win32),变成"幽灵文件"——
  bash 能 ls 出来、Python 看不见、PowerShell Remove-Item 删不掉
- 跨平台项目里这种文件一旦混进 git,所有 Windows 上的开发者都被坑

历史:2026-06-07 把缺货登记类放到 models/aux.py 踩了这个坑,改名 stock.py 后
留下 aux.py 幽灵直到 2026-06-12 才用 bash rm 彻底清理。

运行:
    python -m unittest tests.test_no_reserved_names -v
"""
import os
import sys
import unittest

# 让 import 能找到项目根
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


# Windows 保留设备名(case-insensitive),完整列表见 MSDN:
# https://learn.microsoft.com/en-us/windows/win32/fileio/naming-a-file
RESERVED_NAMES = frozenset(
    name.upper() for name in [
        'CON', 'PRN', 'AUX', 'NUL',
        'COM1', 'COM2', 'COM3', 'COM4', 'COM5',
        'COM6', 'COM7', 'COM8', 'COM9',
        'LPT1', 'LPT2', 'LPT3', 'LPT4', 'LPT5',
        'LPT6', 'LPT7', 'LPT8', 'LPT9',
    ]
)

# 扫描这些目录(项目源码所在),其他略过(__pycache__/.git/备份等)
SCAN_DIRS = ['models', 'blueprints', 'templates', 'static', 'tests', 'sql', 'scripts']

# 跳过这些目录名(出现在 os.walk 任何层都跳)
SKIP_DIR_NAMES = {
    '__pycache__', '.git', 'node_modules', '_safe-snapshot',
    'upload',  # 上传目录里全是 uuid 文件名,扫不出意义
}


def _stem(name: str) -> str:
    """取文件名主部(第一个 . 之前)。Windows 设备名劫持的是 stem,
    'aux.py' / 'aux.tar.gz' / 'aux' 都被劫持。"""
    return name.split('.', 1)[0]


def find_reserved_paths(root: str):
    """从 root 递归扫描,返回所有命中保留名的相对路径列表。"""
    hits = []
    for scan in SCAN_DIRS:
        scan_root = os.path.join(root, scan)
        if not os.path.isdir(scan_root):
            continue
        for dirpath, dirnames, filenames in os.walk(scan_root):
            # 原地剪枝
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES]
            # 查目录名本身
            for d in dirnames:
                if _stem(d).upper() in RESERVED_NAMES:
                    hits.append(_safe_relpath(os.path.join(dirpath, d), root))
            # 查文件名
            for fn in filenames:
                if _stem(fn).upper() in RESERVED_NAMES:
                    hits.append(_safe_relpath(os.path.join(dirpath, fn), root))
    return hits


def _safe_relpath(path: str, root: str) -> str:
    """os.path.relpath 在 Windows 上遇到保留名(如 aux.py)会把 path 解析成
    \\\\.\\aux 设备路径,然后抛 ValueError('path is on mount ...')。
    手工裁掉前缀避免炸,失败也好歹给出可读路径。"""
    try:
        return os.path.relpath(path, root)
    except ValueError:
        # 直接字符串裁前缀作为兜底
        if path.startswith(root):
            stripped = path[len(root):].lstrip('\\/').replace('\\', '/')
            return stripped
        return path


class NoReservedNamesTest(unittest.TestCase):
    """整个仓库不能出现 Windows 保留设备名做文件/目录名。"""

    def test_no_reserved_filenames(self):
        hits = find_reserved_paths(PROJECT_ROOT)
        self.assertFalse(
            hits,
            msg=(
                f"\n发现 Windows 保留名文件(会让 Python/Flask 看不见,"
                f"但 bash 能创建、IDE 能显示,变成幽灵文件):\n  "
                + "\n  ".join(hits)
                + f"\n保留名列表: {sorted(RESERVED_NAMES)}\n"
                + "修法:改名(例如 aux.py → stock.py),然后用 `rm` 在 bash/Git Bash 里删除"
                  "原文件(PowerShell Remove-Item 删不掉这种文件)。"
            ),
        )


class StemHelperTest(unittest.TestCase):
    """sanity:_stem 取文件名主部的行为。"""

    def test_simple_extension(self):
        self.assertEqual(_stem('aux.py'), 'aux')

    def test_no_extension(self):
        self.assertEqual(_stem('aux'), 'aux')

    def test_multiple_dots(self):
        # Windows 劫持的是第一个 . 之前的部分,所以 aux.tar.gz 也踩坑
        self.assertEqual(_stem('aux.tar.gz'), 'aux')

    def test_uppercase(self):
        # 比较时统一 upper(),原文大小写不影响
        self.assertEqual(_stem('AUX.PY').upper(), 'AUX')

    def test_innocent_prefix(self):
        # auxiliary.py 不是保留名(只有完全相等的 stem 才劫持)
        self.assertEqual(_stem('auxiliary.py'), 'auxiliary')
        self.assertNotIn(_stem('auxiliary.py').upper(), RESERVED_NAMES)


class ReservedNameMembershipTest(unittest.TestCase):
    """保留名列表完整性:常见踩坑名都在内。"""

    def test_dos_devices_present(self):
        for name in ('CON', 'PRN', 'AUX', 'NUL'):
            self.assertIn(name, RESERVED_NAMES)

    def test_com_ports_present(self):
        for i in range(1, 10):
            self.assertIn(f'COM{i}', RESERVED_NAMES)

    def test_lpt_ports_present(self):
        for i in range(1, 10):
            self.assertIn(f'LPT{i}', RESERVED_NAMES)

    def test_innocent_names_not_reserved(self):
        # 反例:这些名字不该被误判
        for name in ('STOCK', 'COMMON', 'COMPUTE', 'AUXILIARY', 'COM0', 'LPT0', 'COM10'):
            self.assertNotIn(
                name, RESERVED_NAMES,
                f'{name} 不应在保留名列表里(只有 COM1-9 / LPT1-9 是)'
            )


if __name__ == '__main__':
    unittest.main()
