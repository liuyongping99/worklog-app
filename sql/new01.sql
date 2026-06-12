-- ============================================
DROP TABLE IF EXISTS product;

CREATE TABLE product (
    product_id        BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '商品ID',
    product_code      VARCHAR(50) NOT NULL COMMENT '商品编号',
    product_name      VARCHAR(200) NOT NULL COMMENT '商品全名',
    category_id       BIGINT NOT NULL COMMENT '所属分类ID（叶子节点）',
    specification     VARCHAR(100) COMMENT '规格',
    model             VARCHAR(100) COMMENT '型号',
    barcode           VARCHAR(50) COMMENT '基本条码',
    cost_price        INTEGER COMMENT '以分为计算单位单价',
    base_unit         VARCHAR(50) COMMENT '主单位',
    aux_unit          VARCHAR(50) COMMENT '辅助单位',
    conversion_rate   VARCHAR(100) COMMENT '换算关系',
    stock_quantity    INTEGER COMMENT '库存数量*100后保存',
    aux_quantity      VARCHAR(100) COMMENT '辅助数量',
    preset_price      DECIMAL(18,4) COMMENT '预设售价',
    status            TINYINT DEFAULT 1 COMMENT '0禁用 1启用',
    created_at        DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at        DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    FOREIGN KEY (category_id) REFERENCES product_category(category_id),
    UNIQUE KEY uk_product_code (product_code),
    INDEX idx_category (category_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='商品表';

-- ============================================
-- 商品分类表（邻接表模型）- 完整版含所有节点
-- ============================================
DROP TABLE IF EXISTS product_category;

CREATE TABLE product_category (
    category_id       BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '分类ID',
    category_code     VARCHAR(50) NOT NULL COMMENT '分类编码',
    category_name     VARCHAR(100) NOT NULL COMMENT '分类名称',
    parent_id         BIGINT DEFAULT NULL COMMENT '父分类ID，NULL为根节点',
    level             TINYINT NOT NULL DEFAULT 1 COMMENT '层级：1=大类, 2=中类, 3=小类, 4=细类',
    sort_order        INT DEFAULT 0 COMMENT '同级排序',
    status            TINYINT DEFAULT 1 COMMENT '0禁用 1启用',
    created_at        DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at        DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    FOREIGN KEY (parent_id) REFERENCES product_category(category_id),
    UNIQUE KEY uk_code (category_code),
    INDEX idx_parent (parent_id),
    INDEX idx_level (level)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='商品分类表';

-- ============================================
-- 插入数据 - 第1层：根节点
-- ============================================
INSERT INTO product_category (category_id, category_code, category_name, parent_id, level, sort_order) VALUES
(1, '0', '商品', NULL, 1, 0);

-- 重置自增值，避免后续冲突
ALTER TABLE product_category AUTO_INCREMENT = 100;

-- ============================================
-- 第2层：大类
-- ============================================
SELECT category_id INTO @parent_id FROM product_category WHERE category_code = '0' LIMIT 1;

INSERT INTO product_category (category_code, category_name, parent_id, level, sort_order) VALUES
('01', '纸类',    @parent_id, 2, 1),
('02', '辅料类', @parent_id, 2, 2),
('03', '胶粘类', @parent_id, 2, 3),
('04', '防潮类', @parent_id, 2, 4),
('05', '棉类',    @parent_id, 2, 5),
('06', '塑料类', @parent_id, 2, 6),
('07', '特材类', @parent_id, 2, 7),
('08', '其它类', @parent_id, 2, 8),
('09', '处理货', @parent_id, 2, 9);

-- ============================================
-- 第3层：中类（纸类下）
-- ============================================
SELECT category_id INTO @parent_id FROM product_category WHERE category_code = '01' LIMIT 1;

INSERT INTO product_category (category_code, category_name, parent_id, level, sort_order) VALUES
('0101', '灰板类',  @parent_id, 3, 1),
('0102', '白板类',  @parent_id, 3, 2),
('0103', '塞包纸',  @parent_id, 3, 3),
('0104', '皮糠纸',  @parent_id, 3, 4),
('0105', '日本纸',  @parent_id, 3, 5),
('0106', '快巴纸',  @parent_id, 3, 6),   -- 修正：移除了多余的逗号
('0107', '拷贝纸', @parent_id, 3, 7),
('0108', '腊光纸', @parent_id, 3, 8),
('0109', '牛皮纸', @parent_id, 3, 9),
('0110', '牛卡纸', @parent_id, 3, 10),
('0111', '铜版纸', @parent_id, 3, 11),
('0112', '双面白', @parent_id, 3, 12),
('0113', '书写纸', @parent_id, 3, 13),
('0114', '黑卡纸', @parent_id, 3, 14),
('0115', '雪梨纸', @parent_id, 3, 15);

-- ============================================
-- 第3层：中类（辅料类下）
-- ============================================
SELECT category_id INTO @parent_id FROM product_category WHERE category_code = '02' LIMIT 1;

INSERT INTO product_category (category_code, category_name, parent_id, level, sort_order) VALUES
('0201', '杂胶',    @parent_id, 3, 1),
('0202', '三文治', @parent_id, 3, 2),
('0203', '露华里', @parent_id, 3, 3),
('0204', '无纺布', @parent_id, 3, 4),
('0205', '回力胶', @parent_id, 3, 5),
('0206', '弹力胶', @parent_id, 3, 6),
('0207', '泥胶',    @parent_id, 3, 7),
('0209', '高发泡（轻胶）', @parent_id, 3, 8),
('0210', '7PPVC人造革', @parent_id, 3, 9),
('0211', 'A级杂胶',     @parent_id, 3, 10),
('0212', '磅布三文治', @parent_id, 3, 11),
('0208', '潜水料',       @parent_id, 3, 12);

-- 第3层：中类（胶粘类大类下）
SELECT category_id INTO @parent_id FROM product_category WHERE category_code = '03' LIMIT 1;

INSERT INTO product_category (category_code, category_name, parent_id, level, sort_order) VALUES
('0301', '喷胶',          @parent_id, 3, 1),
('0302', '万能胶',       @parent_id, 3, 2),
('0303', '粉胶',          @parent_id, 3, 3),
('0304', '接枝胶',       @parent_id, 3, 4),
('0305', '白胶',          @parent_id, 3, 5),
('0306', '白油',          @parent_id, 3, 6),
('0307', '伟明',          @parent_id, 3, 7),
('0308', '衣车油',       @parent_id, 3, 8),
('0309', '七B水',        @parent_id, 3, 9),
('0310', '双面胶',       @parent_id, 3, 10),
('0311', '封箱胶',       @parent_id, 3, 11),
('0312', '文具胶',       @parent_id, 3, 12),
('0313', '美纹纸',       @parent_id, 3, 13),
('0314', '牛皮胶',       @parent_id, 3, 14),
('0315', '特殊用途胶', @parent_id, 3, 15),
('0316', '补强带',       @parent_id, 3, 16);

-- 第3层：中类（防潮类大类下）
SELECT category_id INTO @parent_id FROM product_category WHERE category_code = '04' LIMIT 1;

INSERT INTO product_category (category_code, category_name, parent_id, level, sort_order) VALUES
('0401', '防潮珠', @parent_id, 3, 1),
('0402', '防霉片', @parent_id, 3, 2),
('0403', '防霉纸', @parent_id, 3, 3),
('0404', '香片',    @parent_id, 3, 4);

-- 第3层：中类（棉类大类下）
SELECT category_id INTO @parent_id FROM product_category WHERE category_code = '05' LIMIT 1;

INSERT INTO product_category (category_code, category_name, parent_id, level, sort_order) VALUES
('0501', '珍珠棉', @parent_id, 3, 1),
('0502', '海绵', @parent_id, 3, 2),
('0503', '双面水刺布', @parent_id, 3, 3),
('0504', '丝绵', @parent_id, 3, 4),
('0505', '针棉', @parent_id, 3, 5),
('0506', '水刺布（棉朴）', @parent_id, 3, 6),
('0507', '棉绳', @parent_id, 3, 7),
('0508', '棉通', @parent_id, 3, 8),
('0509', '海王', @parent_id, 3, 9),
('0510', '单面水刺布', @parent_id, 3, 10),
('0511', '环保托', @parent_id, 3, 11),
('0512', '成品水刺布袋', @parent_id, 3, 12),
('0513', '棉加工', @parent_id, 3, 13);

-- 第3层：中类（塑料类大类下）
SELECT category_id INTO @parent_id FROM product_category WHERE category_code = '06' LIMIT 1;

INSERT INTO product_category (category_code, category_name, parent_id, level, sort_order) VALUES
('0601', 'PE板',       @parent_id, 3, 1),
('0602', 'PVC胶片', @parent_id, 3, 2),
('0603', '胶骨',       @parent_id, 3, 3),
('0604', '胶通',       @parent_id, 3, 4),
('0605', '胶针',       @parent_id, 3, 5),
('0606', 'PVC软膜', @parent_id, 3, 6),
('0607', '高发泡XPE', @parent_id, 3, 7),
('0608', 'EVA软膜', @parent_id, 3, 8),
('0609', '蜂巢板',   @parent_id, 3, 9),
('0610', '牛津布',   @parent_id, 3, 10),
('0611', '烫布',      @parent_id, 3, 11),
('0612', '仿超',      @parent_id, 3, 12),
('0613', '里布',      @parent_id, 3, 13),
('0614', 'TA特软',  @parent_id, 3, 14),
('0615', 'TP',         @parent_id, 3, 15),
('0616', 'TQ',        @parent_id, 3, 16),
('0617', 'TR',         @parent_id, 3, 17);

-- ============================================
-- 第3层：中类（特材类大类下）
-- ============================================
SELECT category_id INTO @parent_id FROM product_category WHERE category_code = '07' LIMIT 1;

INSERT INTO product_category (category_code, category_name, parent_id, level, sort_order) VALUES
('0701', '猪皮纹HA',      @parent_id, 3, 1),
('0702', '猪皮纹HC',      @parent_id, 3, 2),
('0703', 'LB鱼鳞布特软', @parent_id, 3, 3),
('0704', 'LC鱼鳞布特软', @parent_id, 3, 4),
('0705', 'QP特软',          @parent_id, 3, 5),
('0706', 'LD特软三文治', @parent_id, 3, 6),
('0707', '牛津布',           @parent_id, 3, 7),
('0708', 'GA',                @parent_id, 3, 8),
('0709', 'TA 特软',         @parent_id, 3, 9),
('0710', '烫布',              @parent_id, 3, 10),
('0711', '仿超',              @parent_id, 3, 11),
('0712', '里布',              @parent_id, 3, 12),
('0713', 'HT',                @parent_id, 3, 13),
('0714', 'TQ',                @parent_id, 3, 14),
('0715', 'TK',                 @parent_id, 3, 15);

-- ============================================
-- 第3层：中类（其它类大类下）
-- ============================================
SELECT category_id INTO @parent_id FROM product_category WHERE category_code = '08' LIMIT 1;

INSERT INTO product_category (category_code, category_name, parent_id, level, sort_order) VALUES
('0817', '编织袋', @parent_id, 3, 17);

-- ============================================
-- 第4层：小类（灰板类下）
-- ============================================
SELECT category_id INTO @parent_id FROM product_category WHERE category_code = '0101' LIMIT 1;

INSERT INTO product_category (category_code, category_name, parent_id, level, sort_order) VALUES
('010101', '华实正度',      @parent_id, 4, 1),
('010102', '华实大度',      @parent_id, 4, 1),
('010103', 'A级正度灰板', @parent_id, 4, 2),
('010104', 'A级大度灰板', @parent_id, 4, 3),
('010105', '加工后纸',      @parent_id, 4, 4);

-- ============================================
-- 第4层：小类（白板类下）
-- ============================================
SELECT category_id INTO @parent_id FROM product_category WHERE category_code = '0102' LIMIT 1;

INSERT INTO product_category (category_code, category_name, parent_id, level, sort_order) VALUES
('010201', '正度白板', @parent_id, 4, 1),
('010202', '大度白板', @parent_id, 4, 2),
('010203', '卷筒白板', @parent_id, 4, 3);

-- ============================================
-- 第4层：小类（皮糠纸下）
-- ============================================
SELECT category_id INTO @parent_id FROM product_category WHERE category_code = '0104' LIMIT 1;

INSERT INTO product_category (category_code, category_name, parent_id, level, sort_order) VALUES
('010401', '常规698',        @parent_id, 4, 1),
('010402', '豪博正品698',  @parent_id, 4, 2),
('010403', '398', @parent_id, 4, 3),
('010404', '222', @parent_id, 4, 4),
('010405', '268', @parent_id, 4, 5),
('010406', '意大利进口', @parent_id, 4, 6),
('010407', '普通再生革', @parent_id, 4, 7),
('010408', '环保再生革', @parent_id, 4, 8),
('010409', '888', @parent_id, 4, 9),
('010410', '加硬皮糠纸698', @parent_id, 4, 10);

-- ============================================
-- 第4层：小类（无纺布下）
-- ============================================
SELECT category_id INTO @parent_id FROM product_category WHERE category_code = '0204' LIMIT 1;

INSERT INTO product_category (category_code, category_name, parent_id, level, sort_order) VALUES
('020401', 'B白', @parent_id, 4, 1),
('020402', 'B黑', @parent_id, 4, 2),
('020403', 'BD白', @parent_id, 4, 3),
('020404', 'B3黑', @parent_id, 4, 4),
('020405', 'A白', @parent_id, 4, 5),
('020406', 'A黑', @parent_id, 4, 6),
('020407', '按支算无纺布', @parent_id, 4, 7),
('020408', '自粘无纺布', @parent_id, 4, 8),
('020409', '彩色无纺布', @parent_id, 4, 9);

-- ============================================
-- 第4层：小类（回力胶下）
-- ============================================
SELECT category_id INTO @parent_id FROM product_category WHERE category_code = '0205' LIMIT 1;

INSERT INTO product_category (category_code, category_name, parent_id, level, sort_order) VALUES
('020501', 'A料1.4', @parent_id, 4, 1),
('020502', 'A料1.5', @parent_id, 4, 2),
('020503', 'C料1.4', @parent_id, 4, 3),
('020504', 'C料1.5', @parent_id, 4, 4),
('020505', '加硬黑55度1.4', @parent_id, 4, 5),
('020506', '加硬黑55度1.5', @parent_id, 4, 6),
('020507', '卷材黑色按支', @parent_id, 4, 7),
('020508', '卷材白色（斤）', @parent_id, 4, 8),
('020509', '卷材白色（码）', @parent_id, 4, 9),
('020510', '硬片', @parent_id, 4, 10),
('020511', '硬片黑色', @parent_id, 4, 11),
('020512', '卷材白色（码）', @parent_id, 4, 12),
('020513', '定做', @parent_id, 4, 13);

-- ============================================
-- 第4层：小类（弹力胶下）
-- ============================================
SELECT category_id INTO @parent_id FROM product_category WHERE category_code = '0206' LIMIT 1;

INSERT INTO product_category (category_code, category_name, parent_id, level, sort_order) VALUES
('020601', '弹力胶1.4', @parent_id, 4, 1),
('020602', '弹力胶1.5', @parent_id, 4, 2),
('020603', '硬弹力胶', @parent_id, 4, 3),
('020604', 'RA弹力胶', @parent_id, 4, 4),
('020605', '订做弹力胶', @parent_id, 4, 5);

-- ============================================
-- 第4层：小类（7PPVC人造革下）
-- ============================================
SELECT category_id INTO @parent_id FROM product_category WHERE category_code = '0209' LIMIT 1;

INSERT INTO product_category (category_code, category_name, parent_id, level, sort_order) VALUES
('020901', '7P环保杂胶', @parent_id, 4, 1),
('020902', '7P环保三文治', @parent_id, 4, 2),
('020903', '7P环保磅布三文治', @parent_id, 4, 3),
('020904', '7P环保磅布杂胶', @parent_id, 4, 4),
('020905', '7P环保纯胶', @parent_id, 4, 5),
('020906', '7P环保路华里', @parent_id, 4, 6),
('020907', '7P环保LB鱼鳞布', @parent_id, 4, 7),
('020908', '7P环保HA猪皮纹', @parent_id, 4, 8);

-- ============================================
-- 第4层：小类（A级杂胶下）
-- ============================================
SELECT category_id INTO @parent_id FROM product_category WHERE category_code = '0210' LIMIT 1;

INSERT INTO product_category (category_code, category_name, parent_id, level, sort_order) VALUES
('021001', '中性纯胶', @parent_id, 4, 1),
('021002', '磅布杂胶', @parent_id, 4, 2),
('021003', '鱼麟布纯胶', @parent_id, 4, 3),
('021004', '水刺布杂胶（毛粗）', @parent_id, 4, 4),
('021005', '软性纯胶', @parent_id, 4, 5),
('021006', '硬性纯胶', @parent_id, 4, 6),
('021007', '中软纯胶', @parent_id, 4, 7),
('021008', '中硬纯胶', @parent_id, 4, 8),
('021009', '高弹中性纯胶', @parent_id, 4, 9),
('021010', '高弹软性纯胶', @parent_id, 4, 10),
('021011', '高弹（硬性/特硬）纯胶', @parent_id, 4, 11),
('021012', '高弹中软纯胶', @parent_id, 4, 12),
('021013', '真高弹（特软）纯胶', @parent_id, 4, 13),
('021014', '高弹中硬纯胶', @parent_id, 4, 14),
('021015', '订做 有色纯胶', @parent_id, 4, 15);

-- ============================================
-- 第4层：小类（磅布三文治下）
-- ============================================
SELECT category_id INTO @parent_id FROM product_category WHERE category_code = '0211' LIMIT 1;

INSERT INTO product_category (category_code, category_name, parent_id, level, sort_order) VALUES
('021101', '白磅布三文治', @parent_id, 4, 1),
('021102', '黑磅布三文治', @parent_id, 4, 2),
('021103', 'B级 磅布三文治', @parent_id, 4, 3);

-- ============================================
-- 第4层：小类（潜水料下）
-- ============================================
SELECT category_id INTO @parent_id FROM product_category WHERE category_code = '0208' LIMIT 1;

INSERT INTO product_category (category_code, category_name, parent_id, level, sort_order) VALUES
('021201', '潜水料', @parent_id, 4, 1),
('021202', 'B级潜水料', @parent_id, 4, 2),
('021203', '环保潜水料', @parent_id, 4, 3);


-- 第4层：小类（珍珠棉下）
SELECT category_id INTO @parent_id FROM product_category WHERE category_code = '0501' LIMIT 1;

INSERT INTO product_category ( category_code, category_name, parent_id, level, sort_order) VALUES
( '050101', '白色珍珠棉', @parent_id, 4, 1),
( '050102', '黑色珍珠棉', @parent_id, 4, 2),
( '050103', '铝膜珍珠棉', @parent_id, 4, 3),
( '050104', '加密珍珠棉', @parent_id, 4, 4);

-- 第4层：小类（海绵下）
INSERT INTO product_category ( category_code, category_name, parent_id, level, sort_order) VALUES
( '050201', '卷装海绵329（加密）', @parent_id, 4, 1),
( '050202', '特粗海绵', @parent_id, 4, 2),
( '050203', '卷装海绵330（高弹）', @parent_id, 4, 3),
( '050204', '卷装海绵324（中密）', @parent_id, 4, 4),
( '050205', '称斤高弹海绵', @parent_id, 4, 5);


-- 第4层：小类（双面水刺布下）
INSERT INTO product_category ( category_code, category_name, parent_id, level, sort_order) VALUES
( '050301', '双面水刺布-白色卷装', @parent_id, 4, 1),
( '050302', '双面片装水刺布', @parent_id, 4, 2),
( '050303', '双面水刺布-黑色卷装', @parent_id, 4, 3),
( '050304', '双面水刺布-短幅', @parent_id, 4, 4),
( '050305', '双面水刺布-A级卷装', @parent_id, 4, 5);


-- 第4层：小类（针棉下）
INSERT INTO product_category ( category_code, category_name, parent_id, level, sort_order) VALUES
( '050501', '蓬松针棉', @parent_id, 4, 1),
( '050502', '加密针棉', @parent_id, 4, 2),
( '050503', '高密度针棉', @parent_id, 4, 3);


-- 第4层：小类（棉绳下）
INSERT INTO product_category (category_id, category_code, category_name, parent_id, level, sort_order) VALUES
( '050701', '普通无弹力棉绳', @parent_id, 4, 1),
( '050702', '本白白心棉绳', @parent_id, 4, 2),
( '050703', '本白黑心棉绳', @parent_id, 4, 3),
( '050704', '灰白黑心棉绳', @parent_id, 4, 4);


-- 第4层：小类（棉加工下）
INSERT INTO product_category (category_id, category_code, category_name, parent_id, level, sort_order) VALUES
( '051301', '珍珠棉加工', @parent_id, 4, 1),
( '051302', '海绵加工',    @parent_id, 4, 2);

-- ============================================
-- 商品信息
-- ============================================