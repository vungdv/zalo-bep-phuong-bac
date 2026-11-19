import pytest

from order_extractor.db.connection import open_database
from order_extractor.events import NewOrder
from order_extractor.recorder.orders_repo import DAY_MS, Order, OrdersRepo

NOW = 1_800_000_000_000


def new_order(n: int = 1, **kw) -> NewOrder:
    fields = {
        "conversation_key": "g1",
        "message_key": f"{NOW - 1000 + n}_77",
        "number": n,
        "sent_at_ms": NOW - 1000 + n,
        "conversation_title": "Nhóm khách sỉ",
        "sender": "Lan",
        "text": f"cho em {n} áo",
    }
    fields.update(kw)
    return NewOrder(**fields)


@pytest.fixture
def repo() -> OrdersRepo:
    return OrdersRepo(open_database(":memory:"))


def test_store_inserts_new_order_due_at_once(repo):
    order_id = repo.store(new_order(2), NOW)
    assert order_id == 1
    order = repo.get(order_id)
    assert order == Order(
        id=1,
        conversation_key="g1",
        message_key=f"{NOW - 998}_77",
        number=2,
        sent_at_ms=NOW - 998,
        conversation_title="Nhóm khách sỉ",
        sender="Lan",
        text="cho em 2 áo",
        status="NEW",
        attempts=0,
        created_at_ms=NOW,
        next_attempt_at_ms=NOW,
        sheet_row=None,
        error=None,
    )
    assert [o.id for o in repo.due(NOW, limit=50)] == [1]  # due immediately [R5.1]


def test_duplicate_store_is_ignored(repo):
    assert repo.store(new_order(1), NOW) == 1
    assert repo.store(new_order(1, number=99, text="changed"), NOW + 5) is None
    assert repo.counts()["NEW"] == 1
    assert repo.get(1).number == 1
    # same message key in another conversation is a different order (ids may skip; they are never reused)
    other = repo.store(new_order(1, conversation_key="g2"), NOW)
    assert other is not None and other > 1
    assert repo.get(other).conversation_key == "g2"


def test_get_unknown_returns_none(repo):
    assert repo.get(42) is None
    assert repo.orders_for_ids([]) == []
    assert repo.oldest_pending() is None


def test_due_respects_backoff_order_and_limit(repo):
    for n in range(1, 5):
        repo.store(new_order(n), NOW + n)
    repo.schedule_retry(1, attempts=1, next_attempt_at_ms=NOW + 10_000, error="timeout")
    assert [o.id for o in repo.due(NOW + 9_999, limit=50)] == [2, 3, 4]  # backed-off order not due early
    assert [o.id for o in repo.due(NOW + 2, limit=50)] == [2]  # created later → not due yet
    assert [o.id for o in repo.due(NOW + 10_000, limit=2)] == [2, 3]
    assert [o.id for o in repo.due(NOW + 10_000, limit=50)] == [2, 3, 4, 1]
    retried = repo.get(1)
    assert (retried.status, retried.attempts, retried.error) == ("NEW", 1, "timeout")


def test_mark_recorded_and_failed_leave_due(repo):
    repo.store(new_order(1), NOW)
    repo.store(new_order(2), NOW)
    repo.mark_recorded(1, sheet_row=7)
    repo.mark_failed(2, attempts=5, error="x" * 1000)
    assert repo.due(NOW + DAY_MS, limit=50) == []
    assert repo.get(1).status == "RECORDED"
    assert repo.get(1).sheet_row == 7
    failed = repo.get(2)
    assert (failed.status, failed.attempts, len(failed.error)) == ("FAILED", 5, 500)
    assert repo.counts() == {"NEW": 0, "RECORDED": 1, "FAILED": 1}


def test_mark_recorded_from_failed_and_is_noop_when_recorded(repo):
    repo.store(new_order(1), NOW)
    repo.mark_failed(1, attempts=5, error="boom")
    repo.mark_recorded(1)
    assert (repo.get(1).status, repo.get(1).sheet_row, repo.get(1).error) == ("RECORDED", None, None)
    repo.mark_recorded(1, sheet_row=9)  # already RECORDED: unchanged
    assert repo.get(1).sheet_row is None


