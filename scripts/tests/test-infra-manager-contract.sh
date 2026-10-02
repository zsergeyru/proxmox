#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

for test_script in \
    test-infra-manager-guest-contract.sh \
    test-infra-manager-openbao-contract.sh \
    test-infra-manager-tooling-contract.sh \
    test-infra-manager-ssh-contract.sh
do
    bash "${ROOT}/scripts/tests/${test_script}"
done

printf '[ОК] Все контракты infra-manager проверены\n'
