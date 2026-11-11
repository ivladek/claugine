"""OpenNebula ACL rules: the string form ("@105 VM+NET/@105 USE+MANAGE #0") to the hex values of one.acl.addrule."""

USER_ID, USER_GROUP, USER_ALL, USER_CLUSTER = 0x100000000, 0x200000000, 0x400000000, 0x800000000

RESOURCES = {
    "VM": 0x1000000000, "HOST": 0x2000000000, "NET": 0x4000000000, "IMAGE": 0x8000000000,
    "USER": 0x10000000000, "TEMPLATE": 0x20000000000, "GROUP": 0x40000000000, "DATASTORE": 0x100000000000,
    "CLUSTER": 0x200000000000, "DOCUMENT": 0x400000000000, "ZONE": 0x800000000000, "SECGROUP": 0x1000000000000,
    "VDC": 0x2000000000000, "VROUTER": 0x4000000000000, "MARKETPLACE": 0x8000000000000,
    "MARKETPLACEAPP": 0x10000000000000, "VMGROUP": 0x20000000000000, "VNTEMPLATE": 0x40000000000000,
    "BACKUPJOB": 0x100000000000000,
}
RIGHTS = {"USE": 0x1, "MANAGE": 0x2, "ADMIN": 0x4, "CREATE": 0x8}


def _subject(text):
    if text == "*":
        return USER_ALL
    prefix, num = text[0], int(text[1:])
    return {"#": USER_ID, "@": USER_GROUP, "%": USER_CLUSTER}[prefix] + num


def encode(rule):
    """'@1 VM+NET/@1 USE #0' -> (user, resource, rights, zone) as hex strings for one.acl.addrule."""
    parts = rule.split()
    if len(parts) not in (3, 4):
        raise ValueError(f"bad ACL rule: {rule}")
    user, res, rights = parts[:3]
    zone = parts[3] if len(parts) == 4 else "*"
    types, _sep, obj = res.partition("/")
    resource = sum(RESOURCES[t] for t in types.split("+")) + _subject(obj)
    return tuple(f"{v:x}" for v in (_subject(user), resource, sum(RIGHTS[r] for r in rights.split("+")), _subject(zone)))
