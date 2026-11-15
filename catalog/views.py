import json
from django.conf import settings
from django.db import transaction
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_POST, require_http_methods

from catalog.models import (
    MERGE_DONE,
    MERGE_RUNNING,
    MERGE_WAITING,
    POLICY_CHOICES,
    POLICY_FAIL_ROW,
    POLICY_KEEP_PRIMARY,
    POLICY_NAMES,
    SOURCE_FAILED,
    SOURCE_PARSED,
    SOURCE_WAITING,
    MergeAttempt,
    MergeConflict,
    ParseJob,
)


def _authed(request) -> bool:
    return request.session.get("user") == settings.ADMIN_USER


def _login_redirect():
    return HttpResponseRedirect("/login")


def _policy_choices():
    return [{"value": value, "label": label} for value, label in POLICY_CHOICES]


def _recompute_status(job: ParseJob) -> str:
    """整体状态由双份解析、合并进度与落档进度推导。"""
    if job.merge_status == MERGE_DONE and job.landed and job.landed == job.merged_count:
        return "已入库"
    if job.merge_status == MERGE_DONE:
        return "已合并待入库"
    if job.merge_status == MERGE_RUNNING:
        return "合并中"
    if job.both_parsed:
        return "双份已解析"
    if job.primary_status == SOURCE_FAILED or job.secondary_status == SOURCE_FAILED:
        return "解析失败"
    if job.primary_status == SOURCE_PARSED or job.secondary_status == SOURCE_PARSED:
        return "单份已解析"
    return "待解析"


def _save_notice(job: ParseJob, notice: str, *extra_fields) -> None:
    job.notice = notice
    job.status = _recompute_status(job)
    job.save(update_fields=["notice", "status", *extra_fields])


def _mock_rows(job: ParseJob, side: str) -> list[dict]:
    """模拟单份解析：产出同一键空间下的行，便于合并时比对。"""
    base = job.id * 10
    extra = 1 if side == "secondary" else 0
    count = 4 + (job.id % 3) + extra
    rows = []
    for i in range(count):
        rows.append(
            {
                "id": f"R{base + i:03d}",
                "name": f"条目{base + i}",
                "amount": str(100 + i * 7),
            }
        )
    if side == "secondary" and rows:
        # 同键不同值：用于冲突匣与留主份旁注
        rows[0]["amount"] = str(int(rows[0]["amount"]) + 9)
    return rows


def _key_of(row: dict, merge_key: str) -> str:
    value = row.get(merge_key, "")
    return "" if value is None else str(value).strip()


def _field_diff(primary: dict, secondary: dict) -> tuple[dict, dict, list[str]]:
    fields = list(primary.keys()) + [f for f in secondary.keys() if f not in primary]
    p_view, s_view, diffs = {}, {}, []
    for field in fields:
        p_raw, s_raw = primary.get(field), secondary.get(field)
        p_val = "" if p_raw is None else str(p_raw)
        s_val = "" if s_raw is None else str(s_raw)
        p_view[field] = p_val
        s_view[field] = s_val
        if p_val != s_val:
            diffs.append(field)
    return p_view, s_view, diffs


def _merge_rows(job: ParseJob) -> tuple[list[dict], list[MergeConflict]]:
    """按合并键裁定同键不同值：策略已在页面锁定，这里不再变更。"""
    merge_key = job.merge_key
    primary_rows = list(job.primary_rows or [])
    secondary_rows = list(job.secondary_rows or [])

    primary_index: dict[str, dict] = {}
    secondary_index: dict[str, dict] = {}
    orphan_conflicts: list[MergeConflict] = []
    for side, rows, index in (
        ("主份", primary_rows, primary_index),
        ("次份", secondary_rows, secondary_index),
    ):
        for idx, row in enumerate(rows):
            key = _key_of(row, merge_key)
            if not key:
                # 行内缺键不给配对：留进冲突匣，避免无键行静默丢失
                orphan_conflicts.append(
                    MergeConflict(
                        job=job,
                        key=f"⟨无键·{side}#{idx + 1}⟩",
                        primary_value=json.dumps(row, ensure_ascii=False, sort_keys=True) if side == "主份" else "",
                        secondary_value=json.dumps(row, ensure_ascii=False, sort_keys=True) if side == "次份" else "",
                        resolution="键缺失",
                        annotation=f"{side}第 {idx + 1} 行缺合并键「{merge_key}」，未进入合并结果",
                    )
                )
                continue
            if key in index:
                # 键在单份内重复：后行覆写前行，留痕在旁注
                index[key] = row
            else:
                index[key] = row

    merged: list[dict] = []
    conflicts: list[MergeConflict] = list(orphan_conflicts)

    for key, primary_row in primary_index.items():
        secondary_row = secondary_index.get(key)
        if secondary_row is None:
            merged.append(dict(primary_row))
            continue
        p_view, s_view, diffs = _field_diff(primary_row, secondary_row)
        if not diffs:
            merged.append(dict(primary_row))
            continue

        annotation = "差异字段：" + "、".join(
            f"{field}（主份 {p_view.get(field)!r} / 次份 {s_view.get(field)!r}）" for field in diffs
        )
        conflicts.append(
            MergeConflict(
                job=job,
                key=key,
                primary_value=json.dumps(p_view, ensure_ascii=False, sort_keys=True),
                secondary_value=json.dumps(s_view, ensure_ascii=False, sort_keys=True),
                resolution=job.conflict_policy,
                annotation=annotation,
            )
        )
        if job.conflict_policy == POLICY_KEEP_PRIMARY:
            kept = dict(primary_row)
            kept["_resolution"] = POLICY_KEEP_PRIMARY
            kept["_note"] = annotation
            merged.append(kept)
        # 整行失败：该行不进合并结果，只留在冲突匣

    for key, secondary_row in secondary_index.items():
        if key not in primary_index:
            merged.append(dict(secondary_row))

    merged.sort(key=lambda row: _key_of(row, job.merge_key) or "")
    return merged, conflicts


