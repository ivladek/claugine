"""Running commands on network endpoints over ssh: directly, and through the jump host of their site only when the
direct connection does not work.

Ports: hosts, VMs, the portal and the rest use the standard ssh port 22; the jump hosts, reachable from outside, and
the VyOS network functions (in their management VRF) use the ports of the site's settings (management ports).
Every address of a site is reachable from the portal directly now; the jump host (Infrastructure → Sites → site
settings: jump host, its ssh port and the account whose key logs in) stays as the way round:

    portal ──ssh──▶ endpoint                                     first
    portal ──ssh──▶ jump host of the site (its port) ──▶ endpoint  when the direct connection fails

Keys are written to a private temporary directory for the call only and never logged. A login with a password
(a tenant's own igw) goes through sshpass, which reads the password from its environment, never the command line.
"""

import os
import shutil
import subprocess
import tempfile

DEFAULT_JUMP_PORT = 22  # a site's jump host has its own port (Infrastructure → Sites)


def default_account(site):
    """When nothing names an account: the super admins of the site's platforms (by id, a shared platform first)."""
    from .models import Platform

    ps = sorted(Platform.objects.filter(site__code=site).exclude(sa=""), key=lambda p: (p.kind != "shared", p.key))
    return ps[0].sa if ps else ""


OPTS = [
    "-o",
    "BatchMode=yes",
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "UserKnownHostsFile=/dev/null",
    "-o",
    "ConnectTimeout=10",
    "-o",
    "LogLevel=ERROR",
    "-o",
    "IdentitiesOnly=yes",
    "-o",
    "ServerAliveInterval=10",
]


class SshError(Exception):
    """str(): one short line. .detail: the whole reason (commit-169), for a log - never longer than DETAIL."""

    detail = ""


DETAIL = 4000


def portal_site():
    from tenants.models import PortalSettings

    return (PortalSettings.get().portal_site or "").strip()


def jump_for(site):
    """No jump host: since 2026-10-27 every host of either site is reached directly from either site (the cross-site
    jump host was removed from the site settings)."""
    return None


def _write(d, name, key):
    path = os.path.join(d, name)
    with open(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600), "w") as f:
        f.write(key.strip() + "\n")
    return path


PASSWORD_OPTS = [
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "UserKnownHostsFile=/dev/null",
    "-o",
    "ConnectTimeout=10",
    "-o",
    "LogLevel=ERROR",
    "-o",
    "PubkeyAuthentication=no",
    "-o",
    "NumberOfPasswordPrompts=1",
    "-o",
    "PreferredAuthentications=password,keyboard-interactive",
    "-o",
    "ServerAliveInterval=10",
]


def command(host, port, user, keyfile, jump=None, jumpkey=None):
    if keyfile is None:  # password login
        cmd = ["sshpass", "-e", "ssh", "-p", str(port)] + PASSWORD_OPTS
    else:
        cmd = ["ssh", "-i", keyfile, "-p", str(port)] + OPTS
    if jump:
        proxy = (
            ["ssh", "-i", jumpkey, "-p", str(jump["port"])] + OPTS + ["-W", "%h:%p", f"{jump['user']}@{jump['host']}"]
        )
        cmd += ["-o", "ProxyCommand=" + " ".join(proxy)]
    return cmd + [f"{user}@{host}"]


def _once(host, port, user, private_key, remote, jump, stdin, timeout, password=None, stdout_reason=False):
    via = f" (via {jump['host']}:{jump['port']})" if jump else ""
    d = tempfile.mkdtemp(prefix="ssh-")
    try:
        key = _write(d, "key", private_key) if private_key else None
        jkey = _write(d, "jump", jump["key"]) if jump else None
        cmd = command(host, port, user, key, jump, jkey) + [remote]
        env = {**os.environ, "SHELL": "/bin/sh"}  # ssh runs ProxyCommand with $SHELL; the portal's user has none
        if password:
            env["SSHPASS"] = password
        try:
            r = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired as exc:
            raise SshError(f"{host}:{port}{via}: no answer in {timeout} s") from exc
        if r.returncode != 0:
            lines = [x for x in r.stderr.strip().splitlines() if not x.startswith("Warning: Permanently added")]
            if password and r.returncode == 5 and not lines:
                lines = ["the password was refused"]  # sshpass: wrong password
            # commit-166/168: a script's reason printed on stdout - only for a caller that asks (its own scripts),
            # never for a read whose output may hold secrets (a router's configuration)
            if stdout_reason and not lines and r.returncode != 255:
                lines = [x for x in (r.stdout or "").strip().splitlines() if x.strip()][-1:]
            last = (lines or [f"ssh exit {r.returncode}"])[-1]
            err = SshError(f"{host}:{port}{via}: " + last[:300])
            err.detail = f"{host}:{port}{via}: " + "\n".join(lines or [last])[-DETAIL:]  # commit-169
            err.connect = r.returncode == 255 or (
                bool(password) and r.returncode in (5, 6)
            )  # ssh failed, not the command
            raise err
        return r.stdout
    finally:
        shutil.rmtree(d, ignore_errors=True)


