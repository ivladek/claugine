"""Secrets of the infrastructure, read from the database only (Infrastructure → Secrets; inventory.models.Secret).

The portal is restored from a database backup, the distribution and the encryption key (portal.env).

References (what contributors keep: platforms, hosts, sites, BGP peer groups, settings) go by the secret's id, so
a secret can be renamed freely:

    secret:12             a user (user name, password and/or ssh private key) or an OpenNebula front end
    secret:7#rrs          a BGP password of a network (slot rrs, igw or egw)

Older references by name (``secret:user:cloud.admins.sa.ops``, ``secrets.provider.cloud.one``, ...) still resolve
(migrations rewrite them to ids). Everything that needs a secret asks here:

    value(ref, field)   the secret (OpenNebula fields: oneadmin, service_user.oneadmin, service_user.oneadmin.key, onedb;
                        users: password (default) or key)
    account(ref)        (user name, {"password", "key"}) of a user
    contributors()      {secret id: [what uses it]}

Values never go to logs, messages or the audit log.
"""
import re

from .models import Secret

ONE_FIELDS = {"oneadmin": "one_password", "service_user.oneadmin": "os_password",
              "service_user.oneadmin.key": "os_private_key", "onedb": "db_password"}
SLOT_OF_GROUP = {"rrs": "rrs", "ext": "egw"}  # every other peer group (hosts, egw, cgw) uses the igw password
SERVICE = "claugine.service"           # the portal's own ssh key: reads the configuration of tenant routers
ID_RE = re.compile(r"^secret:(\d+)(?:#(rrs|igw|egw))?$")
CANON_RE = re.compile(r"^secret:(user|bgp|opennebula|key):([^#\s]+)(?:#(rrs|igw|egw))?$")


def _paths(ref):
    parts = ref.split(".")
    if parts and parts[0] == "secrets":
        parts = parts[1:]
    return [p for p in (".".join(parts), ".".join(parts[1:])) if p]


def _live(qs):
    return qs.exclude(state=Secret.State.ARCHIVED)


def parse(ref, field=None):
    """(Secret or None, what to read: a field name or a BGP slot) of a reference."""
    if not isinstance(ref, str) or not ref:
        return None, None
    m = ID_RE.match(ref.strip())
    if m:
        s = _live(Secret.objects.filter(pk=int(m.group(1)))).first()
        if s is None:
            return None, None
        if s.kind == Secret.Kind.BGP:
            return s, m.group(2) or "igw"
        if s.kind == Secret.Kind.ONE:
            return s, ONE_FIELDS.get(field or "oneadmin", "one_password")
        if s.kind == Secret.Kind.S3:
            return s, ("s3_key" if field == "key" else "s3_secret")
        return s, ("private_key" if field == "key" else "password")
    m = CANON_RE.match(ref.strip())
    if m:
        s = _live(Secret.objects.filter(kind=m.group(1), name=m.group(2))).first()
        if s is None:
            return None, None
        if s.kind == Secret.Kind.BGP:
            return s, m.group(3) or "igw"
        if s.kind == Secret.Kind.ONE:
            return s, ONE_FIELDS.get(field or "oneadmin", "one_password")
        return s, ("private_key" if field == "key" else "password")
    # older references, by name
    if field in ONE_FIELDS:
        s = _live(Secret.objects.filter(kind=Secret.Kind.ONE, name__in=_paths(ref))).first()
        return (s, ONE_FIELDS[field]) if s else (None, None)
    for path in _paths(ref):
        parts = path.split(".")
        if len(parts) >= 4 and parts[1] == "network":   # <family>.network.<net>.<site>[.<group>]
            net, site = parts[2], parts[3]
            group = parts[4] if len(parts) > 4 else (field or "")
            s = _live(Secret.objects.filter(kind=Secret.Kind.BGP, site=site, network=net)).first()
            if s:
                return s, SLOT_OF_GROUP.get(group, "igw")
    for path in _paths(ref):
        s = _live(Secret.objects.filter(kind=Secret.Kind.ONE, name=path)).first()
        if s and field in (None, "oneadmin"):
            return s, "one_password"
        s = _live(Secret.objects.filter(kind=Secret.Kind.USER, name=path)).first()
        if s:
            return s, ("private_key" if field == "key" else "password")
        s = _live(Secret.objects.filter(kind=Secret.Kind.USER, name__startswith=path + ".")).order_by("name").first()
        if s:  # an accounts group (…admins.sa) -> its user
            return s, ("private_key" if field == "key" else "password")
    return None, None


