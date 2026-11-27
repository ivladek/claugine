"""The platforms' last observation of each tenant group.

The comparison of the portal's records with the group template that lived here was replaced by the platform
consistency check (tenants.consistency, tenants.quota_check, 2026-11-11).
"""

from .models import Observation


def latest_observations():
    """{group name: observation}; the most recent if several platforms report the group."""
    obs = {}
    for o in Observation.objects.order_by("collected_at"):
        obs[o.group_name] = o
    return obs
