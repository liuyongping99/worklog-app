"""OCR 事件审计只读查询(不写库)。

本模块供 /audit/ocr-events 页面 + 3 个 JSON/CSV 端点使用。
零迁移、零新依赖;仅查询 ocr_match_event + shipping_records + staff。
"""
from ._db import get_db


# CSV 导出列定义(蓝图层 import 用于 header)。顺序固定,共 20 列。
_EXPORT_FIELDS = [
    'event_id', 'created_at', 'event_type',
    'record_id', 'order_id', 'image_id',
    'product_name', 'specification',
    'ocr_text', 'ocr_engine',
    'prompt_version', 'ai_engine', 'prompt_payload',
    'ai_match_status', 'ai_match_score', 'ai_match_reason', 'ai_raw_response',
    'human_status', 'human_reason', 'human_verified_by',
]


class OcrEventAudit:
    """OCR 比对事件审计查询助手。"""

    @staticmethod
    def prompt_stats(*, start_date=None, end_date=None,
                     prompt_versions=None) -> list:
        """按 prompt_version 聚合一致率 + 配对数 + 最后使用时间。

        配对定义:同一 record 必须同时有 ai_match 和 human_verify 事件,
        各取 latest(created_at DESC, id DESC 兜底)配成一对。
        一致率 = consistent_pairs / (consistent_pairs + inconsistent_pairs);
        null_pairs 不计入分母(取消核查或 ai_status NULL 的对)。

        Args:
          start_date, end_date: 'YYYY-MM-DD' 字符串,闭区间 [start, end+1day)
          prompt_versions: 列表,None 表示不限

        Returns:
          [{prompt_version, total_pairs, consistent_pairs,
            inconsistent_pairs, null_pairs, consistency_rate,
            last_used_at}, ...]
          按 last_used_at DESC 排序。
        """
        # 注意:prompt_version IS NOT NULL 只对 ai_match 行有意义(human_verify
        # 写库时没传 prompt_version)。WHERE 拆成两部分用 OR 合并,确保两类事件
        # 都能进入 latest CTE;prompt_versions 过滤仅作用于 ai_match 行。
        ai_params = []
        hv_params = []
        ai_predicates = ["e.event_type = 'ai_match'", "e.prompt_version IS NOT NULL"]
        hv_predicates = ["e.event_type = 'human_verify'"]
        if start_date:
            ai_predicates.append("e.created_at >= ?")
            hv_predicates.append("e.created_at >= ?")
            ai_params.append(f"{start_date} 00:00:00")
            hv_params.append(f"{start_date} 00:00:00")
        if end_date:
            ai_predicates.append("e.created_at <= ?")
            hv_predicates.append("e.created_at <= ?")
            ai_params.append(f"{end_date} 23:59:59")
            hv_params.append(f"{end_date} 23:59:59")
        if prompt_versions:
            pv_placeholders = ','.join('?' for _ in prompt_versions)
            ai_predicates.append(f"e.prompt_version IN ({pv_placeholders})")
            ai_params.extend(prompt_versions)
        ai_sql = ' AND '.join(ai_predicates)
        hv_sql = ' AND '.join(hv_predicates)
        where_sql = f"({ai_sql}) OR ({hv_sql})"
        params = ai_params + hv_params

        sql = f"""
        WITH latest AS (
          SELECT
            e.record_id,
            e.prompt_version,
            e.event_type,
            e.ai_match_status,
            e.human_status,
            e.created_at,
            ROW_NUMBER() OVER (
              PARTITION BY e.record_id, e.event_type
              ORDER BY e.created_at DESC, e.id DESC
            ) AS rn
          FROM ocr_match_event e
          WHERE {where_sql}
        ),
        pairs AS (
          SELECT
            ai.record_id,
            ai.prompt_version,
            ai.ai_match_status,
            hv.human_status,
            CASE
              WHEN ai.ai_match_status IS NULL OR hv.human_status IS NULL THEN NULL
              WHEN ai.ai_match_status = hv.human_status THEN 1
              ELSE 0
            END AS is_consistent
          FROM latest ai
          JOIN latest hv ON ai.record_id = hv.record_id
          WHERE ai.event_type = 'ai_match' AND ai.rn = 1
            AND hv.event_type = 'human_verify' AND hv.rn = 1
        ),
        latest_times AS (
          SELECT record_id, MAX(created_at) AS last_used_at
          FROM latest
          WHERE rn = 1
          GROUP BY record_id
        )
        SELECT
          p.prompt_version,
          SUM(CASE WHEN is_consistent IS NOT NULL THEN 1 ELSE 0 END) AS total_pairs,
          SUM(CASE WHEN is_consistent = 1 THEN 1 ELSE 0 END) AS consistent_pairs,
          SUM(CASE WHEN is_consistent = 0 THEN 1 ELSE 0 END) AS inconsistent_pairs,
          SUM(CASE WHEN is_consistent IS NULL THEN 1 ELSE 0 END) AS null_pairs,
          ROUND(
            100.0 * SUM(CASE WHEN is_consistent = 1 THEN 1 ELSE 0 END)
            / NULLIF(SUM(CASE WHEN is_consistent IS NOT NULL THEN 1 ELSE 0 END), 0),
            1
          ) AS consistency_rate,
          MAX(l.last_used_at) AS last_used_at
        FROM pairs p
        JOIN latest_times l ON l.record_id = p.record_id
        GROUP BY p.prompt_version
        ORDER BY last_used_at DESC
        """
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]

    @staticmethod
    def record_pairs(*, prompt_version, start_date=None, end_date=None,
                     limit=500, offset=0):
        """按 prompt_version 列出每个 record 的配对详情(用于钻入明细)。

        配对定义:同一 record 必须同时存在 ai_match(指定 prompt_version)
        和 human_verify 事件,各取 latest(created_at DESC, id DESC 兜底)
        配成一对;只有 ai 没有 human_verify 的 record 不计入。

        Args:
          prompt_version: 必填,只查该版本下的 ai_match 事件
          start_date, end_date: 'YYYY-MM-DD',仅对 ai_match 行过滤时间
          limit, offset: 分页(默认 500/0,上限 2000 在蓝图层 enforce)

        Returns:
          (rows, total)
          rows: [{record_id, order_id, order_date, customer, product_name,
                  specification, quantity, unit,
                  ai_status, ai_reason, ai_time,
                  human_status, human_reason, operator_name, human_time,
                  is_consistent}, ...]
          按 ai_time DESC, record_id DESC 排序。
          total: COUNT(*),已 JOIN human_verify,代表有配对的 record 数。
        """
        # 日期过滤只对 ai_match 行生效(human_verify 不带 prompt_version)
        ai_predicates = ["e.event_type = 'ai_match'", "e.prompt_version = ?"]
        ai_params = [prompt_version]
        if start_date:
            ai_predicates.append("e.created_at >= ?")
            ai_params.append(f"{start_date} 00:00:00")
        if end_date:
            ai_predicates.append("e.created_at <= ?")
            ai_params.append(f"{end_date} 23:59:59")
        ai_where = ' AND '.join(ai_predicates)

        base_cte = f"""
        WITH latest_ai AS (
          SELECT record_id, ai_match_status, ai_match_reason, created_at,
            ROW_NUMBER() OVER (PARTITION BY record_id ORDER BY created_at DESC, id DESC) AS rn
          FROM ocr_match_event e
          WHERE {ai_where}
        ),
        latest_hv AS (
          SELECT record_id, human_status, human_reason, human_verified_by, created_at,
            ROW_NUMBER() OVER (PARTITION BY record_id ORDER BY created_at DESC, id DESC) AS rn
          FROM ocr_match_event
          WHERE event_type = 'human_verify'
        )
        """

        # total: 要求两侧都存在(JOIN latest_hv WHERE rn=1)
        total_sql = base_cte + """
        SELECT COUNT(*) FROM latest_ai lai
        JOIN latest_hv lhv ON lhv.record_id = lai.record_id AND lhv.rn = 1
        WHERE lai.rn = 1
        """

        # rows: 同上 + JOIN 元数据 + 排序分页
        rows_sql = base_cte + """
        SELECT
          sr.id AS record_id, sr.order_pk AS order_id,
          so.date AS order_date, so.customer AS customer,
          sr.product_name, sr.specification, sr.quantity, sr.unit,
          lai.ai_match_status AS ai_status,
          lai.ai_match_reason AS ai_reason,
          lai.created_at AS ai_time,
          lhv.human_status AS human_status,
          lhv.human_reason AS human_reason,
          staff.name AS operator_name,
          lhv.created_at AS human_time,
          CASE
            WHEN lai.ai_match_status IS NULL OR lhv.human_status IS NULL THEN NULL
            WHEN lai.ai_match_status = lhv.human_status THEN 1
            ELSE 0
          END AS is_consistent
        FROM latest_ai lai
        JOIN latest_hv lhv ON lhv.record_id = lai.record_id AND lhv.rn = 1
        JOIN shipping_records sr ON sr.id = lai.record_id AND lai.rn = 1
        JOIN shipping_orders so ON so.id = sr.order_pk
        LEFT JOIN staff ON staff.id = lhv.human_verified_by
        WHERE lai.rn = 1
        ORDER BY lai.created_at DESC, sr.id DESC
        LIMIT ? OFFSET ?
        """
        rows_params = ai_params + [limit, offset]

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(total_sql, ai_params)
        total = cursor.fetchone()[0]
        cursor.execute(rows_sql, rows_params)
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows], total

    @staticmethod
    def export_rows(*, start_date=None, end_date=None,
                    event_types=None, prompt_versions=None):
        """扁平返回每个事件全字段,供 CSV 导出。

        不走配对,逐事件返。排序 `id ASC`(append-only 写入顺序)。
        过滤参数:start_date/end_date ('YYYY-MM-DD',闭区间含边界);
        event_types/prompt_versions (列表,None 表示不限)。
        """
        params = []
        where_clauses = []
        if start_date:
            where_clauses.append("e.created_at >= ?")
            params.append(f"{start_date} 00:00:00")
        if end_date:
            where_clauses.append("e.created_at <= ?")
            params.append(f"{end_date} 23:59:59")
        if event_types:
            placeholders = ','.join('?' for _ in event_types)
            where_clauses.append(f"e.event_type IN ({placeholders})")
            params.extend(event_types)
        if prompt_versions:
            placeholders = ','.join('?' for _ in prompt_versions)
            where_clauses.append(f"e.prompt_version IN ({placeholders})")
            params.extend(prompt_versions)

        where_sql = ('WHERE ' + ' AND '.join(where_clauses)) if where_clauses else ''

        sql = f"""
        SELECT
          e.id AS event_id, e.created_at, e.event_type,
          e.record_id, e.order_id, e.image_id,
          e.product_name, e.specification,
          e.ocr_text, e.ocr_engine,
          e.prompt_version, e.ai_engine, e.prompt_payload,
          e.ai_match_status, e.ai_match_score, e.ai_match_reason, e.ai_raw_response,
          e.human_status, e.human_reason, e.human_verified_by
        FROM ocr_match_event e
        {where_sql}
        ORDER BY e.id ASC
        """
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]
