#!/usr/bin/env bash
# claugine-install.sh — install, restore or extend a Claugine portal. The one public entry point.
#
# Download it into ~/claugine and run it there, as the user you log in with over ssh — NOT as root:
#
#   mkdir -p ~/claugine && cd ~/claugine
#   curl -fsSLO https://raw.githubusercontent.com/ivladek/claugine/main/deploy/claugine-install.sh
#   bash claugine-install.sh --new --licence-key CLG-… --site-url https://portal.example.com
#
#   --new      a first portal node: fetch the code this licence is entitled to, install it, bind the licence.
#              The installation id is the host of --site-url (v01.00.00), so a re-run after a failed install, or a
#              rebuild of the same portal, is the same installation; --install-id forces another id
#   --restore  the same, then restore one backup over it (asks for the archive password once, before anything
#              is written)
#
#   --second <user>@<address>   the second node of an Enterprise installation (owner 2026-11-10). Required with an
#              Enterprise licence: an Enterprise portal is two VMs, and **nothing starts until the second VM is ready**.
#              This VM sets the other one up itself, over ssh with a key of its own: run once without the key in
#              place and the script prints the line to add to <user>'s ~/.ssh/authorized_keys there, then stops
#              before changing anything. The other VM needs only a clean Ubuntu 26.04 and that user with
#              passwordless sudo. There is no separate "join": the second node is always provisioned from here.
#   --site <code>, --second-site <code>   the two nodes' site codes (default site1 / site2)
#
# Requirements of the VM (Ubuntu 26.04):
#   * you log in over ssh as an ordinary user who is in sudoers **with NOPASSWD**, so nothing here ever prompts for
#     a password. Every privileged step below is a single command prefixed with `sudo`; the script itself never runs
#     as root, so what it writes under ~/claugine stays yours.
#   * outbound HTTPS to my.claugine.com (and to the Ubuntu and Python package mirrors).
#
# A licence is required: the script asks my.claugine.com to bind this installation and **stops if it will not**.
# Which edition you get is the control plane's answer — never a choice made here. Either way the release comes as a
# bundle from my.claugine.com, authorised by the licence key, whose SHA-256 is verified **before** it is unpacked
# (v01.00.00: nothing is cloned from git).
#
# --dry-run prints every privileged command instead of running it, and touches nothing.
set -Eeuo pipefail

CHECKIN_URL="https://my.claugine.com"
MODE="" LICENCE_KEY="" SITE_URL="" LE_EMAIL="" BACKUP="" GIVEN_INSTALL_ID="" DRY=0
LB_STATUS_PORT=8087   # the role endpoint (GET /active …) once HAProxy takes 80 and 443 here (as claugine-add-node.sh)
SECOND="" SITE_CODE="" SECOND_SITE="" DEV_SRC=""
WORK="$HOME/claugine"
SRC=""            # where the code ends up
PW_FILE=""        # the archive password for --restore, readable only by this user, removed on the way out
cleanup() { [[ -z "$PW_FILE" ]] || { rm -f "$PW_FILE"; PW_FILE=""; }; }
trap cleanup EXIT INT TERM

say()  { printf '\n== %s\n' "$*"; }
info() { printf '   %s\n' "$*"; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }
log()  { info "$*"; }   # the shared fetch_bundle below logs with log(); here that is info()

