# OpenBao на 910 infra-manager

**Тип:** локальная спецификация службы
**Статус:** целевое состояние
**Назначение:** полностью описать, каким должен быть OpenBao на госте 910, чтобы по этому документу можно было реализовать, проверить, обновить и восстановить службу.

Этот документ задаёт **требуемое конечное состояние OpenBao на 910**.

Если код, `provision.yaml`, `rootfs/`, Ansible или текущий контейнер расходятся с этим документом, реализацию нужно привести к этой спецификации.

Общий архитектурный смысл OpenBao, его права и взаимодействие с другими компонентами определены в [`740-openbao.md`](../../../../docs/700-security/740-openbao.md). Этот файл определяет **как именно всё это должно быть реализовано на 910**.

## 1. Состав и данные

### Итоговая схема службы

На 910 должен работать один отдельный контейнер:

```text
openbao
```

Он должен предоставлять пять функций:

```text
OpenBao 910
├── KV v2
│   ├── PVE API secret
│   ├── Git read-only credential
│   └── Semaphore runtime secrets
│
├── SSH client CA
│   └── временные user certificates для Ansible
│
├── SSH host CA
│   └── host certificates управляемых Linux-гостей
│
├── AppRole
│   ├── служебные роли infra-manager
│   └── отдельная машинная роль каждого OTP-источника
│
└── SSH OTP
    └── одноразовый guest → guest доступ
```

OpenBao не должен выполнять функции Semaphore, Ansible, OpenTofu или PVE. Он предоставляет им защищённые секреты и операции доверия.

### Контейнер и исходные файлы

OpenBao должен быть описан в локальных файлах гостя 910.

Исходный Compose:

```text
infrastructure/guests/910-infra-manager/
└── rootfs/
    └── opt/
        └── infra-manager/
            └── compose/
                ├── docker-compose.yml
                └── openbao/
                    └── openbao.hcl
```

После Ansible эти файлы должны находиться внутри 910:

```text
/opt/infra-manager/compose/docker-compose.yml
/opt/infra-manager/compose/openbao/openbao.hcl
```

Контейнер должен использовать:

```text
name:     openbao
image:    ghcr.io/openbao/openbao:<version>
network:  host
restart:  unless-stopped
```

Точная версия задаётся в [`../provision.yaml`](../provision.yaml).

OpenBao должен запускаться обычным серверным режимом:

```text
server
-config=/openbao/config/openbao.hcl
```

Встроенный веб-интерфейс OpenBao должен быть включён:

```text
ui = true
```

Он используется человеком для просмотра и администрирования OpenBao и не заменяет машинный API. Основной пользовательский адрес — `https://192.168.9.10:8202/ui/`; браузер должен доверять проектному TLS CA. Публиковать отдельный незашифрованный сетевой вход только ради UI не требуется.

### Постоянное состояние

OpenBao должен использовать Raft.

Логический путь внутри контейнера:

```text
/openbao/file/raft
```

Постоянный каталог внутри 910:

```text
/mnt/persistent-state/openbao
```

Физический источник на PVE:

```text
/mnt/bindmounts/infra-manager/state/openbao
```

Compose должен подключать:

```text
/mnt/persistent-state/openbao
        ↓
/openbao/file
```

Идентификатор Raft-узла:

```text
infra-manager-910
```

Допустимая локальная ссылка для человека и служебного кода:

```text
/var/lib/persistent/openbao
    → /mnt/persistent-state/openbao
```

Raft должен содержать:

- KV v2;
- auth methods;
- AppRole;
- политики;
- SSH client CA;
- SSH host CA;
- роли подписи;
- SSH OTP engine;
- OTP-роли;
- прочую конфигурацию OpenBao.

Каталог Raft нельзя автоматически очищать, заменять новым пустым состоянием или повторно инициализировать при обычном обновлении 910.

## 2. Сеть и TLS

### Сетевые входы

OpenBao должен иметь **два разных API-входа**.

**Локальный административный вход**

Для 910 и PVE-only операций должен оставаться loopback listener:

```text
127.0.0.1:8200
```

Он используется:

- локальным кодом 910;
- PVE-only helper через `pct exec`;
- инициализацией;
- unseal;
- настройкой KV;
- настройкой SSH CA;
- синхронизацией AppRole и OTP.

Этот listener не должен публиковаться в сеть гостей.

