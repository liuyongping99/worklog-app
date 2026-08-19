"""行级图 OCR pipeline 共享层(三套订单共用)。

封装:
  - PaddleOCR 抽字 + 背景色检测
  - DeepSeek 单 record 比对 + 自适应提示词注入(可选)
  - 本地 RapidFuzz 降级
  - 写 image.match_* 字段
  - 写 OcrMatchEvent('record_ocr' + 'ai_match') 两条事件
  - 移动端糊图 reason 追加(avg_conf < 0.5)

设计要点:
  - 显式注入 image_model:三套订单的 ShippingImage / LoadingOrderImage /
    InboundImage 接口一致(get_by_id / set_match / set_bg_color),不需要鸭子类型检查
  - 异步/同步分离:process_async 假设调用方已在外层起线程,内部只负责
    用 _OCR_LOCK 串行化"抽字+比对",写库在锁外;process_full 是同步全流程
  - _OCR_LOCK 与 _ASYNC_JOBS 从 shipping.py 移到此处模块级,所有蓝图 re-export 共用

历史(2026-08-19):三个 blueprint 各自重复 _classify_and_match_image /
_run_label_match 约 600 行,本模块消除 3-way 重复。新行为(如新预处理 kind /
新事件类型)改 ocr_pipeline.py 一处即可,三套订单自动跟随,行为漂移风险归零。
"""
import logging
import threading
import time

from blueprints._helpers import detect_bg_color, match_label_to_row
from blueprints.ocr_engine import (
    OCR_MATCH_PROMPT_VERSION, get_ocr_engine, ocr_preprocess_kind,
)
from blueprints.ocr_log import set_log_context
from models import CategoryPrompt, OcrMatchEvent, classify_record
from models._db import get_db

logger = logging.getLogger(__name__)


# ── 模块级共享状态 ──────────────────────────────────────────────
# 单用户本地部署,用进程内存登记任务进度即可,无需 Redis/Celery。
# _ASYNC_JOBS[image_id] = {'state': 'processing'|'done'|'error', 'started': ts, 'finished': ts}
_ASYNC_JOBS = {}

# PaddleOCR 推理与 DeepSeek 调用都不是严格线程安全的,且 API 有速率限制,
# 统一用一把锁串行化"抽字→比对"过程,避免多图并发上传时互相踩踏。
_OCR_LOCK = threading.Lock()


# ── 私有 helper ──────────────────────────────────────────────────

def _append_blur_reason_if_low_conf(image_model, image_id, avg_conf, threshold=0.5):
    """行级图 OCR 平均置信度低于阈值时,在 image.reason 末尾追加模糊提示。

    移动端拍照易出现糊图(对焦失败/手抖),PaddleOCR 仍会跑出文字但置信度偏低。
    把这条信息写到 reason 上,前端 hover 可提示用户重拍,减少误识。

    幂等:marker 已存在则不再追加;失败/行不存在则静默 return。
    """
    if avg_conf >= threshold:
        return
    try:
        row = image_model.get_by_id(image_id)
        if not row:
            return
        old = row.get('reason') or ''
        marker = '[图像可能模糊，建议重拍]'
        if marker in old:
            return
        new_reason = f"{old} {marker}".strip()
        image_model.set_match(
            image_id,
            row.get('match_status') or 'yellow',
            row.get('match_score') or 0,
            new_reason,
            row.get('match_source') or 'local_fuzzy',
        )
    except Exception:
        logger.exception('append blur reason failed: image_id=%s', image_id)


def _read_cached_ocr(image_id):
    """从 ocr_match_event 取最近一次 record_ocr 的 ocr_text(没有则空串)。

    用于 /fuzzy-match 和 /ai-judge 端点减少重跑 PaddleOCR 的成本。
    image_id 为 None 时永远返回空串(避免空查 SQL)。
    """
    if not image_id:
        return ''
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            'SELECT ocr_text FROM ocr_match_event '
            'WHERE image_id = ? AND event_type = ? AND ocr_text IS NOT NULL '
            'ORDER BY id DESC LIMIT 1',
            (image_id, 'record_ocr'))
        row = cur.fetchone()
        conn.close()
        return (row['ocr_text'] or '') if row else ''
    except Exception:
        return ''


