"""Delete a tenant from OpenNebula and the portal.

1. plan     when the deletion is requested, every resource of the tenant is listed and attached to the request:
            VMs (the tenant's and the provider's egw/cgw/L2 gateways), backup jobs, virtual routers, templates,
            images and backups, security groups, VM groups, networks (the main one included), ACL rules of the
            group and its users, the users (primary group = the tenant's) and the group. A resource belongs to the
            tenant when its group is the tenant's group or its owner is one of the tenant's users.
2. delete   after approval exactly the listed resources are deleted (what is already gone is skipped): VMs are
            terminated and waited for, then the rest in dependency order, the users and the group last.
3. check    before the users and the group, and again at the end, the platform is read again: nothing may be left that belongs to the group or a tenant user;
            otherwise the deletion fails with the list and can be run again (a new plan is made).

core-009 (full review 2026-11-16): a project can have pools - and so VMs, images, networks - in other zones of the
federation (core-003). Steps 1-3 cover every zone the project has, or had, a pool in (deploy.quota_platforms): the
plan lists each other zone's resources under plan["zones"][<platform>], they are deleted and checked there, and the
ACLs, users and group (the federation's, on its primary) go only once every zone is clean. A zone that cannot be
read, or whose platform is read-only, stops the deletion before anything is deleted.
4. portal   addresses released, L2 channels removed, quota records closed, memberships removed, status deleted.

On a platform in read-only mode, or for a tenant never deployed, only the portal step runs.
"""

import re
import time

from django.utils import timezone

from .deploy import DeployError, One

WAIT_SECONDS = 300
POLL_SECONDS = 5
# deleted in the background by OpenNebula (state DELETE while the datastore removes the files): waited for before the check
ASYNC_DELETE = {"images"}
# key, pool method, element tag, delete method, extra arguments of delete
POOLS = (
    ("backupjobs", "one.backupjobpool.info", "BACKUPJOB", "one.backupjob.delete", ()),
    ("vrouters", "one.vrouterpool.info", "VROUTER", "one.vrouter.delete", ()),
    ("templates", "one.templatepool.info", "VMTEMPLATE", "one.template.delete", (False,)),
    ("images", "one.imagepool.info", "IMAGE", "one.image.delete", ()),
    ("secgroups", "one.secgrouppool.info", "SECURITY_GROUP", "one.secgroup.delete", ()),
    ("vmgroups", "one.vmgrouppool.info", "VM_GROUP", "one.vmgroup.delete", ()),
    ("networks", "one.vnpool.info", "VNET", "one.vn.delete", ()),
)


def unsupported(exc):
    """The platform has no such pool method (an older OpenNebula: e.g. no backup jobs) - not a failure to read it."""
    t = str(exc).lower()
    return "method" in t and ("does not exist" in t or "not defined" in t or "not found" in t)


def names_of(tenant):
    """A pattern of the names of the tenant's objects that the provider owns (its gateways, …): <group>-…, the old
    system's tenant_<name>-<what>_<site>, and the tenant VyOS names _<org>_<tenant>_<role>_<site> (correction 11). Exact,
    so that tenant "beta" never takes the objects of tenant "beta-ml"."""
    from .deploy import object_base
    from .naming import tenant_parts

    g = tenant.group_name or ""
    pats = []
    for base in {g, object_base(tenant)} - {""}:  # <base>-<what>_<site> (a tenant name may have "-": exact)
        pats.append(re.escape(base) + r"-[a-z0-9]+_[a-z0-9]+")
    org, ten = tenant_parts(tenant)
    pats.append(f"_{re.escape(org)}_{re.escape(ten)}_[a-z0-9]+_[a-z0-9]+")
    return re.compile("^(?:" + "|".join(pats) + ")$")