Для него допустим локальный HTTP без TLS, поскольку он слушает только loopback:

```hcl
listener "tcp" {
  address         = "127.0.0.1:8200"
  cluster_address = "127.0.0.1:8201"
  tls_disable     = true
}
```

**Защищённый сетевой вход**

Для управляемых Linux-гостей и административного браузера должен существовать отдельный listener:

```text
192.168.9.10:8202
```

Он используется для:

- AppRole login исходного гостя;
- получения OTP;
- проверки OTP целевым гостем;
- встроенного OpenBao UI по пути `/ui/`.

Он **обязан использовать TLS**.

Целевая конфигурация:

```hcl
listener "tcp" {
  address       = "192.168.9.10:8202"
  tls_disable   = false
  tls_cert_file = "/openbao/tls/server.crt"
  tls_key_file  = "/openbao/tls/server.key"
}
```

Открывать `0.0.0.0:8200` или другой общий HTTP listener для гостей запрещено.

Сетевые правила должны разрешать `8202/tcp` только управляемым Linux-гостям, которым нужен OpenBao для OTP, и доверенной административной сети, из которой открывается UI. Для остальных клиентов порт должен быть закрыт.

UI использует те же механизмы аутентификации и полномочий OpenBao, что и API. Сам факт доступности `/ui/` не должен давать анонимного административного доступа.

### TLS для сетевого входа

Для сетевого API и UI должен использоваться отдельный TLS-сертификат OpenBao.

Целевой каталог внутри 910:

```text
/etc/infra-manager/openbao/tls/
├── ca.crt
├── server.crt
└── server.key
```

В контейнер он должен подключаться только для чтения:

```text
/etc/infra-manager/openbao/tls
        ↓
/openbao/tls
```

Требования:

- `server.key` доступен только root и процессу OpenBao;
- `server.crt` содержит SAN для `192.168.9.10`;
- сертификат также может содержать стабильное имя `infra-manager`;
- клиенты обязаны проверять цепочку через `ca.crt`;
- `tls_skip_verify` и эквивалентные отключения проверки запрещены.

**Центр TLS**

Закрытый ключ центра, подписывающего TLS-сертификат OpenBao, должен оставаться вне обычного 910 в PVE-only области.

Целевая структура на PVE:

```text
/mnt/bindmounts/infra-manager/pve-only/openbao-tls/
├── ca.key
└── ca.crt
```

Рабочий серверный комплект, доступный 910 только для чтения:

```text
/mnt/bindmounts/infra-manager/access/openbao-tls/
├── ca.crt
├── server.crt
└── server.key
```

Он должен быть доступен внутри 910 как:

```text
/mnt/pve-access/openbao-tls/
```

Ansible должен устанавливать или связывать эти файлы с:

```text
/etc/infra-manager/openbao/tls/
```

Закрытый ключ TLS CA внутрь 910 не передаётся.

Ротация `server.crt` не должна менять TLS CA без отдельного решения.

## 3. Инициализация и секреты

### Инициализация OpenBao

OpenBao не должен автоматически выполнять `sys/init` при каждом старте контейнера.

Первичная инициализация выполняется отдельной повторяемой операцией.

Целевая схема:

```text
secret_shares    = 1
secret_threshold = 1
```

После `sys/init`:

1. единственный unseal key сохраняется только в PVE-only области;
2. initial root token используется только для первоначальной настройки;
3. настраиваются все обязательные engines, auth methods, policies и roles;
4. рабочие bootstrap-секреты переносятся в KV v2;
5. initial root token отзывается;
6. постоянный root token нигде не сохраняется.

Если OpenBao уже инициализирован, повторная операция должна проверять существующее состояние и приводить конфигурацию к требованиям без создания нового Raft или нового CA.

### PVE-only данные OpenBao

Канонический каталог:

```text
/mnt/bindmounts/infra-manager/pve-only/openbao/
```

Он должен содержать только данные, необходимые PVE для доверенных операций до получения обычного доступа OpenBao.

Обязательные файлы:

```text
unseal.key
ssh-access.json
kv-access.json
```

**unseal.key**

`unseal.key`:

- никогда не хранится в Git;
- не подключается внутрь 910 как постоянный файл;
- используется только PVE-only helper;
- входит в обязательный резервный комплект.

**ssh-access.json**

Файл должен содержать ограниченные AppRole credentials для PVE-only операций:

