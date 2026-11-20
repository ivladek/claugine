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
(one.zone.raftstatus). The portal logs in with the SA secret (owner 2026-11-18: the platform's, or the account secret
Settings → Backups names, login()) and reads the folders with sudo, the database as oneadmin through sudo -u; it reads
only, and removes on the node only its own temporary dump. Old local backups are
removed (keep days) only after a run without errors; the S3 bucket keeps its own (its lifecycle rules).
Run by the appliance timer on the active node (as root), daily at the set time, or at once with "Back up now".
"""

import os
import secrets
import shlex
import shutil
import socket
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import timedelta

from django.utils import timezone

FOLDER = ""  # owner 2026-11-08: straight into the configured folder, beside the portal's backups
PREFIX = "platform_"  # owner 2026-11-08: each backup is platform_<name>_<vip>_YYYYmmdd-HHMM


def _settings():
    from tenants.models import PortalSettings

    return PortalSettings.get()


def platforms(keys=None):
    """The platforms that can be backed up (a front-end VIP; the login is login()'s, owner 2026-11-18);
    u06: narrowed to the chosen keys when `keys` is given."""
    from inventory.models import Platform

    out = [p for p in Platform.objects.exclude(fe_vip="").select_related("site").order_by("key")]
    return [p for p in out if p.key in keys] if keys else out


def dirs(s):
    return s.fe_backup_dir_list()


#: commit-062, commit-084: a platforms' run claim older than this was abandoned (its process died): taken again
RUN_STALE = timedelta(hours=6)


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
    if s.fe_backup_run_id and s.fe_backup_run_since and s.fe_backup_run_since < now - RUN_STALE:
        # commit-084: a claim abandoned (its node died) leaves its slot to be run again, past the usual 12 hours too
        return last is None or last < slot
    return (last is None or last < slot) and now - slot < timedelta(hours=12)


def _nodes(p):
    """[(server id, name, address, role)] of the platform's zone; the node the database is dumped on first (role
    "leader").

    core-019 (sixth full review): the leader was taken from the configured endpoint's own Raft state only, so a front
    end without HA whose zone lists its server (state solo), or an endpoint that is a follower, gave no leader - and a
    backup with no database dump. Each node's own state is read (adoption.collector.frontends): the Raft leader dumps;
    with no leader, a single node (solo) does; anything else has no node to dump on (backup_platform refuses it)."""
    from adoption.collector import client_for
    from inventory.models import PlatformControl

    c = PlatformControl.objects.filter(key=p.key).first()
    if c is None:
        raise RuntimeError("no endpoint (Infrastructure → the platform)")
    cl = client_for(c)
    cfg = ET.fromstring(cl._call("one.system.config"))
    zone = int(cfg.findtext("FEDERATION/ZONE_ID") or 0)
    mode = (cfg.findtext("FEDERATION/MODE") or "STANDALONE").upper()
    fes = cl.frontends()
    leaders = [f for f in fes if f["state"] == "leader"]
    if not leaders and len(fes) == 1 and fes[0]["state"] in ("solo", "leader"):
        leaders = fes  # a front end without HA: the one node
    out = []
    for f in fes:
        addr = f["addr"] or (p.fe_vip if len(fes) == 1 else "")
        name = f["name"] or p.fe_name or p.key
        out.append((f["id"] or "0", name, addr, "leader" if f in leaders[:1] else "follower"))
    out.sort(key=lambda n: n[3] != "leader")
    return zone, mode, out


def _why(exc, user):
    """An ssh step's error; sudo asking for a password is said plainly (the SA user needs it without one)."""
    text = str(exc)
    if "password is required" in text or "a terminal is required" in text:
        return f"{user} cannot use sudo without a password there"
    return text


