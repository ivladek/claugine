"""Backups of the platforms' OpenNebula front ends (owner 2026-10-31, u04): what claugine-cli jobs/fe-backup.sh did from
cron on the admin hosts (fe_backup full=yes of every platform), now done by the portal.

Settings → Backups → Platform front ends: on/off, the time of the daily backup, how many days to keep them, the folders
of each front-end node. They go where the portal's own backups go (Settings → Backups: the local folder and/or the
S3 bucket), beside them:

    platform_<platform>_<VIP>_<YYYYmmdd-HHMM>/     (owner 2026-11-12: one archive and one script, nothing else)
        <name>.7z    AES-256 with the archive password, names encrypted:
            <server id>-<name>-<address>_<leader|follower>/   per front-end node:
                config.tar.gz            the folders (by default /etc/one, /var/lib/one/.ssh, .one, remotes)
                onedb-zone.sql.gz        the leader: onedb backup of the zone's database
                onedb-federated.sql.gz   the leader of the federation's primary: onedb backup --federated
        <name>.sh    its header: the platform, zone, nodes and folders; it checks the archive and unpacks it
                     (owner: "platform - only unpack"), then prints how to put a node back

The nodes are the servers of the platform's zone (XML-RPC one.zone.info), the leader the one the VIP answers from
(one.zone.raftstatus). The portal logs in as the OS user oneadmin with the key of the platform's front-end secret
(Infrastructure → Secrets), reads only, and removes on the node only its own temporary dump. Old local backups are
removed (keep days) only after a run without errors; the S3 bucket keeps its own (its lifecycle rules).
Run by the appliance timer on the active node (as root), daily at the set time, or at once with "Back up now".
"""

import os
import secrets
import shutil
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import timedelta

from django.utils import timezone

FOLDER = ""  # owner 2026-11-08: straight into the configured folder, beside the portal's backups
PREFIX = "platform_"  # owner 2026-11-08: each backup is platform_<name>_<vip>_YYYYmmdd-HHMM


def _settings():
    from tenants.models import PortalSettings

    return PortalSettings.get()


def platforms(keys=None):
    """The platforms that can be backed up (a VIP, an endpoint and a front-end secret with the oneadmin ssh key);
    u06: narrowed to the chosen keys when `keys` is given."""
    from inventory.models import Platform

    out = [p for p in Platform.objects.exclude(fe_vip="").exclude(fe_secrets="").select_related("site").order_by("key")]
    return [p for p in out if p.key in keys] if keys else out


def dirs(s):
    return s.fe_backup_dir_list()


def due(s, now=None):
    """u07: one platform backup a day, at fe_backup_time — due when today's slot has passed and today's has not run,
    or when requested. Local and S3 are written by the same run (local is the source)."""
    from accounts import edition

    if not (
        s.fe_backup_local or (s.fe_backup_s3 and edition.allows("s3_backups", s) and edition.ee("s3") is not None)
    ):  # no destination switched on
        return False
    if s.fe_backup_requested_at:
        return True
    now = timezone.localtime(now or timezone.now())
    slot = now.replace(hour=s.fe_backup_time.hour, minute=s.fe_backup_time.minute, second=0, microsecond=0)
    if slot > now:
        slot -= timedelta(days=1)
    last = timezone.localtime(s.fe_backup_last_at) if s.fe_backup_last_at else None
    return (last is None or last < slot) and now - slot < timedelta(hours=12)


def _nodes(p):
    """[(server id, name, address, role)] of the platform's zone; the leader first."""
    from adoption.collector import client_for
    from inventory.models import PlatformControl

    c = PlatformControl.objects.filter(key=p.key).first()
    if c is None:
        raise RuntimeError("no endpoint (Infrastructure → the platform)")
    cl = client_for(c)
    cfg = ET.fromstring(cl._call("one.system.config"))
    zone = int(cfg.findtext("FEDERATION/ZONE_ID") or 0)
    mode = (cfg.findtext("FEDERATION/MODE") or "STANDALONE").upper()
    z = ET.fromstring(cl._call("one.zone.info", zone))
    raft = ET.fromstring(cl._call("one.zone.raftstatus"))
    leader = raft.findtext("SERVER_ID") if raft.findtext("STATE") == "3" else None
    out = []
    from urllib.parse import urlsplit

    for srv in z.findall("SERVER_POOL/SERVER"):
        sid, name = srv.findtext("ID"), srv.findtext("NAME")
        addr = urlsplit(srv.findtext("ENDPOINT") or "").hostname or ""
        out.append((sid, name, addr, "leader" if sid == leader else "follower"))
    if not out:  # a front end without HA: the VIP is the node
        out = [("0", p.fe_name or p.key, p.fe_vip, "leader")]
    out.sort(key=lambda n: n[3] != "leader")
    return zone, mode, out


