from pathlib import Path


def replace(path: str, old: str, new: str, count: int = 1) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"anchor not found in {path}: {old[:120]!r}")
    p.write_text(text.replace(old, new, count), encoding="utf-8")


# Critical PLAN/preflight fixes.
path = Path("scripts/pve/deploy-guest.py")
text = path.read_text(encoding="utf-8")
text = text.replace(
    'def boolish(value: object) -> bool:\n    return str(value).lower() in {"1", "true", "yes", "on"}\n',
    'def boolish(value: object) -> bool:\n    return str(value).lower() in {"1", "true", "yes", "on"}\n\n\ndef option_enabled(value: object) -> bool:\n    """Parse PVE boolean option whose value may carry comma-separated suboptions."""\n    return str(value or "").split(",", 1)[0].lower() in {"1", "true", "yes", "on"}\n',
    1,
)
text = text.replace(
    'compare_item("PVE guest agent", boolish(cfg.get("agent")), bool(e["vm"]["guest_agent"]), "REQUIRES-STOP"),',
    'compare_item("PVE guest agent", option_enabled(cfg.get("agent")), bool(e["vm"]["guest_agent"]), "REQUIRES-STOP"),',
    1,
)
text = text.replace(
    'items.append(PlanItem("PVE unprivileged", "NO CHANGE", "FORBIDDEN", str(unprivileged)))',
    'items.append(PlanItem("PVE unprivileged", "NO CHANGE", "ONLINE", str(unprivileged)))',
    1,
)
old = '''    if not actual.exists or actual.status != "running":
        after = "APPLY AFTER START"
        items.append(PlanItem("Management SSH identity", after if e["management"]["ssh_identity"] else "NOT REQUESTED", "ONLINE", "guest identity"))
        items.append(PlanItem("Project repo read", after if e["management"]["project_repo_read"] else "NOT REQUESTED", "ONLINE", EXPECTED_REPO if e["management"]["project_repo_read"] else "false"))
        for capability in desired.bootstrap_capabilities:
            items.append(PlanItem(f"Bootstrap {capability}", after, "ONLINE", capability))
        if not desired.bootstrap_capabilities:
            items.append(PlanItem("Bootstrap", "NOT REQUESTED", "ONLINE", "none"))
        return items
    if not host_key_present(desired.management_ip):
        return [PlanItem("SSH trust", "BLOCKED", "FORBIDDEN", "existing guest absent from persistent known_hosts")]
'''
new = '''    if not actual.exists:
        if host_key_present(desired.management_ip):
            return [PlanItem("SSH trust", "BLOCKED", "FORBIDDEN", "new guest address already exists in persistent known_hosts")]
        if e["management"]["ssh_identity"]:
            reg_state, _, _ = registry_key_state(desired.vmid)
            if reg_state != "ABSENT":
                return [PlanItem("Management SSH identity", "BLOCKED", "FORBIDDEN", f"new VMID has stale registry state={reg_state}")]
        after = "APPLY AFTER START"
        items.append(PlanItem("Management SSH identity", after if e["management"]["ssh_identity"] else "NOT REQUESTED", "ONLINE", "guest identity"))
        items.append(PlanItem("Project repo read", after if e["management"]["project_repo_read"] else "NOT REQUESTED", "ONLINE", EXPECTED_REPO if e["management"]["project_repo_read"] else "false"))
        for capability in desired.bootstrap_capabilities:
            items.append(PlanItem(f"Bootstrap {capability}", after, "ONLINE", capability))
        if not desired.bootstrap_capabilities:
            items.append(PlanItem("Bootstrap", "NOT REQUESTED", "ONLINE", "none"))
        return items
    if not host_key_present(desired.management_ip):
        return [PlanItem("SSH trust", "BLOCKED", "FORBIDDEN", "existing guest absent from persistent known_hosts")]
    if actual.status != "running":
        after = "APPLY AFTER START"
        items.append(PlanItem("Management SSH identity", after if e["management"]["ssh_identity"] else "NOT REQUESTED", "ONLINE", "guest identity"))
        items.append(PlanItem("Project repo read", after if e["management"]["project_repo_read"] else "NOT REQUESTED", "ONLINE", EXPECTED_REPO if e["management"]["project_repo_read"] else "false"))
        for capability in desired.bootstrap_capabilities:
            items.append(PlanItem(f"Bootstrap {capability}", after, "ONLINE", capability))
        if not desired.bootstrap_capabilities:
            items.append(PlanItem("Bootstrap", "NOT REQUESTED", "ONLINE", "none"))
        return items
'''
if old not in text:
    raise SystemExit("plan_remote preflight anchor not found")
