DROP TABLE IF EXISTS product;

CREATE TABLE product (
    product_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    product_code    TEXT NOT NULL UNIQUE,
    product_name    TEXT NOT NULL,
    category_id     INTEGER NOT NULL,
    specification   TEXT,
    model           TEXT,
    barcode         TEXT,
    cost_price      INTEGER,
    base_unit       TEXT,
    aux_unit        TEXT,
    conversion_rate TEXT,
    stock_quantity  INTEGER,
    aux_quantity    TEXT,
    preset_price    REAL,
    status          INTEGER DEFAULT 1,
    created_at      TEXT DEFAULT (datetime('now','localtime')),
    updated_at      TEXT DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (category_id) REFERENCES product_category(category_id)
);
CREATE INDEX IF NOT EXISTS idx_product_category ON product(category_id);

DROP TABLE IF EXISTS product_category;

CREATE TABLE product_category (
    category_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    category_code   TEXT NOT NULL UNIQUE,
    category_name   TEXT NOT NULL,
    parent_id       INTEGER DEFAULT NULL,
    level           INTEGER NOT NULL DEFAULT 1,
    sort_order      INT DEFAULT 0,
    status          INTEGER DEFAULT 1,
    created_at      TEXT DEFAULT (datetime('now','localtime')),
    updated_at      TEXT DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (parent_id) REFERENCES product_category(category_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uk_category_code ON product_category(category_code);
CREATE INDEX IF NOT EXISTS idx_pc_parent ON product_category(parent_id);
CREATE INDEX IF NOT EXISTS idx_pc_level ON product_category(level);

INSERT OR IGNORE INTO product_category (category_id, category_code, category_name, parent_id, level, sort_order) VALUES (1, '0', '商品', NULL, 1, 0);

INSERT OR IGNORE INTO product_category (category_code, category_name, parent_id, level, sort_order) VALUES
( '01', '纸类',    (SELECT category_id FROM product_category WHERE category_code = '0'), 2, 1),
( '02', '辅料类', (SELECT category_id FROM product_category WHERE category_code = '0'), 2, 2),
( '03', '胶粘类', (SELECT category_id FROM product_category WHERE category_code = '0'), 2, 3),
( '04', '防潮类', (SELECT category_id FROM product_category WHERE category_code = '0'), 2, 4),
( '05', '棉类',    (SELECT category_id FROM product_category WHERE category_code = '0'), 2, 5),
( '06', '塑料类', (SELECT category_id FROM product_category WHERE category_code = '0'), 2, 6),
( '07', '特材类', (SELECT category_id FROM product_category WHERE category_code = '0'), 2, 7),
( '08', '其它类', (SELECT category_id FROM product_category WHERE category_code = '0'), 2, 8),
( '09', '处理货', (SELECT category_id FROM product_category WHERE category_code = '0'), 2, 9);

INSERT OR IGNORE INTO product_category ( category_code, category_name, parent_id, level, sort_order) VALUES
('0101', '灰板类',  (SELECT category_id FROM product_category WHERE category_code = '01'), 3, 1),
('0102', '白板类',  (SELECT category_id FROM product_category WHERE category_code = '01'), 3, 2),
('0103', '塞包纸',  (SELECT category_id FROM product_category WHERE category_code = '01'), 3, 3),
('0104', '皮糠纸',  (SELECT category_id FROM product_category WHERE category_code = '01'), 3, 4),
('0105', '日本纸',  (SELECT category_id FROM product_category WHERE category_code = '01'), 3, 5),
('0106', '快巴纸',  (SELECT category_id FROM product_category WHERE category_code = '01'), 3, 6),
( '0107', '拷贝纸', (SELECT category_id FROM product_category WHERE category_code = '01'), 3, 7),
( '0108', '腊光纸', (SELECT category_id FROM product_category WHERE category_code = '01'), 3, 8),
( '0109', '牛皮纸', (SELECT category_id FROM product_category WHERE category_code = '01'), 3, 9),
( '0110', '牛卡纸', (SELECT category_id FROM product_category WHERE category_code = '01'), 3, 10),
( '0111', '铜版纸', (SELECT category_id FROM product_category WHERE category_code = '01'), 3, 11),
( '0112', '双面白', (SELECT category_id FROM product_category WHERE category_code = '01'), 3, 12),
( '0113', '书写纸', (SELECT category_id FROM product_category WHERE category_code = '01'), 3, 13),
( '0114', '黑卡纸', (SELECT category_id FROM product_category WHERE category_code = '01'), 3, 14),
( '0115', '雪梨纸', (SELECT category_id FROM product_category WHERE category_code = '01'), 3, 15);

INSERT OR IGNORE INTO product_category (category_code, category_name, parent_id, level, sort_order) VALUES
( '0201', '杂胶',    (SELECT category_id FROM product_category WHERE category_code = '02'), 3, 1),
( '0202', '三文治', (SELECT category_id FROM product_category WHERE category_code = '02'), 3, 2),
( '0203', '露华里', (SELECT category_id FROM product_category WHERE category_code = '02'), 3, 3),
( '0204', '无纺布', (SELECT category_id FROM product_category WHERE category_code = '02'), 3, 4),
( '0205', '回力胶', (SELECT category_id FROM product_category WHERE category_code = '02'), 3, 5),
( '0206', '弹力胶', (SELECT category_id FROM product_category WHERE category_code = '02'), 3, 6),
( '0207', '泥胶',    (SELECT category_id FROM product_category WHERE category_code = '02'), 3, 7),
( '0209', '高发泡（轻胶）', (SELECT category_id FROM product_category WHERE category_code = '02'), 3, 8),
( '0210', '7PPVC人造革', (SELECT category_id FROM product_category WHERE category_code = '02'), 3, 9),
( '0211', 'A级杂胶',     (SELECT category_id FROM product_category WHERE category_code = '02'), 3, 10),
( '0212', '磅布三文治', (SELECT category_id FROM product_category WHERE category_code = '02'), 3, 11),
( '0208', '潜水料',       (SELECT category_id FROM product_category WHERE category_code = '02'), 3, 12);

INSERT OR IGNORE INTO product_category (category_code, category_name, parent_id, level, sort_order) VALUES
( '0301', '喷胶',          (SELECT category_id FROM product_category WHERE category_code = '03'), 3, 1),
( '0302', '万能胶',       (SELECT category_id FROM product_category WHERE category_code = '03'), 3, 2),
( '0303', '粉胶',          (SELECT category_id FROM product_category WHERE category_code = '03'), 3, 3),
( '0304', '接枝胶',       (SELECT category_id FROM product_category WHERE category_code = '03'), 3, 4),
( '0305', '白胶',          (SELECT category_id FROM product_category WHERE category_code = '03'), 3, 5),
( '0306', '白油',          (SELECT category_id FROM product_category WHERE category_code = '03'), 3, 6),
( '0307', '伟明',          (SELECT category_id FROM product_category WHERE category_code = '03'), 3, 7),
( '0308', '衣车油',       (SELECT category_id FROM product_category WHERE category_code = '03'), 3, 8),
( '0309', '七B水',        (SELECT category_id FROM product_category WHERE category_code = '03'), 3, 9),
( '0310', '双面胶',       (SELECT category_id FROM product_category WHERE category_code = '03'), 3, 10),
( '0311', '封箱胶',       (SELECT category_id FROM product_category WHERE category_code = '03'), 3, 11),
( '0312', '文具胶',       (SELECT category_id FROM product_category WHERE category_code = '03'), 3, 12),
( '0313', '美纹纸',       (SELECT category_id FROM product_category WHERE category_code = '03'), 3, 13),
( '0314', '牛皮胶',       (SELECT category_id FROM product_category WHERE category_code = '03'), 3, 14),
( '0315', '特殊用途胶', (SELECT category_id FROM product_category WHERE category_code = '03'), 3, 15),
( '0316', '补强带',       (SELECT category_id FROM product_category WHERE category_code = '03'), 3, 16);

INSERT OR IGNORE INTO product_category ( category_code, category_name, parent_id, level, sort_order) VALUES
( '0401', '防潮珠', (SELECT category_id FROM product_category WHERE category_code = '04'), 3, 1),
( '0402', '防霉片', (SELECT category_id FROM product_category WHERE category_code = '04'), 3, 2),
( '0403', '防霉纸', (SELECT category_id FROM product_category WHERE category_code = '04'), 3, 3),
( '0404', '香片',    (SELECT category_id FROM product_category WHERE category_code = '04'), 3, 4);

INSERT OR IGNORE INTO product_category (category_code, category_name, parent_id, level, sort_order) VALUES
( '0501', '珍珠棉', (SELECT category_id FROM product_category WHERE category_code = '05'), 3, 1),
( '0502', '海绵', (SELECT category_id FROM product_category WHERE category_code = '05'), 3, 2),
( '0503', '双面水刺布', (SELECT category_id FROM product_category WHERE category_code = '05'), 3, 3),
( '0504', '丝绵', (SELECT category_id FROM product_category WHERE category_code = '05'), 3, 4),
( '0505', '针棉', (SELECT category_id FROM product_category WHERE category_code = '05'), 3, 5),
( '0506', '水刺布（棉朴）', (SELECT category_id FROM product_category WHERE category_code = '05'), 3, 6),
( '0507', '棉绳', (SELECT category_id FROM product_category WHERE category_code = '05'), 3, 7),
( '0508', '棉通', (SELECT category_id FROM product_category WHERE category_code = '05'), 3, 8),
( '0509', '海王', (SELECT category_id FROM product_category WHERE category_code = '05'), 3, 9),
( '0510', '单面水刺布', (SELECT category_id FROM product_category WHERE category_code = '05'), 3, 10),
( '0511', '环保托', (SELECT category_id FROM product_category WHERE category_code = '05'), 3, 11),
( '0512', '成品水刺布袋', (SELECT category_id FROM product_category WHERE category_code = '05'), 3, 12),
( '0513', '棉加工', (SELECT category_id FROM product_category WHERE category_code = '05'), 3, 13);

INSERT OR IGNORE INTO product_category ( category_code, category_name, parent_id, level, sort_order) VALUES
( '0601', 'PE板',       (SELECT category_id FROM product_category WHERE category_code = '06'), 3, 1),
( '0602', 'PVC胶片', (SELECT category_id FROM product_category WHERE category_code = '06'), 3, 2),
( '0603', '胶骨',       (SELECT category_id FROM product_category WHERE category_code = '06'), 3, 3),
( '0604', '胶通',       (SELECT category_id FROM product_category WHERE category_code = '06'), 3, 4),
( '0605', '胶针',       (SELECT category_id FROM product_category WHERE category_code = '06'), 3, 5),
( '0606', 'PVC软膜', (SELECT category_id FROM product_category WHERE category_code = '06'), 3, 6),
( '0607', '高发泡XPE', (SELECT category_id FROM product_category WHERE category_code = '06'), 3, 7),
( '0608', 'EVA软膜', (SELECT category_id FROM product_category WHERE category_code = '06'), 3, 8),
( '0609', '蜂巢板',   (SELECT category_id FROM product_category WHERE category_code = '06'), 3, 9),
( '0610', '牛津布',   (SELECT category_id FROM product_category WHERE category_code = '06'), 3, 10),
( '0611', '烫布',      (SELECT category_id FROM product_category WHERE category_code = '06'), 3, 11),
( '0612', '仿超',      (SELECT category_id FROM product_category WHERE category_code = '06'), 3, 12),
( '0613', '里布',      (SELECT category_id FROM product_category WHERE category_code = '06'), 3, 13),
( '0614', 'TA特软',  (SELECT category_id FROM product_category WHERE category_code = '06'), 3, 14),
( '0615', 'TP',         (SELECT category_id FROM product_category WHERE category_code = '06'), 3, 15),
( '0616', 'TQ',        (SELECT category_id FROM product_category WHERE category_code = '06'), 3, 16),
( '0617', 'TR',         (SELECT category_id FROM product_category WHERE category_code = '06'), 3, 17);

INSERT OR IGNORE INTO product_category ( category_code, category_name, parent_id, level, sort_order) VALUES
('0701', '猪皮纹HA',      (SELECT category_id FROM product_category WHERE category_code = '07'), 3, 1),
('0702', '猪皮纹HC',      (SELECT category_id FROM product_category WHERE category_code = '07'), 3, 2),
('0703', 'LB鱼鳞布特软', (SELECT category_id FROM product_category WHERE category_code = '07'), 3, 3),
('0704', 'LC鱼鳞布特软', (SELECT category_id FROM product_category WHERE category_code = '07'), 3, 4),
('0705', 'QP特软',          (SELECT category_id FROM product_category WHERE category_code = '07'), 3, 5),
('0706', 'LD特软三文治', (SELECT category_id FROM product_category WHERE category_code = '07'), 3, 6),
('0707', '牛津布',           (SELECT category_id FROM product_category WHERE category_code = '07'), 3, 7),
('0708', 'GA',                (SELECT category_id FROM product_category WHERE category_code = '07'), 3, 8),
('0709', 'TA 特软',         (SELECT category_id FROM product_category WHERE category_code = '07'), 3, 9),
('0710', '烫布',              (SELECT category_id FROM product_category WHERE category_code = '07'), 3, 10),
('0711', '仿超',              (SELECT category_id FROM product_category WHERE category_code = '07'), 3, 11),
('0712', '里布',              (SELECT category_id FROM product_category WHERE category_code = '07'), 3, 12),
('0713', 'HT',                (SELECT category_id FROM product_category WHERE category_code = '07'), 3, 13),
('0714', 'TQ',                (SELECT category_id FROM product_category WHERE category_code = '07'), 3, 14),
('0715', 'TK',                 (SELECT category_id FROM product_category WHERE category_code = '07'), 3, 15);

INSERT OR IGNORE INTO product_category (category_code, category_name, parent_id, level, sort_order) VALUES
('0817', '编织袋', (SELECT category_id FROM product_category WHERE category_code = '08'), 3, 17);

INSERT OR IGNORE INTO product_category ( category_code, category_name, parent_id, level, sort_order) VALUES
( '010101', '华实大度',      (SELECT category_id FROM product_category WHERE category_code = '0101'), 4, 1),
( '010102', 'A级正度灰板', (SELECT category_id FROM product_category WHERE category_code = '0101'), 4, 2),
( '010103', 'A级大度灰板', (SELECT category_id FROM product_category WHERE category_code = '0101'), 4, 3),
( '010104', '加工后纸',      (SELECT category_id FROM product_category WHERE category_code = '0101'), 4, 4);

INSERT OR IGNORE INTO product_category ( category_code, category_name, parent_id, level, sort_order) VALUES
('010201', '正度白板', (SELECT category_id FROM product_category WHERE category_code = '0102'), 4, 1),
('010202', '大度白板', (SELECT category_id FROM product_category WHERE category_code = '0102'), 4, 2),
('010203', '卷筒白板', (SELECT category_id FROM product_category WHERE category_code = '0102'), 4, 3);

INSERT OR IGNORE INTO product_category ( category_code, category_name, parent_id, level, sort_order) VALUES
('010401', '常规698',        (SELECT category_id FROM product_category WHERE category_code = '0104'), 4, 1),
('010402', '豪博正品698',  (SELECT category_id FROM product_category WHERE category_code = '0104'), 4, 2),
('010403', '398', (SELECT category_id FROM product_category WHERE category_code = '0104'), 4, 3),
('010404', '222', (SELECT category_id FROM product_category WHERE category_code = '0104'), 4, 4),
('010405', '268', (SELECT category_id FROM product_category WHERE category_code = '0104'), 4, 5),
('010406', '意大利进口', (SELECT category_id FROM product_category WHERE category_code = '0104'), 4, 6),
('010407', '普通再生革', (SELECT category_id FROM product_category WHERE category_code = '0104'), 4, 7),
('010408', '环保再生革', (SELECT category_id FROM product_category WHERE category_code = '0104'), 4, 8),
('010409', '888', (SELECT category_id FROM product_category WHERE category_code = '0104'), 4, 9),
('010410', '加硬皮糠纸698', (SELECT category_id FROM product_category WHERE category_code = '0104'), 4, 10);

INSERT OR IGNORE INTO product_category ( category_code, category_name, parent_id, level, sort_order) VALUES
('01010101', '正度灰板250g',   (SELECT category_id FROM product_category WHERE category_code = '010101'), 4, 1),
('01010102', '正度灰板A250g', (SELECT category_id FROM product_category WHERE category_code = '010101'), 4, 2),
('01010103', '正度灰板360g',   (SELECT category_id FROM product_category WHERE category_code = '010101'), 4, 3),
('01010104', '正度灰板A360g', (SELECT category_id FROM product_category WHERE category_code = '010101'), 4, 4),
('01010105', '正度灰板450g',   (SELECT category_id FROM product_category WHERE category_code = '010101'), 4, 5),
('01010106', '正度灰板A450g', (SELECT category_id FROM product_category WHERE category_code = '010101'), 4, 6),
('01010107', '正度灰板560g',   (SELECT category_id FROM product_category WHERE category_code = '010101'), 4, 7),
('01010108', '正度灰板A550g', (SELECT category_id FROM product_category WHERE category_code = '010101'), 4, 8),
('01010109', '正度灰板650g',   (SELECT category_id FROM product_category WHERE category_code = '010101'), 4, 9),
('01010110', '正度灰板760g',   (SELECT category_id FROM product_category WHERE category_code = '010101'), 4, 10),
('01010111', '正度灰板800g',   (SELECT category_id FROM product_category WHERE category_code = '010101'), 4, 11),
('01010112', '正度灰板1000g', (SELECT category_id FROM product_category WHERE category_code = '010101'), 4, 12),
('01010113', '正度灰板A1000g', (SELECT category_id FROM product_category WHERE category_code = '010101'), 4, 13),
('01010115', '正度灰板1200g', (SELECT category_id FROM product_category WHERE category_code = '010101'), 4, 14),
('01010116', '正度灰板1500g', (SELECT category_id FROM product_category WHERE category_code = '010101'), 4, 15),
('01010117', '正度灰板1800g', (SELECT category_id FROM product_category WHERE category_code = '010101'), 4, 16),
('01010118', '正度灰板2000g', (SELECT category_id FROM product_category WHERE category_code = '010101'), 4, 17),
('01010119', '正度灰板2500g', (SELECT category_id FROM product_category WHERE category_code = '010101'), 4, 18),
('01010120', '正度灰板3000g', (SELECT category_id FROM product_category WHERE category_code = '010101'), 4, 19),
('01010121', '正度灰板1300g', (SELECT category_id FROM product_category WHERE category_code = '010101'), 4, 20);

INSERT OR IGNORE INTO product_category ( category_code, category_name, parent_id, level, sort_order) VALUES
('01010201', '大度灰板250g',   (SELECT category_id FROM product_category WHERE category_code = '010102'), 4, 1),
('01010202', '大度灰板350g',   (SELECT category_id FROM product_category WHERE category_code = '010102'), 4, 2),
('01010203', '大度灰板A350g', (SELECT category_id FROM product_category WHERE category_code = '010102'), 4, 3),
('01010204', '大度灰板450g',   (SELECT category_id FROM product_category WHERE category_code = '010102'), 4, 4),
('01010205', '大度灰板500g',   (SELECT category_id FROM product_category WHERE category_code = '010102'), 4, 5),
('01010206', '大度灰板560g',   (SELECT category_id FROM product_category WHERE category_code = '010102'), 4, 6),
('01010207', '大度灰板600g',   (SELECT category_id FROM product_category WHERE category_code = '010102'), 4, 7),
('01010208', '大度灰板650g',   (SELECT category_id FROM product_category WHERE category_code = '010102'), 4, 8),
('01010209', '大度灰板760g',      (SELECT category_id FROM product_category WHERE category_code = '010102'), 4, 9),
('01010210', '大度灰板800g',      (SELECT category_id FROM product_category WHERE category_code = '010102'), 4, 10),
('01010211', '大度灰板A1000g',  (SELECT category_id FROM product_category WHERE category_code = '010102'), 4, 11),
('01010212', '大度灰板A850g',    (SELECT category_id FROM product_category WHERE category_code = '010102'), 4, 12),
('01010213', '大度灰板足1000g', (SELECT category_id FROM product_category WHERE category_code = '010102'), 4, 13),
('01010214', '大度灰板1200g',    (SELECT category_id FROM product_category WHERE category_code = '010102'), 4, 14);

