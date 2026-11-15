import json

import pytest
from django.utils import timezone

from catalog.models import (
    POLICY_FAIL_ROW,
    POLICY_KEEP_PRIMARY,
    MergeAttempt,
    MergeConflict,
    ParseJob,
)
from catalog.seed import ensure_seed


def login(client):
    return client.post("/login", {"username": "admin", "password": "123456"})


def make_job(**kwargs):
    defaults = {
        "title": "双源合并样例",
        "code": "dual_merge",
        "source_uri": "https://mock.local/dual",
        "primary_uri": "https://mock.local/dual-main",
        "secondary_uri": "https://mock.local/dual-supp",
    }
    defaults.update(kwargs)
    return ParseJob.objects.create(**defaults)


def parse_both(client, job):
    assert client.post(f"/jobs/{job.id}/parse/primary/").status_code == 302
    assert client.post(f"/jobs/{job.id}/parse/secondary/").status_code == 302
    job.refresh_from_db()
    return job


def lock_policy(client, job, policy):
    r = client.post(f"/jobs/{job.id}/policy/", {"conflict_policy": policy})
    assert r.status_code == 302
    job.refresh_from_db()
    return job


def merge(client, job, key="id", policy=None):
    return client.post(
        f"/jobs/{job.id}/merge/",
        {"merge_key": key, "conflict_policy": policy if policy is not None else job.conflict_policy},
    )


@pytest.fixture
def authed(client):
    login(client)
    return client


@pytest.mark.django_db
def test_login_page_mentions_password(client):
    r = client.get("/login")
    assert r.status_code == 200
    body = r.content.decode("utf-8")
    assert "登录" in body and "密码" in body


@pytest.mark.django_db
def test_login_and_list(client):
    ensure_seed()
    r = client.post("/login", {"username": "admin", "password": "123456"})
    assert r.status_code == 302
    r = client.get("/jobs/")
    assert r.status_code == 200
    assert ParseJob.objects.count() >= 1
    assert "解析任务" in r.content.decode("utf-8")


@pytest.mark.django_db
def test_list_shows_primary_secondary_conflicts_and_policy(authed):
    job = parse_both(authed, make_job())
    lock_policy(authed, job, POLICY_KEEP_PRIMARY)
    merge(authed, job)
    job.refresh_from_db()

    body = authed.get("/jobs/").content.decode("utf-8")
    assert "主份条数" in body and "次份条数" in body
    assert "冲突数" in body and "处理名" in body
    assert str(job.primary_count) in body
    assert str(job.secondary_count) in body
    assert f">{job.conflict_count}<" in body
    assert POLICY_KEEP_PRIMARY in body


@pytest.mark.django_db
def test_single_source_cannot_merge(authed):
    job = make_job()
    authed.post(f"/jobs/{job.id}/parse/primary/")
    lock_policy(authed, job, POLICY_FAIL_ROW)
    merge(authed, job)
    job.refresh_from_db()
    assert job.merge_status == "未合并"
    assert job.merged_rows == []
    assert "单份未齐" in job.notice


@pytest.mark.django_db
def test_empty_merge_key_blocked(authed):
    job = parse_both(authed, make_job())
    lock_policy(authed, job, POLICY_FAIL_ROW)
    merge(authed, job, key="   ")
    job.refresh_from_db()
    assert job.merge_status == "未合并"
    assert "合并键空着" in job.notice


@pytest.mark.django_db
def test_policy_locked_for_the_day(authed):
    job = make_job()
    lock_policy(authed, job, POLICY_FAIL_ROW)
    assert job.policy_locked_on == timezone.localdate()
    lock_policy(authed, job, POLICY_KEEP_PRIMARY)
    job.refresh_from_db()
    assert job.conflict_policy == POLICY_FAIL_ROW
    assert "不得更换" in job.notice


@pytest.mark.django_db
def test_policy_drift_is_blocked_on_merge(authed):
    job = parse_both(authed, make_job())
    lock_policy(authed, job, POLICY_KEEP_PRIMARY)
    merge(authed, job, policy=POLICY_FAIL_ROW)
    job.refresh_from_db()
    assert job.merge_status == "未合并"
    assert "合并冲突处理偏了" in job.notice
    body = authed.get(f"/jobs/{job.id}/").content.decode("utf-8")
    assert "合并冲突处理偏了" in body


@pytest.mark.django_db
def test_fail_row_policy_puts_conflict_in_box(authed):
    job = parse_both(authed, make_job())
    lock_policy(authed, job, POLICY_FAIL_ROW)
    merge(authed, job)
    job.refresh_from_db()

    conflicts = MergeConflict.objects.filter(job=job)
    assert conflicts.exists()
    assert job.conflict_count == conflicts.count()
    assert job.merged_count == len(job.merged_rows)
    # 整行失败：冲突键不出现在合并结果里
    conflict_keys = {c.key for c in conflicts}
    assert conflict_keys.isdisjoint({row["id"] for row in job.merged_rows})
    assert all(row.get("_resolution") is None for row in job.merged_rows)


@pytest.mark.django_db
def test_keep_primary_policy_keeps_main_value_with_note(authed):
    job = parse_both(authed, make_job())
    lock_policy(authed, job, POLICY_KEEP_PRIMARY)
    merge(authed, job)
    job.refresh_from_db()

    conflict = MergeConflict.objects.get(job=job)
    primary_row = json.loads(conflict.primary_value)
    kept = next(row for row in job.merged_rows if row["id"] == conflict.key)
    assert kept["amount"] == primary_row["amount"]
    assert kept["_resolution"] == POLICY_KEEP_PRIMARY
    assert "次份" not in kept["_note"] or "主份" in kept["_note"]


@pytest.mark.django_db
def test_land_blocked_until_merge_finished(authed):
    job = parse_both(authed, make_job())
    lock_policy(authed, job, POLICY_FAIL_ROW)
    authed.post(f"/jobs/{job.id}/land/")
    job.refresh_from_db()
    assert job.status != "已入库"
    assert job.landed == 0
    assert "合并未收尾" in job.notice

    merge(authed, job)
    authed.post(f"/jobs/{job.id}/land/")
    job.refresh_from_db()
    assert job.status == "已入库"
    assert job.landed == job.merged_count


@pytest.mark.django_db
def test_merge_race_only_first_wins(authed):
    job = parse_both(authed, make_job())
    lock_policy(authed, job, POLICY_FAIL_ROW)
    merge(authed, job)
    job.refresh_from_db()
    first_count = job.merged_count

    merge(authed, job)
    job.refresh_from_db()
    assert job.merge_status == "已合并"
    assert job.merged_count == first_count
    assert MergeAttempt.objects.filter(job=job, outcome=MergeAttempt.OUTCOME_LOST).count() >= 1
    body = authed.get(f"/jobs/{job.id}/").content.decode("utf-8")
    assert "合并争用失败" in body


@pytest.mark.django_db
def test_detail_expands_conflict_rows_and_lost_attempts(authed):
    job = parse_both(authed, make_job())
    lock_policy(authed, job, POLICY_KEEP_PRIMARY)
    merge(authed, job)
    merge(authed, job)
    job.refresh_from_db()

    body = authed.get(f"/jobs/{job.id}/").content.decode("utf-8")
    assert "冲突匣" in body
    assert "<details>" in body
    assert "冲突行" in body or "差异字段" in body
    assert "合并争用失败" in body
