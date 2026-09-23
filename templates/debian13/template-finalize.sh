#!/usr/bin/env bash
set -Eeuo pipefail

if id debian >/dev/null 2>&1; then
    userdel -r debian
fi
! id debian >/dev/null 2>&1

rm -f /var/lib/template-build/bootstrap-complete
cloud-init clean --logs --seed

truncate -s 0 /etc/machine-id
rm -f /var/lib/dbus/machine-id
rm -f /etc/ssh/ssh_host_*
rm -rf /var/lib/dhcp/* /var/lib/NetworkManager/*
rm -f /var/lib/systemd/random-seed

apt-get clean
rm -rf /var/lib/apt/lists/*
journalctl --rotate || true
journalctl --vacuum-time=1s || true
rm -rf /tmp/* /var/tmp/*
rm -f /root/.bash_history
rm -rf /root/.ssh
rm -rf /var/lib/template-build
rm -f /usr/local/sbin/template-bootstrap /usr/local/sbin/template-finalize

# Fail closed: template sealing is allowed only after machine-specific and
# builder-specific state is demonstrably gone.
[[ ! -s /etc/machine-id ]]
[[ ! -e /var/lib/dbus/machine-id ]]
! compgen -G '/etc/ssh/ssh_host_*' >/dev/null
[[ ! -e /root/.ssh ]]
[[ ! -e /var/lib/template-build ]]
[[ ! -e /usr/local/sbin/template-bootstrap ]]
[[ ! -e /usr/local/sbin/template-finalize ]]

fstrim -av || printf 'ПРЕДУПРЕЖДЕНИЕ: fstrim завершился с ошибкой; финализация шаблона продолжается\n' >&2
sync
printf 'FINALIZE_OK\n'