# ---- fetch_bundle: keep this byte-identical to the copy in deploy/claugine-install.sh ----------------------
# It reads CHECKIN_URL, LICENCE_KEY and INSTALL_ID from the environment so both copies are the same text; each
# script sets them its own way. accounts/tests_bundle_mode.py runs this exact function from *both* files against
# the same fixtures and fails if they differ.
fetch_bundle() {  # <version> <dest dir> - verifies the checksum BEFORE unpacking
  local ver="$1" dest="$2" url key id tmp want b64 got
  url="${CHECKIN_URL:-}"; key="${LICENCE_KEY:-}"; id="${INSTALL_ID:-}"
  [[ -n "$url" && -n "$key" && -n "$id" ]] \
    || die "a release download needs the control-plane URL, the licence key and the installation id"
  tmp="$(mktemp -d /tmp/claugine-bundle.XXXXXX)"; chmod 700 "$tmp"
  curl -fsS -m 300 -D "$tmp/headers" -o "$tmp/bundle.tgz" \
       -H "Authorization: Bearer ${key}" -H "X-Install-Id: ${id}" \
       "${url}/api/release/${ver}" \
    || { rm -rf "$tmp"; die "the release download failed: ${url}/api/release/${ver}. The licence may not be entitled to ${ver}; nothing was changed."; }
  want="$(sed -n 's/^[Xx]-[Cc]ontent-[Ss][Hh][Aa]256: *//p' "$tmp/headers" | tr -d '\r' | tail -1)"
  if [[ -z "$want" ]]; then      # the agreed alternative form: Digest: sha-256=<base64>
    b64="$(sed -n 's/^[Dd]igest: *sha-256=//p' "$tmp/headers" | tr -d '\r' | tail -1)"
    [[ -n "$b64" ]] || { rm -rf "$tmp"; die "the download carried no checksum (neither X-Content-SHA256 nor Digest); refusing to unpack it"; }
    want="$(printf '%s' "$b64" | base64 -d | od -An -tx1 | tr -d ' \n')"
  fi
  got="$(sha256sum "$tmp/bundle.tgz" | cut -d' ' -f1)"
  if [[ "${want,,}" != "${got,,}" ]]; then
    rm -rf "$tmp"
    die "the release bundle checksum does not match (expected ${want}, got ${got}).
Refusing to unpack it; the download has been deleted and nothing was changed."
  fi
  log "checksum verified before unpacking: ${got:0:16}…"
  tar -xzf "$tmp/bundle.tgz" -C "$dest" --strip-components=1 || { rm -rf "$tmp"; die "the release bundle could not be unpacked"; }
  rm -rf "$tmp"
  [[ -f "$dest/deploy/install.sh" ]] || die "the fetched release has no deploy/install.sh — refusing to go on"
}

# accounts/tests_bundle_mode.py sources this file to exercise fetch_bundle alone; set only by the test.
[[ "${CLAUGINE_UPDATE_SOURCE_ONLY:-}" == 1 ]] && return 0 || true


# Every privileged step goes through this one function, so there is exactly one place where root is used.
priv() {
  if [[ $DRY -eq 1 ]]; then
    # never echo the licence key, not even in a dry run: this output is what gets pasted into a ticket
    printf '   [dry-run] sudo %s\n' "${*//${LICENCE_KEY:-__nokey__}/CLG-********}"
    return 0
  fi
  sudo -n "$@"
}

# The header above is the usage text: print it up to the first line that is not a comment.
usage() { sed -n '2,/^[^#]/p' "$0" | sed -n 's/^#\( \|$\)//p'; exit 2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --new|--restore)     [[ -z "$MODE" ]] || die "give only one of --new, --restore"
                         MODE="${1#--}"; shift ;;
    --join)              die "--join is gone (2026-11-10): the second node is set up from the first one.
Run on the first VM: claugine-install.sh --new ... --second <user>@<this VM's address>" ;;
    --second)            SECOND="$2"; shift 2 ;;
    --site)              SITE_CODE="$2"; shift 2 ;;
    --second-site)       SECOND_SITE="$2"; shift 2 ;;
    --src)               DEV_SRC="${2%/}"; shift 2 ;;   # development: install this tree instead of fetching one
    --licence-key)       LICENCE_KEY="$2"; shift 2 ;;
    --site-url)          SITE_URL="${2%/}"; shift 2 ;;
    --letsencrypt-email) LE_EMAIL="$2"; shift 2 ;;
    --backup)            BACKUP="$2"; shift 2 ;;
    --install-id)        GIVEN_INSTALL_ID="$2"; shift 2 ;;
    --checkin-url)       CHECKIN_URL="${2%/}"; shift 2 ;;
    --dry-run)           DRY=1; shift ;;
    -h|--help)           usage ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
