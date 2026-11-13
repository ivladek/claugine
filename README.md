# Claugine

**CLoud AUtomation enGINE** — a portal to run an OpenNebula-based cloud for many customers: from an organization's
registration to its projects' networks, gateways and quotas on the platforms, and day-2 operations of the sites
themselves. Project site and documentation: [claugine.com](https://claugine.com) ·
[docs.claugine.com](https://docs.claugine.com).

## What it does

- **Customers:** self-registration of organizations (e-mail confirmation, tax-ID check, checks against automated
  and suspicious registrations), staff approval, organization admins and members, invitations, join requests.
- **Projects:** ordering and approval, deployment on the OpenNebula platforms (group, quotas, users, networks), an
  edge gateway and an internet gateway (VyOS) per project with public subnets, BGP and shaping; changes after
  deployment (speed, subnets, size) as requests for approval; existing projects can be adopted from the platforms.
- **Infrastructure:** sites, platforms, clusters, networks and address registry, secrets (encrypted), router
  configuration templates, core services of a site (name servers, front ends, route reflectors, …), host health
  over IPMI/Redfish, timers that read the platforms and compare routers with their templates.
- **Staff:** roles (super admin, resource manager, support, sales), mandatory two-factor sign-in, audit log of every
  decision, daily reports.
- **The portal itself:** e-mail through its own relay (Postfix + DKIM) or an SMTP server, encrypted backups to a local
  folder, HAProxy with Let's Encrypt in front. The Enterprise edition adds a second site with database replication
  and switch-over, and backups to an S3 bucket.

The provider's name and logo are set in Settings → Provider ("Example Cloud" is only the default).

## Install

Claugine runs on Ubuntu 26.04: the Standard edition on one VM behind HAProxy with Let's Encrypt, the Enterprise
edition on two. Download the installer and run it with your licence key:

```
mkdir -p ~/claugine && cd ~/claugine
curl -fsSLO https://raw.githubusercontent.com/ivladek/claugine/main/deploy/claugine-install.sh
bash claugine-install.sh --new --licence-key CLG-… --site-url https://portal.example.com
```

A licence key comes from [my.claugine.com](https://my.claugine.com); see **Licences** below. The installer downloads the release your licence is entitled to, as a bundle from
my.claugine.com, verifies its checksum and installs it; updates arrive the same way. This repository holds the code to
read; the installation and update tooling comes with each release. The documentation is at
[docs.claugine.com](https://docs.claugine.com). Configuration lives in `/etc/claugine-portal/`; management commands
run as `sudo claugine-manage <command>`.

The Enterprise edition (two nodes across two sites, S3 backups, editable backup and update settings) is delivered
to Enterprise licence holders from my.claugine.com and is not part of this repository.

## Licences

[my.claugine.com](https://my.claugine.com) provides two licences:

- **Standard — free, with no time limit on use.** No payment, ever. The key is renewed every year at no cost, which
  keeps the installer, updates and restore coming; the portal itself never stops working, renewed or not.
- **Enterprise — commercial.** The Enterprise edition (two portal nodes on one or two sites, S3 backups, every
  update channel) with full support, for a fee.

## Development

```
pip install -r requirements.txt
DEBUG=1 python manage.py migrate
DEBUG=1 python manage.py bootstrap_superadmin   # prints a one-time link
DEBUG=1 python manage.py runserver
```

Without an SMTP server, mail is written to `var/mail/`. Tests:
`DEBUG=1 SECRET_KEY=x PORTAL_ENV_FILE=/nonexistent python manage.py test`.

Interface languages: English, Russian, Kazakh.

## Licence

Claugine Standard is **source-available** under the [Business Source License 1.1](LICENSE):

- You may read, copy, modify and build the code. **Production use needs a licence** from
  [my.claugine.com](https://my.claugine.com); the Standard licence is **free of charge** (see **Licences** above).
- With it, you may run a portal for your own cloud services, including giving your own customers access to them.
  You may not offer it, or a work derived from it, to others as a product or as a hosted or managed service that
  gives them a portal for their own cloud services.
- For its term the licence key gives the installer, automatic updates and restore from a backup. When it ends, those
  stop and the portal keeps working, with no obligation of the licensor.
- **Each version becomes open source** under the GNU Affero General Public License 3.0 four years after it is
  published.

### Contributions

Outside contributions are not accepted at this time.