def _readable_tar(path):
    """A gzip tar archive that reads to its end (commit-078): the gzip stream read through to its end first, which
    checks its trailer (CRC and length; commit-081: the tar's end marker can come before it), then the tar's headers."""
    import gzip
    import tarfile
    import zlib

    try:
        with gzip.open(path, "rb") as gz:
            while gz.read(1 << 20):
                pass
        with tarfile.open(path, "r:gz") as t:
            for _m in t:
                pass
        return True
    except (tarfile.TarError, OSError, EOFError, zlib.error):  # commit-085: damaged compressed data too
        return False


def _node_backup(p, node, user, key, content, folders, log):
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
        # owner 2026-11-18: the SA user, with sudo - the folders hold files only root reads
        warn = fetch(
            addr,
            22,
            user,
            key,
            f"sudo -n tar -czf - --ignore-failed-read {quoted} 2>/tmp/.claugine-tar-$$; rc=$?; "
            f"grep -v 'Removing leading' /tmp/.claugine-tar-$$ >&2; rm -f /tmp/.claugine-tar-$$; [ $rc -le 1 ]",
            tgz,
        )
        # commit-078: sudo refusing tar exits 1 too, which the command accepts (tar's own warnings): the output counts
        # only when it is a gzip tar archive that reads to its end
        if not _readable_tar(tgz):
            raise SshError(_why(warn or "no tar archive came back", user))
        parts.append("config.tar.gz")
        # core-036: a file tar could not read, or one that changed or vanished while it was read, leaves the
        # archive incomplete; the archive is kept, but the backup is an error, so the run fails and nothing older
        # is pruned. The leading-slash notice is filtered out on the node.
        missed = [ln.strip() for ln in (warn or "").splitlines() if ln.strip() and "Removing leading" not in ln]
        if missed:
            log(f"{p.key} {name}: {missed[-1][:160]}")
            errors.append(f"{name} config: not complete - " + "; ".join(m[:160] for m in missed[:5]))
    except SshError as exc:
        errors.append(f"{name} config: {_why(exc, user)}")
    if role == "leader":
        kinds = [("zone", "")] + ([("federated", " --federated")] if p.fe_mode == "primary" else [])
        for kind, opt in kinds:
            out = os.path.join(out_dir, f"onedb-{kind}.sql.gz")
            t = f"/tmp/claugine-onedb-{secrets.token_hex(6)}.sql"
            try:
                # the database as oneadmin (its credentials in oned.conf), through the SA user's sudo
                inner = f"onedb backup {t}{opt} --force >/dev/null && gzip -c {t}; rc=$?; rm -f {t}; exit $rc"
                fetch(addr, 22, user, key, f"sudo -n -u oneadmin sh -c {shlex.quote(inner)}", out, timeout=3600)
                parts.append(f"onedb-{kind}.sql.gz")
            except SshError as exc:
                errors.append(f"{name} onedb {kind}: {_why(exc, user)}")
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


def login(p, s):
    """(user, private key) the backup logs in to p's front ends with (owner 2026-11-18 09:55: "now we use sa secret to
    connect to all hosts and vms"): the account secret Settings → Backups names (fe_backup_login), else the platform's
    SA secret, else its site's (inventory.sshexec.default_account). ("", "") when none has a private key."""
    from inventory import credentials as cr
    from inventory.sshexec import default_account

    ref = s.fe_backup_login or p.sa or default_account(p.site.code)
    sec = cr.find(ref) if ref else None
    if sec is None or sec.kind != cr.Secret.Kind.USER or not sec.has("private_key"):
        return "", ""
    return sec.username or sec.name.rsplit(".", 1)[-1], sec.get("private_key")