def test_schedule_retry_and_mark_failed_only_touch_new(repo):
    repo.store(new_order(1), NOW)
    repo.mark_recorded(1, 3)
    repo.schedule_retry(1, attempts=1, next_attempt_at_ms=NOW + 1, error="late")
    repo.mark_failed(1, attempts=5, error="late")
    order = repo.get(1)
    assert (order.status, order.attempts, order.error) == ("RECORDED", 0, None)


def test_reset_failed(repo):
    repo.store(new_order(1), NOW)
    repo.store(new_order(2), NOW)
    repo.mark_failed(1, attempts=5, error="boom")
    assert repo.reset_failed(1, NOW + 100) is True
    order = repo.get(1)
    assert (order.status, order.attempts, order.next_attempt_at_ms, order.error) == ("NEW", 0, NOW + 100, None)
    assert repo.reset_failed(2, NOW) is False  # NEW, not FAILED
    assert repo.reset_failed(99, NOW) is False


def test_unrecorded_orders_for_ids_and_mark_recorded_many(repo):
    for n in range(1, 5):
        repo.store(new_order(n), NOW)
    repo.mark_recorded(2, 5)
    repo.mark_failed(3, attempts=5, error="e")
    assert [(o.id, o.status) for o in repo.unrecorded()] == [(1, "NEW"), (3, "FAILED"), (4, "NEW")]
    assert [o.id for o in repo.orders_for_ids([4, 1, 77])] == [1, 4]
    repo.mark_recorded_many([1, 3])
    assert [o.id for o in repo.unrecorded()] == [4]


def test_oldest_pending_and_counts(repo):
    repo.store(new_order(1), NOW + 5)
    repo.store(new_order(2), NOW + 1)
    repo.store(new_order(3), NOW)
    repo.mark_recorded(3)
    repo.mark_failed(2, attempts=5, error="e")
    assert repo.oldest_pending().id == 2
    assert repo.counts() == {"NEW": 1, "RECORDED": 1, "FAILED": 1}


def test_purge_only_recorded_content_older_than_window(repo):
    old = NOW - 31 * DAY_MS
    repo.store(new_order(1, sent_at_ms=old, message_key="a"), NOW)  # RECORDED, old  → purged
    repo.store(new_order(2, sent_at_ms=old, message_key="b"), NOW)  # NEW, old       → kept
    repo.store(new_order(3, sent_at_ms=old, message_key="c"), NOW)  # FAILED, old    → kept
    repo.store(new_order(4, sent_at_ms=NOW, message_key="d"), NOW)  # RECORDED, new  → kept
    repo.mark_recorded(1, 2)
    repo.mark_failed(3, attempts=5, error="e")
    repo.mark_recorded(4, 3)

    assert repo.purge(NOW, retention_days=30) == 1
    purged = repo.get(1)
    assert (purged.text, purged.sender, purged.conversation_title) == (None, None, None)
    assert (purged.number, purged.message_key, purged.status, purged.sheet_row) == (1, "a", "RECORDED", 2)
    for kept in (2, 3, 4):
        assert repo.get(kept).text is not None
        assert repo.get(kept).sender == "Lan"
    assert repo.purge(NOW, retention_days=30) == 0  # idempotent
    # the purged order still blocks a duplicate store
    assert repo.store(new_order(1, sent_at_ms=old, message_key="a"), NOW) is None


def test_purge_boundary_is_strictly_before_cutoff(repo):
    cutoff = NOW - 30 * DAY_MS
    repo.store(new_order(1, sent_at_ms=cutoff, message_key="a"), NOW)
    repo.mark_recorded(1)
    assert repo.purge(NOW, retention_days=30) == 0
    assert repo.purge(NOW + 1, retention_days=30) == 1


def test_mark_recorded_rows_sets_each_row_in_one_transaction(repo):
    for n in range(1, 4):
        repo.store(new_order(n), NOW)
    repo.mark_recorded_rows([(1, 2), (2, None)])
    assert [(o.id, o.status, o.sheet_row) for o in repo.orders_for_ids([1, 2, 3])] == [
        (1, "RECORDED", 2),
        (2, "RECORDED", None),
        (3, "NEW", None),
    ]

    def interrupted():
        yield (3, 4)
        raise RuntimeError("interrupted")

    with pytest.raises(RuntimeError):
        repo.mark_recorded_rows(interrupted())
    assert repo.get(3).status == "NEW"  # rolled back: all or nothing