def deletion_platforms(tenant, home):
    """commit-022: the platforms - other than ``home`` - of every pool the project ever had (its quota records, removed
    ones too), found by cluster code with retired clusters included: a zone whose cluster was retired may still hold the
    project's resources (deploy.quota_platforms counts them too since the fourth full review, but picks one platform
    per pool; a deletion checks every one).
    Returns (platforms, unplaced): the (site, code) pools no platform has a cluster for - in home's site too, unless
    home is the only platform there (then it is the one the pool was on; commit-022 again: a site can have more)."""
    from inventory.models import Cluster, Platform

    from .models import QuotaRecord

    out, unplaced = [], []
    for site, code in sorted(set(QuotaRecord.objects.filter(tenant=tenant).values_list("site", "cluster"))):
        found = [
            c.platform
            for c in Cluster.objects.select_related("platform", "platform__site").filter(platform__site__code=site)
            if c.order_code == code
        ]
        if not found:
            only_home = (
                site == home.site.code and not Platform.objects.filter(site__code=site).exclude(key=home.key).exists()
            )
            if not only_home:
                unplaced.append((site, code))
            continue
        for p in found:
            if p.key != home.key and p.key not in [x.key for x in out]:
                out.append(p)
    return out, unplaced


class Undeployer:
    def __init__(self, tenant, user=None, dry=False, deletion=None):
        from inventory.models import Platform, PlatformControl

        self.tenant, self.user, self.dry, self.deletion = tenant, user, dry, deletion
        self.steps = []
        o = tenant.order or {}
        self.platform = Platform.objects.select_related("site").filter(key=o.get("platform")).first()
        self.control = PlatformControl.objects.filter(key=o.get("platform")).first() if self.platform else None
        self.live = bool(
            self.control and self.control.mode == "execution" and self.control.configured and tenant.group_name
        )
        if self.live:
            primary = self.platform.fe_primary or self.platform.key
            self.primary = PlatformControl.objects.filter(key=primary).first() or self.control
        # core-013 (third full review): a deployed project whose platform cannot be reached - its control missing or
        # without credentials - is not deleted from the portal alone: its prefixes and quotas would be released while
        # its VMs and networks run on. A read-only platform (adoption) and a project never deployed keep the
        # portal-only deletion.
        read_only = bool(self.control and self.control.mode != "execution")
        self.blocked = ""
        if not self.live and tenant.one_group_id is not None and not read_only:
            self.blocked = (
                f"{o.get('platform') or 'the platform'}: not configured here (no control or no credentials), so the "
                "project's resources there cannot be deleted; nothing is released until it is"
            )
        self.others, self.unplaced = [], []  # core-009: the other zones the project has, or had, pools in
        if self.live:
            others, self.unplaced = deletion_platforms(tenant, self.platform)
            for p in others:
                self.others.append((p, PlatformControl.objects.filter(key=p.key).first()))

    # --- bookkeeping -------------------------------------------------------------------------

    def step(self, name, fn):
        entry = {"name": name, "status": "running", "detail": ""}
        self.steps.append(entry)
        self._save()
        try:
            entry["detail"] = fn() or ""
            entry["status"] = "ok"
        except DeployError as exc:
            entry["status"], entry["detail"] = "failed", str(exc)
            raise
        except Exception as exc:  # noqa: BLE001 - recorded
            entry["status"], entry["detail"] = "failed", f"{type(exc).__name__}: {exc}"
            raise DeployError(entry["detail"]) from exc
        finally:
            self._save()

    def _save(self):
        if self.deletion is not None and not self.dry:
            self.deletion.steps = self.steps
            self.deletion.save(update_fields=["steps", "plan"])

    def _done(self, kind, item, status):
        """One object handled: its status on the request's list and a line in the tenant's log."""
        from accounts.models import audit

        item["status"] = status
        audit(
            self.user,
            "tenant_object_deleted",
            self.tenant,
            kind=kind,
            id=item.get("id"),
            name=item.get("name", "")[:200],
            status=status,
        )

    # --- reading the platform ------------------------------------------------------------------

    def connect(self):
        if not hasattr(self, "one"):
            if getattr(self, "unplaced", None):  # commit-022: a zone that cannot be named cannot be checked
                raise DeployError(
                    "no platform found for the pool(s) "
                    + ", ".join(f"{s}/{c}" for s, c in self.unplaced)
                    + ": their resources cannot be checked, so nothing is deleted"
                )
            zones = {}
            for p, c in self.others:  # core-009: every zone or none - checked before anything is read or deleted
                if c is None or not c.configured or c.mode != "execution":
                    raise DeployError(
                        f"{p.key}: the project has a pool there, but the platform is read-only or not configured: "
                        "its resources cannot be deleted"
                    )
                zones[p.key] = One(c)
            self.one = One(self.control)
            self.gone = One(self.primary) if self.primary.key != self.control.key else self.one
            self.zone_ones = zones

    def _zones(self, plan):
        """(zone, its client, its part of the plan): this platform's first (the plan's top level), then the others."""
        yield "", self.one, plan
        for key, one in self.zone_ones.items():
            yield key, one, (plan.get("zones") or {}).get(key) or {}  # a plan made before: the check finds the rest

    def inventory(self, known=None):
        """Everything of the tenant on the platform: {key: [{"id", "name"}]}, users, group, ACLs.

        `known` (a former plan) adds its group and user ids, so what they owned is found after they are gone."""
        self.connect()
        known = known or {}
        former = known.get("known") or {}  # commit-044: the ids a plan before it carried (a retried deletion)
        g = self.gone.find("one.grouppool.info", "GROUP", self.tenant.group_name)
        gid = int(g.findtext("ID")) if g is not None else None
        owner_gid = (
            gid if gid is not None else ((known.get("group") or {}).get("id") or (former.get("group") or {}).get("id"))
        )
        users, members = [], []
        if gid is not None:
            for u in self.gone.xml("one.userpool.info").findall("USER"):
                groups = {x.text for x in u.findall("GROUPS/ID")}
                item = {"id": int(u.findtext("ID")), "name": u.findtext("NAME")}
                if u.findtext("GID") == str(gid):
                    users.append(item)
                elif str(gid) in groups:
                    members.append(item)
        uids = (
            {str(u["id"]) for u in users}
            | {str(u["id"]) for u in known.get("users") or []}
            | {str(u["id"]) for u in former.get("users") or []}
        )
        named = names_of(self.tenant)

        def mine(el):
            return (
                (owner_gid is not None and el.findtext("GID") == str(owner_gid))
                or el.findtext("UID") in uids
                or bool(named.match(el.findtext("NAME") or ""))
            )

        inv = {
            "group": {"id": gid, "name": self.tenant.group_name} if gid is not None else {},
            "users": users,
            "members": members,
        }
        inv.update(self._zone_inventory(self.one, mine))
        if self.zone_ones:  # core-009: the other zones' VMs, images, networks, … (a zone that cannot be read raises)
            inv["zones"] = {}
            for key, one in self.zone_ones.items():
                try:
                    inv["zones"][key] = self._zone_inventory(one, mine)
                except DeployError as exc:
                    raise DeployError(f"{key}: {exc}") from exc
        inv["acls"] = []
        if gid is not None:
            subjects = {f"@{gid}"} | {f"#{u['id']}" for u in users}
            for a in self.gone.xml("one.acl.info").findall("ACL"):
                rule = a.findtext("STRING") or ""
                parts = rule.split()
                if len(parts) >= 2:
                    target = parts[1].split("/", 1)[1] if "/" in parts[1] else ""
                    if parts[0] in subjects or target in subjects:
                        inv["acls"].append({"id": int(a.findtext("ID")), "name": rule})
        return inv

    @staticmethod
    def _zone_inventory(one, mine):
        """One zone's VMs and pools of the tenant."""
        inv = {
            "vms": [
                {"id": int(v.findtext("ID")), "name": v.findtext("NAME")}
                for v in one.xml("one.vmpool.info", -2, -1, -1, -1).findall("VM")
                if mine(v)
            ]
        }
        for key, pool, tag, _d, _x in POOLS:
            try:
                items = one.xml(pool, -2, -1, -1).findall(tag)
            except DeployError as exc:
                if not unsupported(exc):  # core-005: a pool that cannot be read is not an empty one
                    raise
                items = []
            inv[key] = [{"id": int(el.findtext("ID")), "name": el.findtext("NAME")} for el in items if mine(el)]
        return inv

    # --- deleting what the plan lists -----------------------------------------------------------

    def _present(self, key, pool, tag, one=None):
        """What of the pool is still there. A read that fails stops the deletion here (core-005: it was taken for
        "all gone" and the deletion went on to the ACLs and the users); a pool the platform does not have is empty."""
        try:
            return {
                int(el.findtext("ID"))
                for el in (one or self.one).xml(pool, -2, -1, -1, *([-1] if key == "vms" else [])).findall(tag)
            }
        except DeployError as exc:
            if unsupported(exc):
                return set()
            raise DeployError(f"{key}: cannot read what is left ({exc}); nothing more is deleted") from exc

    def vms_step(self, plan):
        out = []
        for zone, one, zplan in self._zones(plan):
            note = self._vms_in(one, zplan)
            out.append(f"{zone}: {note}" if zone else note)
        return "; ".join(out)

    def _vms_in(self, one, plan):
        vms = plan.get("vms") or []
        left = self._present("vms", "one.vmpool.info", "VM", one)
        todo = [v for v in vms if v["id"] in left]
        for v in vms:
            if v["id"] not in left:
                self._done("vm", v, "gone")
        for v in todo:
            try:
                one.call("one.vm.action", "terminate-hard", v["id"])
            except DeployError:
                self._done("vm", v, "failed")
                raise
        ids = {v["id"] for v in todo}
        deadline = time.monotonic() + WAIT_SECONDS
        while ids:
            ids &= self._present("vms", "one.vmpool.info", "VM", one)
            if not ids:
                break
            if time.monotonic() > deadline:
                for v in todo:
                    if v["id"] in ids:
                        self._done("vm", v, "failed")
                raise DeployError(f"{len(ids)} VM(s) still not gone after {WAIT_SECONDS} s: {sorted(ids)}")
            time.sleep(POLL_SECONDS)
        for v in todo:
            self._done("vm", v, "deleted")
        return f"{len(todo)} terminated, {len(vms) - len(todo)} already gone"

    def pools_step(self, plan):
        out = []
        for zone, one, zplan in self._zones(plan):
            note = self._pools_in(one, zplan)
            out.append(f"{zone}: {note}" if zone else note)
        return "; ".join(out)

    def _pools_in(self, one, plan):
        out = []
        for key, pool, tag, delete, extra in POOLS:
            items = plan.get(key) or []
            if not items:
                continue
            left = self._present(key, pool, tag, one)
            done, waiting = 0, set()
            for it in items:
                if it["id"] not in left:
                    self._done(key, it, "gone")
                    continue
                try:
                    one.call(delete, it["id"], *extra)
                except DeployError:
                    if it["id"] not in self._present(
                        key, pool, tag, one
                    ):  # gone meanwhile (e.g. a background delete ended)
                        self._done(key, it, "gone")
                        continue
                    self._done(key, it, "failed")
                    raise
                self._done(key, it, "deleted")
                done += 1
                if key in ASYNC_DELETE:
                    waiting.add(it["id"])
            deadline = time.monotonic() + WAIT_SECONDS
            while waiting:  # 2026-10-28: gamma-poc's image was still in the pool (being deleted) when the check read it
                waiting &= self._present(key, pool, tag, one)
                if not waiting:
                    break
                if time.monotonic() > deadline:
                    raise DeployError(f"{key} still being deleted after {WAIT_SECONDS} s: {sorted(waiting)}")
                time.sleep(POLL_SECONDS)
            out.append(f"{key} {done}/{len(items)}")
        return ", ".join(out) or "nothing"

    def acl_step(self, plan):
        rules = plan.get("acls") or []
        left = {int(a.findtext("ID")) for a in self.gone.xml("one.acl.info").findall("ACL")}
        done = [r for r in rules if r["id"] in left]
        for r in rules:
            if r["id"] not in left:
                self._done("acl", r, "gone")
        for r in done:
            self.gone.call("one.acl.delrule", r["id"])
            self._done("acl", r, "deleted")
        return f"{len(done)} rule(s) deleted"

    def users_step(self, plan):
        out = []
        left = {int(u.findtext("ID")) for u in self.gone.xml("one.userpool.info").findall("USER")}
        gid = (plan.get("group") or {}).get("id")
        for u in plan.get("users") or []:
            if u["id"] in left:
                self.gone.call("one.user.delete", u["id"])
                self._done("user", u, "deleted")
                out.append(f"{u['name']} deleted")
            else:
                self._done("user", u, "gone")
        for u in plan.get("members") or []:
            if u["id"] in left and gid is not None:
                self.gone.call("one.user.delgroup", u["id"], gid)
                self._done("member", u, "left the group")
                out.append(f"{u['name']} left the group")
        return ", ".join(out) or "none"

    def group_step(self, plan):
        gid = (plan.get("group") or {}).get("id")
        if gid is None:
            return "no group"
        if self.gone.find("one.grouppool.info", "GROUP", self.tenant.group_name) is None:
            self._done("group", plan["group"], "gone")
            return "already gone"
        self.gone.call("one.group.delete", gid)
        self._done("group", plan["group"], "deleted")
        return f"group #{gid} deleted"

    def verify_step(self, plan):
        """Before the users and the group go: no VM, network, image, … of the tenant may be left (a re-run lists them)."""
        return self.check_step(plan, keys=["vms"] + [k for k, *_r in POOLS], group=False)

    def check_step(self, plan, keys=None, group=True):
        """Nothing of the tenant may be left on the platform."""
        left = self.inventory(known=plan)
        rest = []
        for key in keys or (["vms"] + [k for k, *_r in POOLS] + ["acls", "users"]):
            rest += [f"{key} #{i['id']} {i['name']}" for i in left.get(key) or []]
        for zone, zinv in (left.get("zones") or {}).items():  # core-009: every zone the project has pools in
            for key in ["vms"] + [k for k, *_r in POOLS]:
                rest += [f"{zone}: {key} #{i['id']} {i['name']}" for i in zinv.get(key) or []]
        if group and left.get("group"):
            rest.append(f"group #{left['group']['id']}")
        if rest:
            raise DeployError(f"still on the platform: {', '.join(rest[:20])}{' …' if len(rest) > 20 else ''}")
        return "nothing of the project is left"

    # --- portal ------------------------------------------------------------------------------

    def l2_step(self, plan=None):
        """core-009 again (fifteenth full review): the project's L2 channels go first, each with its own removal -
        their gateways and VLAN networks may be on a site's management platform, which the zones of the project's
        pools do not include. A channel is marked removed only by that removal; one that fails stops the deletion."""
        from .models import L2Channel
        from .netops import L2Removal

        done = []
        for ch in self.tenant.l2_channels.exclude(state=L2Channel.State.REMOVED).order_by("pk"):
            r = L2Removal(ch, self.user)
            r.run_change()
            ch.refresh_from_db()
            if ch.state != L2Channel.State.REMOVED:
                raise DeployError(f"L2 channel VNI {ch.vni}: not removed")
            done.append(f"VNI {ch.vni}")
        return f"removed: {', '.join(done)}" if done else "no L2 channels"

    def portal_step(self):
        from .models import L2Channel, Lifecycle, Membership, PrefixAssignment, QuotaRecord

        t = self.tenant
        prefixes = list(PrefixAssignment.active().filter(tenant=t))
        now = timezone.now()
        PrefixAssignment.active().filter(tenant=t).update(released_at=now)
        if not self.live:  # OpenNebula not touched (read-only, not deployed); live, l2_step removed them one by one
            t.l2_channels.exclude(state=L2Channel.State.REMOVED).update(state=L2Channel.State.REMOVED)
        from django.db import transaction

        from .models import lock_quotas

        with transaction.atomic():
            lock_quotas(t)  # core-021: as every quota writer
            for r in t.current_quota_records():
                QuotaRecord.objects.create(
                    tenant=t,
                    site=r.site,
                    cluster=r.cluster,
                    removed=True,
                    version=QuotaRecord.next_version(t, r.site, r.cluster),
                    effective_from=now,
                    created_by=self.user if getattr(self.user, "pk", None) else None,
                )
        Membership.objects.filter(tenant=t).delete()
        t.status = Lifecycle.ARCHIVED
        t.one_group_id = None
        t.save(update_fields=["status", "one_group_id"])
        return f"{len(prefixes)} prefix(es) released" + (
            "" if self.live else "; OpenNebula not touched (read-only platform or not deployed)"
        )

    def run(self, plan):
        if self.live:
            self.connect()
            for name, fn in (
                ("l2", self.l2_step),
                ("vms", self.vms_step),
                ("resources", self.pools_step),
                ("verify", self.verify_step),
                ("acl", self.acl_step),
                ("users", self.users_step),
                ("group", self.group_step),
                ("check", self.check_step),
            ):
                self.step(name, lambda f=fn: f(plan))
        self.step("portal", self.portal_step)
        return self.steps