def backup_platform(p, folder, s, log=print, taken=None):
    """One platform's backup in `folder` (owner 2026-11-12: one archive and one script per backup,
    accounts/backup_archive.py): <name>/<name>.7z holding a folder per node, and <name>/<name>.sh, which checks and
    unpacks it; no MANIFEST or README (owner 2026-11-12), the script's header says what the backup is. Returns
    (path, errors)."""
    from . import backup_archive

    user, key = login(p, s)
    if not key:
        return "", [
            f"{p.key}: no login with an ssh key to its front ends - its SA secret, or Settings → Backups → "
            "Connect to Front-Ends using"
        ]
    password = s.get_secret("fe_backup_password") or s.get_secret("backup_password")  # u06: the platforms' own password
    if not password:
        return "", [f"{p.key}: no archive password (Settings → Backups)"]
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M")
    name = f"{PREFIX}{p.name or p.key}_{p.fe_vip}_{stamp}"  # platform_<name>_<vip>_YYYYmmdd-HHMM
    # commit-062: a name the folder or the bucket (`taken`, S3-only runs) has already gets its seconds
    if os.path.exists(os.path.join(folder, name)) or (taken is not None and taken(name)):
        name += timezone.localtime().strftime("%S")
    final = os.path.join(folder, name)
    work = os.path.join(folder, "." + name + ".partial")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, mode=0o700)
    content = os.path.join(work, "content")
    os.makedirs(content, mode=0o700)
    errors, nodes_done = [], []
    try:
        zone, mode, nodes = _nodes(p)
        if not any(n[3] == "leader" for n in nodes):  # core-019: a backup without the database is no backup
            states = ", ".join(f"{n[1]} {n[2]}" for n in nodes) or "none listed"
            raise RuntimeError(f"no leader among the front-end nodes ({states}): the database cannot be dumped")
        for node in nodes:
            done, errs = _node_backup(p, node, user, key, content, dirs(s), log)
            errors += errs
            if done:
                nodes_done.append(done)
        if not nodes_done:
            raise RuntimeError("nothing was read from the front-end nodes")
        if not any(os.path.exists(os.path.join(content, d, "onedb-zone.sql.gz")) for d in nodes_done):
            raise RuntimeError("the database was not dumped (see the errors): this backup is not kept")
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
        names = [x.rstrip("/") for x in s3.list_objects(conf, prefix=PREFIX) if x.startswith(PREFIX)]  # commit-063
        # commit-076: by the stamp each name ends with, YYYYmmdd-HHMM with its seconds when two shared a minute
        from .appliance import backup_stamp

        return sorted(names, key=backup_stamp, reverse=True)[:n]
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
    """One run of the platforms' backups at a time on this node (commit-062, appliance.run_lock): see _run."""
    from . import appliance

    with appliance.run_lock("platform-backup"):
        # owner 2026-11-18 (option A): not while the maintenance gate is closed; the gate counts this run once it holds
        # its lock (appliance.backups_running), so a switch over or an update waits for it
        held = appliance.gate_held()
        if held:
            log(f"platform backups: {held} is running; they wait until it ends")
            return f"skipped: {held} is running; the backups wait until it ends"
        return _run(s, log)


