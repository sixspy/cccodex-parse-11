from django.db import models
from django.utils import timezone

POLICY_FAIL_ROW = "整行失败"
POLICY_KEEP_PRIMARY = "留主份"
POLICY_CHOICES = (
    (POLICY_FAIL_ROW, "整行失败（整行进冲突匣）"),
    (POLICY_KEEP_PRIMARY, "留主份（保留主份值，旁注次份差异）"),
)
POLICY_NAMES = [value for value, _label in POLICY_CHOICES]

SOURCE_WAITING = "待解析"
SOURCE_PARSED = "已解析"
SOURCE_FAILED = "解析失败"

MERGE_WAITING = "未合并"
MERGE_RUNNING = "合并中"
MERGE_DONE = "已合并"


class ParseJob(models.Model):
    title = models.CharField(max_length=120)
    code = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=32, default="待解析")
    source_uri = models.CharField(max_length=255)

    # 双源：主份 / 次份
    primary_uri = models.CharField(max_length=255, blank=True, default="")
    secondary_uri = models.CharField(max_length=255, blank=True, default="")
    primary_status = models.CharField(max_length=16, default=SOURCE_WAITING)
    secondary_status = models.CharField(max_length=16, default=SOURCE_WAITING)
    primary_count = models.IntegerField(default=0)
    secondary_count = models.IntegerField(default=0)
    primary_rows = models.JSONField(default=list, blank=True)
    secondary_rows = models.JSONField(default=list, blank=True)

    # 合并键与冲突策略
    merge_key = models.CharField(max_length=64, blank=True, default="")
    conflict_policy = models.CharField(max_length=16, blank=True, default="")
    policy_locked_on = models.DateField(null=True, blank=True)

    # 合并结果
    merge_status = models.CharField(max_length=16, default=MERGE_WAITING)
    merge_owner = models.CharField(max_length=64, blank=True, default="")
    merge_started_at = models.DateTimeField(null=True, blank=True)
    merge_finished_at = models.DateTimeField(null=True, blank=True)
    merged_count = models.IntegerField(default=0)
    conflict_count = models.IntegerField(default=0)
    merged_rows = models.JSONField(default=list, blank=True)

    # 页面提示：最近一次操作的结论（合并偏了 / 争用失败 / 禁止落档等）
    notice = models.CharField(max_length=255, blank=True, default="")

    parsed = models.IntegerField(default=0)
    landed = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    @property
    def policy_display(self) -> str:
        return self.conflict_policy or "未定"

    @property
    def policy_locked_today(self) -> bool:
        return bool(self.conflict_policy) and self.policy_locked_on == timezone.localdate()

    @property
    def both_parsed(self) -> bool:
        return self.primary_status == SOURCE_PARSED and self.secondary_status == SOURCE_PARSED

    @property
    def notice_warn(self) -> bool:
        """提示是否为拦阻/失败类；正常收尾（含冲突匣留档）不算警告。"""
        return any(
            word in self.notice
            for word in ("禁止", "不得", "不能", "偏了", "失败", "无效", "未写入")
        )


class MergeConflict(models.Model):
    """冲突匣：同键不同值的每一行，按已锁定策略裁定。"""

    job = models.ForeignKey(ParseJob, on_delete=models.CASCADE, related_name="conflicts")
    key = models.CharField(max_length=64)
    primary_value = models.TextField(blank=True, default="")
    secondary_value = models.TextField(blank=True, default="")
    resolution = models.CharField(max_length=16)
    annotation = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["key"]
        constraints = [
            models.UniqueConstraint(fields=["job", "key"], name="uniq_job_conflict_key"),
        ]


class MergeAttempt(models.Model):
    """合并争用记录：先到者成功，后到者争用失败。"""

    OUTCOME_OK = "成功"
    OUTCOME_LOST = "争用失败"

    job = models.ForeignKey(ParseJob, on_delete=models.CASCADE, related_name="merge_attempts")
    user = models.CharField(max_length=64, blank=True, default="")
    outcome = models.CharField(max_length=16)
    detail = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]
