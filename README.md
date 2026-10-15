# claugine

**CL**oud **AU**tomation en**GINE** - a Bash toolkit for day-0/1/2 operations
on [OpenNebula](https://opennebula.io) platforms: one zone or a federation of
zones.

- front-end and host maintenance: backups, rolling OS updates, services
- tenants: ACLs and quotas
- EVPN/VXLAN networking
- service VMs, virtual networks, images
- Ubuntu and VyOS images from the latest ISOs

claugine is a set of shell functions. You `source` it once with a data set
describing your platforms, then call the functions - every command that works
on a platform names it from the inventory:

```bash
source bin/claugine                          # internal data and modules
data_provider_load inv=data/SAMPLE/inventory secrets=data/SAMPLE/secrets runtime=data/SAMPLE/runtime   # user data; again - switch
fe_cfg_ver_get platform=platforms.dc1.payload1         # read-only
quota_tenant_show platform=platforms.dc1.payload1 tenant=romashka
fe_services_restart platform=platforms.dc1.payload2    # asks for confirmation
```

Requirements: bash 4.3+, `jq`, `yq`, ssh with sudo to the FE nodes - see
[usage](docs/usage/README.md#requirements).

## Layout

```
bin/claugine      loader of internal data and modules - source it; run it for help
lib/              modules: claugine_<area>.sh
config/           data of the tool: data/ (YAML), templates/, files/
data/             data sets: data/<set>/ - inventory/, secrets/, runtime/,
                  tests-data.sh, jobs-data.sh; data/SAMPLE published, the rest ignored by git
tests/            scenario scripts; data set: CLAUGINE_DATA=data/<set>
jobs/             production scripts, by hand or by cron; job data: the argument datafile=data/<set>/jobs-data.sh
binaries/         prebuilt images on Google Drive - the VyOS ISO and the VyOS image, see binaries/README.md
docs/             documentation
```

## Documentation

| Section | Content |
|---|---|
| [platform](docs/platform/README.md) | target platform architecture: sites, management and payload platforms |
| [data](docs/data/README.md) | data used by claugine: source data, inventory, internal data, secrets, sample, runtime data |
| [tool](docs/tool/README.md) | tool design: loader, modules, helpers, conventions |
| [network](docs/network/README.md) | network functions and services: EVPN, IX, L2-over-L3, VyOS image |
| [usage](docs/usage/README.md) | for admins: how to use, examples, commands, included tests |

Release history: [CHANGES.md](CHANGES.md).

## License

[Apache License 2.0](LICENSE)

## Author

Vladislav Kirilin — [@ivladek](https://github.com/ivladek)