def _node_backup(p, node, key, content, folders, log):
    """One node's folder <content>/<id>-<name>-<address>_<role>/; returns (folder name or "", errors)."""
    from inventory.sshexec import SshError, fetch

    sid, name, addr, role = node
    base = f"{sid}-{name}-{addr}_{role}"
    out_dir = os.path.join(content, base)
    os.makedirs(out_dir, mode=0o700)
    errors, parts = [], []
    tgz = os.path.join(out_dir, "config.tar.gz")
    quoted = " ".join("'" + d.replace("'", "") + "'" for d in folders)
    try:
        warn = fetch(
            addr,
            22,
            "oneadmin",
            key,
            f"tar -czf - --ignore-failed-read {quoted} 2>/tmp/.claugine-tar-$$; rc=$?; "
            f"grep -v 'Removing leading' /tmp/.claugine-tar-$$ >&2; rm -f /tmp/.claugine-tar-$$; [ $rc -le 1 ]",
            tgz,
        )
        parts.append("config.tar.gz")
        if warn:
            log(f"{p.key} {name}: {warn.splitlines()[-1][:160]}")
    except SshError as exc:
        errors.append(f"{name} config: {exc}")
    if role == "leader":
        kinds = [("zone", "")] + ([("federated", " --federated")] if p.fe_mode == "primary" else [])
        for kind, opt in kinds:
            out = os.path.join(out_dir, f"onedb-{kind}.sql.gz")
            t = f"/tmp/claugine-onedb-{secrets.token_hex(6)}.sql"
            try:
                fetch(
                    addr,
                    22,
                    "oneadmin",
                    key,
                    f"onedb backup {t}{opt} --force >/dev/null && gzip -c {t}; rc=$?; rm -f {t}; exit $rc",
                    out,
                    timeout=3600,
                )
                parts.append(f"onedb-{kind}.sql.gz")
            except SshError as exc:
                errors.append(f"{name} onedb {kind}: {exc}")
    for f in os.listdir(out_dir):
        if f not in parts:
            os.remove(os.path.join(out_dir, f))  # a partial file of a failed step
    if not parts:
        shutil.rmtree(out_dir)
        return "", errors
    return base, errors


AFTER = """One folder per front-end node, <server id>-<name>-<address>_<leader|follower>:
    config.tar.gz            the node's folders ({folders})
    onedb-zone.sql.gz        the leader only: onedb backup of the zone's database
    onedb-federated.sql.gz   the leader of a federation's primary zone: onedb backup --federated

Nothing is restored automatically. To put a node back, on that node, OpenNebula of the same version installed
and stopped:
    tar -xzf config.tar.gz -C /                                   the folders, with their paths
    gunzip -k onedb-zone.sql.gz && onedb restore -f onedb-zone.sql    the leader's database; start the leader,
                                                                  the followers catch up from it
See the OpenNebula documentation on restoring a front end for your version."""


