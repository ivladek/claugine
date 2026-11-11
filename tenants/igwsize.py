"""The size of a tenant's igw VM (owner 2026-10-29 18:58): vCPU and RAM, 1 and 1 GB by default — chosen when the igw is
made and changed later from Internet → Details. A powered-off igw is resized at once; a running one is resized live
when the platform allows it, otherwise (when asked) powered off, resized and started again — its internet stops for
that minute. What the platform reports (VM name, image, vCPU, RAM, disks) is read with every tenants refresh
(tenants.igwdetect)."""
import time

from .deploy import DeployError, Deployer, One, template

WAIT_S = 180
MAX_CPU, MAX_RAM_GB = 16, 64


def _vm(tenant):
    d = Deployer(tenant)
    d.one = One(d.control)
    vm_id = (tenant.igw_vm or {}).get("id")
    if vm_id is None:
        vm_id = d._vm_exists(d.gw("igw"))
    return d.one, vm_id


def _wait(one, vm_id, states):
    end = time.monotonic() + WAIT_S
    while True:
        x = one.xml("one.vm.info", vm_id, False)
        st, lcm = x.findtext("STATE"), x.findtext("LCM_STATE")
        if (st, lcm) in states or (st == "8" and ("8", None) in states):
            return x
        if time.monotonic() > end:
            raise DeployError(f"igw #{vm_id}: still {st}/{lcm} after {WAIT_S} s")
        time.sleep(3)


def resize(tenant, cpu, ram_gb, restart=False, user=None):
    """Keep the size on the tenant and set it on the igw VM (when there is one). Returns a short report."""
    from accounts.models import audit
    if not (1 <= cpu <= MAX_CPU and 1 <= ram_gb <= MAX_RAM_GB):
        raise DeployError(f"vCPU 1-{MAX_CPU}, RAM 1-{MAX_RAM_GB} GB")
    old = (tenant.igw_cpu, tenant.igw_ram_gb)
    tenant.igw_cpu, tenant.igw_ram_gb = cpu, ram_gb
    tenant.save(update_fields=["igw_cpu", "igw_ram_gb"])
    one, vm_id = _vm(tenant)
    if vm_id is None:
        audit(user, "igw_size_set", tenant, size=f"{cpu} vCPU / {ram_gb} GB", old=f"{old[0]} / {old[1]}", vm="none yet")
        return f"kept: {cpu} vCPU, {ram_gb} GB (the igw is made with it)"
    tpl = template({"CPU": cpu, "VCPU": cpu, "MEMORY": ram_gb * 1024})
    x = one.xml("one.vm.info", vm_id, False)
    how = ""
    if x.findtext("STATE") == "8":
        one.call("one.vm.resize", vm_id, tpl, False)
        how = "resized (powered off)"
    else:
        try:
            one.call("one.vm.resize", vm_id, tpl, False)
            how = "resized live"
        except DeployError as exc:
            if not restart:
                raise DeployError(f"the platform does not resize the running igw ({exc}); tick “power off, resize and "
                                  "start again” — its internet stops for about a minute") from exc
            one.call("one.vm.action", "poweroff", vm_id)
            _wait(one, vm_id, {("8", None), ("8", "0")})
            one.call("one.vm.resize", vm_id, tpl, False)
            one.call("one.vm.action", "resume", vm_id)
            _wait(one, vm_id, {("3", "3")})
            how = "powered off, resized and started again"
    vm = dict(tenant.igw_vm or {})
    vm.update(cpu=cpu, ram_gb=ram_gb)
    tenant.igw_vm = vm
    tenant.save(update_fields=["igw_vm"])
    audit(user, "igw_resized", tenant, vm=vm_id, size=f"{cpu} vCPU / {ram_gb} GB", old=f"{old[0]} / {old[1]}", how=how)
    return f"igw #{vm_id}: {cpu} vCPU, {ram_gb} GB — {how}"
