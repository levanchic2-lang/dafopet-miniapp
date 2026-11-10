"""B超文字稿卡在 processing 时必须能重试，且不改已有正文和测量。"""

import os
import re
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DB_PATH = ROOT / "_test" / "ultrasound_ai_recovery.db"
if DB_PATH.exists():
    DB_PATH.unlink()
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH.as_posix()}"
os.environ["ADMIN_PASSWORD"] = "ultrasound-recovery-test"
os.environ["SESSION_SECRET"] = "ultrasound-recovery-secret"
os.environ["DEEPSEEK_API_KEY"] = ""
os.environ["OPENAI_API_KEY"] = ""

from fastapi.testclient import TestClient

from app.config import settings
from app.database import Base, SessionLocal, engine
from app.main import app

settings.admin_password = "ultrasound-recovery-test"
from app.models import (
    CbcReport,
    Customer,
    ExamOrder,
    InsuranceMaterialShare,
    InsuranceMaterialSnapshot,
    Pet,
    UltrasoundReport,
    Visit,
    VisitConsultationDraft,
)
from app.services.ai_job_recovery import (
    CBC_INTERRUPTED_ANALYSIS_ERROR,
    CBC_INTERRUPTED_OCR_ERROR,
    CONSULTATION_INTERRUPTED_ERROR,
    CONSULTATION_STALE_ERROR,
    INSURANCE_INTERRUPTED_ERROR,
    INSURANCE_STALE_ERROR,
    ULTRASOUND_INTERRUPTED_ERROR,
    ULTRASOUND_STALE_ERROR,
    release_interrupted_jobs,
    release_stale_consultation,
    release_stale_insurance,
    release_stale_ultrasound,
    ultrasound_job_is_stale,
    utc_now_naive,
)


Base.metadata.create_all(bind=engine)

FINDINGS = "原有超声所见，不得被状态回收改写"
CONCLUSION = "原有结论"
ADVICE = "原有建议"
MEASUREMENTS = '[{"group":"M测量·左室","rows":[{"name":"LA Diam","value":"0.96","unit":"cm"}]}]'
VET_FINDINGS = "脾脏略增大"
PHOTOS = '["ultrasound_photos/1/keep.jpg"]'


def _session():
    return SessionLocal()


def _wipe():
    db = _session()
    try:
        for model in (
            InsuranceMaterialSnapshot, UltrasoundReport, CbcReport, VisitConsultationDraft,
            InsuranceMaterialShare, ExamOrder, Visit, Pet, Customer,
        ):
            db.query(model).delete()
        db.commit()
    finally:
        db.close()


def _chain(db):
    customer = Customer(name="回收测试客户", phone="13900006666")
    db.add(customer)
    db.flush()
    pet = Pet(customer_id=customer.id, name="回收测试猫", species="cat", store="横岗店")
    db.add(pet)
    db.flush()
    visit = Visit(
        customer_id=customer.id,
        pet_id=pet.id,
        visit_date="2026-08-01",
        store="横岗店",
        vet_name="测试医生",
        status="open",
    )
    db.add(visit)
    db.flush()
    order = ExamOrder(visit_id=visit.id, items_json="[]", upload_token=os.urandom(8).hex())
    db.add(order)
    db.flush()
    return customer, pet, visit, order


def _ultrasound(db, order, *, status="processing", started=None, **overrides):
    report = UltrasoundReport(
        exam_order_id=order.id,
        customer_id=overrides.get("customer_id"),
        pet_id=overrides.get("pet_id"),
        visit_id=order.visit_id,
        item_label="心脏彩超",
        exam_type="cardiac",
        vet_findings=VET_FINDINGS,
        findings=FINDINGS,
        conclusion=CONCLUSION,
        advice=ADVICE,
        measurements_json=MEASUREMENTS,
        photos_json=PHOTOS,
        ai_status=status,
        ai_error="",
        ai_started_at=started,
        store="",
    )
    db.add(report)
    db.flush()
    return report


def _text_unchanged(report):
    assert report.findings == FINDINGS
    assert report.conclusion == CONCLUSION
    assert report.advice == ADVICE
    assert report.measurements_json == MEASUREMENTS
    assert report.photos_json == PHOTOS
    assert report.vet_findings == VET_FINDINGS


