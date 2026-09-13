#!/usr/bin/env bash
set -Eeuo pipefail

cat >&2 <<'EOF'
ERROR: this create-template.sh version is intentionally disabled.

The previous implementation used virt-customize/libguestfs-tools on the
Proxmox host. That design has been superseded.

Accepted policy:
  templates/debian13/build-policy.md

The replacement script must build tpl-debian13 through a temporary builder VM
and must not install virt-customize/libguestfs-tools on the Proxmox host.

Do not use this script until it is rewritten according to the policy.
EOF

exit 1