done

# ---------------------------------------------------------------- preflight, before anything is changed
say "Checking this machine"
[[ -n "$MODE" ]]        || usage
[[ $EUID -ne 0 ]]       || die "do not run this as root. Log in as your ordinary user; each privileged step uses sudo."
[[ -n "$LICENCE_KEY" ]] || die "--licence-key is required. Register at ${CHECKIN_URL} to get a free Standard licence."
[[ -n "$SITE_URL" ]]    || die "--site-url is required, e.g. --site-url https://portal.example.com"
[[ "$MODE" != "restore" || -n "$BACKUP" ]] || die "--restore needs --backup <folder with database.sql.gz and portal.env.zip>"
[[ -z "$SECOND" || "$SECOND" == *@* ]] || die "--second wants <user>@<address> of the other VM"

for c in curl tar sha256sum python3; do command -v "$c" >/dev/null || die "$c is not installed"; done
if [[ $DRY -eq 0 ]]; then
  sudo -n true 2>/dev/null || die "sudo asked for a password. This user must be in sudoers with NOPASSWD (see the header)."
fi
if [[ -r /etc/os-release ]]; then
  . /etc/os-release
  [[ "${ID:-}" == "ubuntu" ]] || info "warning: this is built and tested for Ubuntu 26.04; found ${PRETTY_NAME:-unknown}"
fi
mkdir -p "$WORK"; cd "$WORK"
if [[ $DRY -eq 1 ]]; then info "working in $WORK as $(id -un) - dry run, so sudo was not checked"
else info "working in $WORK as $(id -un), sudo without a password: yes"; fi

# The archive password is asked **now**, before a single thing is written, so a restore never stops half way.
ARCHIVE_PASSWORD=""
if [[ "$MODE" == "restore" ]]; then
  [[ -d "$BACKUP" ]] || die "no such backup folder: $BACKUP"
  [[ -f "$BACKUP/database.sql.gz" && -f "$BACKUP/portal.env.zip" ]] \
    || die "$BACKUP does not look like a Claugine backup (database.sql.gz and portal.env.zip expected)"
  if [[ $DRY -eq 0 ]]; then
    read -rsp "   The backup's archive password: " ARCHIVE_PASSWORD; echo
    [[ -n "$ARCHIVE_PASSWORD" ]] || die "the archive password is needed to read portal.env.zip"
    PW_FILE="$(mktemp "$WORK/.archive-password.XXXXXX")"; chmod 600 "$PW_FILE"
    printf '%s' "$ARCHIVE_PASSWORD" > "$PW_FILE"; ARCHIVE_PASSWORD=""
  fi
fi

# ---------------------------------------------------------------- the second VM, before anything is changed
# Owner 2026-11-10: an Enterprise installation is two VMs and starts only when the second one is ready. This VM
# reaches it with a key of its own (root's, kept for "Replace node" later), so the checks run as root over ssh.
PEER_KEY=/etc/claugine-portal/peer_ssh/id_ed25519
if [[ -n "$SECOND" ]]; then
  say "Checking the second VM ($SECOND)"
  if [[ $DRY -eq 1 ]]; then
    info "[dry-run] would check ssh, sudo, Ubuntu, a clean VM and the link to this one"
  else
    if ! sudo -n test -s "$PEER_KEY"; then
      sudo -n install -d -m 700 -o root -g root /etc/claugine-portal/peer_ssh
      sudo -n ssh-keygen -q -t ed25519 -N "" -C "claugine-primary@$(hostname -s)" -f "$PEER_KEY"
    fi
    S_ADDR="${SECOND#*@}"
    PSSH=(sudo -n ssh -i "$PEER_KEY" -o BatchMode=yes -o IdentitiesOnly=yes -o ConnectTimeout=10
          -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/etc/claugine-portal/peer_ssh/known_hosts "$SECOND")
    if ! "${PSSH[@]}" true 2>/dev/null; then
      cat >&2 <<MSG
