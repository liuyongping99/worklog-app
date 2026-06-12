-- ============================================
DROP TABLE IF EXISTS product;

CREATE TABLE product (
    product_id        
INTEGER PRIMARY KEY AUTOINCREMENT ,
    product_code      
TEXT NOT NULL UNIQUE ,
    product_name      
TEXT NOT NULL ,
    category_id       
INTEGER NOT NULL ,
    specification     
TEXT ,
    model
TEXT ,
    barcode           
TEXT ,
    cost_price           
INTEGER ,
    base_unit       
TEXT ,
    aux_unit     
TEXT ,
    conversion_rate   
TEXT ,
    stock_quantity    
INTEGER ,
    aux_quantity      
TEXT ,
    preset_price      
REAL ,
    status            INTEGER DEFAULT 1 ,
    created_at        
DATETIME DEFAULT (datetime('now','localtime')),
    updated_at        
DATETIME DEFAULT (datetime('now','localtime')) ,

    FOREIGN KEY (category_id) REFERENCES product_category(category_id)
);
CREATE INDEX IF NOT EXISTS idx_p_cat ON product(category_id);

-- ============================================
-- 商品分类表（邻接表模型）- 完整版含所有节点
-- ============================================
DROP TABLE IF EXISTS product_category;

CREATE TABLE product_category (
    category_id       INTEGER PRIMARY KEY AUTOINCREMENT ,
    category_code     TEXT NOT NULL UNIQUE ,
    category_name     TEXT NOT NULL ,
    parent_id         INTEGER DEFAULT NULL ,
    level             INTEGER NOT NULL DEFAULT 1 ,
    sort_order        INT DEFAULT 0 ,
    status            INTEGER DEFAULT 1 ,
    created_at        DATETIME DEFAULT (datetime('now','localtime')),
    updated_at        DATETIME DEFAULT (datetime('now','localtime')) ,

    FOREIGN KEY (parent_id) REFERENCES product_category(category_id)
);
CREATE INDEX IF NOT EXISTS idx_pc_parent ON product_category(parent_id);
CREATE INDEX IF NOT EXISTS idx_pc_level ON product_category(level);
CREATE INDEX IF NOT EXISTS idx_p_cat ON product(category_id);



-- ============================================
-- 插入数据 - 第1层：根节点
-- ============================================
INSERT INTO product_category (category_id, category_code, category_name, parent_id, level, sort_order) VALUES
(1, '0', '商品', NULL, 1, 0);

-- ============================================
-- 第2层：大类
-- ============================================
SELECT 1 := category_id FROM product_category WHERE category_code = '0'  LIMIT 1;

INSERT INTO product_category (category_code, category_name, parent_id, level, sort_order) VALUES
( '01', '纸类', 1, 2, 1),
( '02', '辅料类', 1, 2, 2),
( '03', '胶粘类', 1, 2, 3),
( '04', '防潮类', 1, 2, 4),
( '05', '棉类', 1, 2, 5),
( '06', '塑料类', 1, 2, 6),
( '07', '特材类', 1, 2, 7),
( '08', '其它类', 1, 2, 8),
( '09', '处理货', 1, 2, 9);