from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="ParseJob",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("title", models.CharField(max_length=120)),
                ("code", models.CharField(max_length=64, unique=True)),
                ("status", models.CharField(default="待解析", max_length=32)),
                ("source_uri", models.CharField(max_length=255)),
                ("primary_uri", models.CharField(blank=True, default="", max_length=255)),
                ("secondary_uri", models.CharField(blank=True, default="", max_length=255)),
                ("primary_status", models.CharField(default="待解析", max_length=16)),
                ("secondary_status", models.CharField(default="待解析", max_length=16)),
                ("primary_count", models.IntegerField(default=0)),
                ("secondary_count", models.IntegerField(default=0)),
                ("primary_rows", models.JSONField(blank=True, default=list)),
                ("secondary_rows", models.JSONField(blank=True, default=list)),
                ("merge_key", models.CharField(blank=True, default="", max_length=64)),
                ("conflict_policy", models.CharField(blank=True, default="", max_length=16)),
                ("policy_locked_on", models.DateField(blank=True, null=True)),
                ("merge_status", models.CharField(default="未合并", max_length=16)),
                ("merge_owner", models.CharField(blank=True, default="", max_length=64)),
                ("merge_started_at", models.DateTimeField(blank=True, null=True)),
                ("merge_finished_at", models.DateTimeField(blank=True, null=True)),
                ("merged_count", models.IntegerField(default=0)),
                ("conflict_count", models.IntegerField(default=0)),
                ("merged_rows", models.JSONField(blank=True, default=list)),
                ("notice", models.CharField(blank=True, default="", max_length=255)),
                ("parsed", models.IntegerField(default=0)),
                ("landed", models.IntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ["id"]},
        ),
        migrations.CreateModel(
            name="MergeConflict",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("key", models.CharField(max_length=64)),
                ("primary_value", models.TextField(blank=True, default="")),
                ("secondary_value", models.TextField(blank=True, default="")),
                ("resolution", models.CharField(max_length=16)),
                ("annotation", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("job", models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="conflicts", to="catalog.parsejob")),
            ],
            options={"ordering": ["key"]},
        ),
        migrations.AddConstraint(
            model_name="mergeconflict",
            constraint=models.UniqueConstraint(fields=("job", "key"), name="uniq_job_conflict_key"),
        ),
        migrations.CreateModel(
            name="MergeAttempt",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("user", models.CharField(blank=True, default="", max_length=64)),
                ("outcome", models.CharField(max_length=16)),
                ("detail", models.CharField(blank=True, default="", max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("job", models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="merge_attempts", to="catalog.parsejob")),
            ],
            options={"ordering": ["-id"]},
        ),
    ]
