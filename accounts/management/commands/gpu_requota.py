"""owner 2026-11-09: bring every GPU project's quota in line with its cluster's bundle.

A GPU project is ordered in bundles, so its vCPU and RAM should be the bundle multiplied by the number of cards it
holds. Projects ordered before the bundle became a setting carry whatever was agreed at the time, which is both
above and below that figure. This recomputes them, writes a new ``QuotaRecord`` version (the records are
append-only, so nothing is overwritten and the old version stays readable) and pushes the result to OpenNebula.

It prints what it would do and changes nothing unless ``--apply`` is given.

    manage.py gpu_requota                                     what would change
    manage.py gpu_requota --disk-per-bundle gi2=2600          ...with a disk rule for one cluster
    manage.py gpu_requota --disk-per-bundle gi2=2600 --apply  write it, and set the quota on the platform
    manage.py gpu_requota --no-reduce                         never lower a value, only raise it to the bundle
    manage.py gpu_requota --exclude aleroy-ltd                leave a project alone

**Reductions are the dangerous half.** OpenNebula does not take anything back from a project already using more
than its new quota: the project simply sits over quota, keeps running, and fails the next time it asks for
something. Read the table before using --apply, and use --no-reduce if that is not what you want.
"""
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Recompute every GPU project's quota from its cluster's bundle."

    def add_arguments(self, p):
        p.add_argument("--apply", action="store_true", help="write the new versions and set the quota in OpenNebula")
        p.add_argument("--no-reduce", action="store_true", help="only raise a value, never lower it")
        p.add_argument("--exclude", action="append", default=[], metavar="PROJECT", help="leave this project alone")
        p.add_argument("--disk-per-bundle", action="append", default=[], metavar="CODE=GIB",
                       help="disk for one cluster's projects: bundles x GiB (e.g. gi2=2600). Repeatable.")

    def handle(self, *a, **o):
        from django.utils import timezone
        from inventory.models import Cluster
        from tenants.models import Lifecycle, QuotaRecord, fit_split

        per_bundle = {}
        for item in o["disk_per_bundle"]:
            code, _, gib = item.partition("=")
            if not gib.isdigit():
                raise CommandError(f"--disk-per-bundle wants CODE=GIB, got {item!r}")
            per_bundle[code.strip()] = int(gib)
        skip = {n.strip() for n in o["exclude"]}

        clusters = {}
        for c in Cluster.objects.select_related("platform", "platform__site").prefetch_related("hosts__profile"):
            clusters[(c.platform.site.code, c.order_code)] = c

        current, seen = [], set()
        for r in (QuotaRecord.objects.exclude(tenant__status=Lifecycle.ARCHIVED)
                  .select_related("tenant").order_by("tenant_id", "site", "cluster", "-version")):
            key = (r.tenant_id, r.site, r.cluster)
            if key in seen:
                continue
            seen.add(key)
            if not r.removed and (r.gpu or 0) > 0:
                current.append(r)

        rows, problems = [], []
        for r in current:
            if r.tenant.name in skip:
                self.stdout.write(f"  skipped: {r.tenant.name}")
                continue
            c = clusters.get((r.site, r.cluster))
            if c is None:
                problems.append(f"{r.tenant.name}: no cluster {r.site}/{r.cluster}")
                continue
            b = c.bundle()
            if not (b["cores"] and b["ram_gb"]):
                problems.append(f"{r.tenant.name}: {c.name} has no bundle set and no GPU hosts to derive one")
                continue
            want = {"cpu": r.gpu * b["cores"], "ram_gb": r.gpu * b["ram_gb"],
                    "disk_gb": r.gpu * per_bundle[r.cluster] if r.cluster in per_bundle else r.disk_gb}
            if o["no_reduce"]:
                want = {k: max(v, getattr(r, k) or 0) for k, v in want.items()}
            if all(want[k] == (getattr(r, k) or 0) for k in want):
                continue
            rows.append((r, want, b))

        if problems:
            self.stdout.write(self.style.WARNING("\nNot changed:"))
            for p in problems:
                self.stdout.write(f"  {p}")
        if not rows:
            self.stdout.write(self.style.SUCCESS("\nEvery GPU project already matches its bundle."))
            return

        self.stdout.write(f"\n{'project':<20}{'cl':<6}{'bundles':>8}{'vCPU':>14}{'RAM GB':>16}{'disk GiB':>16}")
        for r, want, b in rows:
            def col(k):
                was, now = getattr(r, k) or 0, want[k]
                return f"{was}" if was == now else f"{was} -> {now}" + ("  v" if now < was else "")
            self.stdout.write(f"{r.tenant.name:<20}{r.cluster:<6}{r.gpu:>8}"
                              f"{col('cpu'):>14}{col('ram_gb'):>16}{col('disk_gb'):>16}")
        cuts = [r.tenant.name for r, w, _b in rows
                if any(w[k] < (getattr(r, k) or 0) for k in ("cpu", "ram_gb", "disk_gb"))]
        if cuts:
            self.stdout.write(self.style.WARNING(
                f"\n  'v' marks a reduction. Over-quota projects keep running but cannot allocate: {', '.join(cuts)}"))

        if not o["apply"]:
            self.stdout.write(self.style.WARNING("\nNothing written. Add --apply to write these and set the quota."))
            return

        from tenants import deploy
        now = timezone.now()
        for r, want, _b in rows:
            images, files = fit_split(want["disk_gb"], r.images_gb, r.files_gb)
            QuotaRecord.objects.create(
                tenant=r.tenant, site=r.site, cluster=r.cluster,
                version=QuotaRecord.next_version(r.tenant, r.site, r.cluster),
                cpu=want["cpu"], ram_gb=want["ram_gb"], gpu=r.gpu, disk_gb=want["disk_gb"],
                images_gb=images, files_gb=files, backups_gb=r.backups_gb, effective_from=now)
            try:
                out = deploy.apply_quota(r.tenant)
            except Exception as exc:   # noqa: BLE001 - one platform failing must not stop the rest
                out = f"OpenNebula not updated: {exc}"
            self.stdout.write(f"  {r.tenant.name}: new version written. {out}")
        self.stdout.write(self.style.SUCCESS(f"\n{len(rows)} project(s) updated."))
