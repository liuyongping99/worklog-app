"""
丰源工作台 - Flask + SQLite（应用入口）

业务路由已拆分到 blueprints/ 目录，本文件只保留：
- Flask app 工厂
- 全局上下文（inject_notices）
- 缓存控制（add_cache_control_headers）
- 上传配置
- init_db()

各业务模块的端点：
- blueprints/basic_records.py — 工作经验/错误经验/待办/车辆维护
- blueprints/info_pages.py — 首页/价格板/通知彩色版/工作流/仓库/计数要点/换单要点/开单要点/当前缺货
- blueprints/notice.py — 公司通知 + REST API
- blueprints/products.py — 商品单位/商品类型/产品管理 + REST API
- blueprints/shipping.py — 出货记录 + REST API + AI 识别
- blueprints/inbound.py — 入库记录 + REST API
- blueprints/loading.py — 装柜订单 + REST API
- blueprints/upload.py — 静态文件访问
"""
import os
from dotenv import load_dotenv
from flask import Flask
from models import init_db, Notice

# 在 create_app 外加载，import 阶段就读到，方便 models.py 也能用
load_dotenv()


def create_app():
    app = Flask(__name__)
    # 优先用 .env 里的 SECRET_KEY，没设的话 fallback 到 dev 默认值
    app.secret_key = os.environ.get('SECRET_KEY', 'worklog-dev-secret-change-me')

    # === 缓存破坏机制（开发环境）===
    app.config['TEMPLATES_AUTO_RELOAD'] = True
    app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0

    # === 上传大小限制 ===
    # 注意:上传目录由 blueprints/_helpers.py 的 get_upload_dir() 集中管理,
    # 实际写到项目根的 upload/YYYY-MM/ 下,通过 blueprints/upload.py 的
    # /upload/<path> 路由对外暴露——这里不再用 app.config['UPLOAD_FOLDER']。
    app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max

    # === 初始化数据库 ===
    init_db()

    # === 全局上下文：所有模板都注入 notices 列表 ===
    @app.context_processor
    def inject_notices():
        try:
            notices = Notice.get_all()
        except Exception:
            notices = []
        return dict(all_notices=notices)

    # === 缓存控制 ===
    @app.after_request
    def add_cache_control_headers(response):
        if response.mimetype == 'text/html':
            response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
            response.headers['Pragma'] = 'no-cache'
            response.headers['Expires'] = '0'
        return response

    # === 注册蓝图 ===
    from blueprints.upload import bp as upload_bp
    from blueprints.basic_records import bp as basic_records_bp
    from blueprints.info_pages import bp as info_pages_bp
    from blueprints.notice import bp as notice_bp
    from blueprints.products import bp as products_bp
    from blueprints.shipping import bp as shipping_bp
    from blueprints.inbound import bp as inbound_bp
    from blueprints.loading import bp as loading_bp

    app.register_blueprint(upload_bp)
    app.register_blueprint(basic_records_bp)
    app.register_blueprint(info_pages_bp)
    app.register_blueprint(notice_bp)
    app.register_blueprint(products_bp)
    app.register_blueprint(shipping_bp)
    app.register_blueprint(inbound_bp)
    app.register_blueprint(loading_bp)

    return app


app = create_app()


if __name__ == '__main__':
    print('\n  丰源工作台已启动')
    print('  访问地址: http://127.0.0.1:5050\n')
    app.run(debug=True, port=5050)
