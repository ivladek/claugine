"""Loading the inventory files (a revision's YAML tree) into the portal's records.

The portal keeps the infrastructure as its own records, edited only in the UI. The files are a source to load
from: once, when the portal takes over (``manage.py load_inventory``), and in tests as fixtures. Loading adds and
updates records by their names and never deletes what the files do not mention (unless ``replace``), so
everything that refers to a record (tenants, platform networks, datastores) stays valid.
"""
from django.db import transaction

from . import tree as inv
from .tree import _d, _int, _item_state, _ref_name, kind_term


def _lst(v):
    return [str(x) for x in v] if isinstance(v, list) else []


def _profile_values(p):
    p = _d(p)
    cpu, ram, gpus, chassis = _d(p.get("CPU")), _d(p.get("RAM")), _d(p.get("gpus")), _d(p.get("chassis"))
    boot = _d(p.get("boot"))
    vols = [{"name": str(k), "size_gb": _int(_d(v).get("size_gb"), 0), "fs": str(_d(v).get("fs") or ""),
             "path": str(_d(v).get("path") or "")} for k, v in _d(boot.get("volumes")).items()]
    nics = _d(p.get("nics"))
    cards = []
    for name, c in _d(nics.get("hardware")).items():
        c = _d(c)
        cards.append({"name": str(name), "vendor": str(c.get("vendor") or ""), "model": str(c.get("model") or ""),
                      "ports": _int(c.get("ports"), 0), "media": str(c.get("media") or ""), "speed": _int(c.get("speed"), 0),
                      "bus": str(c.get("bus") or ""), "slot": str(c.get("slot") or ""), "mtu": _int(c.get("mtu"), 0) or None,
                      "links": _lst(c.get("links"))})
    bonds = [{"name": str(name), "links": _lst(_d(b).get("links")), "mtu": _int(_d(b).get("mtu"), 0) or None}
             for name, b in _d(nics.get("bonds")).items()]
    dimm = _d(ram.get("dimm"))
    return dict(
        vendor=str(chassis.get("vendor") or ""), chassis=str(chassis.get("model") or ""),
        ipmi_type=str(p.get("ipmi") or ""),
        cpu_vendor=str(cpu.get("vendor") or ""), cpu_name=str(cpu.get("model") or ""),
        sockets=_int(cpu.get("sockets"), 1) or 1, cores_per_socket=_int(cpu.get("cores"), 0),
        cpu_mhz=_int(cpu.get("freq"), 0), ram_gb=_int(ram.get("total_gb"), 0),
        ram={k: _int(dimm.get(k), 0) for k in ("size_gb", "freq", "slots", "modules") if dimm.get(k) is not None},
        gpu_model=str(gpus.get("model") or ""), gpu_count=_int(gpus.get("quantity"), 0),
        gpu_vram_gb=_int(_d(gpus.get("hw")).get("vram_gb"), 0),
        boot={"bus": str(boot.get("bus") or ""), "controller": str(boot.get("controller") or ""),
              "dev": str(boot.get("dev") or ""), "size_gb": _int(boot.get("size_gb"), 0), "volumes": vols} if boot else {},
        nics={"cards": cards, "bonds": bonds} if (cards or bonds) else {},
    )


def _storages(sdata):
    """{name: (data, host name or None)} of a site's storages; local disks are kept per host (storages/local/<host>)."""
    out = {}
    for name, d in _d(_d(sdata).get("storages")).items():
        d = _d(d)
        if name == "local" and "volumes" not in d and "type" not in d:
            for host, hd in d.items():
                out[f"local-{host}"] = (_d(hd), str(host))
        else:
            out[str(name)] = (d, None)
    return out


DS_KINDS = {"vms": "vms", "images": "images", "files": "files", "backups": "backups", "restic": "backups",
            "backup": "backups", "system": "vms"}


