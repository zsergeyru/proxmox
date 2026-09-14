# Инициализация нового Proxmox VE host

## Цель

Нужен отдельный публичный bootstrap-скрипт `init-pve.sh`, который подготавливает новый Proxmox VE host до состояния, из которого он уже способен самостоятельно читать desired state проекта и разворачивать VM/LXC по `guest.yaml`.

Скрипт должен храниться только в публичном репозитории:

```text
zsergeyru/proxmox-bootstrap/init-pve.sh
```

В приватном `zsergeyru/proxmox` хранится эта спецификация, а не executable-копия.

## Исходное состояние

```text
новый компьютер
└── установлен Proxmox VE
```

Не предполагается наличие:

- Git checkout приватного репозитория;
- AI/301;
- Hermes;
- Proximo;
- GitHub credentials;
- template `9000`.

## Целевое состояние

После инициализации PVE должен иметь:

```text
PVE
├── базовые bootstrap packages
├── отдельный read-only GitHub Deploy Key
├── read-only checkout zsergeyru/proxmox
├── PVE roles/users/tokens проекта
├── resource pool managed
├── template 9000 tpl-debian13
└── deploy-guest
```

После этого создание обычного гостя не зависит от AI:

```bash
deploy-guest 311 --apply
```

## Почему Git устанавливается на PVE

Для приватного `zsergeyru/proxmox` выбран SSH Deploy Key. GitHub Deploy Key является SSH credential для Git transport. Он не является bearer-token для HTTP API и не может использоваться обычным `curl` к `raw.githubusercontent.com` или GitHub Contents REST API.

Поэтому штатная схема PVE:

```text
GitHub Deploy Key
→ SSH
→ git clone/fetch
→ private zsergeyru/proxmox
```

Установка пакета `git` на PVE считается допустимой и полезной: он нужен только как read-only transport/source-of-truth client, а не как сервис.

Если когда-нибудь будет принято решение не устанавливать Git, потребуется другой тип credential для HTTPS/API (GitHub App installation token или ограниченный access token). SSH Deploy Key сам по себе эту задачу не решает.

## PVE GitHub identity

`init-pve.sh` создаёт отдельную пару ключей, предназначенную только для PVE:

```text
/etc/proxmox-deployer/ssh/github_proxmox_ed25519
/etc/proxmox-deployer/ssh/github_proxmox_ed25519.pub
```

Правила:

- private key остаётся только на PVE;
- public key показывается оператору;
- оператор добавляет его в `zsergeyru/proxmox → Settings → Deploy keys`;
- **Allow write access не включается**;
- ключ PVE не переиспользуется в `301-ai-control`;
- ключ `301` не копируется на PVE.

SSH config может использовать отдельный alias:

```text
Host github-proxmox-pve
    HostName github.com
    User git
    IdentityFile /etc/proxmox-deployer/ssh/github_proxmox_ed25519
    IdentitiesOnly yes
    StrictHostKeyChecking yes
```

Тогда remote:

```text
git@github-proxmox-pve:zsergeyru/proxmox.git
```

## Локальный checkout

Целевой каталог:

```text
/var/lib/proxmox-deployer/repo
```

PVE не изменяет этот checkout и не делает commit/push. Перед чтением manifest deployer выполняет безопасный update (`fetch` + fast-forward/reset к утверждённой remote branch согласно выбранной policy).

При желании можно использовать shallow/sparse checkout, но это оптимизация, а не требование первой версии. Полный read-only checkout инфраструктурного repo допустим.

## Этапы `init-pve.sh`

Скрипт должен быть идемпотентным и выполнять этапы независимо.

### 1. Проверка PVE

Проверить:

- запуск от `root`;
- наличие `pveversion`, `qm`, `pct`, `pvesh`, `pveum`, `pvesm`;
- node name;
- основные storage/bridge prerequisites;
- доступ в интернет к публичному bootstrap repo/GitHub.

### 2. Bootstrap packages

Установить только нужные инструменты, например:

```text
git
openssh-client
python3
python3-yaml
curl
jq
ca-certificates
```

Не устанавливать Docker и AI runtime на PVE.

### 3. Read-only GitHub Deploy Key

Если пары ещё нет — создать. Если есть — не регенерировать.

Показать оператору `.pub` и инструкцию регистрации.

Если key ещё не зарегистрирован, это не должно разрушать другие завершённые этапы. Повторный запуск продолжает с текущего состояния.

### 4. Checkout приватного repo

После регистрации ключа:

```text
ssh authentication check
→ git clone/fetch zsergeyru/proxmox
→ /var/lib/proxmox-deployer/repo
```

После этого PVE может читать `guest.yaml` без участия AI.

### 5. PVE roles/users/tokens и pool

Скрипт создаёт утверждённые проектом сущности управления, если их ещё нет:

```text
resource pool: managed
Proximo management user/token/roles/ACL
и другие host-side identities, явно принятые документацией проекта
```

Нельзя автоматически выдавать `PVEAdmin` на `/` только ради упрощения.

Создание/ротация secret token должна соблюдать правило: secret показывается один раз, хранится только там, где требуется runtime, и не попадает в Git/logs.

Конкретные ACL должны соответствовать архитектурным решениям проекта, а не быть зашиты произвольным набором прав.

### 6. Создание template 9000

`init-pve.sh` не дублирует builder-код. Он скачивает и запускает канонический публичный:

```text
zsergeyru/proxmox-bootstrap/create-template.sh
```

Если `9000 tpl-debian13` уже существует и соответствует ожидаемой версии/policy, повторно не пересоздаёт его без явного rebuild-флага.

### 7. Установка PVE deployer

После появления приватного checkout устанавливается/обновляется PVE-side команда:

```text
/usr/local/sbin/deploy-guest
```

Она читает:

```text
/var/lib/proxmox-deployer/repo/guests/<VMID>-*/guest.yaml
```

Спецификация manifest: [`guest-manifest.md`](./guest-manifest.md).

## Предлагаемый пользовательский сценарий

Первый запуск:

```bash
curl -fsSL https://raw.githubusercontent.com/zsergeyru/proxmox-bootstrap/main/init-pve.sh \
  -o /root/init-pve.sh
chmod +x /root/init-pve.sh
/root/init-pve.sh
```

Скрипт может завершить всё, что не требует GitHub authorization, затем показать:

```text
Register this read-only Deploy Key in zsergeyru/proxmox:
ssh-ed25519 AAAA... pve-proxmox-readonly
```

После регистрации оператор повторяет:

```bash
/root/init-pve.sh
```

И второй запуск завершает Git checkout/deployer и остальные зависимые шаги.

## Связь с AI Control

`init-pve.sh` и `deploy-guest` не зависят от `301`.

После базовой инициализации можно выполнить:

```text
deploy-guest 301 --apply
→ prepare-ai-control.sh
→ install-ai-agent.sh --agent hermes
```

Таким образом AI становится потребителем уже готовой инфраструктурной платформы, а не обязательным условием восстановления PVE.

## Security boundary

PVE получает read-only доступ к приватному Git и поэтому содержит чувствительный private Deploy Key. Однако этот key:

- не даёт write в GitHub;
- не является personal account credential;
- относится только к одному repo;
- не даёт права на Proxmox — root-доступ deployer получает только потому, что запускается локально на PVE человеком/утверждённой автоматизацией.

PVE private key, API token secrets и другие credentials никогда не помещаются в Git.

## Главный принцип

> Новый PVE после одного публичного `init-pve.sh` и однократной регистрации read-only Deploy Key должен стать способен самостоятельно читать `guest.yaml`, создавать template и разворачивать гости без зависимости от AI control plane.