text = text.replace(old, new, 1)
path.write_text(text, encoding="utf-8")

# PVE Configuration version follows the newly installed working component.
replace("scripts/pve/setup/lib/00-common.sh", "PVE_CONFIGURATION_VERSION=29", "PVE_CONFIGURATION_VERSION=30")
replace("scripts/pve/setup/tests/test-deploy-runtime-contract.sh", "PVE_CONFIGURATION_VERSION=29", "PVE_CONFIGURATION_VERSION=30")
replace("scripts/pve/setup/tests/test-deploy-runtime-contract.sh", "PVE_CONFIGURATION_VERSION must be 29", "PVE_CONFIGURATION_VERSION must be 30")

# CI: compile and run deploy-guest unit tests.
replace(
    ".github/workflows/repository-checks.yml",
    "            scripts/tests/test-sync-management-keys.py \\\n            scripts/pve/sync-management-keys.py \\",
    "            scripts/tests/test-sync-management-keys.py \\\n            scripts/tests/test-deploy-guest.py \\\n            scripts/pve/sync-management-keys.py \\\n            scripts/pve/deploy-guest.py \\",
)
replace(
    ".github/workflows/repository-checks.yml",
    "      - name: Run sync-management-keys unit checks\n        run: python scripts/tests/test-sync-management-keys.py\n",
    "      - name: Run sync-management-keys unit checks\n        run: python scripts/tests/test-sync-management-keys.py\n\n      - name: Run deploy-guest unit checks\n        run: python scripts/tests/test-deploy-guest.py\n",
)

# Runtime contract: source must exist and wrapper must preserve strict boundaries.
contract = Path("scripts/pve/setup/tests/test-deploy-runtime-contract.sh")
ct = contract.read_text(encoding="utf-8")
anchor = 'TOOLING="$ROOT/scripts/pve/setup/lib/70-tooling.sh"\n'
if anchor not in ct:
    raise SystemExit("tooling anchor missing")
ct = ct.replace(anchor, anchor + 'DEPLOY_GUEST="$ROOT/scripts/pve/deploy-guest.py"\n', 1)
marker = "grep -Fq '/usr/bin/python3 \"\\$VALIDATOR\"' \"$TOOLING\" \\\n    || fail \"root wrapper must run the shared repository validator\"\n"
extra = marker + """[[ -f "$DEPLOY_GUEST" ]] \\
    || fail "deploy-guest runtime source must exist"
grep -Fq 'DEPLOY_GUEST_SOURCE_REVISION' "$DEPLOY_GUEST" \\
    || fail "deploy-guest runtime must enforce pinned source revision"
grep -Fq 'DEPLOY_GUEST_PROJECT_REPO_KEY_FD' "$DEPLOY_GUEST" \\
    || fail "deploy-guest runtime must consume Project Git key only through dedicated FD"
grep -Fq 'StrictHostKeyChecking=yes' "$DEPLOY_GUEST" \\
    || fail "deploy-guest must use strict SSH host-key verification after trust establishment"
if grep -Eq '\b(qm|pct|pvesh)\b' "$DEPLOY_GUEST"; then
    fail "deploy-guest runtime must not bypass PVE REST API through qm/pct/pvesh"
fi
"""
if marker not in ct:
    raise SystemExit("runtime contract insertion marker missing")
ct = ct.replace(marker, extra, 1)
contract.write_text(ct, encoding="utf-8")

