"""Tests for the Milestone 1 personal intelligence engine.

Each test asserts a grounding rule as well as the feature: extracted facts keep their
source conversation/message, duplicates are not recreated, ambiguous projects are not
guessed, and nothing becomes completed or confirmed without the owner's decision.
"""

import asyncio
import json
import re
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app import database, intelligence, ollama
from app.time_context import DEFAULT_TIMEZONE

REFERENCE = "2026-03-02T04:00:00+00:00"


def _workspace_id(client) -> str:
    return client.get("/api/projects").json()["workspaces"][0]["id"]


def _project(client, name: str) -> dict:
    response = client.post(
        "/api/projects",
        json={"name": name, "workspace_id": _workspace_id(client)},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _conversation(messages, reference: str = REFERENCE):
    """Creates a conversation with explicit, deterministic message timestamps."""
    with database.session() as connection:
        conversation_id = database.create_conversation(connection, messages[0][1][:60])
        saved = []
        for role, content in messages:
            message_id = str(uuid.uuid4())
            connection.execute(
                """INSERT INTO messages
                   (id, conversation_id, role, content, sources_json, created_at)
                   VALUES (?, ?, ?, ?, '[]', ?)""",
                (message_id, conversation_id, role, content, reference),
            )
            saved.append({"id": message_id, "role": role, "content": content, "created_at": reference})
        connection.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?", (reference, conversation_id)
        )
        connection.commit()
    return conversation_id, saved


