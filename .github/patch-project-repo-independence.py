from pathlib import Path

p = Path("scripts/pve/deploy-guest.py")
text = p.read_text(encoding="utf-8")

old = 'PROJECT_CONFIG_MARKER = "# managed-by=proxmox-deployer project-repo-read"\nMANAGEMENT_PRIVATE ='
new = '''PROJECT_CONFIG_MARKER = "# managed-by=proxmox-deployer project-repo-read"
PROJECT_GIT_CONFIG = "/etc/gitconfig"
PROJECT_GIT_BEGIN = "# BEGIN PROXMOX-PROJECT-REPO-READ"
PROJECT_GIT_END = "# END PROXMOX-PROJECT-REPO-READ"
MANAGEMENT_PRIVATE ='''
if old not in text:
    raise SystemExit("project constants anchor missing")
text = text.replace(old, new, 1)

start = text.index("def project_repo_state(")
end = text.index("\ndef bootstrap_check", start)
new_state = r"""def project_git_config_block() -> str:
    return (
        f"{PROJECT_GIT_BEGIN}\n"
        f'[url "{PROJECT_REWRITTEN_URL}"]\n'
        f"    insteadOf = {PROJECT_URL}\n"
        f"{PROJECT_GIT_END}\n"
    )


def project_repo_state(address: str, expected_fp: str) -> str:
    expected_block_b64 = base64.b64encode(project_git_config_block().encode()).decode()
    script = f'''
set -eu
cred={shlex.quote(PROJECT_CREDENTIAL)}
kh={shlex.quote(PROJECT_KNOWN_HOSTS)}
cfg={shlex.quote(PROJECT_SSH_CONFIG)}
gitcfg={shlex.quote(PROJECT_GIT_CONFIG)}
present=0
for f in "$cred" "$kh" "$cfg"; do [ -e "$f" ] && present=$((present+1)); done
begin_count=0; end_count=0
if [ -e "$gitcfg" ]; then
    [ -f "$gitcfg" ] && [ ! -L "$gitcfg" ] || {{ printf 'BROKEN\\n'; exit 0; }}
    begin_count="$(grep -Fxc {shlex.quote(PROJECT_GIT_BEGIN)} "$gitcfg" || true)"
    end_count="$(grep -Fxc {shlex.quote(PROJECT_GIT_END)} "$gitcfg" || true)"
fi
if [ "$present" -eq 0 ] && [ "$begin_count" -eq 0 ] && [ "$end_count" -eq 0 ]; then printf 'ABSENT\\n'; exit 0; fi
[ "$present" -eq 3 ] || {{ printf 'BROKEN\\n'; exit 0; }}
[ "$begin_count" -eq 1 ] && [ "$end_count" -eq 1 ] || {{ printf 'BROKEN\\n'; exit 0; }}
[ -f "$cred" ] && [ ! -L "$cred" ] || {{ printf 'BROKEN\\n'; exit 0; }}
[ -f "$kh" ] && [ ! -L "$kh" ] || {{ printf 'BROKEN\\n'; exit 0; }}
[ -f "$cfg" ] && [ ! -L "$cfg" ] || {{ printf 'BROKEN\\n'; exit 0; }}
[ "$(stat -c '%U:%G:%a' "$cred")" = "root:root:600" ] || {{ printf 'BROKEN\\n'; exit 0; }}
[ "$(stat -c '%U:%G:%a' "$kh")" = "root:root:644" ] || {{ printf 'BROKEN\\n'; exit 0; }}
[ "$(stat -c '%U:%G:%a' "$cfg")" = "root:root:644" ] || {{ printf 'BROKEN\\n'; exit 0; }}
grep -Fxq {shlex.quote(PROJECT_CONFIG_MARKER)} "$cfg" || {{ printf 'BROKEN\\n'; exit 0; }}
begin_line="$(grep -nFx {shlex.quote(PROJECT_GIT_BEGIN)} "$gitcfg" | cut -d: -f1)"
end_line="$(grep -nFx {shlex.quote(PROJECT_GIT_END)} "$gitcfg" | cut -d: -f1)"
[ "$begin_line" -lt "$end_line" ] || {{ printf 'BROKEN\\n'; exit 0; }}
actual_block="$(sed -n "${{begin_line}},${{end_line}}p" "$gitcfg")"
expected_block="$(printf '%s' {shlex.quote(expected_block_b64)} | base64 -d)"
[ "$actual_block" = "$expected_block" ] || {{ printf 'BROKEN\\n'; exit 0; }}
pub="$(ssh-keygen -y -f "$cred" 2>/dev/null)" || {{ printf 'BROKEN\\n'; exit 0; }}
fp="$(printf '%s\\n' "$pub" | ssh-keygen -lf - -E sha256 2>/dev/null | awk '{{print $2}}')"
[ "$fp" = {shlex.quote(expected_fp)} ] || {{ printf 'FOREIGN\\n'; exit 0; }}
auth="$(ssh -F "$cfg" -T {shlex.quote(PROJECT_ALIAS)} 2>&1 || true)"
printf '%s\\n' "$auth" | grep -qi 'successfully authenticated' || {{ printf 'BROKEN\\n'; exit 0; }}
if command -v git >/dev/null 2>&1; then
    git ls-remote --heads {shlex.quote(PROJECT_URL)} >/dev/null 2>&1 || {{ printf 'BROKEN\\n'; exit 0; }}
fi
printf 'VALID\\n'
'''
    proc = ssh_run(address, script, timeout=45)
    return proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else "BROKEN"

"""
text = text[:start] + new_state + text[end:]