# Implementation status reflects code introduced by this change.
status = Path("docs/29-implementation-status.md")
st = status.read_text(encoding="utf-8")
replacements = {
    "| `deploy-guest` | Принятый контракт, код ещё не реализован | поведение PLAN/APPLY и post-SSH handlers задаёт `31-deploy-guest.md` |":
    "| `deploy-guest` | Реализовано | `scripts/pve/deploy-guest.py` реализует read-only PLAN по умолчанию и `--apply` для VM/LXC через PVE REST API с ownership, SSH trust, management handlers, Bootstrap и final verify |",
    "| `management.ssh_identity` runtime handler | Принятый контракт, код ещё не реализован | создание guest-local keypair, регистрация `<VMID>.pub` и вызов sync появятся вместе с `deploy-guest` |":
    "| `management.ssh_identity` runtime handler | Реализовано | guest-local Ed25519 keypair создаётся только при полном отсутствии, private остаётся в guest, public регистрируется как `<VMID>.pub`, fingerprint conflict блокирует deploy, registry change запускает sync |",
    "| `management.project_repo_read` runtime handler | Принятый контракт, код ещё не реализован | materialize/verify/remove общего Git READ credential появится вместе с `deploy-guest` |":
    "| `management.project_repo_read` runtime handler | Реализовано | root-wrapper передаёт fixed read key только через FD; runtime materialize/verify/remove выполняет fixed credential/known_hosts/SSH alias и точный URL rewrite для `zsergeyru/proxmox` |",
    "| Guest Bootstrap runtime handlers | Принятый контракт, код ещё не реализован полностью | schema/resolver для `bootstrap.capabilities` действуют, фактическое применение будет частью `deploy-guest` |":
    "| Guest Bootstrap runtime handlers | Реализовано v1 | `base`, `git`, `docker`, `ansible_controller` применяются только после verified root SSH, с read-only check → apply → final verify и без произвольного shell/package interface |",
    "| Расширенные CI-проверки management/runtime key contract | Частично реализовано | registry и `sync-management-keys` покрыты contract/unit checks; deploy-specific runtime checks будут расширены вместе с `deploy-guest` |":
    "| Расширенные CI-проверки management/runtime key contract | Реализовано для v1 | registry, sync, deploy-guest PLAN/safety helpers, wrapper trust boundary и source-revision/FD/strict-SSH invariants покрыты contract/unit checks |",
}
for old, new in replacements.items():
    if old not in st:
        raise SystemExit(f"status row missing: {old[:60]}")
    st = st.replace(old, new, 1)
status.write_text(st, encoding="utf-8")

# Runtime CA documentation follows the already deployed v28+ security boundary.
replace(
    "docs/31-deploy-guest.md",
    "/etc/pve/pve-root-ca.pem",
    "/etc/proxmox-deployer/pve-root-ca.pem",
)

# Code map now lists the actual implementation and test.
readme = Path("scripts/README.md")
rt = readme.read_text(encoding="utf-8")
rt = rt.replace(
    "│   ├── test-guest-bootstrap.py\n│   └── test-sync-management-keys.py\n└── pve/\n    ├── sync-management-keys.py",
    "│   ├── test-guest-bootstrap.py\n│   ├── test-sync-management-keys.py\n│   └── test-deploy-guest.py\n└── pve/\n    ├── deploy-guest.py\n    ├── sync-management-keys.py",
    1,
)
rt = rt.replace(
    "После реализации `deploy-guest` здесь также появятся его основной модуль и bootstrap-handlers; до появления реального кода README не изображает их как уже существующие.\n\n",
    "",
    1,
)
rt = rt.replace("будущий `deploy-guest`", "`deploy-guest`")
rt = rt.replace("CI, `sync-management-keys` и будущий `deploy-guest`", "CI, `sync-management-keys` и `deploy-guest`")
rt = rt.replace(
    "scripts/tests/test-sync-management-keys.py\n```",
    "scripts/tests/test-sync-management-keys.py\nscripts/tests/test-deploy-guest.py\n```",
    1,
)
rt = rt.replace(
    "Первый фиксирует контракт Guest Bootstrap v1: явный набор capabilities, порядок, зависимости и требование `start_after_deploy=true`. Второй проверяет безопасную работу management-key registry, managed block `authorized_keys` и guest public catalog без подключения к живому PVE.",
    "Первый фиксирует контракт Guest Bootstrap v1: явный набор capabilities, порядок, зависимости и требование `start_after_deploy=true`. Второй проверяет безопасную работу management-key registry, managed block `authorized_keys` и guest public catalog. Третий проверяет PLAN/safety helpers, ownership/tags, disk rules и CLI `deploy-guest` без подключения к живому PVE.",
    1,
)
rt = rt.replace(
    "## `deploy-guest`\n\nОсновной источник требований к будущему `scripts/pve/deploy-guest.py`:",
    "## `pve/deploy-guest.py`\n\nРабочий PLAN/APPLY runtime одной deployable VM/LXC. По умолчанию команда строит read-only PLAN, а изменения разрешаются только с `--apply`. Основной источник требований:",
    1,
)
readme.write_text(rt, encoding="utf-8")