def find(ref, field=None):
    return parse(ref, field)[0]


def value(ref, field=None):
    """The secret behind a reference, or None."""
    s, what = parse(ref, field)
    if s is None or what is None:
        return None
    return s.get(what) or None


def has(ref, field=None):
    s, what = parse(ref, field)
    return bool(s is not None and what and s.has(what))


def account(ref):
    """(user name, {"password", "key"}) of a user secret, or (None, {})."""
    s = find(ref)
    if s is None or s.kind != Secret.Kind.USER:
        return None, {}
    acc = {}
    if s.has("password"):
        acc["password"] = s.get("password")
    if s.has("private_key"):
        acc["key"] = s.get("private_key")
    elif s.public_key:  # only the public key is known (VMs get public keys only)
        acc["key"] = s.public_key
    if not acc:
        return None, {}
    return s.username or s.name.rsplit(".", 1)[-1], acc


def candidates(ref, field=None):
    """Names a reference may stand for (kept for callers that list them)."""
    parts = _paths(ref) if isinstance(ref, str) else []
    return [p + (f".{field}" if field else "") for p in parts]


def _public_key(private):
    try:
        from cryptography.hazmat.primitives import serialization
        key = serialization.load_ssh_private_key(private.encode(), password=None)
        return key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
    except Exception:  # noqa: BLE001 - a key we cannot read keeps no public part
        return ""


# --- contributors: what uses a secret ---------------------------------------------------------------------------

def references():
    """[(reference, label, (model, pk, field path))] of every contributor in the records."""
    from .models import Host, PeerGroup, Platform, Site
    out = []
    for p in Platform.objects.all():
        if p.fe_secrets:
            out.append((p.fe_secrets, f"platform {p.key}: front end", ("platform", p.pk, "fe_secrets")))
        if p.sa:
            out.append((p.sa, f"platform {p.key}: super admins", ("platform", p.pk, "sa")))
    for h in Host.objects.exclude(ipmi_user="").select_related("site"):
        out.append((h.ipmi_user, f"host {h.site.code}.{h.name}: IPMI", ("host", h.pk, "ipmi_user")))
    for s in Site.objects.all():
        if (s.ipmi or {}).get("user"):
            out.append((s.ipmi["user"], f"site {s.code}: IPMI", ("site", s.pk, "ipmi.user")))
    for g in PeerGroup.objects.select_related("network"):
        if g.password:
            out.append((g.password, f"network {g.network}: BGP {g.name}", ("peergroup", g.pk, "password")))
    from tenants.models import PortalSettings
    ps = PortalSettings.get()
    if ps.service_secret:  # made on first use
        out.append((ps.service_secret, "portal: reads tenant router configurations", ("settings", ps.pk, "service_secret")))
    return out


def contributors():
    """{secret id: [labels]} of every secret something uses."""
    used = {}
    cache = {}
    for ref, label, _where in references():
        key = (ref, None)
        if key not in cache:
            fld = "oneadmin" if label.endswith("front end") else None
            cache[key] = find(ref, fld)
        s = cache[key]
        if s is not None:
            used.setdefault(s.pk, []).append(label)
    return {k: sorted(set(v)) for k, v in used.items()}


def unresolved():
    """[(reference, label)] that point to no secret."""
    out = []
    for ref, label, _where in references():
        if find(ref, "oneadmin" if label.endswith("front end") else None) is None:
            out.append((ref, label))
    return out


def status_of(secret, used=None):
    """new (not used), used, replaced, archived."""
    if secret.state != Secret.State.ACTIVE:
        return secret.state
    used = contributors() if used is None else used
    return "used" if used.get(secret.pk) else "new"


