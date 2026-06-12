"""文件上传服务蓝图。

提供 /upload/<path> 路由，让上传的图片可通过 URL 访问。
"""
import os
from flask import Blueprint, send_from_directory

bp = Blueprint('upload', __name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@bp.route('/upload/<path:filepath>')
def uploaded_file(filepath):
    """提供 upload 目录下文件的访问（支持子目录如 2026-05/xxx.jpg）"""
    upload_dir = os.path.join(BASE_DIR, 'upload')
    return send_from_directory(upload_dir, filepath)