```text
ssh-ca-config
ssh-signer
ssh-otp-config
```

Он не должен содержать root token.

**kv-access.json**

Файл должен содержать ограниченные AppRole credentials:

```text
kv-reader
kv-semaphore-writer
```

Он не должен давать административное управление OpenBao.

Машинные SecretID обычных гостей не должны накапливаться в этих PVE-only файлах как общий реестр.

### KV v2

Должен быть включён KV v2 mount:

```text
infra-secrets/
```

Обязательная логическая структура:

```text
infra-secrets/
├── pve/
│   └── api/
│       └── infra-manager
├── git/
│   └── github/
│       └── proxmox-read
└── services/
    └── semaphore
```

**PVE API**

Secret:

```text
infra-secrets/data/pve/api/infra-manager
```

Поля:

```text
endpoint
token_id
token_secret
```

**Git**

Secret:

```text
infra-secrets/data/git/github/proxmox-read
```

Поле:

```text
private_key
```

**Semaphore**

Secret:

```text
infra-secrets/data/services/semaphore
```

Поля:

```text
admin_password
access_key_encryption
api_token
timezone
```

Если Semaphore перевыпускает API token, новое значение должно быть записано обратно в этот secret до следующего восстановления рабочих файлов.

### Материализация рабочих секретов

Рабочие программы 910 не должны читать Raft напрямую.

После разблокировки OpenBao PVE-only механизм должен материализовать нужные значения во временную область:

```text
/run/infra-manager/secrets/
```

Обязательные рабочие файлы:

```text
pve-api.env
github_proxmox_repo_ed25519
semaphore-server.env
initial-admin-password
semaphore-api-token
.openbao-materialized
```

Требования:

- каталог временный;
- после перезагрузки он восстанавливается из OpenBao;
- файлы создаются атомарно;
- права минимальны;
- secrets не передаются через аргументы командной строки;
- secrets не выводятся в обычный журнал;
- `infra-runtime` получает каталог только для чтения.

До первоначальной миграции допускается:

```text
/run/infra-manager/bootstrap-secrets/
```

После успешной инициализации OpenBao этот каталог не должен оставаться постоянным рабочим источником.

## 4. SSH CA и служебный доступ

### SSH client CA

Должен существовать отдельный SSH secrets engine:

```text
ssh-client-signer/
```

Его CA создаётся внутри OpenBao и не экспортируется.

Открытая часть публикуется в 910:

```text
/etc/infra-manager/ca/ssh-client-ca.pub
```

Целевая роль административного входа:

```text
ssh-client-signer/roles/infra-manager
```

Она должна:

- выпускать только user certificate;
- запрещать host certificate;
- разрешать principal `root`;
- подписывать только поддерживаемые проектом SSH public keys;
- иметь TTL по умолчанию `15m`;
- не использоваться для `guest → guest`.

Ansible должен создавать временную пару ключей для каждого штатного административного запуска и получать для неё сертификат через эту роль.

Закрытый client CA key не должен покидать OpenBao.

### SSH host CA

Должен существовать отдельный SSH secrets engine:

```text
ssh-host-signer/
```

Он использует другой CA key, не совпадающий с client CA.

Открытая часть публикуется в 910:

```text
/etc/infra-manager/ca/ssh-host-ca.pub
```

Роль:

```text
ssh-host-signer/roles/managed-host
```

должна:

- выпускать только host certificate;
- запрещать user certificate;
- подписывать реальный Ed25519 host public key управляемого Linux-гостя;
- включать только principals, подтверждённые PVE;
- использовать TTL `720h`.

Перед подписью PVE обязан проверить:

```text
VMID
имя VM/LXC
административный IP
существование объекта
соответствие фактической конфигурации заявленной цели
```

Для DHCP адрес нельзя доверять только со слов гостя. Нужен отдельный доверенный механизм определения фактического адреса.

### Служебный AppRole infra-manager

Должен быть включён отдельный auth method:

```text
auth/infra-manager/
```

Он предназначен только для PVE/infra-manager служебных операций.

Обязательные роли:

```text
ssh-ca-config
ssh-signer
ssh-otp-config
kv-reader
kv-semaphore-writer
```

**ssh-ca-config**

Разрешает:

- читать список проектных SSH CA roles;
- создавать, обновлять и удалять только проектные роли client CA и host CA;
- проверять собственные capabilities.

Не разрешает:

- подписывать SSH keys;
- читать KV;
- управлять OTP;
- создавать произвольные auth methods.

**ssh-signer**

Разрешает только:

```text
ssh-client-signer/sign/infra-manager
ssh-host-signer/sign/managed-host
```

Не разрешает менять роли CA.

**ssh-otp-config**

Разрешает управлять только:

```text
ssh-otp/roles/guest-*
auth/machine/role/guest-*
```

и связанными узкими policies машинных источников.

Эта роль не должна:

- читать рабочий KV;
- подписывать client/host certificates;
- менять unrelated auth methods;
- получать root capabilities.

**kv-reader**

Разрешает читать только:

```text
pve/api/infra-manager
git/github/proxmox-read
services/semaphore
```

Широкий доступ ко всему `infra-secrets/*` запрещён.

**kv-semaphore-writer**

Разрешает читать и обновлять только:

```text
services/semaphore
```

Он не должен видеть PVE API secret или Git private key.

**Token TTL**

Служебные token должны быть короткоживущими.

Целевые значения для служебных ролей:

```text
token_ttl:     5m
token_max_ttl: 10m
```

Для роли настройки CA допустимо увеличить TTL до `10m/15m`, если операция действительно этого требует.

После операции token должен отзываться.

### Готовность после разблокировки

Состояние `sealed=false` само по себе не означает, что OpenBao уже готов принимать служебные запросы.

Так как используется встроенное Raft-хранилище с HA-механизмом, после запуска и разблокировки узлу нужно завершить выбор активного узла. До этого AppRole может отвечать ошибкой вида `local node not active but active cluster node not found`.

Поэтому штатный контур после unseal обязан ждать `/v1/sys/health` со статусом HTTP 200 и только после этого выполнять AppRole login, подпись SSH, чтение KV или OTP-операции. Standby-состояние HTTP 429 не считается готовностью.

### Восстановление одноузлового Raft

Если после разблокировки `/v1/sys/health` устойчиво остаётся в standby и активный узел отсутствует, обычный `Deploy Guest` не должен автоматически менять состав Raft.

Штатное восстановление выполняется через `Repair Guest` или операторскую команду `infra-manager repair` на PVE.

Перед изменением состава Raft контур обязан подтвердить:

- VMID и имя принадлежат роли `infra-manager`;
- используется встроенное хранилище `raft`;
- `node_id` равен `infra-manager`;
- `cluster_addr` и cluster listener используют `127.0.0.1:8201`;
- отсутствуют `retry_join`, `non_voter` и другие признаки многоузловой схемы;
- постоянное Raft-состояние существует;
- PVE-only unseal key сохранён.

После этого OpenBao останавливается, весь `state/openbao` копируется в PVE-only каталог `openbao/raft-recovery/<дата-время>`, и только затем создаётся одноразовый:

```json
[
  {
    "id": "infra-manager",
    "address": "127.0.0.1:8201",
    "non_voter": false
  }
]
```

Файл помещается в `<storage.path>/raft/peers.json`. После запуска OpenBao должен сам применить и удалить его, получить leadership и пройти обычные проверки KV, SSH CA и AppRole.

Такое восстановление не выполняет `sys/init`, не очищает Raft и не создаёт пустое хранилище. Резервная копия создаётся до изменения peer set.

### Восстановление служебных AppRole

Файлы `ssh-access.json` и `kv-access.json` содержат воспроизводимые служебные RoleID/SecretID, а не сами рабочие данные инфраструктуры.

Если сохранённый SecretID перестал проходить реальную проверку AppRole, штатный контур должен:

1. подтвердить, что Raft и SSH CA сохранны;
2. через PVE-only unseal key выпустить временный root token;
3. привести служебные AppRole и их политики к требуемому состоянию;
4. выпустить новые SecretID только для сломанного служебного контура;
5. атомарно обновить соответствующий PVE-only файл доступа;
6. повторно проверить AppRole и материализацию KV;
7. отозвать временный root token.

Такое восстановление не должно:

- выполнять повторный `sys/init`;
- очищать или заменять Raft;
- перезаписывать содержимое KV рабочими bootstrap-значениями;
- создавать новые SSH CA вместо существующих;
- ослаблять политики или CIDR-ограничения.

## 5. Межмашинный SSH OTP

### SSH OTP engine

Должен быть включён отдельный SSH secrets engine:

