"""The public side: articles, the service components and their events.

`ServiceComponent` plus `ComponentEvent` is what the status page reads. Everything here is public by definition,
so it holds nothing about a client.
"""

from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class Article(models.Model):
    class Kind(models.TextChoices):
        NEWS = "news", _("News")
        ANNOUNCEMENT = "announcement", _("Announcement")
        INCIDENT = "incident", _("Incident banner")

    kind = models.CharField(_("kind"), max_length=16, choices=Kind.choices, default=Kind.NEWS)
    title = models.CharField(_("title"), max_length=200)
    body = models.TextField(_("text"), blank=True)
    is_published = models.BooleanField(_("published"), default=False)
    published_at = models.DateTimeField(_("published at"), null=True, blank=True)
    show_until = models.DateTimeField(
        _("show until"),
        null=True,
        blank=True,
        help_text=_("Announcements and incident banners disappear after this time. Empty: until unpublished."),
    )
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-published_at", "-created_at"]

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        if self.is_published and not self.published_at:
            self.published_at = timezone.now()
        super().save(*args, **kwargs)

    @classmethod
    def visible(cls, kind):
        now = timezone.now()
        return cls.objects.filter(kind=kind, is_published=True, published_at__lte=now).exclude(show_until__lt=now)


class ServiceComponent(models.Model):
    """A service shown on the public health board. No capacity numbers."""

    class State(models.TextChoices):
        OPERATIONAL = "operational", _("Operational")
        DEGRADED = "degraded", _("Degraded")
        OUTAGE = "outage", _("Outage")
        MAINTENANCE = "maintenance", _("Maintenance")

    name = models.CharField(_("name"), max_length=100)
    state = models.CharField(_("state"), max_length=16, choices=State.choices, default=State.OPERATIONAL)
    note = models.CharField(_("note"), max_length=200, blank=True)
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["position", "name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        changed = True
        if self.pk:
            changed = ServiceComponent.objects.filter(pk=self.pk).exclude(state=self.state).exists()
        super().save(*args, **kwargs)
        if changed:
            ComponentEvent.objects.create(component=self, state=self.state)

    def uptime(self, days=30):
        """Share of the last `days` not spent in an outage, in percent."""
        end = timezone.now()
        start = end - timedelta(days=days)
        events = list(self.events.filter(at__lt=end).order_by("at"))
        if not events:
            return None
        # State in effect at the window start
        before = [e for e in events if e.at <= start]
        current = before[-1].state if before else None
        cursor = start if before else events[0].at
        down = timedelta()
        for e in events:
            if e.at <= start:
                continue
            if current == self.State.OUTAGE:
                down += e.at - cursor
            current, cursor = e.state, e.at
        if current == self.State.OUTAGE:
            down += end - cursor
        span = end - (start if before else events[0].at)
        if span.total_seconds() <= 0:
            return 100.0
        return round(100 * (1 - down / span), 3)


class ComponentEvent(models.Model):
    component = models.ForeignKey(ServiceComponent, on_delete=models.CASCADE, related_name="events")
    state = models.CharField(max_length=16, choices=ServiceComponent.State.choices)
    at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-at"]

    def __str__(self):
        return f"ComponentEvent #{self.pk}"
