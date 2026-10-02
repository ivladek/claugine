2026-10-02
New functions:
  acl_tenant_set
Changed functions
  acl_tenant_set

---
SITE_AT1_CLUSTERS="0"
SITE_AT1_CORPNET_PREFIXES=""
SITE_AT1_INET_MBPS="100"
SITE_AT1_INET_PREFIXES="31"
SITE_AT1_L2_VLANS=""

CONNECTION_TYPE = [inet|corpnet|no]
IGW_TYPE        = [vyos,vr,own]

QUOTA_AT1 =[
  CONNECTION_TYPE = "inet",
  IGW_TYPE = "vyos",
  INET_MBPS = "100",
  INET_MAIN_PREFIX = "31",
  INET_PUBLIC_PREFIXES = "29 28",
  L2_VLANS = "205",
  IMAGES_GB = "20",
  FILES_GB = "1",
  BACKUPS_GB = "0"
]
QUOTA_AT1_CA1 = [
  CPU = "20",
  RAM_GB = "100",
  DISK_GB = "1000"
]
QUOTA_AT1_GA1 = [
  CPU = "20",
  RAM_GB = "100",
  DISK_GB = "1000",
  GPU = "2"
]
QUOTA_ZK1 = [
  CONNECTION_TYPE = "corpnet",
  CORPNET_PREFIXES = "25 26",
  IMAGES_GB = "20",
  FILES_GB = "1",
  BACKUPS_GB = "0"
]
QUOTA_ZK1_CI1 = [
  CPU = "10",
  RAM_GB = "200",
  DISK_GB = "500"
]
