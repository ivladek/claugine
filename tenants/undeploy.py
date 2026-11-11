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


def names_of(tenant):
    """A pattern of the names of the tenant's objects that the provider owns (its gateways, …): <group>-…, the old
    system's tenant_<name>-<what>_<site>, and the tenant VyOS names _<org>_<tenant>_<role>_<site> (correction 11). Exact,
    so that tenant "beta" never takes the objects of tenant "beta-ml"."""
    from .deploy import object_base
    from .naming import GROUP_RE, tenant_parts
    g = tenant.group_name or ""
    pats = []
    for base in {g, object_base(tenant)} - {""}:  # <base>-<what>_<site> (a tenant name may have "-": exact)
        pats.append(re.escape(base) + r"-[a-z0-9]+_[a-z0-9]+")
    org, ten = tenant_parts(tenant)
    pats.append(f"_{re.escape(org)}_{re.escape(ten)}_[a-z0-9]+_[a-z0-9]+")
    return re.compile("^(?:" + "|".join(pats) + ")$")


class Undeployer:
    def __init__(self, tenant, user=None, dry=False, deletion=None):
        from inventory.models import Platform, PlatformControl
        self.tenant, self.user, self.dry, self.deletion = tenant, user, dry, deletion
        self.steps = []
        o = tenant.order or {}
        self.platform = Platform.objects.select_related("site").filter(key=o.get("platform")).first()
        self.control = PlatformControl.objects.filter(key=o.get("platform")).first() if self.platform else None
        self.live = bool(self.control and self.control.mode == "execution" and self.control.configured and tenant.group_name)
        if self.live:
            primary = self.platform.fe_primary or self.platform.key
            self.primary = PlatformControl.objects.filter(key=primary).first() or self.control

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
        audit(self.user, "tenant_object_deleted", self.tenant, kind=kind, id=item.get("id"), name=item.get("name", "")[:200],
              status=status)

    # --- reading the platform ------------------------------------------------------------------

    def connect(self):
        if not hasattr(self, "one"):
            self.one = One(self.control)
            self.gone = One(self.primary) if self.primary.key != self.control.key else self.one

    def inventory(self, known=None):
        """Everything of the tenant on the platform: {key: [{"id", "name"}]}, users, group, ACLs.

        `known` (a former plan) adds its group and user ids, so what they owned is found after they are gone."""
        self.connect()
        known = known or {}
        g = self.gone.find("one.grouppool.info", "GROUP", self.tenant.group_name)
        gid = int(g.findtext("ID")) if g is not None else None
        owner_gid = gid if gid is not None else (known.get("group") or {}).get("id")
        users, members = [], []
        if gid is not None:
            for u in self.gone.xml("one.userpool.info").findall("USER"):
                groups = {x.text for x in u.findall("GROUPS/ID")}
                item = {"id": int(u.findtext("ID")), "name": u.findtext("NAME")}
                if u.findtext("GID") == str(gid):
                    users.append(item)
                elif str(gid) in groups:
                    members.append(item)
        uids = {str(u["id"]) for u in users} | {str(u["id"]) for u in known.get("users") or []}
        named = names_of(self.tenant)

        def mine(el):
            return ((owner_gid is not None and el.findtext("GID") == str(owner_gid)) or el.findtext("UID") in uids
                    or bool(named.match(el.findtext("NAME") or "")))

        inv = {"group": {"id": gid, "name": self.tenant.group_name} if gid is not None else {},
               "users": users, "members": members}
        inv["vms"] = [{"id": int(v.findtext("ID")), "name": v.findtext("NAME")}
                      for v in self.one.xml("one.vmpool.info", -2, -1, -1, -1).findall("VM") if mine(v)]
        for key, pool, tag, _d, _x in POOLS:
            try:
                items = self.one.xml(pool, -2, -1, -1).findall(tag)
            except DeployError:
                items = []
            inv[key] = [{"id": int(el.findtext("ID")), "name": el.findtext("NAME")} for el in items if mine(el)]
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

    # --- deleting what the plan lists -----------------------------------------------------------

    def _present(self, key, pool, tag):
        try:
            return {int(el.findtext("ID")) for el in self.one.xml(pool, -2, -1, -1, *([-1] if key == "vms" else [])).findall(tag)}
        except DeployError:
            return set()

    def vms_step(self, plan):
        vms = plan.get("vms") or []
        left = self._present("vms", "one.vmpool.info", "VM")
        todo = [v for v in vms if v["id"] in left]
        for v in vms:
            if v["id"] not in left:
                self._done("vm", v, "gone")
        for v in todo:
            try:
                self.one.call("one.vm.action", "terminate-hard", v["id"])
            except DeployError:
                self._done("vm", v, "failed")
                raise
        ids = {v["id"] for v in todo}
        deadline = time.monotonic() + WAIT_SECONDS
        while ids:
            ids &= self._present("vms", "one.vmpool.info", "VM")
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
        for key, pool, tag, delete, extra in POOLS:
            items = plan.get(key) or []
            if not items:
                continue
            left = self._present(key, pool, tag)
            done, waiting = 0, set()
            for it in items:
                if it["id"] not in left:
                    self._done(key, it, "gone")
                    continue
                try:
                    self.one.call(delete, it["id"], *extra)
                except DeployError:
                    if it["id"] not in self._present(key, pool, tag):  # gone meanwhile (e.g. a background delete ended)
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
                waiting &= self._present(key, pool, tag)
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
        if group and left.get("group"):
            rest.append(f"group #{left['group']['id']}")
        if rest:
            raise DeployError(f"still on the platform: {', '.join(rest[:20])}{' …' if len(rest) > 20 else ''}")
        return "nothing of the project is left"

    # --- portal ------------------------------------------------------------------------------

    def portal_step(self):
        from .models import L2Channel, Lifecycle, Membership, PrefixAssignment, QuotaRecord
        t = self.tenant
        prefixes = list(PrefixAssignment.active().filter(tenant=t))
        now = timezone.now()
        PrefixAssignment.active().filter(tenant=t).update(released_at=now)
        t.l2_channels.exclude(state=L2Channel.State.REMOVED).update(state=L2Channel.State.REMOVED)
        for r in t.current_quota_records():
            QuotaRecord.objects.create(tenant=t, site=r.site, cluster=r.cluster, removed=True,
                                       version=QuotaRecord.next_version(t, r.site, r.cluster), effective_from=now,
                                       created_by=self.user if getattr(self.user, "pk", None) else None)
        Membership.objects.filter(tenant=t).delete()
        t.status = Lifecycle.ARCHIVED
        t.one_group_id = None
        t.save(update_fields=["status", "one_group_id"])
        return f"{len(prefixes)} prefix(es) released" + ("" if self.live else "; OpenNebula not touched (read-only platform "
                                                                               "or not deployed)")

    def run(self, plan):
        if self.live:
            self.connect()
            for name, fn in (("vms", self.vms_step), ("resources", self.pools_step), ("verify", self.verify_step),
                             ("acl", self.acl_step),
                             ("users", self.users_step), ("group", self.group_step), ("check", self.check_step)):
                self.step(name, lambda f=fn: f(plan))
        self.step("portal", self.portal_step)
        return self.steps


def make_plan(deletion):
    """List the resources of the tenant and attach them to the deletion request."""
    u = Undeployer(deletion.tenant)
    deletion.plan, deletion.plan_error = {}, ""
    if u.live:
        try:
            deletion.plan = u.inventory()
        except Exception as exc:  # noqa: BLE001 - shown on the request; planned again at approval
            deletion.plan_error = f"{type(exc).__name__}: {exc}"[:2000]
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
    t = d.tenant
    u = Undeployer(t, user, deletion=d)
    if u.live and (d.plan_error or not d.plan or d.state == TenantDeletion.State.FAILED):
        make_plan(d)  # no list yet, or a repeated run: list again
    t.status = Lifecycle.DELETING
    t.save(update_fields=["status"])
    d.state, d.steps, d.error = TenantDeletion.State.RUNNING, [], ""
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