def repoint(old, new, user=None):
    """Every contributor of `old` refers to `new` (same type; BGP slots kept). Returns the labels changed."""
    from accounts.models import audit
    from .models import Host, PeerGroup, Platform, Site
    from tenants.models import PortalSettings
    done = []
    for ref, label, where in references():
        if where is None:
            continue
        s, slot = parse(ref, "oneadmin" if label.endswith("front end") else None)
        if s is None or s.pk != old.pk:
            continue
        new_ref = new.ref + (f"#{slot}" if new.kind == Secret.Kind.BGP else "")
        model, pk, path = where
        if model == "platform":
            Platform.objects.filter(pk=pk).update(**{path: new_ref})
        elif model == "host":
            Host.objects.filter(pk=pk).update(ipmi_user=new_ref)
        elif model == "peergroup":
            PeerGroup.objects.filter(pk=pk).update(password=new_ref)
        elif model == "settings":
            PortalSettings.objects.filter(pk=pk).update(**{path: new_ref})
        elif model == "site":
            site = Site.objects.get(pk=pk)
            top, key = path.split(".")
            d = dict(getattr(site, top) or {})
            d[key] = new_ref
            setattr(site, top, d)
            site.save(update_fields=[top])
        done.append(label)
    if done:
        audit(user, "secret_replaced", old, now=new.name, kind=old.kind, contributors=len(done))
    return done


# --- the portal's own keys ---------------------------------------------------------------------------------------

def service_key():
    """The portal's own ssh key (a user secret, Settings → service_secret), made on first use: it reads the
    configuration of tenant routers."""
    from tenants.models import PortalSettings
    ps = PortalSettings.get()
    s = find(ps.service_secret) if ps.service_secret else None
    if s is not None and s.kind == Secret.Kind.USER and s.has("private_key"):
        return s
    s = None
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    k = Ed25519PrivateKey.generate()
    private = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                              serialization.NoEncryption()).decode()
    public = k.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
    name, n = SERVICE, 1
    while Secret.objects.filter(kind=Secret.Kind.USER, name=name).exists():
        n += 1
        name = f"{SERVICE}-{n}"
    s = Secret(kind=Secret.Kind.USER, name=name)
    s.username, s.public_key = "claugine", public + " claugine-portal"
    s.purpose = "the portal's service user on tenant routers (made by the portal)"
    s.put("private_key", private)
    s.save()
    PortalSettings.objects.filter(pk=ps.pk).update(service_secret=s.ref)
    return s


def describe(ref):
    """What a reference points to, for people: "users: cloud.admins.sa.ops", "BGP: dc2.cloud-ix (rrs)"."""
    s, slot = parse(ref)
    if s is None:
        return f"{ref} (missing)" if ref else "—"
    # a BGP secret as a whole (a service's secret) has no slot; one of its passwords (a peer group's) has
    return f"{s.get_kind_display()}: {s.name}" + (f" ({slot})" if s.kind == Secret.Kind.BGP and "#" in ref else "")


# --- references chosen from a list --------------------------------------------------------------------------

REF_PURPOSES = {"opennebula": Secret.Kind.ONE, "account": Secret.Kind.USER, "ipmi": Secret.Kind.USER,
                "bgp": Secret.Kind.BGP, "password": Secret.Kind.PASSWORD, "s3": Secret.Kind.S3}


def ref_choices(purpose):
    """[(reference, label)] of the secrets a field of this purpose may refer to (archived and replaced ones not)."""
    kind = REF_PURPOSES[purpose]
    out = []
    for s in Secret.objects.filter(kind=kind, state=Secret.State.ACTIVE).order_by("name"):
        if purpose == "account" and not s.has("private_key"):
            continue
        if purpose == "ipmi" and not s.has("password"):
            continue
        out.append((s.ref, s.name + (f" ({s.username})" if s.username else "")))
    return out