def root(request):
    return HttpResponseRedirect("/jobs/" if _authed(request) else "/login")


@require_http_methods(["GET", "POST"])
def login_page(request):
    err = ""
    if request.method == "POST":
        user = request.POST.get("username", "").strip()
        pwd = request.POST.get("password", "")
        if user == settings.ADMIN_USER and pwd == settings.ADMIN_PASSWORD:
            request.session["user"] = user
            return HttpResponseRedirect("/jobs/")
        err = "账号或密码错误"
    return render(request, "login.html", {"error": err, "lab": settings.LAB_NAME})


def logout_view(request):
    request.session.flush()
    return HttpResponseRedirect("/login")


def job_list(request):
    if not _authed(request):
        return _login_redirect()
    items = list(ParseJob.objects.all())
    return render(
        request,
        "jobs_list.html",
        {"items": items, "lab": settings.LAB_NAME, "user": request.session["user"]},
    )


def job_detail(request, job_id: int):
    if not _authed(request):
        return _login_redirect()
    job = get_object_or_404(ParseJob, pk=job_id)
    merged_display = [
        {
            "key": _key_of(row, job.merge_key) or "—",
            "content": json.dumps(row, ensure_ascii=False, sort_keys=True),
            "resolution": row.get("_resolution", ""),
        }
        for row in (job.merged_rows or [])
    ]
    attempts = list(job.merge_attempts.all())
    lost_attempts = [a for a in attempts if a.outcome == MergeAttempt.OUTCOME_LOST]
    conflicts = list(job.conflicts.all())
    key_options = sorted(
        {field for row in (list(job.primary_rows or []) + list(job.secondary_rows or [])) for field in row}
    )
    return render(
        request,
        "job_detail.html",
        {
            "job": job,
            "lab": settings.LAB_NAME,
            "user": request.session["user"],
            "merged_rows": merged_display,
            "conflicts": conflicts,
            "attempts": attempts,
            "lost_attempts": lost_attempts,
            "key_options": key_options,
            "policy_choices": _policy_choices(),
        },
    )


def _reset_merge_after_reparse(job: ParseJob) -> None:
    """重新解析会让既有合并结果过期，合并必须重跑。"""
    if job.merge_status != MERGE_WAITING:
        job.merge_status = MERGE_WAITING
        job.merge_owner = ""
        job.merge_started_at = None
        job.merge_finished_at = None
        job.merged_rows = []
        job.merged_count = 0
        job.conflict_count = 0
        MergeConflict.objects.filter(job=job).delete()


@require_POST
def job_parse(request, job_id: int, side: str):
    if not _authed(request):
        return _login_redirect()
    if side not in ("primary", "secondary"):
        return HttpResponseRedirect(f"/jobs/{job_id}/")
    job = get_object_or_404(ParseJob, pk=job_id)
    rows = _mock_rows(job, side)
    with transaction.atomic():
        _reset_merge_after_reparse(job)
        if side == "primary":
            job.primary_status = SOURCE_PARSED
            job.primary_rows = rows
            job.primary_count = len(rows)
            job.parsed = job.parsed + len(rows)
        else:
            job.secondary_status = SOURCE_PARSED
            job.secondary_rows = rows
            job.secondary_count = len(rows)
            job.parsed = job.parsed + len(rows)
        label = "主份" if side == "primary" else "次份"
        job.landed = 0
        job.status = _recompute_status(job)
        job.notice = f"{label}解析成功，共 {len(rows)} 条"
        if job.merge_status == MERGE_WAITING and job.merge_owner:
            job.notice += "；既有合并结果已过期，须重新合并"
        job.save()
    return HttpResponseRedirect(f"/jobs/{job.id}/")


