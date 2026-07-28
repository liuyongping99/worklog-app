"""OCR 事件审计只读查询(不写库)。

本模块供 /audit/ocr-events 页面 + 3 个 JSON/CSV 端点使用。
零迁移、零新依赖;仅查询 ocr_match_event + shipping_records + staff。
"""
from ._db import get_db


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