class RecoveryUnitTests(unittest.TestCase):
    def test_startup_releases_only_jobs_from_before_this_process(self):
        _wipe()
        cutoff = utc_now_naive()
        db = _session()
        try:
            _customer, _pet, visit, order = _chain(db)
            old = _ultrasound(db, order, started=cutoff - timedelta(minutes=2))
            current = _ultrasound(db, order, started=cutoff + timedelta(seconds=5))
            completed = _ultrasound(db, order, status="completed", started=cutoff - timedelta(hours=1))
            no_clock = _ultrasound(db, order, started=None)
            cbc_order = ExamOrder(visit_id=visit.id, items_json="[]", upload_token=os.urandom(8).hex())
            db.add(cbc_order)
            db.flush()
            cbc_empty = CbcReport(
                exam_order_id=cbc_order.id, results_json="[]", ai_analysis="不要动",
                ai_status="processing", ai_started_at=cutoff - timedelta(minutes=3),
            )
            cbc_order2 = ExamOrder(visit_id=visit.id, items_json="[]", upload_token=os.urandom(8).hex())
            db.add(cbc_order2)
            db.flush()
            cbc_ready = CbcReport(
                exam_order_id=cbc_order2.id,
                results_json='[{"code":"WBC","value":0.0}]',
                ai_analysis="原分析",
                doctor_conclusion="原结论",
                ai_status="analyzing",
                ai_started_at=cutoff - timedelta(minutes=3),
            )
            share = InsuranceMaterialShare(
                token=os.urandom(8).hex(),
                visit_id=visit.id,
                title="旧材料包",
                generation_status="processing",
                generation_started_at=cutoff - timedelta(minutes=3),
                notes="保留备注",
            )
            db.add(share)
            db.flush()
            snap = InsuranceMaterialSnapshot(share_id=share.id, version=1, manifest_json="[]", file_count=2)
            db.add(snap)
            draft = VisitConsultationDraft(
                visit_id=visit.id,
                status="processing",
                transcript="原始转写",
                chief_complaint="原始主诉",
                created_at=cutoff - timedelta(minutes=3),
            )
            fresh_draft = VisitConsultationDraft(
                visit_id=visit.id,
                status="processing",
                transcript="仍在整理",
                created_at=cutoff + timedelta(seconds=5),
            )
            db.add_all([cbc_empty, cbc_ready, draft, fresh_draft])
            db.commit()
            ids = {
                "old": old.id, "current": current.id, "completed": completed.id, "no_clock": no_clock.id,
                "cbc_empty": cbc_empty.id, "cbc_ready": cbc_ready.id,
                "share": share.id, "snap": snap.id,
                "draft": draft.id, "fresh_draft": fresh_draft.id,
            }
        finally:
            db.close()

        db = _session()
        try:
            counts = release_interrupted_jobs(db, process_started_at=cutoff)
            self.assertEqual(counts["ultrasound"], 2)
            self.assertEqual(counts["cbc"], 2)
            self.assertEqual(counts["insurance"], 1)
            self.assertEqual(counts["consultation"], 1)

            old = db.get(UltrasoundReport, ids["old"])
            current = db.get(UltrasoundReport, ids["current"])
            completed = db.get(UltrasoundReport, ids["completed"])
            no_clock = db.get(UltrasoundReport, ids["no_clock"])
            self.assertEqual(old.ai_status, "failed")
            self.assertEqual(old.ai_error, ULTRASOUND_INTERRUPTED_ERROR)
            _text_unchanged(old)
            self.assertEqual(no_clock.ai_status, "failed")
            _text_unchanged(no_clock)
            self.assertEqual(current.ai_status, "processing")
            _text_unchanged(current)
            self.assertEqual(completed.ai_status, "completed")
            self.assertEqual(completed.findings, FINDINGS)

            empty = db.get(CbcReport, ids["cbc_empty"])
            ready = db.get(CbcReport, ids["cbc_ready"])
            self.assertEqual(empty.ai_status, "failed")
            self.assertEqual(empty.ai_error, CBC_INTERRUPTED_OCR_ERROR)
            self.assertEqual(empty.results_json, "[]")
            self.assertEqual(empty.ai_analysis, "不要动")
            self.assertEqual(ready.ai_status, "partial")
            self.assertEqual(ready.ai_error, CBC_INTERRUPTED_ANALYSIS_ERROR)
            self.assertIn("0.0", ready.results_json)
            self.assertEqual(ready.ai_analysis, "原分析")
            self.assertEqual(ready.doctor_conclusion, "原结论")

            share = db.get(InsuranceMaterialShare, ids["share"])
            self.assertEqual(share.generation_status, "failed")
            self.assertEqual(share.generation_error, INSURANCE_INTERRUPTED_ERROR)
            self.assertEqual(share.notes, "保留备注")
            self.assertEqual(db.get(InsuranceMaterialSnapshot, ids["snap"]).file_count, 2)

            draft = db.get(VisitConsultationDraft, ids["draft"])
            fresh = db.get(VisitConsultationDraft, ids["fresh_draft"])
            self.assertEqual(draft.status, "failed")
            self.assertEqual(draft.ai_error, CONSULTATION_INTERRUPTED_ERROR)
            self.assertEqual(draft.transcript, "原始转写")
            self.assertEqual(draft.chief_complaint, "原始主诉")
            self.assertEqual(fresh.status, "processing")
            self.assertEqual(fresh.transcript, "仍在整理")
        finally:
            db.close()

    def test_stale_release_keeps_report_text_and_ignores_fresh_jobs(self):
        _wipe()
        now = utc_now_naive()
        db = _session()
        try:
            _customer, _pet, visit, order = _chain(db)
            stale = _ultrasound(db, order, started=now - timedelta(minutes=15))
            fresh = _ultrasound(db, order, started=now - timedelta(minutes=14))
            share = InsuranceMaterialShare(
                token=os.urandom(8).hex(), visit_id=visit.id, title="超时材料包",
                generation_status="processing",
                generation_started_at=now - timedelta(minutes=30),
            )
            recent_share = InsuranceMaterialShare(
                token=os.urandom(8).hex(), visit_id=visit.id, title="进行中材料包",
                generation_status="processing",
                generation_started_at=now - timedelta(minutes=5),
            )
            missing_clock = InsuranceMaterialShare(
                token=os.urandom(8).hex(), visit_id=visit.id, title="无开始时间",
                generation_status="processing", generation_started_at=None,
            )
            draft = VisitConsultationDraft(
                visit_id=visit.id, status="processing", transcript="超时转写",
                created_at=now - timedelta(minutes=15),
            )
            db.add_all([share, recent_share, missing_clock, draft])
            db.commit()
            stale_id, fresh_id = stale.id, fresh.id
            share_id, recent_id, missing_id, draft_id = share.id, recent_share.id, missing_clock.id, draft.id
        finally:
            db.close()

        self.assertTrue(ultrasound_job_is_stale(type("R", (), {"ai_status": "processing", "ai_started_at": now - timedelta(minutes=15)})(), now))
        self.assertFalse(ultrasound_job_is_stale(type("R", (), {"ai_status": "processing", "ai_started_at": now - timedelta(minutes=14, seconds=59)})(), now))
        self.assertFalse(ultrasound_job_is_stale(type("R", (), {"ai_status": "completed", "ai_started_at": now - timedelta(hours=2)})(), now))

        db = _session()
        try:
            stale = db.get(UltrasoundReport, stale_id)
            fresh = db.get(UltrasoundReport, fresh_id)
            self.assertTrue(release_stale_ultrasound(db, stale, now=now))
            self.assertFalse(release_stale_ultrasound(db, fresh, now=now))
            self.assertEqual(stale.ai_status, "failed")
            self.assertEqual(stale.ai_error, ULTRASOUND_STALE_ERROR)
            _text_unchanged(stale)
            self.assertEqual(fresh.ai_status, "processing")
            _text_unchanged(fresh)

            share = db.get(InsuranceMaterialShare, share_id)
            recent = db.get(InsuranceMaterialShare, recent_id)
            missing = db.get(InsuranceMaterialShare, missing_id)
            self.assertTrue(release_stale_insurance(db, share, now=now))
            self.assertFalse(release_stale_insurance(db, recent, now=now))
            self.assertTrue(release_stale_insurance(db, missing, now=now))
            self.assertEqual(share.generation_error, INSURANCE_STALE_ERROR)
            self.assertEqual(recent.generation_status, "processing")
            self.assertEqual(missing.generation_status, "failed")

            draft = db.get(VisitConsultationDraft, draft_id)
            self.assertTrue(release_stale_consultation(db, draft, now=now))
            self.assertEqual(draft.status, "failed")
            self.assertEqual(draft.ai_error, CONSULTATION_STALE_ERROR)
            self.assertEqual(draft.transcript, "超时转写")
        finally:
            db.close()