def _run(s=None, log=print):
    """Every chosen platform now (as root, the active node). u07: one run a day. If the local switch is on, the
    backups are written under <local folder>/claugine-platforms/; if S3 is on, each is copied to the bucket (one dir
    per backup, no root folder) — to the portal's bucket when 'use the Portal's S3 settings' is on, otherwise the
    platforms' own S3. If local is off, the backups are staged in a temp folder and only uploaded. Kept in Settings."""
    from tenants.models import PortalSettings

    s = s or _settings()
    do_local = bool(s.fe_backup_local and (s.fe_backup_path or "").strip())
    # owner 2026-11-08 (editions): the platforms' backups have their own S3 settings, so the s3_backups feature has
    # to be checked here too - otherwise "no S3 backups" would hold for the portal's own backups and not for these.
    from accounts import edition

    s3 = edition.ee("s3")  # S3 is the Enterprise edition's (ee/s3.py)
    do_s3 = bool(s.fe_backup_s3 and edition.allows("s3_backups", s) and s3 is not None)
    started = timezone.now()
    # commit-062, commit-084: the run is claimed in the database before it starts - a claim of its own (run id, node,
    # since), apart from the last finished run. Scheduled runs and "Back up now" both need it: while another node's run
    # holds it (a switchover during a run) this one does not start, and a request made meanwhile stays queued for
    # after. Only the run that holds the claim ends it; one abandoned (its process died) expires after RUN_STALE and
    # the slot is run again.
    run_id = uuid.uuid4().hex
    from django.db.models import Q

    # commit-086: also only if the last finished run is still the one this node read when it found the run due - a
    # timer that decided before another run ended does not run it again; commit-084: the request is kept with the
    # claim and cleared only when the run records its end
    if (
        not PortalSettings.objects.filter(pk=s.pk, fe_backup_last_at=s.fe_backup_last_at)
        .filter(Q(fe_backup_run_id="") | Q(fe_backup_run_since__lt=started - RUN_STALE))
        .update(
            fe_backup_run_id=run_id,
            fe_backup_run_node=socket.gethostname()[:120],
            fe_backup_run_since=started,
        )
    ):
        run_now, held = (
            PortalSettings.objects.filter(pk=s.pk).values_list("fe_backup_run_id", "fe_backup_run_node").first()
        )
        why = f"a run is in progress on {held or '?'}" if run_now else "the run was made meanwhile"
        log(f"platform backups: {why}")
        return f"skipped: {why}"
    try:
        return _claimed_run(s, log, started, run_id, do_local, do_s3, s3)
    finally:
        # the claim ends with the run, however it ends - only this run's own
        PortalSettings.objects.filter(pk=s.pk, fe_backup_run_id=run_id).update(
            fe_backup_run_id="", fe_backup_run_node="", fe_backup_run_since=None
        )


def _claimed_run(s, log, started, run_id, do_local, do_s3, s3):
    from accounts.models import audit
    from tenants.models import PortalSettings

    from . import appliance

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
        chosen = platforms(s.fe_backup_platforms or None)
        # core-042 (twentieth full review): a chosen platform that is not there to back up (renamed before the rename
        # carried the choice over, deleted, or without its VIP) is an error of the run - it was
        # left out silently, the run was "ok" and pruned its older backups
        found = {p.key for p in chosen}
        for key in s.fe_backup_platforms or []:
            if key not in found:
                errors.append(
                    f"{key}: chosen for backups, but no such platform can be backed up (renamed or deleted, "
                    "or no front-end VIP): choose the platforms again in Settings"
                )
        taken = None
        if s3conf and not do_local:  # commit-062/070: as the portal's, only for an S3-only run, best effort

            def taken(name):
                return s3.folder_exists(s3conf, name)

        for p in chosen:
            path, errs = backup_platform(p, folder, s, log, taken)
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
    # only the run that holds the claim records its end (one whose claim expired and was taken does not)
    # commit-088: one write - the result, and the request this run served (made before it started; one made later
    # stays for the next, commit-084) - so a run stopped in between cannot replay a request it served
    from django.db.models import Case, F, Q, When

    if not PortalSettings.objects.filter(pk=s.pk, fe_backup_run_id=run_id).update(
        fe_backup_last_at=started,
        fe_backup_last_result=result,
        fe_backup_requested_at=Case(
            When(Q(fe_backup_requested_at__lte=started), then=None), default=F("fe_backup_requested_at")
        ),
    ):
        log("platform backups: this run's claim was taken over meanwhile; its result is not recorded")
    audit(None, "platform_backup", s, ok=result.startswith("ok"), result=result[:300])
    if not result.startswith("ok"):  # u06: e-mail only on errors
        from django.utils.translation import gettext as _g

        appliance.notify_backup_failure(_g("Platform backups failed"), result)
    return result


def tick(log=print):
    s = _settings()
    if due(s):  # u07: one platform backup a day
        log("platform backups: " + run(s, log))
