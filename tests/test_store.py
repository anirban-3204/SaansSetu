"""The same contract for both stores. The DynamoDB one runs against a local fake (moto) and skips without it."""
import json

import pytest

from core.store import MemoryStore


@pytest.fixture(params=["memory", "dynamo"])
def store(request):
    if request.param == "memory":
        yield MemoryStore()
        return
    boto3 = pytest.importorskip("boto3")
    moto = pytest.importorskip("moto")
    from core.store import DynamoStore, create_table
    with moto.mock_aws():
        resource = boto3.resource("dynamodb", region_name="ap-south-1", aws_access_key_id="x", aws_secret_access_key="x")
        create_table("saanssetu-test", resource)
        create_table("saanssetu-test", resource)   # calling it again must be harmless
        yield DynamoStore("saanssetu-test", resource)


def test_put_get_overwrite_delete(store):
    assert store.get("run", "asansol", "2025-01-14T06:30") is None
    store.put("run", "asansol", "2025-01-14T06:30", {"a": 1, "hi": "हवा", "none": None, "f": 1.5, "list": [1, {"b": 2}]})
    assert store.get("run", "asansol", "2025-01-14T06:30") == {"a": 1, "hi": "हवा", "none": None, "f": 1.5, "list": [1, {"b": 2}]}
    store.put("run", "asansol", "2025-01-14T06:30", {"a": 2})
    assert store.get("run", "asansol", "2025-01-14T06:30") == {"a": 2}
    store.delete("run", "asansol", "2025-01-14T06:30")
    store.delete("run", "asansol", "2025-01-14T06:30")   # deleting what is not there is fine
    assert store.get("run", "asansol", "2025-01-14T06:30") is None


def test_keys_do_not_leak_between_kinds_or_towns(store):
    store.put("run", "asansol", "x", {"v": 1})
    store.put("run", "angul", "x", {"v": 2})
    store.put("latest", "asansol", "x", {"v": 3})
    assert store.get("run", "asansol", "x") == {"v": 1} and store.get("run", "angul", "x") == {"v": 2}
    assert [d for _, d in store.query("run", "asansol")] == [{"v": 1}]
    assert store.query("run", "nowhere") == []


def test_query_range_order_and_limit(store):
    hours = [f"2025-01-14T{h:02d}:00" for h in range(24)]
    store.put_many("aq", "site_1", {h: {"pm25": i} for i, h in enumerate(hours)}, ttl_days=20)
    assert [s for s, _ in store.query("aq", "site_1")] == hours
    got = store.query("aq", "site_1", start="2025-01-14T05:00", end="2025-01-14T07:00")
    assert [(s, d["pm25"]) for s, d in got] == [("2025-01-14T05:00", 5), ("2025-01-14T06:00", 6), ("2025-01-14T07:00", 7)]
    assert [s for s, _ in store.query("aq", "site_1", start="2025-01-14T22:00")] == hours[22:]
    assert [s for s, _ in store.query("aq", "site_1", end="2025-01-14T01:00")] == hours[:2]
    assert [s for s, _ in store.query("aq", "site_1", newest_first=True, limit=1)] == [hours[-1]]
    assert [s for s, _ in store.query("aq", "site_1", newest_first=True, limit=3)] == hours[:-4:-1]


def test_put_many_overwrites_the_same_hour(store):
    store.put_many("aq", "site_1", {"2025-01-14T06:00": {"pm25": 80}})
    store.put_many("aq", "site_1", {"2025-01-14T06:00": {"pm25": 95}, "2025-01-14T07:00": {"pm25": 99}})
    assert store.query("aq", "site_1") == [("2025-01-14T06:00", {"pm25": 95}), ("2025-01-14T07:00", {"pm25": 99})]


def test_put_if_absent_lets_only_the_first_writer_win(store):
    assert store.put_if_absent("alert", "asansol", "2025-01-14", {"who": "first"}) is True
    assert store.put_if_absent("alert", "asansol", "2025-01-14", {"who": "second"}) is False
    assert store.get("alert", "asansol", "2025-01-14") == {"who": "first"}
    assert store.put_if_absent("alert", "asansol", "2025-01-15", {"who": "next day"}) is True


def test_many_rows_come_back_complete(store):
    """More rows than one DynamoDB page of this size would hold, to exercise paging."""
    big = "x" * 4000
    store.put_many("aq", "site_big", {f"h{i:04d}": {"i": i, "pad": big} for i in range(400)})
    got = store.query("aq", "site_big")
    assert [d["i"] for _, d in got] == list(range(400))
    assert len(store.query("aq", "site_big", limit=350)) == 350


def test_bad_documents_are_refused(store):
    with pytest.raises(ValueError):
        store.put("run", "asansol", "x", {"pm25": float("nan")})   # NaN is not JSON; the dashboard would choke on it
    with pytest.raises(TypeError):
        store.put("run", "asansol", "x", {"when": object()})
    assert store.get("run", "asansol", "x") is None


def test_dynamo_refuses_an_item_too_large_to_store():
    boto3 = pytest.importorskip("boto3")
    moto = pytest.importorskip("moto")
    from core.store import DynamoStore, create_table
    with moto.mock_aws():
        resource = boto3.resource("dynamodb", region_name="ap-south-1", aws_access_key_id="x", aws_secret_access_key="x")
        create_table("t", resource)
        s = DynamoStore("t", resource)
        with pytest.raises(ValueError, match="400 KB"):
            s.put("run", "asansol", "x", {"pad": "x" * 500_000})
        s.put("aq", "site_1", "h", {"pm25": 1}, ttl_days=20)
        item = resource.Table("t").get_item(Key={"pk": "aq#site_1", "sk": "h"})["Item"]
        assert json.loads(item["doc"]) == {"pm25": 1} and int(item["ttl"]) > 1_700_000_000