def run(host, port, user, private_key, remote, site=None, stdin=None, timeout=60, password=None, stdout_reason=False):
    """stdout of `remote` run on host:port as user (key login, or `password` when no key): directly, else through
    the site's jump host."""
    if not user or not (private_key or password):
        raise SshError(f"{host}:{port}: no login (user and key or password)")
    if remote.startswith("vbash"):  # a configuration change: only from the active portal node
        from accounts import ha

        try:
            ha.forbid_writes(f"{host} configuration")
        except ha.ReadOnlyHere as exc:
            raise SshError(str(exc)) from exc
    if private_key:
        password = None
    try:
        return _once(host, port, user, private_key, remote, None, stdin, timeout, password, stdout_reason)
    except SshError as exc:
        if not getattr(exc, "connect", True):
            raise
        try:
            jump = jump_for(site)
        except SshError:
            jump = None
        if not jump:
            raise
        try:
            return _once(host, port, user, private_key, remote, jump, stdin, timeout, password, stdout_reason)
        except SshError as exc2:
            err = SshError(f"{exc}; {exc2}")
            err.connect = getattr(exc2, "connect", None)  # commit-254: how the attempt through the jump host ended
            # commit-170: both attempts' reasons - why the direct one failed, and why the jump host's did
            half = DETAIL // 2 - 40  # commit-170: each attempt its own half, so neither pushes the other out
            err.detail = (
                f"direct: {str(exc.detail or exc)[-half:]}\nvia {jump['host']}: {str(exc2.detail or exc2)[-half:]}"
            )
            raise err from exc2


def fetch(host, port, user, private_key, remote, out_path, timeout=1800):
    """u04: the binary stdout of `remote` (key login) into out_path; returns stderr (warnings). Raises SshError when
    the command fails."""
    d = tempfile.mkdtemp(prefix="ssh-")
    try:
        key = _write(d, "key", private_key)
        cmd = command(host, port, user, key) + [remote]
        env = {**os.environ, "SHELL": "/bin/sh"}
        with open(os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as fh:
            try:
                r = subprocess.run(cmd, stdout=fh, stderr=subprocess.PIPE, timeout=timeout, env=env)
            except subprocess.TimeoutExpired as exc:
                raise SshError(f"{host}:{port}: no answer in {timeout} s") from exc
        err = r.stderr.decode(errors="replace").strip()
        if r.returncode != 0:
            lines = [x for x in err.splitlines() if not x.startswith("Warning: Permanently added")]
            raise SshError(f"{host}:{port}: " + ((lines or [f"ssh exit {r.returncode}"])[-1])[:300])
        return err
    finally:
        shutil.rmtree(d, ignore_errors=True)


def push(host, port, user, private_key, remote, in_path, timeout=1800, password=None):
    """T34 B6: the file in_path as the stdin of `remote` (a key login, or a password one when no key); returns its
    stdout. Raises SshError when the command fails."""
    if not user or not (private_key or password):
        raise SshError(f"{host}:{port}: no login (user and key or password)")
    d = tempfile.mkdtemp(prefix="ssh-")
    try:
        key = _write(d, "key", private_key) if private_key else None
        cmd = command(host, port, user, key) + [remote]
        env = {**os.environ, "SHELL": "/bin/sh"}
        if key is None:
            env["SSHPASS"] = password
        with open(in_path, "rb") as fh:
            try:
                r = subprocess.run(cmd, stdin=fh, capture_output=True, timeout=timeout, env=env)
            except subprocess.TimeoutExpired as exc:
                raise SshError(f"{host}:{port}: no answer in {timeout} s") from exc
        if r.returncode != 0:
            err = r.stderr.decode(errors="replace").strip()
            lines = [x for x in err.splitlines() if not x.startswith("Warning: Permanently added")]
            if not lines:
                lines = [x for x in r.stdout.decode(errors="replace").strip().splitlines() if x.strip()][-1:]
            raise SshError(f"{host}:{port}: " + ((lines or [f"ssh exit {r.returncode}"])[-1])[:300])
        return r.stdout.decode(errors="replace")
    finally:
        shutil.rmtree(d, ignore_errors=True)
