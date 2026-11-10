"""把无法再继续的后台生成任务标成可重试。

FastAPI 的 BackgroundTasks 只活在接单的那个 worker 里。部署重启时
uvicorn 会等这些任务，systemd 的 TimeoutStopUSec 到点后直接 SIGKILL，
任务的 except/finally 来不及把状态从 processing 改掉。页面随后一直看到
「正在生成」，重试接口也因为「已有任务」而拒绝。

两层回收，都不改报告正文、测量值、图片或已生成的材料文件：

- 进程启动时，只处理「本进程启动之前」就处于进行中的记录。后台任务
  不可能活过重启；本进程启动之后才开始的任务留给正在跑的 worker。
- 服务还活着时，超过各自时限仍是进行中的记录视为失联，放开重试并让
  页面停止轮询。时限晚于单次模型请求的超时与应用层重试，避免误伤。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from app.models import CbcReport, InsuranceMaterialShare, UltrasoundReport, VisitConsultationDraft
from app.time_utils import beijing_now

logger = logging.getLogger(__name__)

# 本模块被 app.main 在导入时加载，因此这是 worker 进程启动时刻（UTC）。
_PROCESS_STARTED_AT = beijing_now().astimezone(timezone.utc).replace(tzinfo=None)

# 见 report_llm.REPORT_LLM_TIMEOUT：单次 180 秒，测量整理与正文起草各最多两次。
ULTRASOUND_STALE_AFTER = timedelta(minutes=15)
CONSULTATION_STALE_AFTER = timedelta(minutes=15)
# 与保险材料页原有的 30 分钟清理保持一致。
INSURANCE_STALE_AFTER = timedelta(minutes=30)

ULTRASOUND_INTERRUPTED_ERROR = "生成任务在服务重启时被中断，请重新生成文字稿"
ULTRASOUND_STALE_ERROR = "生成任务超时未完成，已停止等待，请重新生成文字稿"
CBC_INTERRUPTED_OCR_ERROR = "图片识别在服务重启时被中断，请重新识别原图"
CBC_INTERRUPTED_ANALYSIS_ERROR = "生成任务在服务重启时被中断，数值已保留，请重新生成分析"
INSURANCE_INTERRUPTED_ERROR = "生成任务在服务重启时被中断，请重新生成"
INSURANCE_STALE_ERROR = "后台生成任务已中断，请重新生成"
CONSULTATION_INTERRUPTED_ERROR = "整理任务在服务重启时被中断，请重新整理"
CONSULTATION_STALE_ERROR = "整理任务超时未完成，请重新整理"


def utc_now_naive() -> datetime:
    """当前时刻的 UTC naive 时间，与库存的 DateTime 同一口径。"""
    return beijing_now().astimezone(timezone.utc).replace(tzinfo=None)


def _as_utc_naive(value: datetime | None) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _is_older_than(started: datetime | None, now: datetime, threshold: timedelta) -> bool:
    started_utc = _as_utc_naive(started)
    if started_utc is None:
        return True
    return _as_utc_naive(now) - started_utc >= threshold


def _started_before_process(started: datetime | None, cutoff: datetime) -> bool:
    started_utc = _as_utc_naive(started)
    if started_utc is None:
        return True
    return started_utc < _as_utc_naive(cutoff)


def ultrasound_job_is_stale(report, now: datetime | None = None) -> bool:
    if (getattr(report, "ai_status", "") or "") != "processing":
        return False
    return _is_older_than(getattr(report, "ai_started_at", None), now or utc_now_naive(), ULTRASOUND_STALE_AFTER)


def consultation_job_is_stale(row, now: datetime | None = None) -> bool:
    if (getattr(row, "status", "") or "") != "processing":
        return False
    return _is_older_than(getattr(row, "created_at", None), now or utc_now_naive(), CONSULTATION_STALE_AFTER)


def insurance_generation_is_stale(share, now: datetime | None = None) -> bool:
    if (getattr(share, "generation_status", "") or "") != "processing":
        return False
    return _is_older_than(
        getattr(share, "generation_started_at", None),
        now or utc_now_naive(),
        INSURANCE_STALE_AFTER,
    )


def _cbc_has_results(report: CbcReport) -> bool:
    try:
        rows = json.loads(report.results_json or "[]")
    except Exception:
        return False
    return any(isinstance(row, dict) and row.get("value") is not None for row in rows)


def _set_ultrasound_failed(db, report_id: int, error: str, now: datetime) -> bool:
    updated = (
        db.query(UltrasoundReport)
        .filter(UltrasoundReport.id == report_id, UltrasoundReport.ai_status == "processing")
        .update(
            {"ai_status": "failed", "ai_error": error[:1000], "ai_completed_at": now},
            synchronize_session="fetch",
        )
    )
    return bool(updated)


def release_stale_ultrasound(db, report, *, now: datetime | None = None) -> bool:
    """进行中的 B超任务已超时则标为失败。返回是否由本次调用改写。"""
    now = now or utc_now_naive()
    if not ultrasound_job_is_stale(report, now):
        return False
    changed = _set_ultrasound_failed(db, report.id, ULTRASOUND_STALE_ERROR, now)
    db.commit()
    db.refresh(report)
    if changed:
        logger.info("[ai-job] B超报告 %s 生成超时，已停止等待", report.id)
    return changed


def release_stale_insurance(db, share, *, now: datetime | None = None) -> bool:
    now = now or utc_now_naive()
    if not insurance_generation_is_stale(share, now):
        return False
    changed = _set_insurance_failed(db, share.id, INSURANCE_STALE_ERROR, now)
    db.commit()
    db.refresh(share)
    if changed:
        logger.info("[ai-job] 保险材料包 %s 生成超时，已停止等待", share.id)
    return changed


def release_stale_consultation(db, row, *, now: datetime | None = None) -> bool:
    now = now or utc_now_naive()
    if not consultation_job_is_stale(row, now):
        return False
    changed = _set_consultation_failed(db, row.id, CONSULTATION_STALE_ERROR)
    db.commit()
    db.refresh(row)
    if changed:
        logger.info("[ai-job] 接诊草稿 %s 整理超时，已停止等待", row.id)
    return changed


def _set_insurance_failed(db, share_id: int, error: str, now: datetime) -> bool:
    updated = (
        db.query(InsuranceMaterialShare)
        .filter(
            InsuranceMaterialShare.id == share_id,
            InsuranceMaterialShare.generation_status == "processing",
        )
        .update(
            {
                "generation_status": "failed",
                "generation_error": error[:1000],
                "generation_completed_at": now,
            },
            synchronize_session="fetch",
        )
    )
    return bool(updated)


def _set_consultation_failed(db, draft_id: int, error: str) -> bool:
    updated = (
        db.query(VisitConsultationDraft)
        .filter(
            VisitConsultationDraft.id == draft_id,
            VisitConsultationDraft.status == "processing",
        )
        .update(
            {"status": "failed", "ai_error": error[:1000]},
            synchronize_session="fetch",
        )
    )
    return bool(updated)


def _interrupt_ultrasound(db, now: datetime, cutoff: datetime) -> int:
    count = 0
    rows = db.query(UltrasoundReport).filter(UltrasoundReport.ai_status == "processing").all()
    for row in rows:
        if not _started_before_process(row.ai_started_at, cutoff):
            continue
        if _set_ultrasound_failed(db, row.id, ULTRASOUND_INTERRUPTED_ERROR, now):
            count += 1
    return count


def _interrupt_cbc(db, now: datetime, cutoff: datetime) -> int:
    count = 0
    rows = db.query(CbcReport).filter(CbcReport.ai_status.in_(("processing", "analyzing"))).all()
    for row in rows:
        if not _started_before_process(row.ai_started_at, cutoff):
            continue
        if row.ai_status == "analyzing" or _cbc_has_results(row):
            new_status, error = "partial", CBC_INTERRUPTED_ANALYSIS_ERROR
        else:
            new_status, error = "failed", CBC_INTERRUPTED_OCR_ERROR
        updated = (
            db.query(CbcReport)
            .filter(CbcReport.id == row.id, CbcReport.ai_status == row.ai_status)
            .update(
                {"ai_status": new_status, "ai_error": error[:1000], "ai_completed_at": now},
                synchronize_session="fetch",
            )
        )
        count += int(bool(updated))
    return count


def _interrupt_insurance(db, now: datetime, cutoff: datetime) -> int:
    count = 0
    rows = (
        db.query(InsuranceMaterialShare)
        .filter(InsuranceMaterialShare.generation_status == "processing")
        .all()
    )
    for row in rows:
        if not _started_before_process(row.generation_started_at, cutoff):
            continue
        if _set_insurance_failed(db, row.id, INSURANCE_INTERRUPTED_ERROR, now):
            count += 1
    return count


def _interrupt_consultation(db, _now: datetime, cutoff: datetime) -> int:
    # 接诊草稿没有单独的完成时间列，创建时间就是任务起点。
    count = 0
    rows = db.query(VisitConsultationDraft).filter(VisitConsultationDraft.status == "processing").all()
    for row in rows:
        if not _started_before_process(row.created_at, cutoff):
            continue
        if _set_consultation_failed(db, row.id, CONSULTATION_INTERRUPTED_ERROR):
            count += 1
    return count


def release_interrupted_jobs(db, *, process_started_at: datetime | None = None) -> dict[str, int]:
    """启动时回收重启前遗留的进行中任务。已提交的正文和文件保持原样。"""
    now = utc_now_naive()
    cutoff = process_started_at or _PROCESS_STARTED_AT
    counts: dict[str, int] = {}
    for name, fn in (
        ("ultrasound", _interrupt_ultrasound),
        ("cbc", _interrupt_cbc),
        ("insurance", _interrupt_insurance),
        ("consultation", _interrupt_consultation),
    ):
        try:
            counts[name] = fn(db, now, cutoff)
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("[ai-job] 回收 %s 失败", name)
            counts[name] = 0
    released = {name: count for name, count in counts.items() if count}
    if released:
        logger.warning("[ai-job] 服务重启，已中断的后台任务改为可重试：%s", released)
    return counts