def archive(tenant, user=None):
    """Archive a project: everything it still has goes, on the platform (its group, users, VMs, images, backups, …)
    and in the portal (its subnets, quota records, L2 channels, memberships); the record stays as history (owner
    2026-11-11: "archived project always releases all resources"). Raises DeployError when something could not be
    deleted: the project is then left as it was, for the next round."""
    u = Undeployer(tenant, user)
    if u.blocked:
        raise DeployError(u.blocked)
    if u.live:
        u.run(u.inventory())  # ends with the portal step
    else:
        u.step("portal", u.portal_step)
    return u.steps


def carried_ids(plan):
    """commit-044: the group and user ids a plan knows - its own and those it carried from a plan before it - so what
    they owned is still found after they are deleted, through any number of retries."""
    plan = plan or {}
    former = plan.get("known") or {}
    group = plan.get("group") or former.get("group") or {}
    users = {u["id"]: u for u in (former.get("users") or []) + (plan.get("users") or []) if u.get("id") is not None}
    return {"group": group, "users": list(users.values())} if (group or users) else {}


def make_plan(deletion, known=None):
    """List the resources of the tenant and attach them to the deletion request. known: the plan of a deletion that
    ran before (Retry deletion) - its group and user ids are kept (carried_ids)."""
    u = Undeployer(deletion.tenant)
    deletion.plan, deletion.plan_error = {}, ""
    if u.blocked:
        deletion.plan_error = u.blocked
    elif u.live:
        try:
            deletion.plan = u.inventory(known=known)
            if known:
                p = deletion.plan
                ids = carried_ids({"known": carried_ids(known), "group": p.get("group"), "users": p.get("users")})
                if ids:
                    p["known"] = ids
        except Exception as exc:  # noqa: BLE001 - shown on the request; planned again at approval
            deletion.plan_error = f"{type(exc).__name__}: {exc}"[:2000]
            if carried_ids(known):  # commit-044: not listed - the ids are kept for the next listing
                deletion.plan = {"known": carried_ids(known)}
    else:
        deletion.plan_error = "OpenNebula is not touched: read-only platform or not deployed"
    deletion.planned_at = timezone.now()
    deletion.save(update_fields=["plan", "plan_error", "planned_at"])
    return deletion