def _login(client: TestClient) -> None:
    page = client.get("/admin/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    response = client.post("/admin/login", data={
        "username": "admin",
        "password": "ultrasound-recovery-test",
        "csrf_token": token,
    })
    assert response.status_code == 303, response.text[:300]


class UltrasoundHttpTests(unittest.TestCase):
    def test_restart_and_stale_generation_can_be_retried_without_rewriting_text(self):
        started = utc_now_naive()
        db = _session()
        try:
            customer, pet, _visit, order = _chain(db)
            restarted = _ultrasound(
                db, order, customer_id=customer.id, pet_id=pet.id,
                started=started - timedelta(days=1),
            )
            live = _ultrasound(
                db, order, customer_id=customer.id, pet_id=pet.id,
                started=started,
            )
            db.commit()
            restarted_id, live_id = restarted.id, live.id
        finally:
            db.close()

        async def fail_draft(payload):
            fail_draft.calls += 1
            return {"ok": False, "error": "测试中止生成"}

        fail_draft.calls = 0

        with TestClient(app, base_url="https://testserver", follow_redirects=False) as client, \
             patch("app.services.ultrasound_report.draft_ultrasound_text", fail_draft):
            db = _session()
            try:
                restarted = db.get(UltrasoundReport, restarted_id)
                live = db.get(UltrasoundReport, live_id)
                self.assertEqual(restarted.ai_status, "failed")
                self.assertEqual(restarted.ai_error, ULTRASOUND_INTERRUPTED_ERROR)
                _text_unchanged(restarted)
                self.assertEqual(live.ai_status, "processing")
                _text_unchanged(live)
            finally:
                db.close()

            _login(client)
            page = client.get(f"/admin/ultrasound-reports/{restarted_id}/edit")
            csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
            self.assertEqual(page.status_code, 200)
            self.assertIn(ULTRASOUND_INTERRUPTED_ERROR, page.text)
            self.assertIn(FINDINGS, page.text)
            self.assertIn("LA Diam", page.text)
            self.assertNotIn("disabled>重新识别原PDF", page.text)
            # 页面脚本里仍有手动重试时的 pollAiStatus()，但失败状态不能在加载时自动开轮询。
            self.assertIsNone(re.search(r"pollAiStatus\(\);\s*</script>", page.text))

            blocked = client.post("/admin/ultrasound/ai-draft", json={
                "csrf_token": csrf,
                "report_id": live_id,
                "exam_type": "cardiac",
                "vet_findings": VET_FINDINGS,
                "groups": [{"group": "M测量·左室", "rows": [{"name": "LA Diam", "value": "0.96", "unit": "cm"}]}],
            })
            self.assertEqual(blocked.status_code, 409)
            self.assertEqual(blocked.json()["error"], "已有生成任务正在进行，请稍候")
            self.assertEqual(fail_draft.calls, 0)

            db = _session()
            try:
                stale = db.get(UltrasoundReport, live_id)
                stale.ai_started_at = utc_now_naive() - timedelta(minutes=16)
                db.commit()
            finally:
                db.close()

            status = client.get(f"/admin/ultrasound-reports/{live_id}/ai-status")
            body = status.json()
            self.assertEqual(body["status"], "failed")
            self.assertEqual(body["error"], ULTRASOUND_STALE_ERROR)
            self.assertEqual(body["findings"], FINDINGS)
            self.assertEqual(body["conclusion"], CONCLUSION)
            self.assertEqual(body["advice"], ADVICE)
            self.assertEqual(body["group_count"], 1)
            self.assertEqual(fail_draft.calls, 0)

            allowed = client.post("/admin/ultrasound/ai-draft", json={
                "csrf_token": csrf,
                "report_id": live_id,
                "exam_type": "cardiac",
                "vet_findings": VET_FINDINGS,
                "groups": [{"group": "M测量·左室", "rows": [{"name": "LA Diam", "value": "0.96", "unit": "cm"}]}],
            })
            self.assertEqual(allowed.status_code, 202, allowed.text)
            self.assertTrue(allowed.json()["ok"])
            self.assertEqual(fail_draft.calls, 1)

            db = _session()
            try:
                report = db.get(UltrasoundReport, live_id)
                self.assertEqual(report.ai_status, "failed")
                self.assertEqual(report.ai_error, "测试中止生成")
                _text_unchanged(report)
            finally:
                db.close()

            db = _session()
            try:
                _customer, _pet, _visit, order = _chain(db)
                retry_report = _ultrasound(db, order, started=utc_now_naive() - timedelta(minutes=20))
                db.commit()
                retry_id = retry_report.id
            finally:
                db.close()

            # 编辑页打开时就应放开超时任务，而不是继续显示「正在生成」。
            edit = client.get(f"/admin/ultrasound-reports/{retry_id}/edit")
            self.assertIn(ULTRASOUND_STALE_ERROR, edit.text)
            self.assertIn(FINDINGS, edit.text)

            db = _session()
            try:
                again = db.get(UltrasoundReport, retry_id)
                again.ai_status = "processing"
                again.ai_error = ""
                again.ai_started_at = utc_now_naive() - timedelta(minutes=20)
                db.commit()
            finally:
                db.close()

            retried = client.post(
                f"/admin/ultrasound-reports/{retry_id}/retry-ai",
                data={"csrf_token": csrf},
            )
            self.assertEqual(retried.status_code, 200, retried.text)
            self.assertEqual(retried.json()["status"], "processing")
            self.assertEqual(fail_draft.calls, 2)
            db = _session()
            try:
                report = db.get(UltrasoundReport, retry_id)
                self.assertNotEqual(report.ai_status, "processing")
                _text_unchanged(report)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