def _supplement_for_record(record):
    """取该商品行比对时要拼进 DeepSeek prompt 的自定义提示词(自适应)。"""
    if not record:
        return ''
    cls = classify_record(
        product_name=record.get('product_name', ''),
        specification=record.get('specification', ''))
    cc = ''
    if cls:
        raw_cc = cls.get('category_code')
        if isinstance(raw_cc, str):
            cc = raw_cc
    return CategoryPrompt.compose_for_record(
        category_code=cc,
        product_name=record.get('product_name', ''),
        specification=record.get('specification', ''),
    )


# ── 主类 ────────────────────────────────────────────────────────

class RecordImageProcessor:
    """行级图 OCR pipeline — 出货/入库/装柜三套订单共用。

    用法:
        processor = RecordImageProcessor(ShippingImage)
        extracted = processor.extract_ocr(filepath, record)
        result = processor.classify(extracted['ocr_text'], record)
        processor.persist_match(image_id=..., ocr_text=..., result=result, ...)
        # 或一步到位:
        processor.process_full(image_id=..., filepath=..., record=..., ...)
        processor.process_async(image_id=..., filepath=..., record=...)  # 已在子线程内
    """

    def __init__(self, image_model):
        # 契约(image_model 必须实现):
        #   get_by_id(image_id) -> dict | None
        #   set_match(image_id, status, score, reason, source=...) -> None
        #   set_bg_color(image_id, bg_color) -> None
        self.image_model = image_model

    # ── 核心方法 1:OCR + 背景色 ──────────────────────────────
    def extract_ocr(self, filepath, record=None, *, image_id=None, use_cached=True):
        """抽 OCR 文字 + 背景色 + 平均置信度。

        Args:
            filepath: 已落盘图片的绝对路径。
            record: 明细行 dict(用于决定 preprocess_kind)。可为 None。
            image_id: 用于查缓存(use_cached=True 时)与写 bg_color。
            use_cached: True 时优先从 ocr_match_event 历史取 OCR 文字。

        Returns:
            dict {
                'ocr_text': str,       # 已拼接 [标签背景: 黑色|白色] 的 OCR 文字
                'bg_color': 'black' | 'white' | None,
                'avg_conf': float,     # 1.0 表示无文字或缓存命中
                'from_cache': bool,
            }
        """
        ocr_text = ''
        avg_conf = 1.0
        from_cache = False

        # 1. 优先从缓存取 OCR 文字
        if use_cached:
            ocr_text = _read_cached_ocr(image_id)
            if ocr_text:
                from_cache = True

        # 2. 缓存未命中,跑 PaddleOCR
        if not ocr_text:
            try:
                product_name = (record or {}).get('product_name', '')
                preprocess_kind = ocr_preprocess_kind(product_name)
                with open(filepath, 'rb') as _f:
                    _ocr_text, _conf = get_ocr_engine('paddleocr').extract_text_with_conf(
                        _f.read(), preprocess_kind=preprocess_kind)
                    ocr_text = _ocr_text or ''
                    avg_conf = _conf
            except Exception:
                logger.exception('PaddleOCR extract_text failed: %s', filepath)
                ocr_text = ''
                avg_conf = 1.0

        # 3. 检测背景色,非空时拼到 OCR 文字末尾 + 写回 image_model
        bg = detect_bg_color(filepath)
        if bg and ocr_text.strip():
            ocr_text = ocr_text + '\n[标签背景: ' + (
                '黑色' if bg == 'black' else '白色') + ']'
        if bg and image_id:
            try:
                self.image_model.set_bg_color(image_id, bg)
            except Exception:
                logger.exception('set_bg_color 失败(不阻断) image_id=%s', image_id)

        return {
            'ocr_text': ocr_text,
            'bg_color': bg,
            'avg_conf': avg_conf,
            'from_cache': from_cache,
        }

    # ── 核心方法 2:DeepSeek → 本地降级 ──────────────────────
    def classify(self, ocr_text, record, *, with_supplement=True):
        """对一张行级图跑 AI 比对,返回结构化结果。

        Args:
            ocr_text: extract_ocr 返回的文字(已含 [标签背景: ...] 拼接)。
            record: 明细行 dict。
            with_supplement: True 时拼自适应提示词(layer2 大类 + layer3 规格);
                             False 时不拼(loading 当前保留此行为)。

        Returns:
            dict {
                'status': 'green'|'yellow'|'red'|'',
                'score': float | None,
                'reason': str,
                'source': 'deepseek'|'local_fuzzy'|'',
                'prompt_text': str,   # DeepSeek 时有效
                'raw_response': str,  # DeepSeek 时有效
            }
        """
        empty = {
            'status': '', 'score': None, 'reason': '', 'source': '',
            'prompt_text': '', 'raw_response': '',
        }

        # OCR 为空 → 跳过所有引擎,不打徽章(语义:没证据 ≠ 不符)
        if not (ocr_text or '').strip():
            return empty

        # 1. 优先 DeepSeek(有 key 才联网)
        try:
            ds = get_ocr_engine('deepseek')
            if getattr(ds, 'API_KEY', ''):
                supplement = (_supplement_for_record(record)
                              if with_supplement else '')
                res = ds.compare_single_record(
                    ocr_text, record, supplement_prompt=supplement)
                ms = (res.get('match_status') or '').lower()
                if ms in ('green', 'yellow', 'red'):
                    return {
                        'status': ms,
                        'score': res.get('score'),
                        'reason': res.get('reason') or '',
                        'source': 'deepseek',
                        'prompt_text': res.get('prompt_text', ''),
                        'raw_response': res.get('raw_response', ''),
                    }
                # 非合法状态:fallback 本地
                logger.warning('DeepSeek 返回非合法状态: %r, 改走本地', ms)
            else:
                logger.info('DeepSeek API key 未配置, 行级图走本地 RapidFuzz')
        except Exception as e:
            # 联网但调用失败(超时/网络/配额):fallback 本地
            logger.warning('DeepSeek 单 record 比对失败, fall back 本地: %s', e)

        # 2. fallback: 本地 RapidFuzz
        try:
            status, score, reason = match_label_to_row(
                ocr_text,
                record.get('product_name', ''),
                record.get('specification', ''),
            )
            return {
                'status': status or '', 'score': score, 'reason': reason or '',
                'source': 'local_fuzzy', 'prompt_text': '', 'raw_response': '',
            }
        except Exception:
            logger.exception('本地匹配失败')
            return empty

    # ── 核心方法 3:写库 + 写事件 ─────────────────────────────
    def persist_match(self, *, image_id, ocr_text, result, record,
                      order_id, avg_conf=1.0):
        """写 image.match_* + OcrMatchEvent('record_ocr' + 'ai_match') + blur_reason。

        Args:
            image_id: 已落库的图片 id。
            ocr_text: extract_ocr 返回的文字。
            result: classify 返回的字典。
            record: 明细行 dict(给 event 记录 product_name/specification)。
            order_id: 订单 id。
            avg_conf: 平均置信度,低于阈值时追加 blur_reason。
        """
        status = result.get('status')
        score = result.get('score')
        reason = result.get('reason') or ''
        source_label = result.get('source') or 'local_fuzzy'
        # record_id 必须为 int(OcrMatchEvent.create 契约);缺 record / 缺 'id' 用 0 占位
        rid = 0
        if record:
            raw_rid = record.get('id')
            if raw_rid is not None:
                rid = int(raw_rid)
        pn = record.get('product_name', '') if record else ''
        sp = record.get('specification', '') if record else ''

        # 1. 写 image.match_status/match_score/reason/match_source
        if status:
            try:
                self.image_model.set_match(
                    image_id, status, score, reason, source=source_label)
            except Exception:
                logger.exception(
                    'image.set_match 失败(不阻断) image_id=%s', image_id)

        # 2. 移动端糊图提示(avg_conf < 0.5)
        try:
            _append_blur_reason_if_low_conf(self.image_model, image_id, avg_conf)
        except Exception:
            logger.exception('append blur reason failed: image_id=%s', image_id)

        # 3. append-only record_ocr 事件
        try:
            OcrMatchEvent.create(
                'record_ocr',
                record_id=rid, order_id=order_id, image_id=image_id,
                ocr_text=ocr_text, ocr_engine='paddleocr',
                ai_engine=source_label or 'local_fuzzy',
                ai_match_status=status or None,
                ai_match_score=score,
                ai_match_reason=reason or None,
                product_name=pn, specification=sp,
            )
        except Exception:
            logger.exception('record_ocr 事件写库失败(不阻断)')

        # 4. DeepSeek 参与 → 额外写 ai_match 事件
        if source_label == 'deepseek' and result.get('prompt_text'):
            try:
                OcrMatchEvent.create(
                    'ai_match',
                    record_id=rid, order_id=order_id, image_id=image_id,
                    ocr_text=ocr_text, ocr_engine='paddleocr',
                    prompt_payload=result['prompt_text'],
                    ai_engine='deepseek',
                    ai_match_status=status,
                    ai_match_score=score,
                    ai_match_reason=reason or None,
                    ai_raw_response=result['raw_response'],
                    prompt_version=OCR_MATCH_PROMPT_VERSION,
                    product_name=pn, specification=sp,
                )
            except Exception:
                logger.exception('ai_match 事件写库失败(不阻断)')

    # ── 编排方法:同步全流程 ─────────────────────────────────
    def process_full(self, *, image_id, filepath, record, order_id, record_id):
        """同步:extract + classify + persist(inbound 用)。

        Returns:
            dict — extract_ocr 与 classify 结果的合并。
        """
        set_log_context(biz=self.image_model.__name__,
                        image_id=image_id, record_id=record_id)
        extracted = self.extract_ocr(filepath, record, image_id=image_id,
                                      use_cached=False)
        result = self.classify(extracted['ocr_text'], record)
        self.persist_match(
            image_id=image_id, ocr_text=extracted['ocr_text'],
            result=result, record=record, order_id=order_id,
            avg_conf=extracted.get('avg_conf', 1.0),
        )
        return {**extracted, **result}

    # ── 编排方法:异步全流程(锁内 OCR+classify,锁外 persist) ──
    def process_async(self, *, image_id, filepath, record, order_id, record_id):
        """在调用方已起的子线程内执行整张图的 OCR + classify + persist。

        锁设计:OCR 抽字 + DeepSeek 比对持锁(避免并发上传的图互踩),
                image 写库 + 事件写库在锁外(缩短临界区)。

        调用方约定:本方法运行在 threading.Thread + copy_context 包裹的子线程内。
        完成后 _ASYNC_JOBS[image_id]['state'] 会变成 'done' 或 'error',
        给前端 /match-status 端点轮询用。
        """
        set_log_context(biz=self.image_model.__name__,
                        image_id=image_id, record_id=record_id)
        try:
            with _OCR_LOCK:
                extracted = self.extract_ocr(filepath, record,
                                              image_id=image_id,
                                              use_cached=False)
                result = self.classify(extracted['ocr_text'], record)
            # 锁外:写 image + 写事件
            self.persist_match(
                image_id=image_id, ocr_text=extracted['ocr_text'],
                result=result, record=record, order_id=order_id,
                avg_conf=extracted.get('avg_conf', 1.0),
            )
            _ASYNC_JOBS[image_id] = {'state': 'done', 'finished': time.time()}
        except Exception:
            logger.exception('行级图异步处理失败 image_id=%s', image_id)
            _ASYNC_JOBS[image_id] = {
                'state': 'error', 'finished': time.time(),
                'error': 'processing failed',
            }