"""Credentials become secrets by type (Infrastructure → Secrets: users, BGP, OpenNebula, keys) and every reference
to them is rewritten as secret:<kind>:<name>[#slot].

    password / ipmi / other           user <key>                     (username, password)
    <x>.key (ssh private key)         user <x> (its private key)     claugine.service.key -> user claugine.service
    claugine.source.ssh               user claugine.source.ssh       (key, known_hosts)
    <fe>.oneadmin / .onedb /          opennebula <fe>                (OpenNebula oneadmin, database, OS user oneadmin
      .service_user.oneadmin(.key)                                     password and private key)
    <family>.network.<net>.<site>.<g> bgp <site>.<net>               (rrs; igw = hosts / egw / cgw; egw = ext)
    cloud.admins.jumps                dropped (the jump hosts are in the sites)

The credentials table stays as it was (not read any more)."""
from django.db import migrations
from django.utils import timezone

SLOT = {"rrs": "rrs", "ext": "egw"}  # every other group (hosts, egw, cgw) is the igw slot


def _public(private):
    try:
        from cryptography.hazmat.primitives import serialization
        k = serialization.load_ssh_private_key(private.encode(), password=None)
        return k.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
    except Exception:  # noqa: BLE001
        return ""


def forward(apps, schema_editor):
    from inventory.crypto import decrypt
    Credential = apps.get_model("inventory", "Credential")
    Secret = apps.get_model("inventory", "Secret")
    Network = apps.get_model("inventory", "Network")
    Platform = apps.get_model("inventory", "Platform")
    Host = apps.get_model("inventory", "Host")
    Site = apps.get_model("inventory", "Site")
    PeerGroup = apps.get_model("inventory", "PeerGroup")
    now = timezone.now()

    def get(kind, name, **defaults):
        s, _c = Secret.objects.get_or_create(kind=kind, name=name, defaults={"created_at": now, **defaults})
        return s

    creds = list(Credential.objects.filter(retired_at__isnull=True).order_by("key"))
    fronts = {c.key[: -len(".oneadmin")] for c in creds if c.kind == "opennebula" and c.key.endswith(".oneadmin")}
    for c in creds:
        k, val = c.key, (c.secret_encrypted and c.secret_encrypted) or ""
        if c.kind == "jump":
            continue
        fe = next((f for f in fronts if k.startswith(f + ".")), None)
        if fe is not None:
            s = get("opennebula", fe, db_name="onedb", db_user="onedbuser")
            rest = k[len(fe) + 1:]
            if rest == "oneadmin":
                s.one_password_encrypted = val
            elif rest == "onedb":
                s.db_password_encrypted = val
                s.db_user = c.username or s.db_user
                s.db_name = (c.attrs or {}).get("db") or s.db_name
            elif rest == "service_user.oneadmin":
                s.os_password_encrypted = val
            elif rest == "service_user.oneadmin.key":
                s.os_private_key_encrypted = val
                s.public_key = _public(decrypt(val)) if val else ""
            else:
                continue
            s.save()
            continue
        if c.kind == "bgp":
            parts = k.split(".")
            if len(parts) >= 5 and parts[1] == "network":
                net, site, group = parts[2], parts[3], parts[4]
                n = Network.objects.filter(site=site, name=net).first()
                s = get("bgp", f"{site}.{net}", site=site, network=net, net_type=(n.kind if n else "ix"))
                setattr(s, f"{SLOT.get(group, 'igw')}_encrypted", val)
                s.save()
            continue
        if c.kind == "ssh_key":
            name = k[: -len(".key")] if k.endswith(".key") else k
            s = get("user", name, username=c.username or "")
            s.private_key_encrypted = val
            s.public_key = c.public or (_public(decrypt(val)) if val else "")
            if (c.attrs or {}).get("known_hosts"):
                s.extra = {**(s.extra or {}), "known_hosts": c.attrs["known_hosts"]}
            s.username = s.username or c.username or ""
            s.purpose = s.purpose or c.description or c.scope or ""
            s.save()
            continue
        s = get("user", k, username=c.username or "")
        s.password_encrypted = val
        s.username = c.username or s.username
        s.purpose = s.purpose or c.description or c.scope or ""
        s.save()

    names = {(s.kind, s.name) for s in Secret.objects.all()}

    def canon(ref, kind):
        """A legacy reference (secrets.<provider>.<path>) to secret:<kind>:<name>; unchanged when nothing matches."""
        if not ref or not ref.startswith("secrets."):
            return ref
        parts = ref.split(".")[1:]
        for path in (".".join(parts), ".".join(parts[1:])):
            if (kind, path) in names:
                return f"secret:{kind}:{path}"
            if kind == "user":  # an accounts group (…admins.sa) -> its one user
                users = sorted(n for k2, n in names if k2 == "user" and n.startswith(path + "."))
                if users:
                    return f"secret:user:{users[0]}"
        return ref

    for p in Platform.objects.all():
        p.fe_secrets, p.sa = canon(p.fe_secrets, "opennebula"), canon(p.sa, "user")
        p.save(update_fields=["fe_secrets", "sa"])
    for h in Host.objects.exclude(ipmi_user=""):
        h.ipmi_user = canon(h.ipmi_user, "user")
        h.save(update_fields=["ipmi_user"])
    for s in Site.objects.all():
        j, i = dict(s.jump or {}), dict(s.ipmi or {})
        if j.get("account"):
            j["account"] = canon(j["account"], "user")
        if i.get("user"):
            i["user"] = canon(i["user"], "user")
        s.jump, s.ipmi = j, i
        s.save(update_fields=["jump", "ipmi"])
    for g in PeerGroup.objects.select_related("network"):
        n = g.network
        name = f"{n.site}.{n.name}"
        if ("bgp", name) in names:
            g.password = f"secret:bgp:{name}#{SLOT.get(g.name, 'igw')}"
            g.save(update_fields=["password"])


class Migration(migrations.Migration):
    dependencies = [("inventory", "0032_secrets_by_type")]

    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