def execute(deletion_id, user_id=None):
    """Run an approved deletion with the resources listed on the request. Never raises."""
    from accounts.models import User, audit

    from .models import Lifecycle, TenantDeletion

    d = TenantDeletion.objects.select_related("tenant").get(pk=deletion_id)
    user = User.objects.filter(pk=user_id).first() if user_id else None
    if d.state != TenantDeletion.State.RUNNING or d.finished_at:
        # core-025: only a deletion its approval claimed (pending → running, in deletion_decide_view) is run; a
        # request still pending, cancelled, rejected or already finished is left as it is
        return d
    t = d.tenant
    u = Undeployer(t, user, deletion=d)
    if u.live and (d.plan_error or not d.plan):
        make_plan(d, known=d.plan or None)  # no list yet, or a retry: list again (commit-044: with the ids it knew)
    if u.blocked:  # core-013: nothing is touched, the project stays as it is
        d.state, d.error, d.finished_at = TenantDeletion.State.FAILED, u.blocked[:2000], timezone.now()
        d.save()
        audit(user, "tenant_delete_failed", t, error=u.blocked[:300])
        return d
    t.status = Lifecycle.DELETING
    t.save(update_fields=["status"])
    from .models import current_runner

    d.state, d.steps, d.error, d.runner = TenantDeletion.State.RUNNING, [], "", current_runner()  # core-007
    d.save()
    try:
        if u.live and d.plan_error:
            raise DeployError(f"the resources could not be listed: {d.plan_error}")
        u.run(d.plan)
        d.state = TenantDeletion.State.DONE
        audit(user, "tenant_deleted", t, group=t.group_name, opennebula=u.live, resources=d.plan_total)
    except DeployError as exc:
        d.state, d.error = TenantDeletion.State.FAILED, str(exc)[:2000]
        audit(user, "tenant_delete_failed", t, error=str(exc)[:300])
    d.steps, d.finished_at = u.steps, timezone.now()
    d.save()
    return d


def start(deletion, user=None):
    import threading

    from django.conf import settings
    from django.db import connection

    if not getattr(settings, "DEPLOY_ASYNC", True):
        return execute(deletion.pk, getattr(user, "pk", None))

    def worker():
        try:
            execute(deletion.pk, getattr(user, "pk", None))
        finally:
            connection.close()

    threading.Thread(target=worker, name=f"delete-{deletion.tenant_id}", daemon=True).start()
    return deletion
