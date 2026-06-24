-- ============================================
DROP TABLE IF EXISTS product;

-- ============================================
-- 商品分类表（邻接表模型）- 完整版含所有节点
-- ============================================
DROP TABLE IF EXISTS product_category;

CREATE TABLE product_category (
    category_id          BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '分类ID',
    category_code     VARCHAR(50) NOT NULL COMMENT '分类编码',
    category_name    VARCHAR(100) NOT NULL COMMENT '分类名称',
    parent_id             BIGINT DEFAULT NULL COMMENT '父分类ID，NULL为根节点',
    level                    TINYINT NOT NULL DEFAULT 1 COMMENT '层级：1=大类, 2=中类, 3=小类, 4=细类',
    sort_order           INT DEFAULT 0 COMMENT '同级排序',
    status                 TINYINT DEFAULT 1 COMMENT '0禁用 1启用',
    created_at          DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at         DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    FOREIGN KEY (parent_id) REFERENCES product_category(category_id),
    UNIQUE KEY uk_code (category_code),
    INDEX idx_parent (parent_id),
    INDEX idx_level (level)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='商品分类表';

CREATE TABLE product (
    product_id        
BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '商品ID',
    product_code      
VARCHAR(50) NOT NULL COMMENT '商品编号',
    product_name      
VARCHAR(200) NOT NULL COMMENT '商品全名',
    category_id       
BIGINT NOT NULL COMMENT '所属分类ID（叶子节点）',
    specification     
VARCHAR(100) COMMENT '规格',
    model
VARCHAR(100) COMMENT '型号',
    barcode           
VARCHAR(50) COMMENT '基本条码',
    cost_price           
INTEGER COMMENT '以分为计算单位单价',
    base_unit       
VARCHAR(50) COMMENT '主单位',
    aux_unit     
VARCHAR(50) COMMENT '辅助单位',
    conversion_rate   
VARCHAR(100) COMMENT '换算关系',
    stock_quantity    
INTEGER COMMENT '库存数量*100后保存',
    aux_quantity      
VARCHAR(100) COMMENT '辅助数量',
    preset_price      
DECIMAL(18,4) COMMENT '预设售价',
    status            TINYINT DEFAULT 1 COMMENT '0禁用 1启用',
    created_at        
DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at        
DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    FOREIGN KEY (category_id) REFERENCES product_category(category_id),
    UNIQUE KEY uk_product_code (product_code),
    INDEX idx_category (category_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='商品表';


-- ============================================
-- 插入数据 - 第1层：根节点
-- ============================================
INSERT INTO product_category (category_id, category_code, category_name, parent_id, level, sort_order) VALUES
(1, '0', '商品', NULL, 1, 0);

ALTER TABLE product_category AUTO_INCREMENT = 100;

-- ============================================
-- 第2层：大类
-- ============================================
SET @category_id = NULL;
SELECT @category_id := category_id FROM product_category WHERE category_code = '0'  LIMIT 1;

INSERT INTO product_category (category_code, category_name, parent_id, level, sort_order) VALUES
( '01', '纸类',    @category_id, 2, 1),
( '02', '辅料类', @category_id, 2, 2),
( '03', '胶粘类', @category_id, 2, 3),
( '04', '防潮类', @category_id, 2, 4),
( '05', '棉类',    @category_id, 2, 5),
( '06', '塑料类', @category_id, 2, 6),
( '07', '特材类', @category_id, 2, 7),
( '08', '其它类', @category_id, 2, 8),
( '09', '处理货', @category_id, 2, 9);

-- ============================================
-- 第3层：中类（纸类下）
-- ============================================
SET @category_id = NULL;
SELECT @category_id := category_id FROM product_category WHERE category_code = '01'  LIMIT 1;

INSERT INTO product_category ( category_code, category_name, parent_id, level, sort_order) VALUES
('0101', '灰板类',  @category_id, 3, 1),
('0102', '白板类',  @category_id, 3, 2),
('0103', '塞包纸',  @category_id, 3, 3),
('0104', '皮糠纸',  @category_id, 3, 4),
('0105', '日本纸',  @category_id, 3, 5),
('0106', '快巴纸',  @category_id, 3, 6),
( '0107', '拷贝纸', @category_id, 3, 7),
( '0108', '腊光纸', @category_id, 3, 8),
( '0109', '牛皮纸', @category_id, 3, 9),
( '0110', '牛卡纸', @category_id, 3, 10),
( '0111', '铜版纸', @category_id, 3, 11),
( '0112', '双面白', @category_id, 3, 12),
( '0113', '书写纸', @category_id, 3, 13),
( '0114', '黑卡纸', @category_id, 3, 14),
( '0115', '雪梨纸', @category_id, 3, 15);

-- ============================================
-- 第3层：中类（辅料类下）
-- ============================================
SET @category_id = NULL;
SELECT @category_id := category_id FROM product_category WHERE category_code = '02'  LIMIT 1;

INSERT INTO product_category (category_code, category_name, parent_id, level, sort_order) VALUES
( '0201', '杂胶',    @category_id, 3, 1),
( '0202', '三文治', @category_id, 3, 2),
( '0203', '露华里', @category_id, 3, 3),
( '0204', '无纺布', @category_id, 3, 4),
( '0205', '回力胶', @category_id, 3, 5),
( '0206', '弹力胶', @category_id, 3, 6),
( '0207', '泥胶',    @category_id, 3, 7),
( '0209', '高发泡（轻胶）', @category_id, 3, 8),
( '0210', '7PPVC人造革', @category_id, 3, 9),
( '0211', 'A级杂胶',     @category_id, 3, 10),
( '0212', '磅布三文治', @category_id, 3, 11),
( '0208', '潜水料',       @category_id, 3, 12);

-- 第3层：中类（胶粘类大类下）
SET @category_id = NULL;
SELECT @category_id := category_id FROM product_category WHERE category_code = '03'  LIMIT 1;

INSERT INTO product_category (category_code, category_name, parent_id, level, sort_order) VALUES
( '0301', '喷胶',          @category_id, 3, 1),
( '0302', '万能胶',       @category_id, 3, 2),
( '0303', '粉胶',          @category_id, 3, 3),
( '0304', '接枝胶',       @category_id, 3, 4),
( '0305', '白胶',          @category_id, 3, 5),
( '0306', '电油',          @category_id, 3, 6),
( '0307', '伟明',          @category_id, 3, 7),
( '0308', '衣车油',       @category_id, 3, 8),
( '0309', '七B水',        @category_id, 3, 9),
( '0311', '双面胶',       @category_id, 3, 10),
( '0312', '封箱胶',       @category_id, 3, 11),
( '0313', '文具胶',       @category_id, 3, 12),
( '0314', '美纹纸',       @category_id, 3, 13),
( '0315', '牛皮胶',       @category_id, 3, 14),
( '0319', '特殊用途胶', @category_id, 3, 15),
( '0320', '补强带',       @category_id, 3, 16);

-- 第3层：中类（防潮类大类下）
SET @category_id = NULL;
SELECT @category_id := category_id FROM product_category WHERE category_code = '04'  LIMIT 1;

INSERT INTO product_category ( category_code, category_name, parent_id, level, sort_order) VALUES
( '0401', '防潮珠', @category_id, 3, 1),
( '0402', '防霉片', @category_id, 3, 2),
( '0403', '防霉纸', @category_id, 3, 3),
( '0405', '香片',    @category_id, 3, 4);

-- 第3层：中类（棉类大类下）
SET @category_id = NULL;
SELECT @category_id := category_id FROM product_category WHERE category_code = '05'  LIMIT 1;

INSERT INTO product_category (category_code, category_name, parent_id, level, sort_order) VALUES
( '0501', '珍珠棉',       @category_id, 3, 1),
( '0502', '海绵',          @category_id, 3, 2),
( '0503', '珍珠棒',        @category_id, 3, 2),
( '0504', '双面水刺布', @category_id, 3, 3),
( '0505', '丝绵',          @category_id, 3, 4),
( '0506', '针棉',          @category_id, 3, 5),
( '0507', '水刺布（棉朴）', @category_id, 3, 6),
( '0508', '棉绳',        @category_id, 3, 7),
( '0509', '棉通',        @category_id, 3, 8),
( '0510', '港宝',       @category_id, 3, 9),
( '0511', '单面水刺布', @category_id, 3, 10),
( '0512', '环保托',       @category_id, 3, 11),
( '0513', '成品水刺布袋', @category_id, 3, 12),
( '0514', '棉加工',       @category_id, 3, 13);

-- 第3层：中类（塑料类大类下）
SET @category_id = NULL;
SELECT @category_id := category_id FROM product_category WHERE category_code = '06'  LIMIT 1;

INSERT INTO product_category ( category_code, category_name, parent_id, level, sort_order) VALUES
( '0601', 'PE板',       @category_id, 3, 1),
( '0602', 'PP板',       @category_id, 3, 2),
( '0603', 'PVC胶片', @category_id, 3, 3),
( '0604', '胶骨',       @category_id, 3, 4),
( '0605', '胶通',       @category_id, 3, 5),
( '0606', '胶针',       @category_id, 3, 6),
( '0607', 'PVC软膜', @category_id, 3, 7),
( '0608', '高发泡XPE', @category_id, 3, 8),
( '0609', 'EVA软膜',  @category_id, 3, 9),
( '0610', '蜂巢板',     @category_id, 3, 10);

-- ============================================
-- 第3层：中类（特材类大类下）
-- ============================================
SET @category_id = NULL;
SELECT @category_id := category_id FROM product_category WHERE category_code = '07'  LIMIT 1;

INSERT INTO product_category ( category_code, category_name, parent_id, level, sort_order) VALUES
('0701', '猪皮纹HA',      @category_id, 3, 1),
('0702', '猪皮纹HC',      @category_id, 3, 2),
('0703', 'LB鱼鳞布特软', @category_id, 3, 3),
('0704', 'LC鱼鳞布特软', @category_id, 3, 4),
('0705', 'QB特软',          @category_id, 3, 5),
('0706', 'LD特软三文治', @category_id, 3, 6),
('0707', '牛津布',           @category_id, 3, 7),
('0708', 'GA',                @category_id, 3, 8),
('0709', 'TA 特软',         @category_id, 3, 9),
('0710', '烫布',              @category_id, 3, 10),
('0711', '仿超',              @category_id, 3, 11),
('0712', '里布',              @category_id, 3, 12),
('0713', 'HT',                @category_id, 3, 13),
('0714', 'TQ',                @category_id, 3, 14),
('0715', 'TK',                 @category_id, 3, 15);

-- ============================================
-- 第3层：中类（其它类大类下）
-- ============================================
SET @category_id = NULL;
SELECT @category_id := category_id FROM product_category WHERE category_code = '08'  LIMIT 1;

INSERT INTO product_category (category_code, category_name, parent_id, level, sort_order) VALUES
('0817', '编织袋', @category_id, 3, 17);

-- ============================================
-- 第4层：小类（灰板类下）
-- ============================================
SET @category_id = NULL;
SELECT @category_id := category_id FROM product_category WHERE category_code = '0101'  LIMIT 1;

INSERT INTO product_category ( category_code, category_name, parent_id, level, sort_order) VALUES
( '010101', '华实正度',      @category_id, 4, 1),
( '010102', '华实大度',      @category_id, 4, 2),
( '010103', 'A级正度灰板', @category_id, 4, 3),
( '010104', 'A级大度灰板', @category_id, 4, 4),
( '010105', '加工后纸',      @category_id, 4, 5);

-- ============================================
-- 第4层：小类（白板类下）
-- ============================================
SET @category_id = NULL;
SELECT @category_id := category_id FROM product_category WHERE category_code = '0102'  LIMIT 1;

INSERT INTO product_category ( category_code, category_name, parent_id, level, sort_order) VALUES
('010201', '正度白板',   @category_id, 4, 1),
('010202', '大度白板',   @category_id, 4, 2),
('010203', '卷筒白板',   @category_id, 4, 3);


-- ============================================
-- 第4层：小类（皮糠纸下）
-- ============================================
SET @category_id = NULL;
SELECT @category_id := category_id FROM product_category WHERE category_code = '0104'  LIMIT 1;

INSERT INTO product_category ( category_code, category_name, parent_id, level, sort_order) VALUES
('010401', '常规698',        @category_id, 4, 1),
('010402', '豪博正品698',  @category_id, 4, 2),
('010403', '398',               @category_id, 4, 3),
('010404', '222',               @category_id, 4, 4),
('010405', '268',               @category_id, 4, 5),
('010406', '888',              @category_id, 4, 6),
('010407', '意大利进口',    @category_id, 4, 7),
('010408', '普通再生革',    @category_id, 4, 8),
('010409', '环保再生革',    @category_id, 4, 9),
('010410', '加硬皮糠纸698', @category_id, 4, 10);


-- ============================================
-- 第4层：小类（无纺布下）- 根据第三张截图补充
-- ============================================
SET @category_id = NULL;
SELECT @category_id := category_id FROM product_category WHERE category_code = '0204'  LIMIT 1;

INSERT INTO product_category ( category_code, category_name, parent_id, level, sort_order) VALUES
('020401', 'B白',      @category_id, 4, 1),
('020402', 'B黑',      @category_id, 4, 2),
('020403', 'BD白',   @category_id, 4, 3),
('020404', 'B3黑',   @category_id, 4, 4),
('020405', 'A白',     @category_id, 4, 5),
('020406', 'A黑',     @category_id, 4, 6),
('020407', '按支算无纺布', @category_id, 4, 7),
('020408', '自粘无纺布',    @category_id, 4, 8),
('020409', '彩色无纺布',    @category_id, 4, 9);


-- ============================================
-- 第4层：小类（回力胶下）- 根据第三张截图补充
-- ============================================
SET @category_id = NULL;
SELECT @category_id := category_id FROM product_category WHERE category_code = '0205'  LIMIT 1;

INSERT INTO product_category (category_code, category_name, parent_id, level, sort_order) VALUES
( '020501', 'A料1.4',       @category_id, 4, 1),
( '020502', 'A料1.5',       @category_id, 4, 2),
( '020503', 'C料1.4',       @category_id, 4, 3),
( '020504', 'C料1.5',       @category_id, 4, 4),
( '020505', '加硬黑55度1.4',   @category_id, 4, 5),
( '020506', '加硬黑55度1.5',   @category_id, 4, 6),
( '020507', '卷材黑色按支',    @category_id, 4, 7),
( '020508', '卷材白色（斤）', @category_id, 4, 8),
( '020509', '卷材白色（码）', @category_id, 4, 9),
( '020510', '硬片',                 @category_id, 4, 10),
( '020511', '硬片黑色',          @category_id, 4, 11),
( '020512', '刀模胶',              @category_id, 4, 12),
( '020513', '定做',                 @category_id, 4, 13);


-- ============================================
-- 第4层：小类（弹力胶下）- 根据第三张截图补充
-- ============================================
SET @category_id = NULL;
SELECT @category_id := category_id FROM product_category WHERE category_code = '0206'  LIMIT 1;

INSERT INTO product_category ( category_code, category_name, parent_id, level, sort_order) VALUES
( '020601', '弹力胶1.4',   @category_id, 4, 1),
( '020602', '弹力胶1.5',   @category_id, 4, 2),
( '020603', '硬弹力胶',    @category_id, 4, 3),
( '020604', 'RA弹力胶',   @category_id, 4, 4),
( '020605', '订做弹力胶', @category_id, 4, 5);


-- ============================================
-- 第4层：小类（7PPVC人造革下）- 根据第三张截图补充
-- ============================================
SET @category_id = NULL;
SELECT @category_id := category_id FROM product_category WHERE category_code = '0210'  LIMIT 1;

INSERT INTO product_category ( category_code, category_name, parent_id, level, sort_order) VALUES
( '021001', '7P环保杂胶', @category_id, 4, 1),
( '021002', '7P环保三文治', @category_id, 4, 2),
( '021003', '7P环保磅布三文治', @category_id, 4, 3),
( '021004', '7P环保磅布杂胶', @category_id, 4, 4),
( '021005', '7P环保纯胶', @category_id, 4, 5),
( '021006', '7P环保路华里', @category_id, 4, 6),
( '021007', '7P环保LB鱼鳞布', @category_id, 4, 7),
( '021008', '7P环保HA猪皮纹', @category_id, 4, 8),
( '021009', '7P环保毛底底胶', @category_id, 4, 9),
( '021010', '7P环保里布革', @category_id, 4, 10);

-- 第4层：小类（A级杂胶下）
SET @category_id = NULL;
SELECT @category_id := category_id FROM product_category WHERE category_code = '0211'  LIMIT 1;

INSERT INTO product_category (category_code, category_name, parent_id, level, sort_order) VALUES
( '021101', '中性纯胶',                @category_id, 4, 1),
( '021105', '软性纯胶',                @category_id, 4, 2),
( '021106', '硬性纯胶',                @category_id, 4, 3),
( '021107', '中软纯胶',                @category_id, 4, 4),
( '021108', '中硬纯胶',                @category_id, 4, 5),
( '021109', '高弹中性纯胶',          @category_id, 4, 6),
( '021110', '高弹软性纯胶',          @category_id, 4, 7),
( '021111', '高弹（硬性/特硬）纯胶', @category_id, 4, 8),
( '021112', '高弹中软纯胶',          @category_id, 4, 9),
( '021114', '高弹中硬纯胶',          @category_id, 4, 10),
( '021113', '真高弹（特软）纯胶', @category_id, 4, 11),
( '021102', '磅布杂胶',               @category_id, 4, 12),
( '021103', '鱼麟布纯胶',             @category_id, 4, 13),
( '021104', '水刺布杂胶（毛粗）', @category_id, 4, 14),
( '021115', '订做 有色纯胶',         @category_id, 4, 15);


-- 第4层：小类（磅布三文治下）
SET @category_id = NULL;
SELECT @category_id := category_id FROM product_category WHERE category_code = '0212'  LIMIT 1;

INSERT INTO product_category ( category_code, category_name, parent_id, level, sort_order) VALUES
( '021201', '白磅布三文治',    @category_id, 4, 1),
( '021202', '黑磅布三文治',    @category_id, 4, 2),
( '021203', 'B级 磅布三文治', @category_id, 4, 3);


-- 第4层：小类（潜水料下）
SET @category_id = NULL;
SELECT @category_id := category_id FROM product_category WHERE category_code = '0208'  LIMIT 1;

INSERT INTO product_category ( category_code, category_name, parent_id, level, sort_order) VALUES
( '020801', '潜水料',       @category_id, 4, 1),
( '020802', 'B级潜水料',  @category_id, 4, 2),
( '020803', '环保潜水料', @category_id, 4, 3);

-- 第4层：小类（珍珠棉下）
SET @category_id = NULL;
SELECT @category_id := category_id FROM product_category WHERE category_code = '0501'  LIMIT 1;

INSERT INTO product_category (category_code, category_name, parent_id, level, sort_order) VALUES
( '050101', '白色珍珠棉', @category_id, 4, 1),
( '050102', '黑色珍珠棉', @category_id, 4, 2),
( '050103', '铝膜珍珠棉', @category_id, 4, 3),
( '050104', '加密珍珠棉', @category_id, 4, 4);


-- 第4层：小类（海绵下）
SET @category_id = NULL;
SELECT @category_id := category_id FROM product_category WHERE category_code = '0502'  LIMIT 1;

INSERT INTO product_category ( category_code, category_name, parent_id, level, sort_order) VALUES
( '050201', '卷装海绵329（加密）', @category_id, 4, 1),
( '050204', '卷装海绵324（中密）', @category_id, 4, 2),
( '050203', '卷装海绵330（高弹）', @category_id, 4, 3),
( '050205', '称斤高弹海绵',             @category_id, 4, 4),
( '050202', '特粗海绵',                   @category_id, 4, 5);



-- 第4层：小类（双面水刺布下）
SET @category_id = NULL;
SELECT @category_id := category_id FROM product_category WHERE category_code = '0504'  LIMIT 1;

INSERT INTO product_category (category_code, category_name, parent_id, level, sort_order) VALUES
( '050401', '双面水刺布-白色卷装', @category_id, 4, 1),
( '050402', '双面片装水刺布',         @category_id, 4, 2),
( '050403', '双面水刺布-黑色卷装', @category_id, 4, 3),
( '050404', '双面水刺布-短幅',        @category_id, 4, 4),
( '050405', '双面水刺布-A级卷装',   @category_id, 4, 5);

-- 第4层：小类（针棉下）
SET @category_id = NULL;
SELECT @category_id := category_id FROM product_category WHERE category_code = '0506'  LIMIT 1;

INSERT INTO product_category ( category_code, category_name, parent_id, level, sort_order) VALUES
( '050601', '蓬松针棉',    @category_id, 4, 1),
( '050602', '加密针棉',    @category_id, 4, 2),
( '050603', '高密度针棉', @category_id, 4, 3);


-- 第4层：小类（棉绳下）
SET @category_id = NULL;
SELECT @category_id := category_id FROM product_category WHERE category_code = '0508'  LIMIT 1;

INSERT INTO product_category ( category_code, category_name, parent_id, level, sort_order) VALUES
( '050801', '普通无弹力棉绳', @category_id, 4, 1),
( '050805', '普通弹力棉绳',    @category_id, 4, 1),
( '050802', '本白白心棉绳',    @category_id, 4, 2),
( '050803', '本白黑心棉绳',    @category_id, 4, 3),
( '050804', '灰白黑心棉绳',    @category_id, 4, 4);

-- 第4层：小类（棉加工下）
SET @category_id = NULL;
SELECT @category_id := category_id FROM product_category WHERE category_code = '0514'  LIMIT 1;

INSERT INTO product_category ( category_code, category_name, parent_id, level, sort_order) VALUES
( '051401', '珍珠棉加工', @category_id, 4, 1),
( '051402', '海绵加工',    @category_id, 4, 2);

-- ============================================
-- 第4层：小类（高发泡XPE下）
-- ============================================
SET @category_id = NULL;
SELECT @category_id := category_id FROM product_category WHERE category_code = '0608'  LIMIT 1;

INSERT INTO product_category ( category_code, category_name, parent_id, level, sort_order) VALUES
( '060801', '25-30倍', @category_id, 4, 1),
( '060802', '15-20倍', @category_id, 4, 2),
( '060803', '8-10倍',   @category_id, 4, 3),
( '060804', '3-5倍',     @category_id, 4, 4),
( '060805', 'XPE切片', @category_id, 4, 5);

-- ============================================
-- 第4层：小类（烫布下）
-- ============================================
SET @category_id = NULL;
SELECT @category_id := category_id FROM product_category WHERE category_code = '0710'  LIMIT 1;

INSERT INTO product_category ( category_code, category_name, parent_id, level, sort_order) VALUES
( '071001', '烫布',    @category_id, 4, 1),
( '071002', '弹力布', @category_id, 4, 2);

