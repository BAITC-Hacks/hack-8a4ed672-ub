"""MVP browser API contracts: protocol review, exports and action permissions."""
from __future__ import annotations

import pytest
from conftest import create_meeting, login, make_user
from hattama.db.models import (
    ActionItem,
    ExtractionRun,
    Job,
    Meeting,
    MeetingMember,
    ReviewIssue,
    SummaryRevision,
    TranscriptRevision,
    TranscriptSegment,
)
from hattama.db.session import session_scope

pytestmark = pytest.mark.integration


def reviewed_meeting(secretary):
    meeting = create_meeting(secretary)
    with session_scope() as db:
        m = db.get(Meeting, meeting["id"])
        m.status = "NEEDS_REVIEW"
        revision = TranscriptRevision(meeting_id=m.id, number=1, kind="final", status="complete")
        db.add(revision)
        db.flush()
        m.active_transcript_revision_id = revision.id
        segment = TranscriptSegment(meeting_id=m.id, revision_id=revision.id, source_key="room", start_ms=0,
                                    end_ms=3000, text="Подготовить отчёт", original_text="Подготовить отчёт",
                                    language="ru", state="final")
        summary = SummaryRevision(meeting_id=m.id, number=1, origin="machine",
                                  transcript_revision_id=revision.id,
                                  content={"decisions": [{"text": "Подготовить отчёт", "flags": []}]})
        run = ExtractionRun(meeting_id=m.id, kind="final", idempotency_key="test-final",
                            transcript_revision_id=revision.id, status="succeeded")
        action = ActionItem(meeting_id=m.id, fingerprint="mvp", number=1, action="Подготовить отчёт",
                            assignee={"name": "Секретарь"}, assignee_user_id=secretary.user_id,
                            deadline={"kind": "missing"})
        db.add_all([segment, summary, action, run])
        db.flush()
        m.active_summary_id = summary.id
        return m.id, action.id, segment.id


def test_review_execution_approval_and_exports(secretary):
    mid, aid, sid = reviewed_meeting(secretary)
    client, headers = secretary.client, secretary.headers()
    listed = client.get("/api/v1/meetings").json()
    assert listed[0]["actions"] == 1
    assert client.get(f"/api/v1/meetings/{mid}/summary").json()["content"]["decisions"]
    assert client.get(f"/api/v1/meetings/{mid}/transcript").json()["segments"][0]["id"] == sid
    action = client.get("/api/v1/actions?scope=mine").json()[0]
    assert action["can_update_execution"] is True
    assert action["can_view_meeting"] is True
    updated = client.patch(f"/api/v1/actions/{aid}", headers=headers,
                           json={"version": action["version"], "action": "Подготовить итоговый отчёт",
                                 "review_state": "confirmed", "deadline_date": "2026-10-12"})
    assert updated.status_code == 200, updated.text
    assert updated.json()["deadline_confirmed"]
    stale = client.patch(f"/api/v1/actions/{aid}", headers=headers,
                         json={"version": action["version"], "action": "Устаревшая правка"})
    assert stale.status_code == 409
    result = client.post(f"/api/v1/actions/{aid}/execution", headers=headers, json={"execution_state": "done"})
    assert result.status_code == 200
    assert client.get("/api/v1/actions").json()[0]["execution_state"] == "done"
    segment = client.get(f"/api/v1/meetings/{mid}/transcript").json()["segments"][0]
    edited = client.patch(f"/api/v1/segments/{sid}", headers=headers,
                          json={"rev": segment["rev"], "text": "Подготовить итоговый отчёт"})
    assert edited.status_code == 200
    meeting = client.get(f"/api/v1/meetings/{mid}").json()
    approved = client.post(f"/api/v1/meetings/{mid}/approve", headers=headers, json={"version": meeting["version"]})
    assert approved.status_code == 200, approved.text
    assert client.get(f"/api/v1/meetings/{mid}").json()["status"] == "APPROVED"
    for fmt, signature in [("pdf", b"%PDF"), ("docx", b"PK")]:
        exported = client.post(f"/api/v1/meetings/{mid}/exports", headers=headers, json={"format": fmt})
        assert exported.status_code == 201, exported.text
        assert exported.json()["draft"] is False
        download = client.get(exported.json()["download"])
        assert download.status_code == 200
        assert download.content.startswith(signature)