def backup_platform(p, folder, s, log=print):
    """One platform's backup in `folder` (owner 2026-11-12: one archive and one script per backup,
    accounts/backup_archive.py): <name>/<name>.7z holding a folder per node, and <name>/<name>.sh, which checks and
    unpacks it; no MANIFEST or README (owner 2026-11-12), the script's header says what the backup is. Returns
    (path, errors)."""
    from inventory import credentials as cr

    from . import backup_archive

    sec = cr.find(p.fe_secrets)
    key = sec.get("os_private_key") if sec is not None and sec.has("os_private_key") else ""
    if not key:
        return "", [f"{p.key}: no ssh key of oneadmin in its front-end secret"]
    password = s.get_secret("fe_backup_password") or s.get_secret("backup_password")  # u06: the platforms' own password
    if not password:
        return "", [f"{p.key}: no archive password (Settings → Backups)"]
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M")
    name = f"{PREFIX}{p.name or p.key}_{p.fe_vip}_{stamp}"  # platform_<name>_<vip>_YYYYmmdd-HHMM
    final = os.path.join(folder, name)
    work = os.path.join(folder, "." + name + ".partial")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, mode=0o700)
    content = os.path.join(work, "content")
    os.makedirs(content, mode=0o700)
    errors, nodes_done = [], []
    try:
        zone, mode, nodes = _nodes(p)
        for node in nodes:
            done, errs = _node_backup(p, node, key, content, dirs(s), log)
            errors += errs
            if done:
                nodes_done.append(done)
        if not nodes_done:
            raise RuntimeError("nothing was read from the front-end nodes")
        info = [
            f"made      {timezone.localtime().isoformat(timespec='seconds')} by the Claugine portal",
            f"platform  {p.name or p.key} ({p.key}), VIP {p.fe_vip}, zone {zone}, federation {mode}",
            *[
                f"node      {n[0]} {n[1]} {n[2]} {n[3]}"
                + ("" if f"{n[0]}-{n[1]}-{n[2]}_{n[3]}" in nodes_done else " - not read")
                for n in nodes
            ],
            f"folders   {', '.join(dirs(s))}",
            *[f"error     {e}" for e in errors],
        ]
        archive = os.path.join(work, name + backup_archive.ARCHIVE)
        backup_archive.make(content, archive, password, level=1)  # the dumps are gzipped already
        shutil.rmtree(content)
        backup_archive.write_script(
            work, name, "platform", archive, info=info, after=AFTER.format(folders=", ".join(dirs(s)))
        )
        from .appliance import _fix_modes, _owner

        _fix_modes(work, _owner(folder, s))
        os.rename(work, final)
    except Exception as exc:  # noqa: BLE001 - kept in the result
        shutil.rmtree(work, ignore_errors=True)
        return "", errors + [f"{p.key}: {exc}"[:300]]
    return final, errors


def last_local(s, n=10):
    """The newest platform backups in the local folder [(name, size, time)], newest first (owner 2026-11-11: the
    Platforms block lists its backups like the Portal block does). [] when the folder can't be read."""
    from datetime import datetime

    from . import appliance

    folder = (os.path.join(s.fe_backup_path, FOLDER) if FOLDER else s.fe_backup_path) if s.fe_backup_path else ""
    try:
        names = [x for x in os.listdir(folder) if x.startswith(PREFIX) and os.path.isdir(os.path.join(folder, x))]
    except OSError:
        return []
    out = []
    for name in names:
        p = os.path.join(folder, name)
        try:
            out.append(
                (
                    name,
                    appliance._size(p),
                    datetime.fromtimestamp(os.path.getmtime(p), tz=timezone.get_current_timezone()),
                )
            )
        except OSError:
            pass
    return sorted(out, key=lambda x: x[2], reverse=True)[:n]


def s3_backups(s, n=10):
    """The platform backups in their bucket (newest name first), or [] - S3 off, a build without ee/, or a listing
    that fails: a listing problem must never blank the Settings page."""
    from accounts import edition

    s3 = edition.ee("s3")
    if s3 is None or not (s.fe_backup_s3 and edition.allows("s3_backups", s)):
        return []
    try:
        conf = s3.conf_of(s) if s.fe_backup_s3_use_portal else s3.conf_of_fe(s, inherit=False)
        if not conf or not conf.get("bucket"):
            return []
        names = [x.rstrip("/") for x in s3.list_objects(conf) if x.startswith(PREFIX)]
        return sorted(names, key=lambda x: x[-13:], reverse=True)[:n]  # the stamp YYYYmmdd-HHMM ends each name
    except Exception:  # noqa: BLE001 - display only
        return []


def prune(folder, days):
    cut = time.time() - days * 86400
    n = 0
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        if name.startswith(PREFIX) and os.path.isdir(path) and os.path.getmtime(path) < cut:
            shutil.rmtree(path)
            n += 1
    return n