```text
ssh-otp/
```

Он не является SSH CA.

Он предназначен только для одноразовых паролей `guest → guest`.

Для каждого субъекта, имеющего одновременно:

```text
ssh / identity / self / issue
```

и хотя бы одну цель:

```text
ssh / guest / <targets> / connect-root
```

должна существовать отдельная роль:

```text
ssh-otp/roles/guest-<VMID>
```

**Параметры OTP-роли**

Роль должна использовать:

```text
key_type     = otp
default_user = root
port         = 22
cidr_list    = только разрешённые адреса целей
```

`cidr_list` строится из фактических административных IP после раскрытия селекторов `access.yaml`.

Каждая цель должна быть представлена минимально необходимой сетью, обычно:

```text
<IPv4>/32
```

Запрещены:

```text
0.0.0.0/0
произвольная management-сеть целиком
адреса, которых нет в access.yaml
```

### Машинный AppRole для OTP

Должен быть включён отдельный auth method:

```text
auth/machine/
```

Для каждого OTP-источника создаётся:

```text
auth/machine/role/guest-<VMID>
```

Его policy должна разрешать только:

```text
ssh-otp/creds/guest-<VMID>
```

с действиями, необходимыми для получения OTP.

Машинный token не должен иметь доступа к:

- `infra-secrets/*`;
- `ssh-client-signer/*`;
- `ssh-host-signer/*`;
- `ssh-otp/roles/*`;
- чужой `ssh-otp/creds/guest-*`;
- `auth/infra-manager/*`;
- настройке собственного AppRole;
- системным административным API OpenBao.

**Ограничения AppRole**

Для каждой машинной роли должны применяться:

```text
bind_secret_id = true
token_ttl      = 5m
token_max_ttl  = 10m
```

Если адрес источника статический, должны также применяться ограничения:

```text
secret_id_bound_cidrs = <source-ip>/32
token_bound_cidrs     = <source-ip>/32
```

Если источник использует DHCP, сначала должен быть определён доверенный способ получить его фактический адрес; ослаблять роль до произвольной сети только ради удобства запрещено.

### Выдача машинной идентичности гостю

Для каждого OTP-источника нужно получить:

```text
RoleID
SecretID
```

SecretID является секретом конкретной машины.

Он должен доставляться только соответствующему гостю через доверенный Ansible-контур.

Целевой файл на исходном госте:

```text
/etc/infra-manager/openbao/machine.env
```

Содержимое:

```text
OPENBAO_ADDR=https://192.168.9.10:8202
OPENBAO_ROLE_ID=<role-id>
OPENBAO_SECRET_ID=<secret-id>
OPENBAO_CA=/etc/infra-manager/openbao/ca.crt
OPENBAO_SSH_OTP_ROLE=guest-<VMID>
```

Требования:

```text
owner: root
group: root
mode: 0600
```

Публичный TLS CA должен находиться на госте:

```text
/etc/infra-manager/openbao/ca.crt
```

SecretID разных гостей не должен совпадать или копироваться между машинами.

PVE/910 не должны использовать общий машинный SecretID для нескольких источников.

### Получение OTP исходным гостем

Клиентский механизм гостя должен:

1. прочитать свой RoleID/SecretID;
2. выполнить AppRole login через:
   ```text
   https://192.168.9.10:8202/v1/auth/machine/login
   ```
3. проверить TLS через `ca.crt`;
4. получить короткоживущий OpenBao token;
5. запросить:
   ```text
   /v1/ssh-otp/creds/guest-<VMID>
   ```
6. передать точный адрес разрешённой цели и пользователя `root`;
7. получить одноразовый пароль;
8. использовать его только для одного SSH-подключения;
9. не сохранять OTP на диск;
10. удалить token и OTP из памяти после завершения операции.

Запрос OTP к адресу, отсутствующему в `cidr_list`, должен завершаться отказом OpenBao.

### Проверка OTP целевым гостем

Любой Linux-гость, являющийся допустимой целью OTP, должен иметь совместимый OpenBao SSH OTP helper.

Целевая логика:

```text
sshd
 ↓ keyboard-interactive
PAM
 ↓
OpenBao SSH helper
 ↓ TLS
https://192.168.9.10:8202
 ↓
ssh-otp/verify
```

Целевому гостю не выдаётся машинный AppRole только для проверки OTP.

Для helper должны быть заданы:

```text
OpenBao address: https://192.168.9.10:8202
SSH mount:       ssh-otp
CA certificate:  /etc/infra-manager/openbao/ca.crt
TLS verify:       enabled
```

SSH на цели должен:

- использовать PAM;
- разрешать keyboard-interactive только для OTP-механизма;
- не включать обычный постоянный пароль root;
- одновременно сохранять административный вход по client CA;
- использовать host certificate для проверки сервера клиентом.

### Синхронизация OTP из access.yaml

Должна существовать повторяемая операция `Sync SSH Access`, которая отвечает **только за состояние OpenBao**:

```text
access.yaml
    ↓
разрешённые OTP-источники и цели
    ↓
OpenBao auth/machine AppRole
    +
узкие policy источников
    +
OpenBao ssh-otp role
```

Алгоритм обязан:

1. прочитать `access.yaml`;
2. найти субъектов с `ssh/identity/issue`;
3. определить их правила `ssh/guest/connect-root`;
4. раскрыть групповые селекторы в конкретные Linux-гости;
5. получить доверенные административные IP целей;
6. создать или обновить `auth/machine/role/guest-<VMID>`;
7. создать или обновить узкую policy источника;
8. создать или обновить `ssh-otp/roles/guest-<VMID>`;
9. сделать `cidr_list` точным набором разрешённых целей;
10. удалить лишние OTP-роли, policy и AppRole, которых больше нет в `access.yaml`;
11. отозвать доступные token удалённой машинной роли.

`Sync SSH Access` должен быть идемпотентным и не должен:

- запускать `Deploy Guest`;
- устанавливать файлы на гости;
- выдавать гостю RoleID/SecretID;
- настраивать PAM или `sshd`;
- выполнять сквозные приёмочные SSH-тесты.

Настройка конкретного гостя выполняется только через `Deploy Guest`: он устанавливает TLS CA, OTP-клиент, OTP helper/PAM и при необходимости выдаёт собственные RoleID/SecretID гостя.

Сквозная проверка разрешённых и запрещённых соединений выполняется отдельным приёмочным сценарием:

```text
scripts/acceptance/verify-ssh-access.py
```

## 6. Запуск и жизненный цикл

### Разблокировка после запуска 910

На 910 должна существовать systemd-служба:

```text
infra-manager-openbao-startup-unseal.service
```

Исходник:

```text
rootfs/etc/systemd/system/infra-manager-openbao-startup-unseal.service.j2
```

Установленный файл:

```text
/etc/systemd/system/infra-manager-openbao-startup-unseal.service
```

Служба должна запускать:

```text
/usr/local/sbin/infra-manager-openbao-startup-unseal <PVE-node>
```

Порядок:

1. дождаться локального API `127.0.0.1:8200`;
2. прочитать `sys/seal-status`;
3. если OpenBao не инициализирован — не выполнять автоматический init и завершить службу ошибкой;
4. для любого `initialized=true` вызвать PVE-only helper;
5. если `sealed=true`, PVE использует `unseal.key` и разблокирует OpenBao;
6. если `sealed=false`, повторная разблокировка не нужна, но запуск всё равно продолжается;
7. подтвердить целевое состояние `initialized=true, sealed=false`;
8. материализовать рабочие KV secrets;
9. проверить доступность обоих SSH CA;
10. проверить наличие `ssh-otp` и `auth/machine`;
11. подтвердить TLS listener `192.168.9.10:8202`.

`sealed=false` означает, что OpenBao уже разблокирован, но само по себе не подтверждает готовность всего контура 910. Рабочие секреты и обязательные функции OpenBao всё равно должны быть восстановлены и проверены.

Unseal key внутрь 910 не передаётся.

### PVE-only helper

Команда OpenBao на PVE является единым комплектом:

```text
/usr/local/sbin/infra-manager-openbao-unseal
/usr/local/lib/infra-manager/openbao_host/
    __init__.py
    errors.py
    snippets.py
    tls.py
/etc/infra-manager/openbao-host.json
```

Нельзя обновлять только исполняемый файл без соответствующего пакета `openbao_host`. Перед первым обращением `Deploy Guest` к OpenBao для подписи временного SSH-ключа весь комплект должен быть приведён к версии выполняемой рабочей копии проекта. Только после этого допускается `--sign-client-key`, host signing и запуск Ansible.