This VM cannot log in to $SECOND yet. Add this line to ~${SECOND%@*}/.ssh/authorized_keys on $S_ADDR,
then run the same command again. Nothing has been changed on either VM.

$(sudo -n cat "$PEER_KEY.pub")
MSG
      exit 1
    fi
    "${PSSH[@]}" sudo -n true 2>/dev/null || die "${SECOND%@*} on $S_ADDR has no passwordless sudo. Nothing was changed."
    s_os="$("${PSSH[@]}" '. /etc/os-release; echo "$ID $VERSION_ID"' 2>/dev/null || true)"
    [[ "$s_os" == ubuntu* ]] || die "$S_ADDR runs ${s_os:-an unknown system}; Ubuntu 26.04 is required. Nothing was changed."
    # A re-run after a failure finds the second VM half set up by this same installation: that is not a foreign
    # portal, so it is carried on with rather than refused.
    my_id="${GIVEN_INSTALL_ID:-$(printf '%s' "$SITE_URL" | sed -E 's#^[A-Za-z]+://##; s#[:/].*$##; s#\.$##' | tr '[:upper:]' '[:lower:]')}"
    s_id="$("${PSSH[@]}" 'sudo -n sed -n "s/^CLAUGINE_INSTALL_ID=//p" /etc/claugine-portal/portal.env 2>/dev/null | tail -1' 2>/dev/null || true)"
    if [[ -z "$my_id" || "$s_id" != "$my_id" ]]; then
      "${PSSH[@]}" 'sudo -n test ! -e /etc/claugine-portal/portal.env && sudo -n test ! -d /opt/claugine-portal/app' \
        || die "$S_ADDR already has a portal: give a clean VM. Nothing was changed."
    fi
    my_addr="$(ip -4 route get "$S_ADDR" 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -1)"
    [[ -n "$my_addr" && "$my_addr" != "$S_ADDR" ]] || die "no route from this VM to $S_ADDR. Nothing was changed."
    "${PSSH[@]}" "timeout 5 bash -c '</dev/tcp/$my_addr/22'" 2>/dev/null \
      || die "$S_ADDR cannot reach this VM ($my_addr); the two nodes must reach each other. Nothing was changed."
    info "ok: $SECOND logs in, has passwordless sudo, runs $s_os, is clean and reaches this VM ($my_addr)"
  fi
fi

# ---------------------------------------------------------------- the licence decides what is installed
say "Asking ${CHECKIN_URL} about this licence"
if true; then
  # The installation id is the host of the site URL (owner 2026-11-11), so rebuilding the same portal (new VMs, a
  # restore) is the same installation. --install-id still forces an id.
  FQDN="$(printf '%s' "$SITE_URL" | sed -E 's#^[A-Za-z]+://##; s#[:/].*$##; s#\.$##' | tr '[:upper:]' '[:lower:]')"
  [[ "$FQDN" =~ ^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63}$ ]] \
    || die "the installation id is the host of --site-url, and ${FQDN:-that} is not a fully qualified domain name"
  INSTALL_ID="${GIVEN_INSTALL_ID:-$FQDN}"
  info "installation id: ${INSTALL_ID}"
  FP="$( { cat /etc/machine-id 2>/dev/null; hostname; } | sha256sum | cut -c1-32 )"

  act_body="{\"licence_key\":\"${LICENCE_KEY}\",\"install_id\":\"${INSTALL_ID}\",\"fingerprints\":[\"${FP}\"]}"
  if [[ $DRY -eq 1 ]]; then
    # Activation **binds** the licence to this id, first-wins, and a later real run would mint another and be
    # refused with 409. A dry run must therefore not call it (2026-11-09).
    info "[dry-run] would activate ${INSTALL_ID} against ${CHECKIN_URL}"
  else
  act="$(curl -fsS -m 25 -X POST "${CHECKIN_URL}/api/activate" -H 'Content-Type: application/json' -d "$act_body" 2>&1)" \
    || die "the licence was refused, or ${CHECKIN_URL} could not be reached:
  ${act}
