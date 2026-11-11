"""What the old system and the platforms said: an import, an observation, and a platform's counts.

Records of what was *seen*, never a source of truth for a client's project — the portal's own records are
(`tenants/models.py`). Nothing here is on a client's path.
"""
from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class SourceImport(models.Model):
    """One read of the old system's tenant data (CSV upload or SQLite read)."""

    class Kind(models.TextChoices):
        CSV = "csv", _("CSV upload")
        SQLITE = "sqlite", _("Old system database")

    class State(models.TextChoices):
        PREVIEW = "preview", _("Preview")
        APPLIED = "applied", _("Applied")
        DISCARDED = "discarded", _("Discarded")

    kind = models.CharField(max_length=16, choices=Kind.choices)
    filename = models.CharField(max_length=300, blank=True)
    data = models.JSONField(default=dict, help_text="Normalized projects from the source")
    problems = models.JSONField(default=list, blank=True)
    diff = models.JSONField(default=dict, blank=True)
    state = models.CharField(max_length=16, choices=State.choices, default=State.PREVIEW)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    applied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"#{self.pk} {self.get_kind_display()} {self.created_at:%Y-%m-%d %H:%M}"


class Observation(models.Model):
    """A tenant group as last seen in OpenNebula."""

    platform_key = models.CharField(max_length=100, db_index=True)
    group_id = models.IntegerField()
    group_name = models.CharField(max_length=128, db_index=True)
    template = models.JSONField(default=dict)
    quotas = models.JSONField(default=dict)
    users = models.JSONField(default=list)
    collected_at = models.DateTimeField()

    class Meta:
        unique_together = [("platform_key", "group_id")]
        ordering = ["group_name"]

    @property
    def tenant_name(self):
        return self.group_name[len("tenant-"):] if self.group_name.startswith("tenant-") else ""


class PlatformStats(models.Model):
    """Live numbers of one OpenNebula front end, refreshed by each collection."""

    platform_key = models.CharField(max_length=100, unique=True)
    collected_at = models.DateTimeField()
    vms = models.JSONField(default=dict, help_text="total, running, poweroff, other, tenant_total, tenant_running, by_group")
    hosts = models.JSONField(default=dict, help_text="total, on, error, off")
    address_ranges = models.JSONField(default=list, blank=True,
                                      help_text="IPv4 address ranges of all virtual networks: [network name, first IP, size]")
    datastores = models.JSONField(default=list, blank=True,
                                  help_text="[{id, name, type, total_mb, used_mb, free_mb}] of every datastore")
    clusters = models.JSONField(default=dict, blank=True, help_text="{cluster id: name} (a project's usage per cluster)")
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["platform_key"]
