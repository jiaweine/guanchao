import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from guanchao.harness import ActiveRunError, AgentHarness
from guanchao.store import Store


def _target() -> dict:
    return {
        "platform": "weibo",
        "handle": "shared-case",
        "posts": [
            {"id": "p1", "text": "今天散步"},
            {"id": "p2", "text": "今天看书"},
        ],
    }


def _running_count(store: Store, case_id: str) -> int:
    conn = store._connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE case_id = ? AND status = 'running'",
            (case_id,),
        ).fetchone()
        return int(row[0])
    finally:
        store._close(conn)


def test_two_harness_instances_cannot_claim_the_same_case_simultaneously(tmp_path, monkeypatch):
    db = str(tmp_path / "shared.sqlite")
    first_store = Store(db)
    case = first_store.create_case("共享任务", "核查", [_target()])
    second_store = Store(db)
    first = AgentHarness(first_store)
    second = AgentHarness(second_store)

    # Keep the winning run in `running` long enough for the losing claimant to
    # observe it. The old implementation still raced because each Harness had a
    # different in-memory guard.
    release = Event()
    monkeypatch.setattr(first, "_execute", lambda run_id: release.wait(2))
    monkeypatch.setattr(second, "_execute", lambda run_id: release.wait(2))

    # Widen the old check/insert race deterministically. With run_claim the second
    # Harness cannot enter this delayed check until the first has inserted.
    first_active = first_store.active_run_for_case
    second_active = second_store.active_run_for_case

    def delayed_first(case_id):
        result = first_active(case_id)
        time.sleep(0.08)
        return result

    def delayed_second(case_id):
        result = second_active(case_id)
        time.sleep(0.08)
        return result

    monkeypatch.setattr(first_store, "active_run_for_case", delayed_first)
    monkeypatch.setattr(second_store, "active_run_for_case", delayed_second)

    def attempt(harness):
        try:
            return ("ok", harness.start(case["id"], "并发核查"))
        except ActiveRunError as exc:
            return ("active", str(exc))

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(attempt, (first, second)))

    assert sorted(kind for kind, _ in outcomes) == ["active", "ok"]
    active = first_store.active_run_for_case(case["id"])
    assert active is not None
    assert _running_count(first_store, case["id"]) == 1

    # Clean up the deliberately frozen worker and durable row.
    first_store.update_run(active["id"], active["state"], "failed")
    release.set()
    first.close()
    second.close()