def test_action_permissions_match_available_controls(app, secretary):
    mid, aid, _ = reviewed_meeting(secretary)
    viewer_id = make_user("viewer@example.test", "assignee")
    assignee_id = make_user("assignee@example.test", "assignee")
    with session_scope() as db:
        db.add(MeetingMember(meeting_id=mid, user_id=viewer_id, role="viewer"))
        db.get(ActionItem, aid).assignee_user_id = assignee_id
    # Match the existing execution policy even for a viewer with the assignee global role.
    viewer = login(app, "viewer@example.test")
    item = viewer.client.get("/api/v1/actions").json()[0]
    actual = viewer.client.post(f"/api/v1/actions/{aid}/execution", headers=viewer.headers(),
                                json={"execution_state": "in_progress"})
    assert item["can_update_execution"] == (actual.status_code == 200)
    assignee = login(app, "assignee@example.test")
    item = assignee.client.get("/api/v1/actions").json()[0]
    assert item["can_update_execution"] is True
    assert item["can_view_meeting"] is False
    assert assignee.client.get(f"/api/v1/meetings/{mid}").status_code == 404
    assert assignee.client.patch(f"/api/v1/actions/{aid}", headers=assignee.headers(),
                                 json={"version": item["version"], "action": "Нельзя"}).status_code == 404