def load(tree, replace=False, report=None):
    """Create or update the records from `tree`. Returns {what: count}; notes go to `report` (a list)."""
    from . import models as m
    from .networks import ensure_default_ranges, mirror_site, sync_index

    report = [] if report is None else report
    counts = {}
    with transaction.atomic():
        if replace:
            m.PlatformDatastore.objects.all().delete()
            m.PlatformNetwork.objects.all().delete()
            m.Cluster.objects.all().delete()
            m.Platform.objects.all().delete()
            m.StorageVolume.objects.all().delete()
            m.Storage.objects.all().delete()
            m.DelegatedSubnet.objects.all().delete()
            m.Segment.objects.all().delete()
            m.Host.objects.all().delete()
            m.Site.objects.all().delete()
            m.ServerProfile.objects.all().delete()

        profiles = {}
        for code, p in _d(tree.get("server_profiles")).items():
            obj, _c = m.ServerProfile.objects.update_or_create(code=code, defaults=_profile_values(p))
            profiles[code] = obj
        counts["profiles"] = len(profiles)

        sites, hosts = {}, {}
        resources = _d(tree.get("resources"))
        providers = list(_d(tree.get("providers")))
        for code in sorted(set(resources) | set(_d(tree.get("platforms")))):
            sd = _d(resources.get(code))
            loc = _d(sd.get("location"))
            extra = {k: sd[k] for k in ("repos",) if sd.get(k)}
            vals = dict(country=str(loc.get("country", "")), city=str(loc.get("city", "")), dc=str(loc.get("dc", "")),
                        timezone=str(sd.get("timezone") or "").replace("timezones.", ""),
                        ntp_servers=_lst(sd.get("ntp_servers"))[:4], dns_servers=_lst(sd.get("dns_servers"))[:3],
                        bootstrap_dns=_lst(sd.get("bootstrap_dns_servers"))[:2], ipmi=_d(sd.get("ipmi")),
                        jump=_d(sd.get("ssh_jump")), management=_d(sd.get("management")), extra=extra)
            number = _int(sd.get("site_code"), None)
            if number:
                vals["number"] = number
            sites[code], _c = m.Site.objects.update_or_create(code=code, defaults=vals)
        counts["sites"] = len(sites)

        for code, sdata in resources.items():
            site = sites[code]
            for hname, h in _d(_d(sdata).get("hosts")).items():
                h = _d(h)
                prof = _ref_name(h.get("server_profile"), "server_profiles")
                ipmi = _d(h.get("ipmi"))
                obj, _c = m.Host.objects.update_or_create(site=site, name=hname, defaults=dict(
                    profile=profiles.get(prof), ip=str(h.get("ip", "")), state=_item_state(h.get("state")),
                    ipmi_host=str(ipmi.get("host") or ""), ipmi_type=str(ipmi.get("type") or ""),
                    ipmi_user=str(ipmi.get("user") or "")))
                hosts[f"resources.{code}.hosts.{hname}"] = obj
        counts["hosts"] = len(hosts)

        # networks first: platform networks refer to them and to their ranges
        for code, sdata in resources.items():
            nets = {n: _d(d) for n, d in _d(_d(sdata).get("networks")).items()}
            for note in mirror_site(code, nets, remove=replace):
                report.append(f"{code}: {note}")
            for n in m.Network.objects.filter(site=code):
                report.extend(ensure_default_ranges(n))
        sync_index()
        counts["networks"] = m.Network.objects.count()

        volumes = {}
        n_storages = 0
        for code, sdata in resources.items():
            site = sites[code]
            for name, (d, host) in _storages(sdata).items():
                kind = str(d.get("type") or ("local" if host else "")).lower()
                if kind not in m.Storage.Kind.values:
                    report.append(f"{code}: storage {name} has type {kind or '—'}; loaded as nfs")
                    kind = "nfs"
                host_obj = hosts.get(f"resources.{code}.hosts.{host}") if host else None
                if host and host_obj is None:
                    report.append(f"{code}: local storage of {host}: no such host")
                st, _c = m.Storage.objects.update_or_create(site=site, name=name, defaults=dict(
                    kind=kind, targets=_lst(d.get("targets")), fstab=str(d.get("parameters") or ""),
                    port=_int(d.get("port"), None) or (3260 if kind == "iscsi" else None), host=host_obj))
                n_storages += 1
                ref = f"resources.{code}.storages." + (f"local.{host}" if host else name)
                for vname, v in _d(d.get("volumes")).items():
                    v = _d(v)
                    vol, _c = m.StorageVolume.objects.update_or_create(storage=st, name=str(vname), defaults=dict(
                        device=str(v.get("dev") or ""), size_gb=max(_int(v.get("size_gb"), 0) or 0, 0),
                        clients=_lst(v.get("clients")), mount=str(v.get("mount") or "")))
                    volumes[f"{ref}.volumes.{vname}"] = vol
        counts["storages"] = n_storages
        counts["volumes"] = len(volumes)

        n_pl = n_cl = n_ds = n_vn = 0
        for scode, sp in _d(tree.get("platforms")).items():
            for pname, p in _d(sp).items():
                p = _d(p)
                meta, fe = _d(p.get("platform")), _d(p.get("fe"))
                provider = _ref_name(meta.get("provider"), "providers") or (providers[0] if providers else "")
                plat, _c = m.Platform.objects.update_or_create(key=f"{scode}.{pname}", defaults=dict(
                    site=sites[scode], name=pname, kind=kind_term(meta.get("kind", "")),
                    state=str(meta.get("state", "deployed")), fe_name=str(fe.get("name", "")),
                    fe_mode=str(fe.get("mode", "")), fe_vip=str(fe.get("vip", "")),
                    fe_secrets=str(fe.get("secrets", "") or ""),
                    fe_primary=str(fe.get("primary", "")).replace("platforms.", ""),
                    provider=provider, sa=str(meta.get("sa") or ""), shared_owner=str(meta.get("shared_owner") or ""),
                    fe={k: fe[k] for k in ("type", "nodes", "nic", "hostname", "empty_bridges", "secondaries") if k in fe}))
                n_pl += 1
                pstate = str(meta.get("state", "deployed"))
                clusters = {}
                for cname, c in _d(p.get("clusters")).items():
                    c = _d(c)
                    cstate = "planned" if pstate == "planned" else _item_state(c.get("state"))
                    cl, _c = m.Cluster.objects.update_or_create(platform=plat, name=cname, defaults=dict(
                        kind=kind_term(c.get("kind", "")), quota_code=str(c.get("quota_code") or "").lower(), state=cstate))
                    members = [hosts[h] for h in c.get("hosts") or [] if h in hosts]
                    cl.hosts.set(members)
                    if _c and any(h.profile_id and h.profile.gpu_count for h in members):
                        # owner 2026-11-09 (gpu-bundles): spare hosts now apply to GPU clusters too, but this is
                        # the value a newly imported one *starts* at, not a rule any more. A GPU host is too
                        # expensive to hold back by default, and on a one-host cluster the default of 1 would hold
                        # back the only host and offer nothing. Staff can set a spare in the cluster's settings.
                        cl.spare_hosts = 0
                        cl.save(update_fields=["spare_hosts"])
                    if cstate == "planned":
                        for h in members:  # hosts that serve only planned clusters are not in service yet either
                            if h.state == "active" and not h.clusters.exclude(state="planned").exists():
                                h.state = "planned"
                                h.save(update_fields=["state"])
                    clusters[cname] = cl
                    n_cl += 1

                def members_of(o, what, oname):
                    listed = _lst(_d(o).get("clusters"))
                    found = [clusters[x] for x in listed if x in clusters]
                    unknown = [x for x in listed if x not in clusters]
                    if unknown:
                        report.append(f"{plat.key}: {what} {oname} names clusters that do not exist: {', '.join(unknown)}"
                                      + ("; given to every cluster of the platform" if not found else ""))
                    return found or list(clusters.values())

                for dname, o in _d(p.get("datastores")).items():
                    o = _d(o)
                    t = str(o.get("type") or "")
                    kind = DS_KINDS.get(t)
                    if kind is None:
                        report.append(f"{plat.key}: datastore {dname} has type {t or '—'}; loaded as files")
                        kind = "files"
                    vref = str(o.get("volume") or "")
                    vol = volumes.get(vref)
                    if vref and vol is None:
                        report.append(f"{plat.key}: datastore {dname}: volume {vref} not found")
                    extra = {k: o[k] for k in ("service",) if o.get(k)}
                    if t and t != kind:
                        extra["type"] = t
                    ds, _c = m.PlatformDatastore.objects.update_or_create(platform=plat, name=dname, defaults=dict(
                        kind=kind, volume=vol, path=str(o.get("path") or ""), extra=extra))
                    for cl in members_of(o, "datastore", dname):
                        cl.datastores.add(ds)
                    n_ds += 1

                for vname, o in _d(p.get("networks")).items():
                    o = _d(o)
                    net_name = _ref_name(o.get("network"), f"resources.{scode}.networks")
                    net = m.Network.objects.filter(site=scode, name=net_name).first() if net_name else None
                    if o.get("network") and net is None:
                        report.append(f"{plat.key}: network {vname}: {o.get('network')} not found")
                    wanted = set(_lst(o.get("ranges")))
                    own = str(vname).rsplit("_", 1)[0]
                    if net is not None and wanted - set(net.ranges.values_list("name", flat=True)) and own != net.name:
                        alt = m.Network.objects.filter(site=scode, name=own).first()
                        if alt is not None and not wanted - set(alt.ranges.values_list("name", flat=True)):
                            report.append(f"{plat.key}: network {vname} pointed to {net.name}, whose ranges it does not "
                                          f"have; {own} has them and is used")
                            net, net_name = alt, own
                    kind = "bridge" if str(o.get("type") or "vlan") == "bridge" else "vlan"
                    vn, _c = m.PlatformNetwork.objects.update_or_create(platform=plat, name=vname, defaults=dict(
                        kind=kind, network=net, bridge=str(o.get("bridge") or "")))
                    rngs = [r for r in (net.ranges.filter(name__in=_lst(o.get("ranges"))) if net else [])]
                    missing = set(_lst(o.get("ranges"))) - {r.name for r in rngs}
                    if missing:
                        report.append(f"{plat.key}: network {vname}: ranges {', '.join(sorted(missing))} not in "
                                      f"{net_name or '—'}")
                    vn.ranges.set(rngs)
                    for cl in members_of(o, "network", vname):
                        cl.networks.add(vn)
                    n_vn += 1
                refresh_segments(plat)
        counts.update(platforms=n_pl, clusters=n_cl, datastores=n_ds, platform_networks=n_vn)
    return counts


def refresh_segments(platform):
    """Platform.segments (site networks it is connected to) from its networks."""
    names = sorted({vn.network.name for vn in platform.vnets.select_related("network") if vn.network_id})
    if names != list(platform.segments or []):
        platform.segments = names
        platform.save(update_fields=["segments"])


def load_revision(rev, user=None, replace=False):
    """Load a revision (the current one by default) and keep a record of it."""
    from .models import InventoryLoad
    tree, errors = inv.build_tree(rev.files)
    report = [f"file: {e}" for e in errors]
    counts = load(tree, replace=replace, report=report)
    return InventoryLoad.objects.create(revision=rev, by=user, counts=counts, report=report)