def norm_ref(value, choices):
    """The option a stored reference stands for (an older reference -> the secret it resolves to)."""
    values = [c[0] for c in choices]
    if not value or value in values:
        return value
    s = find(value)
    return s.ref if s is not None and s.ref in values else value


def ref_choice_field(label, purpose, required=False, help_text=""):
    """A dropdown of the secrets of `purpose` (by id)."""
    from django import forms as djforms
    return djforms.ChoiceField(label=label, required=required, help_text=help_text,
                               choices=lambda: [("", "—")] + ref_choices(purpose))


def ref_field(form, name, purpose):
    """Make form field `name` a dropdown of the secrets of `purpose`; a stored value not in the list stays offered."""
    from django import forms as djforms
    old = form.fields[name]
    choices = ref_choices(purpose)
    cur = form.initial.get(name) or (form.data.get(form.add_prefix(name)) if form.is_bound else "")
    cur = norm_ref(cur, choices)
    if cur and cur not in [c[0] for c in choices]:
        choices.append((cur, f"{cur} (?)"))
    form.fields[name] = djforms.ChoiceField(label=old.label, required=old.required, help_text=old.help_text,
                                            choices=[("", "—")] + choices, initial=cur)
    if name in form.initial:
        form.initial[name] = cur


# --- import of a secrets directory (a data set's; not offered in the portal now) ----------------------------------

def load_from_secrets(user=None):
    """Make secrets from the secrets directory (INVENTORY_SECRETS_DIR): users, OpenNebula front ends, BGP. Returns
    (created, updated). Used by tests and by data sets that bring their secrets as files."""
    from .secrets import _tree
    tree = _tree()
    created = updated = 0

    def get(kind, name, **defaults):
        nonlocal created, updated
        s = Secret.objects.filter(kind=kind, name=name).first()
        if s is None:
            created += 1
            return Secret(kind=kind, name=name, **defaults)
        updated += 1
        return s

    def walk(node, path):
        if not isinstance(node, dict):
            return
        p = ".".join(path)
        if "oneadmin" in node and isinstance(node.get("oneadmin"), str):  # an OpenNebula front end
            s = get(Secret.Kind.ONE, p, db_name="onedb", db_user="onedbuser")
            s.put("one_password", node["oneadmin"])
            db = node.get("onedb")
            if isinstance(db, dict):
                s.db_name = str(db.get("db") or s.db_name)
                s.db_user = str(db.get("user") or s.db_user)
                if db.get("password"):
                    s.put("db_password", str(db["password"]))
            su = (node.get("service_user") or {}).get("oneadmin") if isinstance(node.get("service_user"), dict) else None
            if isinstance(su, dict):
                if su.get("password"):
                    s.put("os_password", str(su["password"]))
                if su.get("key"):
                    s.put("os_private_key", str(su["key"]))
            s.save()
            return
        if len(path) >= 2 and path[-1] == "network":  # <family>.network.<net>.<site>.<group>
            for net, sites in node.items():
                for site, groups in (sites or {}).items() if isinstance(sites, dict) else ():
                    if not isinstance(groups, dict):
                        continue
                    from .models import Network
                    n = Network.objects.filter(site=site, name=net).first()
                    s = get(Secret.Kind.BGP, f"{site}.{net}", site=site, network=net, net_type=n.kind if n else "ix")
                    for g, v in groups.items():
                        if isinstance(v, str) and v:
                            s.put(SLOT_OF_GROUP.get(g, "igw"), v)
                    s.save()
            return
        if isinstance(node.get("password"), str) or isinstance(node.get("key"), str):  # one user
            s = get(Secret.Kind.USER, p, username=str(node.get("user") or path[-1]))
            if isinstance(node.get("password"), str):
                s.put("password", node["password"])
            k = node.get("key")
            if isinstance(k, str) and "PRIVATE KEY" in k:
                s.put("private_key", k)
                s.public_key = _public_key(k)
            elif isinstance(k, str) and k.strip().startswith(("ssh-", "ecdsa-")):
                s.public_key = k.strip()
            s.save()
            return
        for k, v in node.items():
            walk(v, path + [str(k)])

    walk(tree, [])
    return created, updated