Nothing has been changed on this machine. Check the key, and that this machine can reach ${CHECKIN_URL}."
  info "licence bound to ${INSTALL_ID}"
  fi
fi

ci_body="{\"licence_key\":\"${LICENCE_KEY}\",\"install_id\":\"${INSTALL_ID}\",\"current_version\":\"\",\"stream\":\"stable\",\"usage\":{}}"
ci="$(curl -fsS -m 25 -X POST "${CHECKIN_URL}/api/checkin" -H 'Content-Type: application/json' -d "$ci_body" 2>&1)" \
  || die "the licence check-in failed: ${ci}"
read -r EDITION TARGET STATE <<<"$(printf '%s' "$ci" | python3 -c '
import json,sys
d=json.load(sys.stdin)
print(d.get("edition") or "standard", d.get("target_version") or "-", d.get("state") or "active")')"
[[ "$TARGET" != "-" ]] || TARGET=""
# retired means this installation id was released from its licence (claugine-web, 2026-11-09): this id will never
# be served again, so installing under it must stop here.
if [[ "$STATE" == "retired" ]]; then
  die "installation ${INSTALL_ID} was released on ${CHECKIN_URL} and receives nothing further.
Ask ${CHECKIN_URL} to reinstate it; a portal under another name is a new installation."
fi
[[ "$STATE" == "active" || "$STATE" == "grace" ]] \
  || die "this licence is ${STATE} on ${CHECKIN_URL}; nothing was changed. Resolve it there and run this again."
info "edition: ${EDITION} · release: ${TARGET:-the newest} · licence: ${STATE}"
if [[ "$EDITION" == "enterprise" && -z "$SECOND" ]]; then
  die "an Enterprise installation is two VMs: give the second one with --second <user>@<address>.
Nothing has been installed. Run it again with --second."
fi
if [[ "$EDITION" != "enterprise" && -n "$SECOND" ]]; then
  die "a second node needs the Enterprise edition; this licence is ${EDITION}, which runs one node.
Nothing has been installed. Run it again without --second."
fi

# ---------------------------------------------------------------- fetch the code this licence is entitled to
if [[ -n "$DEV_SRC" ]]; then
  say "Using the tree given with --src (development)"
  [[ -f "$DEV_SRC/deploy/install.sh" ]] || die "$DEV_SRC has no deploy/install.sh"
  SRC="$(cd "$DEV_SRC" && pwd)"
  info "$SRC"
else
  # v01.00.00 (owner 2026-11-11): both editions come as bundles from the control plane - each licence gets its own
  # edition's bundle; nothing is cloned from git.
  say "Downloading the ${EDITION^} release from ${CHECKIN_URL}"
  [[ -n "$TARGET" ]] || die "the control plane named no release to install; try again shortly"
  SRC="$WORK/src-${TARGET}"
  rm -rf "$SRC"; mkdir -p "$SRC"
  fetch_bundle "$TARGET" "$SRC"
  # A bundle has no git history: the release IS its identity (install.sh records it in /opt/claugine-portal/COMMIT).
  printf '%s\n' "$TARGET" > "$SRC/COMMIT"
fi
[[ -f "$SRC/deploy/install.sh" ]] || die "the fetched release has no deploy/install.sh — refusing to go on"