@require_POST
def job_policy(request, job_id: int):
    if not _authed(request):
        return _login_redirect()
    job = get_object_or_404(ParseJob, pk=job_id)
    posted = request.POST.get("conflict_policy", "").strip()
    if posted not in POLICY_NAMES:
        _save_notice(job, "冲突策略无效，未写入")
        return HttpResponseRedirect(f"/jobs/{job.id}/")
    if job.conflict_policy == posted:
        _save_notice(job, f"冲突策略维持：{posted}")
        return HttpResponseRedirect(f"/jobs/{job.id}/")
    if job.policy_locked_today:
        _save_notice(job, f"冲突策略当天已锁定为「{job.conflict_policy}」，不得更换")
        return HttpResponseRedirect(f"/jobs/{job.id}/")
    job.conflict_policy = posted
    job.policy_locked_on = timezone.localdate()
    job.status = _recompute_status(job)
    job.notice = f"冲突策略锁定为「{posted}」（当天不得更换）"
    job.save(update_fields=["conflict_policy", "policy_locked_on", "status", "notice"])
    return HttpResponseRedirect(f"/jobs/{job.id}/")


@require_POST
def job_merge(request, job_id: int):
    if not _authed(request):
        return _login_redirect()
    job = get_object_or_404(ParseJob, pk=job_id)
    merge_key = request.POST.get("merge_key", "").strip()
    posted_policy = request.POST.get("conflict_policy", "").strip()

    if not merge_key:
        _save_notice(job, "合并键空着，禁止开跑")
        return HttpResponseRedirect(f"/jobs/{job.id}/")
    if not job.both_parsed:
        _save_notice(job, "单份未齐，不能合并：两份都须先解析成功")
        return HttpResponseRedirect(f"/jobs/{job.id}/")
    if not job.conflict_policy:
        _save_notice(job, "冲突策略未定，不得合并")
        return HttpResponseRedirect(f"/jobs/{job.id}/")
    if posted_policy != job.conflict_policy:
        _save_notice(
            job,
            f"合并冲突处理偏了：请求为「{posted_policy or '空'}」，锁定策略为「{job.conflict_policy}」",
        )
        return HttpResponseRedirect(f"/jobs/{job.id}/")
    if job.merge_status != MERGE_WAITING:
        MergeAttempt.objects.create(
            job=job,
            user=request.session["user"],
            outcome=MergeAttempt.OUTCOME_LOST,
            detail="合并已收尾，重复请求未成",
        )
        # 只写提示，不覆盖先到者的合并状态
        ParseJob.objects.filter(pk=job.pk).update(
            notice="合并争用失败：先到者已接手，本次未成"
        )
        return HttpResponseRedirect(f"/jobs/{job.id}/")

    with transaction.atomic():
        # 条件更新即先到者胜：后到者看到的状态已不是“未合并”
        claimed = ParseJob.objects.filter(pk=job.pk, merge_status=MERGE_WAITING).update(
            merge_status=MERGE_RUNNING,
            merge_owner=request.session["user"],
            merge_started_at=timezone.now(),
            merge_key=merge_key,
            notice=f"合并中：按「{job.conflict_policy}」裁定",
        )
        if not claimed:
            MergeAttempt.objects.create(
                job=job,
                user=request.session["user"],
                outcome=MergeAttempt.OUTCOME_LOST,
                detail="合并争用失败：先到者已接手",
            )
            # 争用失败者只留提示，不改动先到者写入的状态与结果
            ParseJob.objects.filter(pk=job.pk).update(
                notice="合并争用失败：先到者已接手，本次未成"
            )
            return HttpResponseRedirect(f"/jobs/{job.id}/")

        job.refresh_from_db()
        merged_rows, conflicts = _merge_rows(job)
        MergeConflict.objects.filter(job=job).delete()
        MergeConflict.objects.bulk_create(conflicts)
        job.merged_rows = merged_rows
        job.merged_count = len(merged_rows)
        job.conflict_count = len(conflicts)
        job.merge_status = MERGE_DONE
        job.merge_finished_at = timezone.now()
        job.status = _recompute_status(job)
        job.notice = (
            f"合并收尾：{len(merged_rows)} 条入合并结果，{len(conflicts)} 条进冲突匣"
        )
        job.save()
        MergeAttempt.objects.create(
            job=job,
            user=request.session["user"],
            outcome=MergeAttempt.OUTCOME_OK,
            detail=f"合并键「{merge_key}」· 策略「{job.conflict_policy}」",
        )
    return HttpResponseRedirect(f"/jobs/{job.id}/")


@require_POST
def job_land(request, job_id: int):
    if not _authed(request):
        return _login_redirect()
    job = get_object_or_404(ParseJob, pk=job_id)
    if job.merge_status != MERGE_DONE:
        _save_notice(job, "合并未收尾，禁止落档")
        return HttpResponseRedirect(f"/jobs/{job.id}/")
    with transaction.atomic():
        ParseJob.objects.filter(pk=job.pk, merge_status=MERGE_DONE).update(
            landed=job.merged_count,
            status="已入库",
            notice=f"已落档：{job.merged_count} 条，冲突匣留档 {job.conflict_count} 条",
        )
    return HttpResponseRedirect(f"/jobs/{job.id}/")