start = text.index("def project_ssh_config_text()")
end = text.index("\ndef apt_install", start)
new_apply = r"""def project_ssh_config_text() -> str:
    return (
        f"{PROJECT_CONFIG_MARKER}\n"
        f"Host {PROJECT_ALIAS}\n"
        "    HostName github.com\n"
        "    User git\n"
        f"    IdentityFile {PROJECT_CREDENTIAL}\n"
        "    IdentitiesOnly yes\n"
        f"    UserKnownHostsFile {PROJECT_KNOWN_HOSTS}\n"
        "    StrictHostKeyChecking yes\n"
        "    BatchMode yes\n"
    )


def apply_project_repo_read(desired: Desired, private_data: bytes) -> None:
    expected_fp = project_master_fingerprint()
    if private_key_fingerprint(private_data) != expected_fp:
        raise DeployError("Project Git READ private key не соответствует canonical public key")
    known_hosts = GITHUB_KNOWN_HOSTS.read_text(encoding="utf-8")
    payload = {
        "credential": base64.b64encode(private_data).decode(),
        "known_hosts": known_hosts,
        "ssh_config": project_ssh_config_text(),
        "git_block": project_git_config_block(),
    }
    remote = r'''
set -eu
payload="$(mktemp)"; trap 'rm -f "$payload"' EXIT; cat >"$payload"
python3 - "$payload" <<'PY'
import base64, json, os, pathlib, sys, tempfile
data=json.load(open(sys.argv[1], encoding="utf-8"))
items=[
(pathlib.Path("/etc/proxmox-guest/credentials/github-proxmox-read"),base64.b64decode(data["credential"]),0o600,0o700),
(pathlib.Path("/etc/proxmox-guest/ssh/github-proxmox-known_hosts"),data["known_hosts"].encode(),0o644,0o700),
(pathlib.Path("/etc/ssh/ssh_config.d/90-proxmox-project-repo-read.conf"),data["ssh_config"].encode(),0o644,0o755)]
for path,content,mode,parent_mode in items:
    if path.is_symlink(): raise SystemExit(f"refuse symlink {path}")
    path.parent.mkdir(parents=True,exist_ok=True); os.chown(path.parent,0,0); os.chmod(path.parent,parent_mode)
    fd,tmp_name=tempfile.mkstemp(prefix=f".{path.name}.",dir=path.parent); tmp=pathlib.Path(tmp_name)
    try:
        with os.fdopen(fd,"wb") as fh: fh.write(content); fh.flush(); os.fsync(fh.fileno())
        os.chown(tmp,0,0); os.chmod(tmp,mode); os.replace(tmp,path)
    finally:
        if tmp.exists(): tmp.unlink()

gitcfg=pathlib.Path("/etc/gitconfig")
if gitcfg.is_symlink() or (gitcfg.exists() and not gitcfg.is_file()): raise SystemExit("unsafe /etc/gitconfig")
current=gitcfg.read_text(encoding="utf-8") if gitcfg.exists() else ""
lines=current.splitlines()
begin="# BEGIN PROXMOX-PROJECT-REPO-READ"; end="# END PROXMOX-PROJECT-REPO-READ"
bi=[i for i,v in enumerate(lines) if v==begin]; ei=[i for i,v in enumerate(lines) if v==end]
block=data["git_block"].rstrip("\n").splitlines()
if not bi and not ei:
    out=lines + ([""] if lines and lines[-1] else []) + block
elif len(bi)==1 and len(ei)==1 and bi[0] < ei[0]:
    out=lines[:bi[0]] + block + lines[ei[0]+1:]
else:
    raise SystemExit("ambiguous project repo block in /etc/gitconfig")
fd,tmp_name=tempfile.mkstemp(prefix=".gitconfig.",dir=gitcfg.parent); tmp=pathlib.Path(tmp_name)
try:
    with os.fdopen(fd,"w",encoding="utf-8") as fh: fh.write("\n".join(out).rstrip("\n")+"\n"); fh.flush(); os.fsync(fh.fileno())
    os.chown(tmp,0,0); os.chmod(tmp,0o644); os.replace(tmp,gitcfg)
finally:
    if tmp.exists(): tmp.unlink()
PY
auth="$(ssh -F /etc/ssh/ssh_config.d/90-proxmox-project-repo-read.conf -T github-proxmox-read 2>&1 || true)"
printf '%s\n' "$auth" | grep -qi 'successfully authenticated'
if command -v git >/dev/null 2>&1; then git ls-remote --heads 'git@github.com:zsergeyru/proxmox.git' >/dev/null; fi
'''
    ssh_run(desired.management_ip, remote, input_text=json.dumps(payload), timeout=90)
    if project_repo_state(desired.management_ip, expected_fp) != "VALID":
        raise DeployError("Project Git READ final verify не пройден")


def remove_project_repo_read(desired: Desired) -> None:
    expected_fp = project_master_fingerprint()
    state = project_repo_state(desired.management_ip, expected_fp)
    if state == "ABSENT":
        return
    if state != "VALID":
        raise BlockedError(f"Project Git READ REMOVE запрещён для state={state}")
    remote = r'''
set -eu
python3 - <<'PY'
import os, pathlib, tempfile
for raw in (
    "/etc/proxmox-guest/credentials/github-proxmox-read",
    "/etc/proxmox-guest/ssh/github-proxmox-known_hosts",
    "/etc/ssh/ssh_config.d/90-proxmox-project-repo-read.conf",
):
    path=pathlib.Path(raw)
    if path.is_symlink() or (path.exists() and not path.is_file()): raise SystemExit(f"unsafe managed path {path}")
    if path.exists(): path.unlink()
gitcfg=pathlib.Path("/etc/gitconfig")
if gitcfg.is_symlink() or (gitcfg.exists() and not gitcfg.is_file()): raise SystemExit("unsafe /etc/gitconfig")
if gitcfg.exists():
    lines=gitcfg.read_text(encoding="utf-8").splitlines()
    begin="# BEGIN PROXMOX-PROJECT-REPO-READ"; end="# END PROXMOX-PROJECT-REPO-READ"
    bi=[i for i,v in enumerate(lines) if v==begin]; ei=[i for i,v in enumerate(lines) if v==end]
    if len(bi)!=1 or len(ei)!=1 or bi[0]>=ei[0]: raise SystemExit("ambiguous project repo block in /etc/gitconfig")
    out=lines[:bi[0]]+lines[ei[0]+1:]
    fd,tmp_name=tempfile.mkstemp(prefix=".gitconfig.",dir=gitcfg.parent); tmp=pathlib.Path(tmp_name)
    try:
        with os.fdopen(fd,"w",encoding="utf-8") as fh: fh.write(("\n".join(out).rstrip("\n")+"\n") if out else ""); fh.flush(); os.fsync(fh.fileno())
        os.chown(tmp,0,0); os.chmod(tmp,0o644); os.replace(tmp,gitcfg)
    finally:
        if tmp.exists(): tmp.unlink()
PY
'''
    ssh_run(desired.management_ip, remote, timeout=30)
    if project_repo_state(desired.management_ip, expected_fp) != "ABSENT":
        raise DeployError("Project Git READ REMOVE final verify не пройден")

"""
text = text[:start] + new_apply + text[end:]
p.write_text(text, encoding="utf-8")