# ---------------------------------------------------------------- install
inst_args=(--site-url "$SITE_URL")
# owner 2026-11-11: HAProxy in front of every node, the Standard edition's one node too ("an important security
# guard"). It takes 80 and 443 and asks Let's Encrypt for the certificate itself, so the e-mail goes to it, not to
# install.sh; Apache stays behind it on 8080/8443, and the role endpoint gets a port of its own on this VM's address.
SELF_ADDR=""
if [[ -z "$SECOND" ]]; then
  SELF_ADDR="$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -1)"
  [[ -n "$SELF_ADDR" ]] || SELF_ADDR="$(hostname -I 2>/dev/null | awk '{print $1}')"
  [[ -n "$SELF_ADDR" ]] || die "cannot tell this VM's own address (no default route): HAProxy needs it"
  inst_args+=(--addr "$SELF_ADDR" --lb-addrs "$SELF_ADDR" --status-ports "$LB_STATUS_PORT")
fi

case "$MODE" in
  new)
    say "Installing the portal"
    priv bash "$SRC/deploy/install.sh" "${inst_args[@]}"
    ;;
  restore)
    say "Installing the portal, then restoring the backup"
    priv bash "$SRC/deploy/install.sh" "${inst_args[@]}"
    priv bash "$SRC/deploy/restore.sh" --site-url "$SITE_URL" --backup "$BACKUP" \
         ${PW_FILE:+--password-file "$PW_FILE"}
    info "the backup carries portal.env, so this installation keeps its original install id and its licence binding"
    ;;
esac

# ---------------------------------------------------------------- bind the licence into portal.env
if [[ "$MODE" != "restore" ]]; then
  say "Writing the licence settings"
  # **Always** hand over the id this run is using, and never let activate.sh bind a second one. Found on the first
  # real-VM test, 2026-11-09: --new activated inst-A at step 2, then activate.sh found no portal.env, minted
  # inst-B, activated *that* and got 409 — leaving the portal configured with an id the licence is not bound to.
  # The check-in then answered `conflict` and updates were stopped, on a brand-new installation.
  act_args=(--licence-key "$LICENCE_KEY" --checkin-url "$CHECKIN_URL" --install-id "$INSTALL_ID" --no-activate)
  priv bash "$SRC/deploy/server/activate.sh" "${act_args[@]}"

  # The backup owner is this account, and it is what a pinned `~` path expands against. Found on the first real-VM
  # test, 2026-11-09: left empty, Standard's `~/claugine/backups` resolved against the home of the user the portal
  # *runs* as - www-data - so a fresh installation would have written its backups into /var/www/claugine/backups.
  # The rule is the service account's home, and the installer is the only thing that knows which account that is.
  say "Setting the backup owner to $(id -un):$(id -gn)"
  if [[ $DRY -eq 1 ]]; then
    info "[dry-run] would set backup_owner to $(id -un):$(id -gn) if it is empty"
  else
    {
      echo "from tenants.models import PortalSettings"
      echo "s = PortalSettings.get()"
      echo "if not (s.backup_owner or '').strip():"
      echo "    s.backup_owner = '$(id -un):$(id -gn)'"
      echo "    s.save(update_fields=['backup_owner'])"
      echo "    print('   backup_owner set to', s.backup_owner)"
      echo "else:"
      echo "    print('   backup_owner already set:', s.backup_owner)"
    } > "$WORK/.set-owner.py"
    priv bash -c "cd /opt/claugine-portal/app && /opt/claugine-portal/venv/bin/python manage.py shell < $WORK/.set-owner.py" \
      2>/dev/null | tail -1 || info "could not set the backup owner - set it in Settings, Backups"
    rm -f "$WORK/.set-owner.py"
  fi

  priv systemctl reload apache2

  # One check-in now, rather than leaving it to the timer. The appliance timer is created before the licence is
  # written, so its first tick finds no check-in configured, takes the GitHub branch and caches no edition - and
  # its ten-minute rate limit then leaves the node without one for up to ten minutes more. A Standard installation
  # enforces nothing until the edition is cached, so that window is exactly the limit it is supposed to have
  # (owner 2026-11-09, test VM). force=True is needed because the first tick already stamped checked_at.
  say "Telling the licence server what this installation is"
  if [[ $DRY -eq 1 ]]; then
    info "[dry-run] would check in once so the edition is in force immediately"
  else
    {
      echo "from accounts import autoupdate"
      echo "print('  ', autoupdate.tick(log=lambda *a: None, force=True))"
    } > "$WORK/.checkin.py"
    priv bash -c "cd /opt/claugine-portal/app && /opt/claugine-portal/venv/bin/python manage.py shell < $WORK/.checkin.py" \
      2>/dev/null | tail -1 || info "the check-in did not run - the timer will do it within ten minutes"
    rm -f "$WORK/.checkin.py"
  fi