def _scan(client, **payload) -> dict:
    response = client.post("/api/intelligence/scan", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def test_scan_extracts_tasks_deadlines_and_matched_project(client):
    project = _project(client, "R Matrix Personal AI Command Center")
    conversation_id, _ = _conversation(
        [
            (
                "user",
                "I need to finish the R Matrix dashboard by 15 May 2030. "
                "I need to write the deployment docs by Friday. "
                "I must fix the auth bug by 2026-04-01, this is urgent.",
            )
        ]
    )

    run = _scan(client, conversation_id=conversation_id)

    assert run["status"] == "succeeded"
    assert run["messages_scanned"] == 1
    assert run["tasks_detected"] == 3
    tasks = client.get("/api/tasks").json()
    titles = {task["title"] for task in tasks}
    assert {"Finish the R Matrix dashboard", "Write the deployment docs", "Fix the auth bug"} <= titles

    dashboard = next(task for task in tasks if task["title"] == "Finish the R Matrix dashboard")
    assert dashboard["due_at"] == "2030-05-15T18:29:00+00:00"  # 23:59 Asia/Kolkata, never a guessed time
    assert dashboard["due_precision"] == "date"
    assert dashboard["project_id"] == project["id"]
    assert dashboard["lifecycle"] == "pending_confirmation"
    assert dashboard["confirmation_status"] == "pending"
    assert dashboard["origin"] == "conversation"
    assert dashboard["conversation_id"] == conversation_id
    assert dashboard["source_message_id"]
    assert "finish the r matrix dashboard" in dashboard["source_excerpt"].casefold()
    assert 0 < dashboard["confidence"] < 1

    local_reference = datetime.fromisoformat(REFERENCE).astimezone(ZoneInfo(DEFAULT_TIMEZONE))
    expected_friday = local_reference.date() + timedelta(days=(4 - local_reference.weekday()) % 7)
    docs = next(task for task in tasks if task["title"] == "Write the deployment docs")
    assert docs["due_at"].startswith(expected_friday.isoformat())
    assert docs["lifecycle"] == "detected"  # no project matched for this task

    bug = next(task for task in tasks if task["title"] == "Fix the auth bug")
    assert bug["priority"] == "urgent"
    assert bug["due_at"].startswith("2026-04-01")


def test_scan_is_incremental_and_never_reprocesses_messages(client):
    conversation_id, _ = _conversation(
        [("user", "I need to review the budget by 2030-06-01.")]
    )
    before = client.get("/api/intelligence/status").json()
    assert before["checkpoints"]["unprocessed_messages"] == 1
    assert before["checkpoints"]["conversations"] == 0
    assert before["last_run"] is None

    first = _scan(client)
    assert first["messages_scanned"] == 1
    assert first["conversations_scanned"] == 1
    assert len(client.get("/api/tasks").json()) == 1

    second = _scan(client)
    assert second["messages_scanned"] == 0
    assert second["tasks_detected"] == 0
    assert len(client.get("/api/tasks").json()) == 1

    after = client.get("/api/intelligence/status").json()
    assert after["checkpoints"]["unprocessed_messages"] == 0
    assert after["checkpoints"]["messages_processed"] == 1
    assert after["checkpoints"]["conversations"] == 1
    assert after["tasks"]["detected"] == 1
    assert after["last_run"]["messages_scanned"] == 0
    runs = client.get("/api/intelligence/runs").json()
    assert [run["messages_scanned"] for run in runs] == [0, 1]


def test_reprocess_mode_does_not_duplicate_detected_work(client):
    conversation_id, _ = _conversation(
        [("user", "I need to rotate the API docs by 2030-12-09.")]
    )
    _scan(client, conversation_id=conversation_id)
    before = client.get("/api/tasks").json()

    run = _scan(client, conversation_id=conversation_id, mode="reprocess")

    after = client.get("/api/tasks").json()
    assert len(before) == len(after) == 1
    assert run["messages_scanned"] == 1
    assert run["duplicates_skipped"] == 1


def test_duplicate_tasks_are_not_created_twice(client):
    first_conversation, _ = _conversation(
        [("user", "I need to renew the domain by 2030-08-01.")]
    )
    _scan(client, conversation_id=first_conversation)

    second_conversation, _ = _conversation(
        [("user", "I need to renew the domain by 2030-08-01.")]
    )
    run = _scan(client, conversation_id=second_conversation)

    assert run["duplicates_skipped"] == 1
    assert run["tasks_detected"] == 0
    tasks = client.get("/api/tasks").json()
    assert len([task for task in tasks if "domain" in task["title"].casefold()]) == 1


def test_assistant_messages_never_become_stored_facts(client):
    project = _project(client, "Deployment Pipeline")
    conversation_id, _ = _conversation(
        [
            ("user", "What is left on the deployment pipeline?"),
            (
                "assistant",
                "I completed the release checklist and the deployment pipeline is 90% done.",
            ),
        ]
    )

    run = _scan(client, conversation_id=conversation_id)

    assert run["messages_scanned"] == 2
    assert run["messages_skipped"] == 1
    assert run["tasks_detected"] == 0
    assert client.get("/api/tasks").json() == []
    assert client.get("/api/intelligence/proposals").json() == []
    assert client.get(f"/api/projects/{project['id']}").json()["progress"] == 0


def test_progress_is_proposed_and_applied_only_when_confirmed(client):
    project = _project(client, "Deployment Pipeline")
    conversation_id, _ = _conversation(
        [("user", "The deployment pipeline is 80% done.")]
    )

    _scan(client, conversation_id=conversation_id)

    proposals = client.get("/api/intelligence/proposals?kind=project_progress").json()
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal["status"] == "pending"
    assert proposal["progress_value"] == 80
    assert proposal["confidence"] < 1
    assert proposal["evidence"][0]["conversation_id"] == conversation_id
    assert proposal["evidence"][0]["message_id"]
    assert "80%" in proposal["evidence"][0]["excerpt"]
    assert client.get(f"/api/projects/{project['id']}").json()["progress"] == 0

    review = client.post(
        f"/api/intelligence/proposals/{proposal['id']}/review", json={"approve": True}
    )
    assert review.status_code == 200, review.text
    assert review.json()["applied"]["progress"] == 80
    assert client.get(f"/api/projects/{project['id']}").json()["progress"] == 80

    again = client.post(
        f"/api/intelligence/proposals/{proposal['id']}/review", json={"approve": True}
    )
    assert again.status_code == 409


def test_rejected_proposal_does_not_change_project_progress(client):
    project = _project(client, "Quarterly Reporting")
    conversation_id, _ = _conversation([("user", "The quarterly reporting work is 40% done.")])
    _scan(client, conversation_id=conversation_id)

    proposal = client.get("/api/intelligence/proposals?kind=project_progress").json()[0]
    review = client.post(
        f"/api/intelligence/proposals/{proposal['id']}/review", json={"approve": False}
    )

    assert review.status_code == 200
    assert review.json()["proposal"]["status"] == "rejected"
    assert review.json()["applied"] is None
    assert client.get(f"/api/projects/{project['id']}").json()["progress"] == 0


def test_new_project_requires_confirmation_and_is_not_duplicated(client):
    conversation_id, _ = _conversation(
        [("user", "I'm building the Greenhouse Monitor using an ESP32 and a soil sensor.")]
    )

    run = _scan(client, conversation_id=conversation_id)

    assert run["proposals_created"] >= 1
    assert all(
        project["name"] != "Greenhouse Monitor"
        for project in client.get("/api/projects").json()["projects"]
    )
    proposals = client.get("/api/intelligence/proposals?kind=project").json()
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal["candidate_project_name"] == "Greenhouse Monitor"
    assert proposal["confidence"] < 1

    review = client.post(
        f"/api/intelligence/proposals/{proposal['id']}/review", json={"approve": True}
    )
    assert review.status_code == 200, review.text
    applied = review.json()["applied"]
    assert applied["created"] is True
    project = client.get(f"/api/projects/{applied['project_id']}").json()
    assert project["name"] == "Greenhouse Monitor"
    assert project["status"] == "active"
    assert project["progress"] == 0

    later_conversation, _ = _conversation(
        [("user", "I need to calibrate the Greenhouse Monitor sensor by 2030-07-01.")]
    )
    _scan(client, conversation_id=later_conversation)
    matched = [
        task
        for task in client.get("/api/tasks").json()
        if task["project_id"] == project["id"]
    ]
    assert matched and matched[0]["title"] == "Calibrate the Greenhouse Monitor sensor"
    assert client.get("/api/intelligence/proposals?kind=project").json() == []


def test_project_matching_uses_aliases_and_keeps_projects_separate(client):
    rocket = _project(client, "Alpha Rocket")
    garden = _project(client, "Beta Garden")
    alias = client.post(
        f"/api/intelligence/projects/{rocket['id']}/aliases",
        json={"alias": "telemetry stack"},
    )
    assert alias.status_code == 201, alias.text
    assert client.get(f"/api/intelligence/projects/{rocket['id']}/aliases").json()[0]["alias"] == "telemetry stack"

    conflict = client.post(
        f"/api/intelligence/projects/{garden['id']}/aliases",
        json={"alias": "telemetry stack"},
    )
    assert conflict.status_code == 409

    conversation_id, _ = _conversation(
        [
            ("user", "I need to verify the telemetry stack by 2030-10-01."),
            ("user", "I need to order garden seeds by 2030-10-02."),
        ]
    )
    _scan(client, conversation_id=conversation_id)

    tasks = {task["title"]: task for task in client.get("/api/tasks").json()}
    assert tasks["Verify the telemetry stack"]["project_id"] == rocket["id"]
    assert tasks["Order garden seeds"]["project_id"] == garden["id"]


def test_ambiguous_project_is_not_attributed(client):
    _project(client, "Alpha Rocket")
    _project(client, "Alpha Rocket Mobile")
    conversation_id, _ = _conversation(
        [("user", "I need to test rocket telemetry by 2030-11-01.")]
    )

    _scan(client, conversation_id=conversation_id)

    task = client.get("/api/tasks").json()[0]
    assert task["project_id"] is None
    assert task["lifecycle"] == "detected"
    assert task["confirmation_status"] == "pending"
    assert "project" in (task["confirmation_note"] or "").casefold()


def test_completion_requires_evidence_and_confirmation(client):
    project = _project(client, "Release Train")
    task = client.post(
        "/api/tasks",
        json={"title": "Finish the release checklist", "project_id": project["id"]},
    ).json()
    conversation_id, _ = _conversation(
        [("user", "I finished the release checklist today.")]
    )

    _scan(client, conversation_id=conversation_id)

    proposals = client.get("/api/intelligence/proposals?kind=task_completion").json()
    assert len(proposals) == 1
    assert proposals[0]["task_id"] == task["id"]
    assert proposals[0]["evidence"][0]["conversation_id"] == conversation_id
    assert client.get("/api/tasks").json()[0]["status"] == "pending"

    review = client.post(
        f"/api/intelligence/proposals/{proposals[0]['id']}/review", json={"approve": True}
    )
    assert review.status_code == 200, review.text
    completed = client.get("/api/tasks").json()[0]
    assert completed["status"] == "done"
    assert completed["lifecycle"] == "completed"
    with database.session() as connection:
        row = connection.execute(
            "SELECT completion_evidence FROM application_tasks WHERE id = ?", (task["id"],)
        ).fetchone()
    assert "finished the release checklist" in row["completion_evidence"].casefold()


def test_completion_without_a_matching_task_is_not_invented(client):
    _project(client, "Release Train")
    conversation_id, _ = _conversation(
        [("user", "I finished the quarterly tax filing today.")]
    )

    _scan(client, conversation_id=conversation_id)

    assert client.get("/api/intelligence/proposals?kind=task_completion").json() == []
    assert client.get("/api/tasks").json() == []


def test_blocker_is_proposed_then_tracked_as_blocked_task(client):
    project = _project(client, "Payments Service")
    conversation_id, _ = _conversation(
        [("user", "The payments service is blocked by the missing webhook credentials.")]
    )

    _scan(client, conversation_id=conversation_id)

    proposals = client.get("/api/intelligence/proposals?kind=blocker").json()
    assert len(proposals) == 1
    assert proposals[0]["project_id"] == project["id"]

    review = client.post(
        f"/api/intelligence/proposals/{proposals[0]['id']}/review", json={"approve": True}
    )
    assert review.status_code == 200, review.text
    blocked = review.json()["applied"]["task"]
    assert blocked["status"] == "blocked"
    assert blocked["lifecycle"] == "blocked"
    assert blocked["project_id"] == project["id"]


def test_milestone_proposal_creates_an_existing_style_milestone(client):
    project = _project(client, "Docs Revamp")
    conversation_id, _ = _conversation(
        [("user", "Milestone: publish the new docs site by 2030-09-15.")]
    )

    _scan(client, conversation_id=conversation_id)

    proposals = client.get("/api/intelligence/proposals?kind=milestone").json()
    assert len(proposals) == 1
    assert proposals[0]["milestone_target_date"] == "2030-09-15"

    review = client.post(
        f"/api/intelligence/proposals/{proposals[0]['id']}/review", json={"approve": True}
    )
    assert review.status_code == 200, review.text
    milestones = client.get(f"/api/projects/{project['id']}/milestones").json()
    assert len(milestones) == 1
    assert milestones[0]["target_date"] == "2030-09-15"


def test_task_lifecycle_states_are_derived_from_stored_facts():
    now = "2026-06-01T00:00:00+00:00"
    assert database.task_lifecycle({"status": "pending"}, now) == "detected"
    assert (
        database.task_lifecycle(
            {"status": "pending", "confirmation_status": "pending"}, now
        )
        == "detected"
    )
    assert (
        database.task_lifecycle(
            {"status": "pending", "confirmation_status": "pending", "project_id": "p"}, now
        )
        == "pending_confirmation"
    )
    assert (
        database.task_lifecycle(
            {"status": "pending", "due_at": "2026-07-01T00:00:00+00:00"}, now
        )
        == "upcoming"
    )
    assert (
        database.task_lifecycle(
            {"status": "pending", "due_at": "2026-05-01T00:00:00+00:00"}, now
        )
        == "overdue"
    )
    assert database.task_lifecycle({"status": "in_progress"}, now) == "in_progress"
    assert database.task_lifecycle({"status": "blocked"}, now) == "blocked"
    assert database.task_lifecycle({"status": "done"}, now) == "completed"
    assert (
        database.task_lifecycle({"status": "pending", "archived_at": "2026-01-01"}, now)
        == "archived"
    )


def test_confirm_archive_and_lifecycle_filters(client):
    project = _project(client, "Lifecycle Project")
    conversation_id, _ = _conversation(
        [("user", "I need to draft the launch email by 2030-09-01.")]
    )
    _scan(client, conversation_id=conversation_id)

    detected = client.get("/api/tasks?lifecycle=detected").json()
    assert len(detected) == 1
    task_id = detected[0]["id"]

    confirmed = client.post(
        f"/api/tasks/{task_id}/confirm", json={"project_id": project["id"]}
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["confirmation_status"] == "confirmed"
    assert confirmed.json()["lifecycle"] == "upcoming"
    assert confirmed.json()["project_id"] == project["id"]

    archived = client.post(f"/api/tasks/{task_id}/archive")
    assert archived.status_code == 200
    assert archived.json()["lifecycle"] == "archived"
    assert client.get("/api/tasks").json() == []
    archived_list = client.get("/api/tasks?include_archived=true&lifecycle=archived").json()
    assert len(archived_list) == 1
    assert client.get("/api/tasks?lifecycle=archived").json() == []
    assert client.get("/api/tasks?lifecycle=not-a-state").status_code == 422


def test_archived_task_is_not_redetected(client):
    conversation_id, _ = _conversation(
        [("user", "I need to clean the archive bucket by 2030-12-12.")]
    )
    _scan(client, conversation_id=conversation_id)
    task = client.get("/api/tasks").json()[0]
    client.post(f"/api/tasks/{task['id']}/archive")

    run = _scan(client, conversation_id=conversation_id, mode="reprocess")

    assert run["duplicates_skipped"] >= 1
    assert client.get("/api/tasks").json() == []


def test_intelligence_summary_reports_progress_deadlines_and_risks(client):
    project = _project(client, "Summary Project")
    client.post(
        "/api/tasks",
        json={
            "title": "Pay the invoice",
            "project_id": project["id"],
            "due_at": "2020-01-01T10:00",
        },
    )
    upcoming = client.post(
        "/api/tasks",
        json={
            "title": "Ship the beta",
            "project_id": project["id"],
            "due_at": "2035-01-01T10:00",
        },
    ).json()
    blocked = client.post(
        "/api/tasks",
        json={"title": "Waiting on API keys", "project_id": project["id"]},
    ).json()
    client.post(f"/api/tasks/{blocked['id']}/status", json={"status": "blocked"})
    client.post(f"/api/tasks/{upcoming['id']}/complete")

    summary = client.get("/api/intelligence/summary").json()

    assert summary["scope"] == {"project_id": None}
    project_summary = next(
        item for item in summary["projects"] if item["project_id"] == project["id"]
    )
    assert "Summary Project" in project_summary["summary"]
    assert project_summary["current_priorities"][0]["title"] == "Pay the invoice"
    priorities = {task["title"] for task in summary["current_priorities"]}
    assert {"Pay the invoice", "Waiting on API keys"} <= priorities
    assert any(item["kind"] == "task_completed" for item in summary["recent_progress"])
    risk_kinds = {risk["kind"] for risk in summary["risks"]}
    assert {"overdue_task", "blocked_task"} <= risk_kinds
    assert summary["pending_confirmations"]["tasks"] == 0

    scoped = client.get(f"/api/intelligence/summary?project_id={project['id']}").json()
    assert scoped["scope"] == {"project_id": project["id"]}
    assert len(scoped["projects"]) == 1


def test_status_reports_pending_work_without_scanning(client):
    _conversation([("user", "I need to test the backup restore by 2030-12-01.")])

    status = client.get("/api/intelligence/status").json()

    assert status["checkpoints"]["unprocessed_messages"] == 1
    assert status["tasks"]["detected"] == 0
    assert status["proposals"]["pending"] == 0
    assert status["last_run"] is None
    assert status["schedule"]["max_messages_per_scan"] >= 1


def test_model_hints_drop_ungrounded_facts(monkeypatch):
    async def fake_chat(messages, model=None):
        return json.dumps(
            {
                "tasks": [
                    {
                        "message_id": "m1",
                        "title": "prepare the sprint demo",
                        "deadline_phrase": None,
                        "priority": "high",
                    },
                    {
                        "message_id": "m1",
                        "title": "book the venue",
                        "deadline_phrase": "by 2030-01-01",
                        "priority": "high",
                    },
                    {
                        "message_id": "m1",
                        "title": "invent something unrelated",
                        "deadline_phrase": None,
                        "priority": "urgent",
                    },
                ]
            }
        )

    monkeypatch.setattr(ollama, "chat", fake_chat)
    hints = asyncio.run(
        intelligence.model_task_hints(
            [
                {
                    "id": "m1",
                    "created_at": REFERENCE,
                    "content": "I should prepare the sprint demo this week.",
                }
            ]
        )
    )

    assert [item["title"] for item in hints["m1"]] == ["prepare the sprint demo"]


def test_scan_can_add_grounded_local_model_tasks(client, monkeypatch):
    async def fake_chat(messages, model=None):
        prompt = messages[-1]["content"]
        message_id = re.search(r"- id=(\S+)", prompt).group(1)
        return json.dumps(
            {
                "tasks": [
                    {
                        "message_id": message_id,
                        "title": "prepare the sprint demo",
                        "deadline_phrase": None,
                        "priority": "high",
                    }
                ]
            }
        )

    monkeypatch.setattr(ollama, "chat", fake_chat)
    conversation_id, _ = _conversation(
        [("user", "Prepare the sprint demo before the review.")]
    )

    run = _scan(client, conversation_id=conversation_id, use_model=True)

    assert run["model_used"] is True
    assert run["tasks_detected"] == 1
    task = client.get("/api/tasks").json()[0]
    assert task["title"] == "Prepare the sprint demo"
    assert task["priority"] == "high"
    assert task["confirmation_status"] == "pending"
    assert "local model" in (task["confirmation_note"] or "")


def test_model_outage_does_not_break_the_scan(client, monkeypatch):
    async def failing_chat(messages, model=None):
        raise ollama.OllamaUnavailable("Ollama is not running")

    monkeypatch.setattr(ollama, "chat", failing_chat)
    conversation_id, _ = _conversation(
        [("user", "I need to update the status page by 2030-12-20.")]
    )

    run = _scan(client, conversation_id=conversation_id, use_model=True)

    assert run["status"] == "succeeded"
    assert run["model_used"] is False
    assert run["tasks_detected"] == 1


def test_deleting_a_conversation_keeps_task_evidence_and_checkpoint(client):
    conversation_id, _ = _conversation(
        [("user", "I need to archive the old builds by 2030-12-05.")]
    )
    _scan(client, conversation_id=conversation_id)
    task = client.get("/api/tasks").json()[0]

    assert client.delete(f"/api/conversations/{conversation_id}").status_code == 204

    remaining = client.get("/api/tasks").json()
    assert [item["id"] for item in remaining] == [task["id"]]
    assert remaining[0]["lifecycle"] == "detected"
    assert _scan(client)["messages_scanned"] == 0
    assert client.get("/api/intelligence/status").json()["checkpoints"]["unprocessed_messages"] == 0


def test_scan_rejects_unknown_conversation_and_unsafe_reprocess(client):
    assert client.post(
        "/api/intelligence/scan", json={"conversation_id": "missing"}
    ).status_code == 422
    assert client.post(
        "/api/intelligence/scan", json={"mode": "reprocess"}
    ).status_code == 422
    assert client.post(
        "/api/intelligence/scan", json={"mode": "everything"}
    ).status_code == 422
