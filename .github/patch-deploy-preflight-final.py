from pathlib import Path

p = Path("scripts/pve/deploy-guest.py")
text = p.read_text(encoding="utf-8")
old = '''    try:
        resolved = resolve_effective_guest(source, defaults)
    except GuestConfigError as exc:
        raise DeployError(f"не удалось построить effective state VMID {vmid}: {exc}") from exc
    schema = read_yaml(EFFECTIVE_SCHEMA)
'''
new = '''    try:
        resolved = resolve_effective_guest(source, defaults)
    except GuestConfigError as exc:
        raise DeployError(f"не удалось построить effective state VMID {vmid}: {exc}") from exc
    if resolved.effective.get("boot", {}).get("start_after_deploy") is not True:
        raise BlockedError(
            f"VMID {vmid}: deploy-guest v1 требует boot.start_after_deploy=true; "
            "final acceptance без verified running SSH пока не поддерживается"
        )
    schema = read_yaml(EFFECTIVE_SCHEMA)
'''
if old not in text:
    raise SystemExit("load_desired resolver anchor missing")
text = text.replace(old, new, 1)
# The late guard is now redundant and could only be reached after mutation.
old = '''    start_guest_if_needed(api, desired)
    if not desired.effective["boot"]["start_after_deploy"]:
        raise BlockedError("v1 final acceptance требует running guest")
    establish_ssh_trust(desired, created_now)
'''
new = '''    start_guest_if_needed(api, desired)
    establish_ssh_trust(desired, created_now)
'''
if old not in text:
    raise SystemExit("late start_after_deploy guard missing")
text = text.replace(old, new, 1)
p.write_text(text, encoding="utf-8")

test = Path("scripts/tests/test-deploy-guest.py")
t = test.read_text(encoding="utf-8")
anchor = '''    assert "DEPLOY_GUEST_SOURCE_REVISION" in text
    assert "deploy-incomplete" in text
'''
insert = '''    assert "DEPLOY_GUEST_SOURCE_REVISION" in text
    load_start = text.index("def load_desired(")
    load_end = text.index("\\ndef load_local_config", load_start)
    assert "start_after_deploy" in text[load_start:load_end]
    assert "deploy-incomplete" in text
'''
if anchor not in t:
    raise SystemExit("test preflight anchor missing")
t = t.replace(anchor, insert, 1)
test.write_text(t, encoding="utf-8")

doc = Path("docs/31-deploy-guest.md")
d = doc.read_text(encoding="utf-8")
marker = "PLAN никогда не генерирует management key, не пишет credential и не запускает sync.\n"
addition = marker + "\nВ runtime v1 `boot.start_after_deploy=false` блокируется на preflight до любых мутаций: финальное принятие объекта требует verified running SSH. Текущие deployable manifests 301/311 используют `true`.\n"
if marker not in d:
    raise SystemExit("docs31 PLAN marker missing")
d = d.replace(marker, addition, 1)
doc.write_text(d, encoding="utf-8")
