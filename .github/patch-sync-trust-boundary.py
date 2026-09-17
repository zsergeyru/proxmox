from pathlib import Path

sync = Path("scripts/pve/sync-management-keys.py")
text = sync.read_text(encoding="utf-8")
old = '''    if known_host_exists(participant):
        known_hosts_file = KNOWN_HOSTS
    else:
        prepared.new_host_keys = scan_host_keys(participant)
        known_hosts_file = temporary_known_hosts(prepared.new_host_keys)
        temp_paths.append(known_hosts_file)
    prepared.known_hosts_file = known_hosts_file
'''
new = '''    if not known_host_exists(participant):
        raise UnsafeRemoteState(
            f"VMID {participant.vmid}: SSH host key ещё не доверен; "
            "первичное доверие разрешено только deploy-guest для только что созданного ожидаемого объекта"
        )
    known_hosts_file = KNOWN_HOSTS
    prepared.known_hosts_file = known_hosts_file
'''
if old not in text:
    raise SystemExit("sync unknown-host anchor not found")
text = text.replace(old, new, 1)
sync.write_text(text, encoding="utf-8")

# Unit-level invariant: prepare_participant must refuse untrusted hosts before SSH scan/inspection.
test = Path("scripts/tests/test-sync-management-keys.py")
t = test.read_text(encoding="utf-8")
anchor = '''def test_helpers() -> None:
    if mod.parse_tags("foo;management-ssh;bar") != {"foo", "management-ssh", "bar"}:
'''
insert = '''def test_untrusted_host_boundary() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    start = source.index("def prepare_participant(")
    end = source.index("\\ndef persist_new_host_keys", start)
    block = source[start:end]
    if "if not known_host_exists(participant):" not in block:
        fail("prepare_participant must reject missing persistent host trust")
    if "scan_host_keys(participant)" in block or "temporary_known_hosts" in block:
        fail("bulk sync must not TOFU an unknown management-ssh participant")


def test_helpers() -> None:
    if mod.parse_tags("foo;management-ssh;bar") != {"foo", "management-ssh", "bar"}:
'''
if anchor not in t:
    raise SystemExit("sync test helper anchor missing")
t = t.replace(anchor, insert, 1)
t = t.replace(
    "    test_remote_catalog_policy()\n    test_helpers()\n",
    "    test_remote_catalog_policy()\n    test_untrusted_host_boundary()\n    test_helpers()\n",
    1,
)
test.write_text(t, encoding="utf-8")

readme = Path("scripts/README.md")
r = readme.read_text(encoding="utf-8")
old = "- при первом PVE-side знакомстве с новым ожидаемым адресом staging-ом проверяет SSH host key и сохраняет его только после успешного общего preflight;"
new = "- не принимает неизвестные SSH host keys: первичное доверие только что созданному ожидаемому объекту выполняет `deploy-guest`, а bulk sync требует уже сохранённый persistent host key;"
if old not in r:
    raise SystemExit("scripts README sync trust bullet missing")
r = r.replace(old, new, 1)
readme.write_text(r, encoding="utf-8")

doc = Path("docs/28-management-ssh-keys.md")
d = doc.read_text(encoding="utf-8")
old = "- tag является необходимым, но не достаточным условием записи: дополнительно проверяются объект, адрес, SSH trust и management-контракт;"
new = "- tag является необходимым, но не достаточным условием записи: дополнительно проверяются объект, адрес, уже установленный persistent SSH trust и management-контракт; bulk `sync-management-keys` не выполняет TOFU неизвестного host key;"
if old not in d:
    raise SystemExit("docs28 tag trust bullet missing")
d = d.replace(old, new, 1)
old = "Offline guest не запускается автоматически только ради sync. Его состояние остаётся pending/error до следующей успешной синхронизации."
new = "Offline guest не запускается автоматически только ради sync. Его состояние остаётся pending/error до следующей успешной синхронизации. Неизвестный SSH host key также блокирует bulk sync: первичное доверие разрешено `deploy-guest` только для только что созданного ожидаемого VMID/IP."
if old not in d:
    raise SystemExit("docs28 offline paragraph missing")
d = d.replace(old, new, 1)
doc.write_text(d, encoding="utf-8")