test = Path("scripts/tests/test-deploy-guest.py")
t = test.read_text(encoding="utf-8")
anchor = '''    text = SOURCE.read_text(encoding="utf-8")
    assert "CERT_NONE" not in text
'''
insert = '''    text = SOURCE.read_text(encoding="utf-8")
    assert "PROJECT_GIT_BEGIN" in text and "PROJECT_GIT_END" in text
    state_start = text.index("def project_repo_state(")
    state_end = text.index("\\ndef bootstrap_check", state_start)
    state_block = text[state_start:state_end]
    assert "command -v git" in state_block
    assert "successfully authenticated" in state_block
    assert "git ls-remote" in state_block
    assert "CERT_NONE" not in text
'''
if anchor not in t:
    raise SystemExit("deploy project test anchor missing")
t = t.replace(anchor, insert, 1)
test.write_text(t, encoding="utf-8")

doc = Path("docs/31-deploy-guest.md")
d = doc.read_text(encoding="utf-8")
old = '''→ обеспечить SSH alias github-proxmox-read
→ обеспечить точный Git URL rewrite только для zsergeyru/proxmox
→ verify git ls-remote по каноническому URL
'''
new = '''→ обеспечить SSH alias github-proxmox-read
→ обеспечить точный managed block Git URL rewrite только для zsergeyru/proxmox
→ verify GitHub SSH authentication этим credential без требования установленного Git client
→ если Git client уже есть: дополнительно verify git ls-remote по каноническому URL
→ после Bootstrap capability `git`: final verify обязательно включает git ls-remote
'''
if old not in d:
    raise SystemExit("docs31 project verify sequence missing")
d = d.replace(old, new, 1)
doc.write_text(d, encoding="utf-8")

doc = Path("docs/33-guest-bootstrap-and-provisioning.md")
d = doc.read_text(encoding="utf-8")
old = "означает фиксированный read-only доступ только к `zsergeyru/proxmox`. Это отдельный credential, не management SSH key и не capability `git`."
new = "означает фиксированный read-only credential/SSH transport только к `zsergeyru/proxmox`. Это не management SSH key и не capability `git`: handler не устанавливает Git client, а при его наличии дополнительно проверяет `git ls-remote`."
if old not in d:
    raise SystemExit("docs33 project independence paragraph missing")
d = d.replace(old, new, 1)
doc.write_text(d, encoding="utf-8")