def run(s=None, log=print):
    """Every chosen platform now (as root, the active node). u07: one run a day. If the local switch is on, the
    backups are written under <local folder>/claugine-platforms/; if S3 is on, each is copied to the bucket (one dir
    per backup, no root folder) — to the portal's bucket when 'use the Portal's S3 settings' is on, otherwise the
    platforms' own S3. If local is off, the backups are staged in a temp folder and only uploaded. Kept in Settings."""
    from accounts.models import audit
    from tenants.models import PortalSettings

    from . import appliance

    s = s or _settings()
    do_local = bool(s.fe_backup_local and (s.fe_backup_path or "").strip())
    # owner 2026-11-08 (editions): the platforms' backups have their own S3 settings, so the s3_backups feature has
    # to be checked here too - otherwise "no S3 backups" would hold for the portal's own backups and not for these.
    from accounts import edition

    s3 = edition.ee("s3")  # S3 is the Enterprise edition's (ee/s3.py)
    do_s3 = bool(s.fe_backup_s3 and edition.allows("s3_backups", s) and s3 is not None)
    started = timezone.now()
    PortalSettings.objects.filter(pk=s.pk).update(fe_backup_requested_at=None)
    tmp = tempfile.mkdtemp(prefix="claugine-fe-backup-")
    os.chmod(tmp, 0o700)
    done, errors = [], []
    try:
        if not do_local and not do_s3:
            raise RuntimeError("no destination: switch on a local folder and/or an S3 bucket for the platform backups")
        folder = (os.path.join(s.fe_backup_path, FOLDER) if FOLDER else s.fe_backup_path) if do_local else tmp
        made_folder = not os.path.isdir(folder)  # we only own what we create (owner 2026-11-08)
        os.makedirs(folder, mode=appliance.DIR_MODE, exist_ok=True)
        made = []  # the backup folders this run created
        s3conf = None
        if do_s3:  # u07: the portal's bucket, or the platforms' own S3 (no inherit-if-empty)
            s3conf = s3.conf_of(s) if s.fe_backup_s3_use_portal else s3.conf_of_fe(s, inherit=False)
        for p in platforms(s.fe_backup_platforms or None):
            path, errs = backup_platform(p, folder, s, log)
            errors += errs
            if not path:
                continue
            where = [path] if do_local else []
            try:
                if s3conf:
                    where.append(s3.upload_folder(s3conf, path, ""))  # one dir per backup, directly under the bucket
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{p.key}: copy: {exc}"[:300])
            made.append(path)
            done.append(
                f"{p.key} ({sum(os.path.getsize(os.path.join(path, f)) for f in os.listdir(path)) // 1024} KiB)"
            )
        if do_local:
            # owner 2026-11-08: assign the owner to each backup dir/file this run created, and nothing else - the
            # folder is shared with the portal's backups, so walking all of it would restamp someone else's files
            _own = appliance._owner(folder, s)
            if made_folder:
                appliance._fix_one(folder, _own)
            for _d in made:
                appliance._fix_modes(_d, _own)
            if not errors:
                prune(folder, s.fe_backup_keep_days)
        took = int((timezone.now() - started).total_seconds())
        result = (
            (f"ok: {len(done)} platforms" if not errors else f"failed: {len(errors)} errors; {len(done)} platforms")
            + f" in {took} s: "
            + ", ".join(done)
            + ("; " + "; ".join(errors) if errors else "")
        )[:2000]
    except Exception as exc:  # noqa: BLE001
        result = f"failed: {type(exc).__name__}: {exc}"[:1000]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    PortalSettings.objects.filter(pk=s.pk).update(fe_backup_last_at=started, fe_backup_last_result=result)
    audit(None, "platform_backup", s, ok=result.startswith("ok"), result=result[:300])
    if not result.startswith("ok"):  # u06: e-mail only on errors
        from django.utils.translation import gettext as _g

        appliance.notify_backup_failure(_g("Platform backups failed"), result)
    return result


def tick(log=print):
    s = _settings()
    if due(s):  # u07: one platform backup a day
        log("platform backups: " + run(s, log))