fi

# ---------------------------------------------------------------- HAProxy in front of the one node
if [[ -n "$SELF_ADDR" ]]; then
  say "HAProxy in front of this node (${SITE_URL#https://} → Apache on this VM)"
  https_port="$(sudo -n sed -n 's/^HTTPS_PORT=//p' /etc/claugine-portal/node.env 2>/dev/null | tail -1)"
  lb_args=(--apps "$SELF_ADDR" --public-name "$(printf '%s' "$SITE_URL" | sed -E 's#^https?://##; s#[:/].*$##')"
           --status-port "$LB_STATUS_PORT" --app-port "${https_port:-8443}")
  [[ -z "$LE_EMAIL" ]] || lb_args+=(--letsencrypt-email "$LE_EMAIL")
  priv bash "$SRC/deploy/install-lb.sh" "${lb_args[@]}" \
    || die "HAProxy could not be set up (see above). The portal is installed behind it on port ${https_port:-8443}; run this
again once the cause is fixed"
fi

# ---------------------------------------------------------------- the second node, set up from here
if [[ -n "$SECOND" ]]; then
  say "Setting up the second node ($SECOND) from this one"
  an_args=(--target "$SECOND" --src "$SRC")
  [[ -z "$SITE_CODE" ]]   || an_args+=(--site "$SITE_CODE")
  [[ -z "$SECOND_SITE" ]] || an_args+=(--target-site "$SECOND_SITE")
  [[ -z "$LE_EMAIL" ]]    || an_args+=(--letsencrypt-email "$LE_EMAIL")
  priv bash "$SRC/deploy/server/claugine-add-node.sh" "${an_args[@]}" \
    || die "the second node could not be set up (see above). This node is installed; once the cause is fixed run,
on this VM:  sudo claugine-add-node --target $SECOND"
fi

# ---------------------------------------------------------------- what the operator does next
say "Done"
if [[ $DRY -eq 1 ]]; then info "dry run: nothing was changed."; exit 0; fi
port="$(sudo -n sed -n 's/^HTTP_PORT=//p' /etc/claugine-portal/node.env 2>/dev/null | tail -1)"
code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${port:-8080}/" || true)"
info "the portal answers locally with HTTP ${code} (301 to https, or 200, is expected)"
# A cheap self-check for the fault this test found: the id in portal.env must be the one we bound.
written="$(sudo -n sed -n 's/^CLAUGINE_INSTALL_ID=//p' /etc/claugine-portal/portal.env 2>/dev/null | tail -1)"
if [[ -n "$written" && "$written" != "$INSTALL_ID" ]]; then
  printf "\n   WARNING: portal.env says %s but this run bound %s.\n   The portal will check in as an installation the licence does not cover.\n" \
         "$written" "$INSTALL_ID" >&2
fi
info "open ${SITE_URL} and sign in as the first super admin:"
info "  sudo claugine-manage bootstrap_superadmin        prints a one-time link, valid 24 hours"
case "$MODE" in
  new)
    if [[ "$EDITION" == "enterprise" ]]; then
      info "two nodes: this one active, ${SECOND#*@} standby and replicating; HAProxy on both sends ${SITE_URL} to the active one"
    else
      info "the Standard edition runs one node, behind HAProxy on this VM, with its backups and update window fixed - Settings shows them."
    fi
    ;;
esac