Такой порядок позволяет автоматически восстановить PVE после незавершённого старого обновления, когда исполняемый файл и библиотека оказались разных версий.

### Обычный запуск

Compose должен запускать OpenBao:

```bash
docker compose \
  --env-file /opt/infra-manager/compose/.versions.env \
  -f /opt/infra-manager/compose/docker-compose.yml \
  up -d openbao
```

После запуска обязательны:

```text
container running
127.0.0.1:8200 отвечает
initialized=true
sealed=false
192.168.9.10:8202 принимает TLS
сертификат TLS валиден
рабочие secrets материализованы
```

Полный `infra-runtime` нельзя считать готовым до подтверждения этих условий.

### Обновление OpenBao

Версия меняется только через:

```text
provision.yaml
```

Штатное обновление должно:

1. сохранить существующий Raft;
2. обновить образ;
3. не выполнять повторный `sys/init`;
4. запустить новый контейнер с тем же постоянным состоянием;
5. выполнить unseal;
6. проверить KV;
7. проверить client CA;
8. проверить host CA;
9. проверить `auth/infra-manager`;
10. проверить `auth/machine`;
11. проверить `ssh-otp`;
12. проверить TLS listener;
13. подтвердить итог с PVE командой `infra-manager status`.

Если изменён `access.yaml`, после обновления нужно выполнить `Sync SSH Access`, чтобы привести OTP-роли и машинные AppRole OpenBao к новым правилам. Если изменена конфигурация конкретного гостя, его нужно обновить через `Deploy Guest`. Сквозная проверка соединений выполняется отдельно через `scripts/acceptance/verify-ssh-access.py`.

Ручное изменение конфигурации только внутри контейнера запрещено: после пересоздания контейнера оно исчезнет.

### Восстановление OpenBao

При потере LXC 910, но сохранном состоянии, OpenBao должен восстанавливаться без создания нового центра доверия.

Обязательный набор:

```text
state/openbao
+
pve-only/openbao/unseal.key
+
pve-only/openbao/ssh-access.json
+
pve-only/openbao/kv-access.json
+
PVE-only TLS CA
+
server TLS material или возможность безопасно перевыпустить его тем же CA
```

Порядок:

1. восстановить PVE bind mount;
2. создать новый LXC 910;
3. подключить `state` и `access`;
4. установить Compose/HCL из `rootfs`;
5. установить TLS server material;
6. запустить OpenBao на старом Raft;
7. выполнить PVE-only unseal;
8. проверить KV;
9. проверить оба SSH CA;
10. проверить `auth/infra-manager`;
11. проверить `auth/machine`;
12. проверить `ssh-otp`;
13. материализовать рабочие secrets;
14. проверить TLS listener;
15. синхронизировать OTP из актуального `access.yaml`;
16. выполнить полный статус.

Если существует прежний Raft, но OpenBao сообщает `initialized=false`, нельзя выполнять новый init до выяснения причины.

Если потерян Raft, нельзя считать новый пустой OpenBao обычным восстановлением: будут потеряны KV secrets и SSH CA.

Если потерян только машинный SecretID отдельного гостя, он должен быть отозван и выпущен заново без пересоздания OpenBao.

## 7. Критерии готовности

### Обязательные проверки

Готовая реализация должна автоматически подтверждать всё перечисленное ниже.

**Базовое состояние**

```text
OpenBao container running
initialized = true
sealed = false
Raft state persistent
root token not stored
```

**KV**

Проверить:

- mount `infra-secrets` существует как KV v2;
- все три обязательных logical secrets существуют;
- `kv-reader` читает только разрешённые secrets;
- `kv-semaphore-writer` не читает PVE/Git secrets;
- рабочие файлы материализуются после перезапуска.

**SSH CA**

Проверить:

- `ssh-client-signer` существует;
- `ssh-host-signer` существует;
- их public keys различаются;
- client CA выпускает user certificate;
- client CA не выпускает host certificate;
- host CA выпускает host certificate;
- host CA не выпускает user certificate;
- PVE проверяет цель до host signing;
- срок сертификатов соответствует политике.

**OTP и AppRole**

Проверить:

- `ssh-otp` существует;
- `auth/machine` существует;
- на каждый разрешённый источник существует ровно один AppRole;
- на каждый разрешённый источник существует ровно одна OTP-role;
- OTP targets точно совпадают с `access.yaml`;
- нет `0.0.0.0/0`;
- источник не может использовать чужую OTP-role;
- источник не может читать KV;
- источник не может подписывать SSH certificate;
- разрешённая цель выдаёт OTP;
- запрещённая цель не выдаёт OTP;
- один OTP нельзя использовать дважды;
- удалённое право перестаёт выдавать новый OTP.

**TLS**

Проверить:

- `192.168.9.10:8202` доступен по TLS;
- сертификат проверяется через проектный OpenBao TLS CA;
- неправильный CA приводит к отказу;
- просроченный сертификат приводит к отказу;
- HTTP на сетевом OTP-интерфейсе не принимается;
- административный loopback listener не публикуется в гостевую сеть.

**Восстановление**

Проверить повторяемым тестом:

- контейнер можно пересоздать без потери Raft;
- после unseal восстанавливаются рабочие secrets;
- CA fingerprints остаются прежними;
- OTP/AppRole сохраняются или корректно синхронизируются из `access.yaml`;
- новый root token после обычного восстановления не остаётся сохранённым.

## 8. Реализация и источники

### Что нужно привести к этой спецификации

Команда «настрой OpenBao по этому документу» означает, что нужно привести к описанному состоянию **весь технический контур**, а не только изменить HCL.

Как минимум нужно проверить и при необходимости изменить:

```text
infrastructure/guests/910-infra-manager/provision.yaml

infrastructure/guests/910-infra-manager/rootfs/
├── opt/infra-manager/compose/docker-compose.yml
├── opt/infra-manager/compose/openbao/openbao.hcl
└── etc/systemd/system/...

automation/ansible/roles/infra_manager/
automation/ansible/roles/linux_base/

scripts/infra-manager/infra_manager/openbao.py
scripts/infra-manager/host/openbao-unseal.py
scripts/infra-manager/jobs/

infrastructure/security/access.yaml
infrastructure/security/access.schema.yaml

scripts/validate_repo.py
scripts/tests/
```

Реализация должна включить:

- TLS listener OpenBao для гостей;
- генерацию и доставку TLS material;
- `ssh-otp`;
- `auth/machine`;
- `ssh-otp-config`;
- машинные policies/AppRole;
- синхронизацию ролей из `access.yaml`;
- доставку RoleID/SecretID источникам;
- OTP client на источниках;
- OTP helper/PAM на целях;
- проверку host CA при OTP SSH;
- обновление итогового status;
- отдельный приёмочный сценарий положительных и отрицательных SSH OTP-связей.

Реализация не считается законченной, пока все критерии раздела 7 не выполняются.

### Источники точных значений после реализации

Когда эта спецификация полностью реализована, машинные файлы должны совпадать с ней.

Точные исполняемые значения находятся в:

- [`../provision.yaml`](../provision.yaml);
- [`../rootfs/opt/infra-manager/compose/docker-compose.yml`](../rootfs/opt/infra-manager/compose/docker-compose.yml);
- [`../rootfs/opt/infra-manager/compose/openbao/openbao.hcl`](../rootfs/opt/infra-manager/compose/openbao/openbao.hcl);
- [`../rootfs/etc/systemd/system/`](../rootfs/etc/systemd/system/);
- Ansible-ролях;
- Python-коде OpenBao;
- `access.yaml`.

### Связанные документы

- [`../provision.yaml`](../provision.yaml) — машинное описание 910.
- [`data-and-access.md`](data-and-access.md) — постоянные области и доступы 910.
- [`system-services.md`](system-services.md) — systemd и служебные команды.
- [`infra-runtime.md`](infra-runtime.md) — Compose и рабочий контейнер.
- [`../../../../docs/700-security/720-ssh-access.md`](../../../../docs/700-security/720-ssh-access.md) — целевая схема SSH client CA, host CA и OTP.
- [`../../../../docs/700-security/740-openbao.md`](../../../../docs/700-security/740-openbao.md) — архитектурная роль OpenBao.
- [`../../../../docs/700-security/750-access-contract.md`](../../../../docs/700-security/750-access-contract.md) — правила `access.yaml`.
- [`../../../../docs/700-security/780-implementation-status.md`](../../../../docs/700-security/780-implementation-status.md) — текущие расхождения реализации с целевой схемой.
- [`../../../../docs/800-operations/830-recovery.md`](../../../../docs/800-operations/830-recovery.md) — общий аварийный процесс.