def test_dashboard_assets_are_local_and_served(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "script-src 'self'" in page.headers["content-security-policy"]
        for path in ("app.js", "ui.js", "api.js", "style.css", "fonts/onest-cyrillic.woff2", "fonts/onest-latin.woff2"):
            assert client.get(f"/{path}").status_code == 200


def test_empty_reviewable_meeting_cannot_be_approved(secretary):
    mid = create_meeting(secretary)["id"]
    with session_scope() as db:
        db.get(Meeting, mid).status = "NEEDS_REVIEW"
    state = secretary.client.get(f"/api/v1/meetings/{mid}/readiness").json()
    assert not state["can_approve"] and not state["transcript_ready"] and not state["summary_ready"]
    version = secretary.client.get(f"/api/v1/meetings/{mid}").json()["version"]
    response = secretary.client.post(f"/api/v1/meetings/{mid}/approve", headers=secretary.headers(),
                                     json={"version": version})
    assert response.status_code == 409
    assert secretary.client.get(f"/api/v1/meetings/{mid}").json()["status"] == "NEEDS_REVIEW"


@pytest.mark.parametrize("invalid", ["blank_transcript", "empty_summary", "stale_summary", "failed_analysis",
                                      "processing", "blocker"])
def test_approval_checks_current_complete_data(secretary, invalid):
    from sqlalchemy import select

    mid, _, sid = reviewed_meeting(secretary)
    with session_scope() as db:
        m = db.get(Meeting, mid)
        if invalid == "blank_transcript":
            db.get(TranscriptSegment, sid).text = "  "
        elif invalid == "empty_summary":
            db.get(SummaryRevision, m.active_summary_id).content = {"decisions": [], "topics": ["Отчёт"]}
        elif invalid == "stale_summary":
            db.get(SummaryRevision, m.active_summary_id).transcript_revision_id = None
        elif invalid == "failed_analysis":
            db.scalar(select(ExtractionRun).where(ExtractionRun.meeting_id == mid)).status = "failed"
        elif invalid == "processing":
            db.add(Job(key=f"retry:{mid}", kind="extract_final", meeting_id=mid, state="running"))
        else:
            db.add(ReviewIssue(meeting_id=mid, dedupe_key="test", kind="test", severity="blocker", question="Ошибка"))
    ready = secretary.client.get(f"/api/v1/meetings/{mid}/readiness").json()
    assert ready["can_approve"] is False and ready["reasons"]
    version = secretary.client.get(f"/api/v1/meetings/{mid}").json()["version"]
    assert secretary.client.post(f"/api/v1/meetings/{mid}/approve", headers=secretary.headers(),
                                  json={"version": version}).status_code == 409


def test_ready_summary_without_action_items_can_be_approved(secretary):
    mid, aid, _ = reviewed_meeting(secretary)
    with session_scope() as db:
        db.get(ActionItem, aid).review_state = "rejected"
    assert secretary.client.get(f"/api/v1/meetings/{mid}/actions").json() == []
    assert secretary.client.get(f"/api/v1/meetings/{mid}/readiness").json()["can_approve"] is True
    version = secretary.client.get(f"/api/v1/meetings/{mid}").json()["version"]
    assert secretary.client.post(f"/api/v1/meetings/{mid}/approve", headers=secretary.headers(),
                                  json={"version": version}).status_code == 200


def test_retry_analysis_blocks_approval_and_keeps_prior_summary_until_ready(secretary):
    mid, _, _ = reviewed_meeting(secretary)
    response = secretary.client.post(f"/api/v1/meetings/{mid}/retry-analysis", headers=secretary.headers())
    assert response.status_code == 202
    with session_scope() as db:
        job = db.get(Job, response.json()["job_id"])
        assert job.payload["retry_id"] and job.kind == "extract_final"
        assert db.get(Meeting, mid).status == "PROCESSING"
    ready = secretary.client.get(f"/api/v1/meetings/{mid}/readiness").json()
    assert ready["processing"] is True and ready["can_approve"] is False


@pytest.mark.parametrize("url", ["https://meet.google.com:invalid/aaa-bbbb-ccc", "https://evil.test/meeting",
                                 "https://meet.google.com.evil.test/meeting", "http://meet.google.com/test"])
def test_invalid_meeting_url_is_validation_error(secretary, url):
    response = secretary.client.post("/api/v1/meetings", headers=secretary.headers(),
                                     json={"title": "Тест", "meeting_date": "2026-09-23",
                                           "platform": "google_meet", "meeting_url": url})
    assert response.status_code == 422


def test_meeting_url_persists_and_platform_cannot_invalidate_it(secretary):
    m = create_meeting(secretary, meeting_url="https://meet.google.com/aaa-bbbb-ccc")
    assert secretary.client.get(f"/api/v1/meetings/{m['id']}").json()["meeting_url"] == m["meeting_url"]
    r = secretary.client.patch(f"/api/v1/meetings/{m['id']}", headers=secretary.headers(),
                               json={"version": m["version"], "platform": "zoom_web"})
    assert r.status_code == 422


def test_empty_transcript_pipeline_is_failed_not_ready(secretary):
    from hattama.config import get_settings
    from hattama.pipeline.stages import stage_extract
    from sqlalchemy import select

    mid = create_meeting(secretary)["id"]
    with session_scope() as db:
        meeting = db.get(Meeting, mid)
        meeting.status = "PROCESSING"
        revision = TranscriptRevision(meeting_id=mid, number=1, kind="final", status="complete")
        db.add(revision)
        db.flush()
        meeting.active_transcript_revision_id = rid = revision.id
    assert stage_extract(get_settings(), mid, {"revision_id": rid}, live=False)["error"] == "empty_transcript"
    with session_scope() as db:
        assert db.get(Meeting, mid).status == "FAILED"
        assert db.scalar(select(ExtractionRun).where(ExtractionRun.meeting_id == mid)).status == "failed"
        issue = db.scalar(select(ReviewIssue).where(ReviewIssue.meeting_id == mid))
        assert issue.kind == "empty_transcript" and issue.severity == "blocker"
    assert secretary.client.get(f"/api/v1/meetings/{mid}/readiness").json()["can_approve"] is False
    assert secretary.client.post(f"/api/v1/meetings/{mid}/retry-analysis",
                                  headers=secretary.headers()).status_code == 409
