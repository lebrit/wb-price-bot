#!/usr/bin/env bash
set -Eeuo pipefail

APP_NAME="wb-price-bot"
APP_TITLE="WB Price Bot"
GITHUB_REPOSITORY="${WB_GITHUB_REPOSITORY:-lebrit/wb-price-bot}"
APP_ROOT="/opt/${APP_NAME}"
RELEASES_DIR="${APP_ROOT}/releases"
CURRENT_LINK="${APP_ROOT}/current"
CONFIG_DIR="/etc/${APP_NAME}"
CONFIG_FILE="${CONFIG_DIR}/config.env"
SECRETS_DIR="${CONFIG_DIR}/secrets"
DATA_DIR="/var/lib/${APP_NAME}"
BACKUP_DIR="/var/backups/${APP_NAME}"
WRAPPER="/usr/local/bin/${APP_NAME}"
LOCK_FILE="/run/lock/${APP_NAME}.lock"
CONTAINER_NAME="wb-price-bot"
AUTH_CONTAINER_NAME="wb-price-bot-auth"
CADDY_CONTAINER_NAME="wb-price-bot-caddy"
PROMPT_FD=""
COMPOSE_MODE=""
LOCKED=0
LAST_BACKUP_PATH=""

say() { printf '%s\n' "$*"; }
warn() { printf 'Внимание: %s\n' "$*" >&2; }
die() { printf 'Ошибка: %s\n' "$*" >&2; exit 1; }

require_root() {
  [[ "${EUID}" -eq 0 ]] || die "запустите через sudo"
}

open_prompt_tty() {
  if [[ -n "${PROMPT_FD}" ]]; then
    return 0
  fi
  if { exec 9<>/dev/tty; } 2>/dev/null && [[ -t 9 ]]; then
    PROMPT_FD=9
    return 0
  fi
  return 1
}

read_prompt() {
  local __target="$1" prompt="$2" default="${3:-}" input_value
  open_prompt_tty || die "интерактивному меню нужен терминал; подключитесь через ssh -t"
  if [[ -n "${default}" ]]; then
    printf '%s [%s]: ' "${prompt}" "${default}" >&9
  else
    printf '%s: ' "${prompt}" >&9
  fi
  IFS= read -r -u 9 input_value
  input_value="${input_value:-${default}}"
  printf -v "${__target}" '%s' "${input_value}"
}

read_secret() {
  local __target="$1" prompt="$2" input_value
  open_prompt_tty || die "для ввода секрета нужен терминал"
  printf '%s: ' "${prompt}" >&9
  IFS= read -r -s -u 9 input_value
  printf '\n' >&9
  printf -v "${__target}" '%s' "${input_value}"
}

confirm_phrase() {
  local expected="$1" prompt="$2" value
  read_prompt value "${prompt} (введите ${expected})"
  [[ "${value}" == "${expected}" ]]
}

lock_operations() {
  if [[ "${LOCKED}" -eq 1 ]]; then
    return 0
  fi
  mkdir -p "$(dirname "${LOCK_FILE}")"
  exec 8>"${LOCK_FILE}"
  flock -n 8 || die "установщик уже запущен в другом процессе"
  LOCKED=1
}

ensure_base_tools() {
  export DEBIAN_FRONTEND=noninteractive
  local missing=()
  for command in curl tar openssl python3 flock; do
    command -v "${command}" >/dev/null 2>&1 || missing+=("${command}")
  done
  if ((${#missing[@]})); then
    apt-get update
    apt-get install -y ca-certificates curl tar gzip openssl python3 util-linux
  fi
}

detect_compose() {
  if docker compose version >/dev/null 2>&1; then
    COMPOSE_MODE="plugin"
  elif command -v docker-compose >/dev/null 2>&1; then
    local legacy_version
    legacy_version="$(docker-compose version --short 2>/dev/null | sed 's/[^0-9.].*$//' || true)"
    if [[ -n "${legacy_version}" ]] \
      && [[ "$(printf '%s\n%s\n' "1.27.0" "${legacy_version}" | sort -V | head -n1)" == "1.27.0" ]]; then
      COMPOSE_MODE="legacy"
    else
      return 1
    fi
  else
    return 1
  fi
}

ensure_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    say "Устанавливаю Docker из репозитория Ubuntu/Debian…"
    apt-get update
    apt-get install -y docker.io
  fi
  if ! detect_compose; then
    apt-get update
    if apt-cache show docker-compose-v2 >/dev/null 2>&1; then
      apt-get install -y docker-compose-v2
    elif apt-cache show docker-compose-plugin >/dev/null 2>&1; then
      apt-get install -y docker-compose-plugin
    else
      apt-get install -y docker-compose
    fi
    detect_compose || die "Docker Compose не установлен"
  fi
  systemctl enable --now docker >/dev/null 2>&1 || true
  docker info >/dev/null 2>&1 || die "служба Docker недоступна"
}

compose_at() {
  local release="$1"
  local auth_domain app_version
  shift
  [[ -f "${release}/VERSION" ]] || { warn "в каталоге релиза нет VERSION"; return 1; }
  export WB_CONFIG_FILE="${CONFIG_FILE}"
  export WB_DATA_DIR="${DATA_DIR}"
  export WB_SECRETS_DIR="${SECRETS_DIR}"
  export COMPOSE_PROJECT_NAME="wb-price-bot"
  auth_domain="$(read_config_value AUTH_DOMAIN 2>/dev/null || true)"
  export AUTH_DOMAIN="${auth_domain:-localhost}"
  app_version="$(tr -d '[:space:]' <"${release}/VERSION")" || return 1
  [[ "${app_version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] \
    || { warn "в каталоге релиза указан неверный VERSION"; return 1; }
  export APP_VERSION="${app_version}"
  if [[ "${COMPOSE_MODE}" == "plugin" ]]; then
    docker compose -f "${release}/compose.yaml" "$@"
  else
    docker-compose -f "${release}/compose.yaml" "$@"
  fi
}

compose_current() {
  [[ -f "${CURRENT_LINK}/compose.yaml" ]] || die "приложение ещё не установлено"
  compose_at "${CURRENT_LINK}" "$@"
}

github_curl() {
  local token="$1" url="$2" output="$3"
  local config
  config="$(mktemp)" || return 1
  chmod 600 "${config}" || { rm -f "${config}"; return 1; }
  if ! {
    printf 'url = "%s"\n' "${url}"
    [[ -n "${token}" ]] && printf 'header = "Authorization: Bearer %s"\n' "${token}"
    printf 'header = "Accept: application/vnd.github+json"\n'
    printf 'header = "X-GitHub-Api-Version: 2022-11-28"\n'
    printf 'location\nfail\nsilent\nshow-error\n'
    printf 'output = "%s"\n' "${output}"
  } >"${config}"; then
    rm -f "${config}" "${output}"
    return 1
  fi
  if ! curl --config "${config}"; then
    rm -f "${config}" "${output}"
    return 1
  fi
  rm -f "${config}"
}

download_latest_release() {
  local token="$1" destination="$2" metadata tag archive
  metadata="$(mktemp)"
  archive="$(mktemp --suffix=.tar.gz)"
  if ! github_curl "${token}" \
    "https://api.github.com/repos/${GITHUB_REPOSITORY}/releases/latest" "${metadata}"; then
    rm -f "${metadata}" "${archive}"
    die "не удалось получить последний GitHub Release"
  fi
  tag="$(python3 - "${metadata}" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as file:
    print(json.load(file).get("tag_name", ""))
PY
)"
  [[ -n "${tag}" ]] || die "у последнего GitHub Release нет tag_name"
  say "Скачиваю ${GITHUB_REPOSITORY} ${tag}…" >&2
  github_curl "${token}" \
    "https://api.github.com/repos/${GITHUB_REPOSITORY}/tarball/${tag}" "${archive}" \
    || die "не удалось скачать архив релиза"
  mkdir -p "${destination}"
  tar -xzf "${archive}" --strip-components=1 -C "${destination}"
  rm -f "${metadata}" "${archive}"
  [[ -f "${destination}/VERSION" && -f "${destination}/compose.yaml" ]] \
    || die "архив релиза неполный"
}

source_tree_from_script() {
  local script_dir root
  script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
  root="$(cd -- "${script_dir}/.." && pwd -P)"
  if [[ -f "${root}/VERSION" && -f "${root}/compose.yaml" && -d "${root}/src" ]]; then
    printf '%s' "${root}"
    return 0
  fi
  return 1
}

telegram_api() {
  local token="$1" method="$2" data="${3:-}" output config
  output="$(mktemp)" || return 1
  config="$(mktemp)" || { rm -f "${output}"; return 1; }
  chmod 600 "${config}" || { rm -f "${config}" "${output}"; return 1; }
  if ! {
    printf 'url = "https://api.telegram.org/bot%s/%s"\n' "${token}" "${method}"
    printf 'request = "POST"\nfail\nsilent\nshow-error\n'
    [[ -n "${data}" ]] && printf 'data = "%s"\n' "${data}"
    printf 'output = "%s"\n' "${output}"
  } >"${config}"; then
    rm -f "${config}" "${output}"
    return 1
  fi
  if ! curl --config "${config}"; then
    rm -f "${config}" "${output}"
    return 1
  fi
  rm -f "${config}"
  cat "${output}" || { rm -f "${output}"; return 1; }
  rm -f "${output}"
}

validate_telegram_token() {
  local token="$1" response
  response="$(telegram_api "${token}" getMe)" || return 1
  python3 -c 'import json,sys; raise SystemExit(0 if json.load(sys.stdin).get("ok") else 1)' \
    <<<"${response}"
}

telegram_username() {
  local token response
  token="$(<"${SECRETS_DIR}/telegram-token")"
  response="$(telegram_api "${token}" getMe)" || return 1
  python3 -c 'import json,sys; print(json.load(sys.stdin).get("result",{}).get("username",""))' \
    <<<"${response}"
}

validate_allowed_ids() {
  local value="${1//;/,}" id
  local ids=()
  [[ "${value}" =~ ^[0-9]+(,[0-9]+)*$ ]] || return 1
  IFS=',' read -r -a ids <<<"${value}"
  for id in "${ids[@]}"; do
    [[ "${id}" =~ ^[1-9][0-9]*$ ]] || return 1
  done
}

validate_domain() {
  [[ "$1" =~ ^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$ ]]
}

write_config() {
  local ids="$1" interval="$2" destination="$3" domain="$4" registration="$5" slots="$6" tmp
  tmp="$(mktemp "${CONFIG_DIR}/.config.XXXXXX")"
  cat >"${tmp}" <<EOF
TELEGRAM_ALLOWED_USERS=${ids//;/,}
CHECK_INTERVAL_SECONDS=${interval}
CHECK_JITTER_SECONDS=120
MAX_PRODUCTS_PER_USER=200
MAX_WB_BATCH_SIZE=25
WB_DESTINATION=${destination}
WB_CURRENCY=rub
WB_LANGUAGE=ru
PRICE_HISTORY_DAYS=180
LOG_LEVEL=INFO
WB_BROWSER_HEADLESS=false
APP_TIMEZONE=Asia/Irkutsk
MAX_BULK_IMPORT=50
MAX_RULES_PER_PRODUCT=10
MPSTATS_MAX_AGE_HOURS=24
AUTH_DOMAIN=${domain}
AUTH_PUBLIC_URL=https://${domain}
AUTH_BIND_HOST=0.0.0.0
AUTH_PORT=8080
AUTH_SESSION_TTL_SECONDS=600
AUTH_MAX_CONCURRENT_SESSIONS=${slots}
REGISTRATION_MODE=${registration}
EOF
  chown root:10001 "${tmp}"
  chmod 640 "${tmp}"
  mv -f "${tmp}" "${CONFIG_FILE}"
}

set_config_value() {
  local key="$1" value="$2" tmp
  [[ "${value}" != *$'\n'* ]] || die "недопустимое значение настройки"
  tmp="$(mktemp "${CONFIG_DIR}/.config.XXXXXX")"
  awk -F= -v key="${key}" -v value="${value}" '
    BEGIN { found=0 }
    $1 == key { print key "=" value; found=1; next }
    { print }
    END { if (!found) print key "=" value }
  ' "${CONFIG_FILE}" >"${tmp}"
  chown root:10001 "${tmp}"
  chmod 640 "${tmp}"
  mv -f "${tmp}" "${CONFIG_FILE}"
}

read_config_value() {
  local key="$1"
  awk -F= -v key="${key}" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' "${CONFIG_FILE}"
}

prepare_directories() {
  install -d -m 0755 "${APP_ROOT}" "${RELEASES_DIR}"
  install -d -m 0750 "${CONFIG_DIR}" "${SECRETS_DIR}" "${BACKUP_DIR}"
  chown root:10001 "${SECRETS_DIR}"
  install -d -m 0750 "${DATA_DIR}"
  chown 10001:10001 "${DATA_DIR}"
}

create_session_key() {
  if [[ ! -s "${SECRETS_DIR}/session-key" ]]; then
    local key
    key="$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '\n')"
    printf '%s\n' "${key}" >"${SECRETS_DIR}/session-key"
  fi
  chown root:10001 "${SECRETS_DIR}/session-key"
  chmod 640 "${SECRETS_DIR}/session-key"
  if [[ ! -e "${SECRETS_DIR}/mpstats-token" ]]; then
    : >"${SECRETS_DIR}/mpstats-token"
  fi
  chown root:10001 "${SECRETS_DIR}/mpstats-token"
  chmod 640 "${SECRETS_DIR}/mpstats-token"
}

configure_first_install() {
  local token ids interval destination domain registration slots
  if [[ -s "${SECRETS_DIR}/telegram-token" && -s "${CONFIG_FILE}" ]]; then
    return 0
  fi
  read_secret token "Токен Telegram-бота от @BotFather"
  validate_telegram_token "${token}" || die "Telegram отклонил токен"
  read_prompt ids "Telegram ID администраторов через запятую"
  validate_allowed_ids "${ids}" || die "Telegram ID должны быть положительными числами"
  read_prompt interval "Интервал проверки в секундах (минимум 900)" "1800"
  [[ "${interval}" =~ ^[0-9]+$ && "${interval}" -ge 900 ]] || die "неверный интервал"
  read_prompt destination "WB dest региона (Иркутск по умолчанию)" "-5827722"
  [[ "${destination}" =~ ^-?[0-9]+$ ]] || die "WB dest должен быть целым числом"
  read_prompt domain "Домен для защищённого окна авторизации WB (A/AAAA-запись должна вести на сервер)"
  domain="${domain,,}"
  validate_domain "${domain}" || die "укажите домен вида auth.example.com без https:// и пути"
  read_prompt registration "Режим регистрации: approval, open или allowlist" "approval"
  [[ "${registration}" =~ ^(approval|open|allowlist)$ ]] || die "неверный режим регистрации"
  read_prompt slots "Одновременных окон авторизации, 1–5" "2"
  [[ "${slots}" =~ ^[1-5]$ ]] || die "количество окон должно быть от 1 до 5"
  printf '%s\n' "${token}" >"${SECRETS_DIR}/telegram-token"
  chown root:10001 "${SECRETS_DIR}/telegram-token"
  chmod 640 "${SECRETS_DIR}/telegram-token"
  write_config "${ids}" "${interval}" "${destination}" "${domain}" "${registration}" "${slots}"
}

ensure_auth_config() {
  local domain public_url registration slots
  domain="$(read_config_value AUTH_DOMAIN || true)"
  if [[ -z "${domain}" || "${domain}" == "localhost" ]] || ! validate_domain "${domain}"; then
    read_prompt domain "Домен для защищённого окна авторизации WB"
    domain="${domain,,}"
    validate_domain "${domain}" || die "укажите домен вида auth.example.com"
    set_config_value AUTH_DOMAIN "${domain}"
  fi
  public_url="$(read_config_value AUTH_PUBLIC_URL || true)"
  [[ "${public_url}" == "https://${domain}" ]] \
    || set_config_value AUTH_PUBLIC_URL "https://${domain}"
  registration="$(read_config_value REGISTRATION_MODE || true)"
  if [[ ! "${registration}" =~ ^(approval|open|allowlist)$ ]]; then
    set_config_value REGISTRATION_MODE approval
  fi
  slots="$(read_config_value AUTH_MAX_CONCURRENT_SESSIONS || true)"
  if [[ ! "${slots}" =~ ^[1-5]$ ]]; then
    set_config_value AUTH_MAX_CONCURRENT_SESSIONS 2
  fi
  [[ -n "$(read_config_value AUTH_BIND_HOST || true)" ]] || set_config_value AUTH_BIND_HOST 0.0.0.0
  [[ -n "$(read_config_value AUTH_PORT || true)" ]] || set_config_value AUTH_PORT 8080
  [[ -n "$(read_config_value AUTH_SESSION_TTL_SECONDS || true)" ]] \
    || set_config_value AUTH_SESSION_TTL_SECONDS 600
}

install_wrapper() {
  local tmp
  tmp="$(mktemp /usr/local/bin/.wb-price-bot.XXXXXX)" || return 1
  if ! cat >"${tmp}" <<EOF
#!/usr/bin/env bash
exec "${CURRENT_LINK}/scripts/install.sh" "\$@"
EOF
  then
    rm -f "${tmp}"
    return 1
  fi
  chmod 755 "${tmp}" || { rm -f "${tmp}"; return 1; }
  bash -n "${tmp}" || { rm -f "${tmp}"; return 1; }
  mv -f "${tmp}" "${WRAPPER}" || { rm -f "${tmp}"; return 1; }
}

copy_release() {
  local source="$1" version release
  version="$(tr -d '[:space:]' <"${source}/VERSION")" || return 1
  [[ "${version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { warn "неверный VERSION"; return 1; }
  release="${RELEASES_DIR}/${version}-$(date -u +%Y%m%d%H%M%S)"
  mkdir -p "${release}" || return 1
  if ! cp -a "${source}/." "${release}/" \
    || ! chmod 755 "${release}/scripts/install.sh"; then
    rm -rf -- "${release}"
    return 1
  fi
  printf '%s' "${release}" || return 1
}

wait_container_healthy() {
  local container="$1" timeout="${2:-240}" elapsed=0 health
  while ((elapsed < timeout)); do
    health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "${container}" 2>/dev/null || true)"
    if [[ "${health}" == "healthy" ]]; then
      return 0
    fi
    if [[ "${health}" == "unhealthy" ]]; then
      return 1
    fi
    sleep 5
    elapsed=$((elapsed + 5))
  done
  return 1
}

wait_healthy() {
  local timeout="${1:-300}"
  wait_container_healthy "${CONTAINER_NAME}" "${timeout}" || return 1
  if docker inspect "${AUTH_CONTAINER_NAME}" >/dev/null 2>&1; then
    wait_container_healthy "${AUTH_CONTAINER_NAME}" "${timeout}" || return 1
  fi
  if docker inspect "${CADDY_CONTAINER_NAME}" >/dev/null 2>&1; then
    wait_container_healthy "${CADDY_CONTAINER_NAME}" "${timeout}" || return 1
  fi
}

activate_release() {
  local release="$1" old="" link_tmp
  [[ -f "${release}/compose.yaml" ]] || { warn "каталог релиза повреждён"; return 1; }
  if [[ -L "${CURRENT_LINK}" ]]; then
    old="$(readlink -f "${CURRENT_LINK}")"
  fi
  say "Собираю контейнер ${APP_TITLE}; первая сборка может занять несколько минут…"
  if ! compose_at "${release}" build; then
    warn "сборка контейнера завершилась ошибкой"
    return 1
  fi
  link_tmp="${APP_ROOT}/.current.$$.tmp"
  if ! ln -s "${release}" "${link_tmp}"; then
    warn "не удалось подготовить ссылку нового релиза"
    return 1
  fi
  if ! mv -Tf "${link_tmp}" "${CURRENT_LINK}"; then
    rm -f "${link_tmp}"
    warn "не удалось переключить активный релиз"
    return 1
  fi
  if ! install_wrapper; then
    warn "не удалось обновить команду управления"
    rollback_release "${old}" "${release}"
    return 1
  fi
  if ! compose_current up -d --remove-orphans; then
    warn "Docker Compose не запустил новый релиз"
    rollback_release "${old}" "${release}"
    return 1
  fi
  if wait_healthy 300; then
    say "Сервис запущен и прошёл healthcheck."
    return 0
  fi
  warn "новый релиз не прошёл healthcheck"
  compose_current logs --tail 80 || true
  rollback_release "${old}" "${release}"
  return 1
}

rollback_release() {
  local old="$1" failed_release="$2" link_tmp
  if [[ -z "${old}" || ! -d "${old}" ]]; then
    warn "предыдущего релиза нет; останавливаю неудачную установку"
    if ! compose_at "${failed_release}" down --remove-orphans >/dev/null 2>&1; then
      warn "не удалось остановить контейнер неудачной установки; current и wrapper сохранены для ручного восстановления"
      return 1
    fi
    rm -f "${CURRENT_LINK}" "${WRAPPER}"
    return 1
  fi
  warn "возвращаю предыдущий релиз"
  link_tmp="${APP_ROOT}/.current.rollback.$$.tmp"
  if ! ln -s "${old}" "${link_tmp}" \
    || ! mv -Tf "${link_tmp}" "${CURRENT_LINK}" \
    || ! install_wrapper \
    || ! compose_current up -d --remove-orphans \
    || ! wait_healthy 300; then
    rm -f "${link_tmp}"
    warn "rollback не вернул предыдущий релиз в healthy-состояние"
    return 1
  fi
  say "Предыдущий релиз восстановлен и прошёл healthcheck."
  return 0
}

create_backup() {
  local output
  mkdir -p "${BACKUP_DIR}" || return 1
  output="/data/backups/wb-price-bot-$(date -u +%Y%m%d-%H%M%S).sqlite3"
  mkdir -p "${DATA_DIR}/backups" || return 1
  chown 10001:10001 "${DATA_DIR}/backups" || return 1
  compose_current run --rm -T bot python -m wb_price_bot backup "${output}" >/dev/null \
    || return 1
  cp -a "${DATA_DIR}/backups/$(basename "${output}")" "${BACKUP_DIR}/" || return 1
  chmod 600 "${BACKUP_DIR}/$(basename "${output}")" || return 1
  LAST_BACKUP_PATH="${BACKUP_DIR}/$(basename "${output}")"
  say "Резервная копия: ${LAST_BACKUP_PATH}"
}

command_install() {
  require_root
  lock_operations
  ensure_base_tools
  ensure_docker
  prepare_directories
  create_session_key
  configure_first_install
  ensure_auth_config

  local source temp_source="" release
  if source="$(source_tree_from_script)"; then
    say "Использую локальный исходный код: ${source}"
  else
    temp_source="$(mktemp -d)"
    download_latest_release "${GH_TOKEN:-${GITHUB_TOKEN:-}}" "${temp_source}"
    source="${temp_source}"
  fi
  if ! release="$(copy_release "${source}")"; then
    [[ -n "${temp_source}" ]] && rm -rf -- "${temp_source}"
    die "не удалось подготовить каталог релиза"
  fi
  if ! activate_release "${release}"; then
    [[ -n "${temp_source}" ]] && rm -rf -- "${temp_source}"
    die "установка не завершена"
  fi
  [[ -n "${temp_source}" ]] && rm -rf -- "${temp_source}"
  local username
  username="$(telegram_username || true)"
  say
  say "Установка завершена."
  [[ -n "${username}" ]] && say "Бот: https://t.me/${username}"
  say "Меню управления: sudo ${APP_NAME}"
}

command_update() {
  require_root
  lock_operations
  ensure_base_tools
  ensure_docker
  [[ -f "${CURRENT_LINK}/VERSION" ]] || die "сначала установите приложение"
  prepare_directories
  create_session_key
  ensure_auth_config
  local source release
  create_backup || die "не удалось создать резервную копию перед обновлением"
  source="$(mktemp -d)"
  download_latest_release "${GH_TOKEN:-${GITHUB_TOKEN:-}}" "${source}"
  if ! release="$(copy_release "${source}")"; then
    rm -rf -- "${source}"
    die "не удалось подготовить каталог релиза"
  fi
  rm -rf -- "${source}"
  activate_release "${release}" || die "обновление отменено, выполнен rollback"
  say "Текущая версия: $(<"${CURRENT_LINK}/VERSION")"
}

telegram_menu() {
  local choice token old ids response chat_id username
  while true; do
    say
    say "Настройки Telegram"
    say "1) Показать подключённого бота"
    say "2) Добавить или заменить токен"
    say "3) Изменить Telegram ID администраторов"
    say "4) Отправить тестовое сообщение"
    say "0) Назад"
    read_prompt choice "Выберите пункт"
    case "${choice}" in
      1)
        username="$(telegram_username || true)"
        [[ -n "${username}" ]] && say "@${username} — https://t.me/${username}" || warn "токен не прошёл getMe"
        ;;
      2)
        read_secret token "Новый токен от @BotFather"
        validate_telegram_token "${token}" || { warn "Telegram отклонил токен"; continue; }
        old="$(<"${SECRETS_DIR}/telegram-token")"
        printf '%s\n' "${token}" >"${SECRETS_DIR}/.telegram-token.tmp"
        chown root:10001 "${SECRETS_DIR}/.telegram-token.tmp"
        chmod 640 "${SECRETS_DIR}/.telegram-token.tmp"
        mv -f "${SECRETS_DIR}/.telegram-token.tmp" "${SECRETS_DIR}/telegram-token"
        if ! compose_current up -d --force-recreate bot auth || ! wait_healthy 240; then
          warn "новый токен не запустился, возвращаю старый"
          printf '%s\n' "${old}" >"${SECRETS_DIR}/telegram-token"
          chown root:10001 "${SECRETS_DIR}/telegram-token"
          chmod 640 "${SECRETS_DIR}/telegram-token"
          compose_current up -d --force-recreate bot auth
        else
          say "Токен заменён."
        fi
        ;;
      3)
        ids="$(read_config_value TELEGRAM_ALLOWED_USERS)"
        read_prompt ids "Telegram ID администраторов через запятую" "${ids}"
        validate_allowed_ids "${ids}" || { warn "неверный список"; continue; }
        set_config_value TELEGRAM_ALLOWED_USERS "${ids//;/,}"
        compose_current up -d --force-recreate bot auth
        wait_healthy 180 || warn "контейнер ещё не healthy; проверьте журнал"
        ;;
      4)
        read_prompt chat_id "Telegram ID получателя" "$(read_config_value TELEGRAM_ALLOWED_USERS | cut -d, -f1)"
        [[ "${chat_id}" =~ ^[0-9]+$ ]] || { warn "неверный ID"; continue; }
        token="$(<"${SECRETS_DIR}/telegram-token")"
        response="$(telegram_api "${token}" sendMessage "chat_id=${chat_id}&text=WB%20Price%20Bot%3A%20test%20OK")" || true
        python3 -c 'import json,sys; print("Отправлено" if json.load(sys.stdin).get("ok") else "Telegram вернул ошибку")' <<<"${response:-{}}"
        ;;
      0) return ;;
      *) warn "неизвестный пункт" ;;
    esac
  done
}

wb_session_menu() {
  local choice telegram_id path reference
  while true; do
    say
    say "Аккаунт Wildberries (beta)"
    say "1) Показать инструкцию безопасного входа"
    say "2) Импортировать wb-session.json с сервера"
    say "3) Удалить WB-сессию"
    say "4) Проверить публичную карточку WB"
    say "0) Назад"
    read_prompt choice "Выберите пункт"
    case "${choice}" in
      1)
        say "На компьютере из каталога проекта выполните:"
        say "  python -m pip install '.[browser]'"
        say "  python -m playwright install chromium"
        say "  python scripts/capture_wb_session.py"
        say "Не отправляйте файл боту. Импортируйте прямо по SSH/stdin:"
        say "  ssh USER@SERVER 'sudo wb-price-bot wb-session-import TELEGRAM_ID' < wb-session.json"
        ;;
      2)
        read_prompt telegram_id "Telegram ID владельца сессии" "$(read_config_value TELEGRAM_ALLOWED_USERS | cut -d, -f1)"
        [[ "${telegram_id}" =~ ^[0-9]+$ ]] || { warn "неверный ID"; continue; }
        read_prompt path "Полный путь к wb-session.json"
        [[ -f "${path}" && ! -L "${path}" ]] || { warn "обычный файл не найден"; continue; }
        [[ "$(stat -c %s "${path}")" -le 2097152 ]] || { warn "файл больше 2 МБ"; continue; }
        if compose_current run --rm -T bot python -m wb_price_bot set-session --telegram-id "${telegram_id}" <"${path}"; then
          say "Сессия импортирована. Удалите исходный файл с сервера."
        fi
        ;;
      3)
        read_prompt telegram_id "Telegram ID" "$(read_config_value TELEGRAM_ALLOWED_USERS | cut -d, -f1)"
        compose_current run --rm -T bot python -m wb_price_bot remove-session --telegram-id "${telegram_id}"
        ;;
      4)
        read_prompt reference "Ссылка или артикул" "28436956"
        compose_current run --rm -T bot python -m wb_price_bot check-wb "${reference}"
        ;;
      0) return ;;
      *) warn "неизвестный пункт" ;;
    esac
  done
}

licensed_provider_menu() {
  local choice token old
  while true; do
    say
    say "Лицензированный источник MPSTATS"
    say "Статус: $([[ -s "${SECRETS_DIR}/mpstats-token" ]] && echo настроен || echo выключен)"
    say "1) Добавить или заменить API token"
    say "2) Проверить подключение"
    say "3) Удалить token и выключить fallback"
    say "0) Назад"
    read_prompt choice "Выберите пункт"
    case "${choice}" in
      1)
        read_secret token "MPSTATS API token"
        [[ -n "${token}" && "${token}" != *$'\n'* ]] || { warn "пустой или неверный token"; continue; }
        old="$(<"${SECRETS_DIR}/mpstats-token")"
        printf '%s\n' "${token}" >"${SECRETS_DIR}/.mpstats-token.tmp"
        chown root:10001 "${SECRETS_DIR}/.mpstats-token.tmp"
        chmod 640 "${SECRETS_DIR}/.mpstats-token.tmp"
        mv -f "${SECRETS_DIR}/.mpstats-token.tmp" "${SECRETS_DIR}/mpstats-token"
        if ! compose_current run --rm -T bot python -m wb_price_bot check-mpstats 28436956; then
          warn "MPSTATS отклонил token, возвращаю предыдущее значение"
          printf '%s' "${old}" >"${SECRETS_DIR}/mpstats-token"
          chown root:10001 "${SECRETS_DIR}/mpstats-token"
          chmod 640 "${SECRETS_DIR}/mpstats-token"
          continue
        fi
        compose_current restart bot
        say "MPSTATS fallback включён."
        ;;
      2)
        [[ -s "${SECRETS_DIR}/mpstats-token" ]] || { warn "token не настроен"; continue; }
        compose_current run --rm -T bot python -m wb_price_bot check-mpstats 28436956
        ;;
      3)
        confirm_phrase DELETE-MPSTATS "Удалить MPSTATS token" || { say "Отменено."; continue; }
        : >"${SECRETS_DIR}/mpstats-token"
        chown root:10001 "${SECRETS_DIR}/mpstats-token"
        chmod 640 "${SECRETS_DIR}/mpstats-token"
        compose_current restart bot
        say "MPSTATS fallback выключен."
        ;;
      0) return ;;
      *) warn "неизвестный пункт" ;;
    esac
  done
}

monitoring_menu() {
  local choice value
  while true; do
    say
    say "Настройки мониторинга"
    say "Интервал: $(read_config_value CHECK_INTERVAL_SECONDS) сек."
    say "WB dest: $(read_config_value WB_DESTINATION)"
    say "1) Изменить интервал"
    say "2) Изменить WB dest региона"
    say "3) Запустить проверку сейчас"
    say "0) Назад"
    read_prompt choice "Выберите пункт"
    case "${choice}" in
      1)
        read_prompt value "Интервал в секундах, минимум 900" "$(read_config_value CHECK_INTERVAL_SECONDS)"
        [[ "${value}" =~ ^[0-9]+$ && "${value}" -ge 900 ]] || { warn "неверный интервал"; continue; }
        set_config_value CHECK_INTERVAL_SECONDS "${value}"
        compose_current up -d --force-recreate bot
        ;;
      2)
        read_prompt value "Числовой dest из xinfo Wildberries" "$(read_config_value WB_DESTINATION)"
        [[ "${value}" =~ ^-?[0-9]+$ ]] || { warn "неверный dest"; continue; }
        set_config_value WB_DESTINATION "${value}"
        compose_current up -d --force-recreate bot
        ;;
      3)
        compose_current restart bot
        say "Контейнер перезапущен; цикл проверки начинается сразу."
        ;;
      0) return ;;
      *) warn "неизвестный пункт" ;;
    esac
  done
}

web_auth_menu() {
  local choice value
  while true; do
    say
    say "Web-авторизация и пользователи"
    say "Домен: $(read_config_value AUTH_DOMAIN)"
    say "Регистрация: $(read_config_value REGISTRATION_MODE)"
    say "Одновременных окон: $(read_config_value AUTH_MAX_CONCURRENT_SESSIONS)"
    say "1) Изменить домен"
    say "2) Изменить режим регистрации"
    say "3) Изменить число браузерных окон"
    say "4) Проверить HTTPS и auth-сервис"
    say "5) Перезапустить web-авторизацию"
    say "0) Назад"
    read_prompt choice "Выберите пункт"
    case "${choice}" in
      1)
        read_prompt value "Новый домен без https://" "$(read_config_value AUTH_DOMAIN)"
        value="${value,,}"
        validate_domain "${value}" || { warn "неверный домен"; continue; }
        set_config_value AUTH_DOMAIN "${value}"
        set_config_value AUTH_PUBLIC_URL "https://${value}"
        compose_current up -d --force-recreate bot auth caddy
        wait_healthy 300 || warn "сервисы ещё не healthy; проверьте DNS, 80/443 и журнал"
        ;;
      2)
        read_prompt value "approval, open или allowlist" "$(read_config_value REGISTRATION_MODE)"
        [[ "${value}" =~ ^(approval|open|allowlist)$ ]] || { warn "неверный режим"; continue; }
        set_config_value REGISTRATION_MODE "${value}"
        compose_current up -d --force-recreate bot auth
        wait_healthy 240 || warn "bot/auth ещё не healthy"
        ;;
      3)
        read_prompt value "Одновременных окон, 1–5" "$(read_config_value AUTH_MAX_CONCURRENT_SESSIONS)"
        [[ "${value}" =~ ^[1-5]$ ]] || { warn "нужно число от 1 до 5"; continue; }
        set_config_value AUTH_MAX_CONCURRENT_SESSIONS "${value}"
        compose_current up -d --force-recreate auth
        wait_healthy 240 || warn "auth-сервис ещё не healthy"
        ;;
      4)
        value="$(read_config_value AUTH_DOMAIN)"
        if curl -fsS --max-time 20 "https://${value}/health"; then
          say
          say "HTTPS и auth-сервис: OK"
        else
          warn "проверка не прошла; проверьте DNS домена, доступность 80/443 и журнал Caddy"
        fi
        ;;
      5)
        compose_current restart auth caddy
        wait_healthy 240 || warn "web-авторизация ещё не healthy"
        ;;
      0) return ;;
      *) warn "неизвестный пункт" ;;
    esac
  done
}

restore_backup() {
  local name source database_file pre_restore
  ls -1 "${BACKUP_DIR}"/*.sqlite3 2>/dev/null || { warn "резервных копий нет"; return; }
  read_prompt name "Имя файла из списка"
  [[ "${name}" == "$(basename "${name}")" ]] || { warn "укажите только имя файла"; return; }
  source="${BACKUP_DIR}/${name}"
  [[ -f "${source}" && ! -L "${source}" ]] || { warn "копия не найдена"; return; }
  python3 - "${source}" <<'PY' || { warn "копия повреждена"; return; }
import sqlite3, sys
with sqlite3.connect(sys.argv[1]) as db:
    result = db.execute("PRAGMA integrity_check").fetchone()
raise SystemExit(0 if result and result[0] == "ok" else 1)
PY
  confirm_phrase RESTORE "Восстановление заменит текущую базу" || { say "Отменено."; return; }
  create_backup || { warn "не удалось создать предвосстановительную копию"; return 1; }
  database_file="${DATA_DIR}/wb-price-bot.sqlite3"
  pre_restore="${database_file}.pre-restore"
  rm -f "${pre_restore}" || return 1
  cp -a "${LAST_BACKUP_PATH}" "${pre_restore}" \
    || { warn "не удалось подготовить rollback-копию; bot не остановлен"; return 1; }
  compose_current stop bot auth \
    || { rm -f "${pre_restore}"; warn "не удалось остановить bot/auth; восстановление отменено"; return 1; }
  if ! install -o 10001 -g 10001 -m 0640 "${source}" "${database_file}.tmp" \
    || ! mv -f "${database_file}.tmp" "${database_file}"; then
    warn "не удалось заменить файл базы"
    rm -f "${database_file}.tmp" "${database_file}-wal" "${database_file}-shm"
    [[ -f "${pre_restore}" ]] && mv -f "${pre_restore}" "${database_file}"
    if ! compose_current up -d bot auth caddy || ! wait_healthy 240; then
      warn "исходная база возвращена, но bot не восстановил healthy-состояние"
    fi
    return 1
  fi
  rm -f "${database_file}-wal" "${database_file}-shm"
  if ! compose_current up -d bot auth caddy || ! wait_healthy 240; then
    warn "восстановленная база не запустилась, возвращаю предыдущую"
    if ! compose_current stop bot auth; then
      warn "не удалось остановить bot/auth; автоматический rollback базы небезопасен"
      return 1
    fi
    rm -f "${database_file}" "${database_file}-wal" "${database_file}-shm"
    if [[ -f "${pre_restore}" ]]; then
      mv -f "${pre_restore}" "${database_file}"
      chown 10001:10001 "${database_file}"
    fi
    if ! compose_current up -d bot auth caddy || ! wait_healthy 240; then
      warn "предыдущая база не вернулась в healthy-состояние"
      return 1
    fi
    return 1
  fi
  rm -f "${pre_restore}"
  say "База восстановлена."
}

backup_menu() {
  local choice
  while true; do
    say
    say "Резервные копии"
    say "1) Создать сейчас"
    say "2) Показать список"
    say "3) Восстановить"
    say "0) Назад"
    read_prompt choice "Выберите пункт"
    case "${choice}" in
      1) create_backup ;;
      2) ls -lh "${BACKUP_DIR}"/*.sqlite3 2>/dev/null || say "Копий пока нет." ;;
      3) restore_backup ;;
      0) return ;;
      *) warn "неизвестный пункт" ;;
    esac
  done
}

doctor() {
  say "Версия: $(<"${CURRENT_LINK}/VERSION")"
  say "Docker: $(docker --version)"
  if [[ "${COMPOSE_MODE}" == "plugin" ]]; then
    docker compose version
  else
    docker-compose version
  fi
  say "Свободное место:"
  df -h "${DATA_DIR}" | tail -n 1
  say "Права секретов:"
  stat -c '%a %U:%G %n' \
    "${SECRETS_DIR}/telegram-token" \
    "${SECRETS_DIR}/session-key" \
    "${SECRETS_DIR}/mpstats-token"
  say "MPSTATS fallback: $([[ -s "${SECRETS_DIR}/mpstats-token" ]] && echo настроен || echo выключен)"
  say "Контейнеры:"
  for container in "${CONTAINER_NAME}" "${AUTH_CONTAINER_NAME}" "${CADDY_CONTAINER_NAME}"; do
    docker inspect --format '{{.Name}} status={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${container}" 2>/dev/null || true
  done
  compose_current run --rm -T bot python -m wb_price_bot integrity-check
  if validate_telegram_token "$(<"${SECRETS_DIR}/telegram-token")"; then
    say "Telegram getMe: OK"
  else
    warn "Telegram getMe: ошибка"
  fi
  if curl -fsS --max-time 20 "$(read_config_value AUTH_PUBLIC_URL)/health" >/dev/null; then
    say "Web-авторизация HTTPS: OK"
  else
    warn "Web-авторизация HTTPS: ошибка"
  fi
}

safe_remove_tree() {
  local target="$1" allowed="$2" resolved
  [[ -e "${target}" || -L "${target}" ]] || return 0
  resolved="$(realpath -m -- "${target}")"
  [[ "${resolved}" == "${allowed}" ]] || die "отказ удалять неожиданный путь ${resolved}"
  rm -rf --one-file-system -- "${target}"
}

uninstall_menu() {
  local choice
  say "1) Удалить приложение, сохранив настройки, базу и копии"
  say "2) Полностью удалить приложение, базу, секреты и локальные копии"
  say "0) Назад"
  read_prompt choice "Выберите вариант"
  case "${choice}" in
    1)
      confirm_phrase DELETE-APP "Удалить runtime и код" || { say "Отменено."; return; }
      if ! compose_current down --remove-orphans --rmi all; then
        warn "не удалось остановить контейнеры; удаление отменено"
        return 1
      fi
      rm -f "${WRAPPER}"
      safe_remove_tree "${APP_ROOT}" "${APP_ROOT}"
      say "Приложение удалено. Данные сохранены в ${DATA_DIR}."
      exit 0
      ;;
    2)
      warn "Telegram/MPSTATS token не отзываются автоматически, а WB-сессия удаляется только локально."
      confirm_phrase DELETE-WB-BOT "Полностью удалить WB Price Bot" || { say "Отменено."; return; }
      if ! compose_current down --remove-orphans --rmi all --volumes; then
        warn "не удалось остановить контейнеры; полное удаление отменено"
        return 1
      fi
      rm -f "${WRAPPER}"
      safe_remove_tree "${APP_ROOT}" "${APP_ROOT}"
      safe_remove_tree "${CONFIG_DIR}" "${CONFIG_DIR}"
      safe_remove_tree "${DATA_DIR}" "${DATA_DIR}"
      safe_remove_tree "${BACKUP_DIR}" "${BACKUP_DIR}"
      say "WB Price Bot полностью удалён. Docker и чужие ресурсы не затронуты."
      exit 0
      ;;
    0) return ;;
    *) warn "неизвестный пункт" ;;
  esac
}

menu_header() {
  local version status health username domain
  version="$(<"${CURRENT_LINK}/VERSION")"
  status="$(docker inspect --format '{{.State.Status}}' "${CONTAINER_NAME}" 2>/dev/null || echo stopped)"
  health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}—{{end}}' "${CONTAINER_NAME}" 2>/dev/null || echo '—')"
  username="$(telegram_username 2>/dev/null || true)"
  domain="$(read_config_value AUTH_DOMAIN 2>/dev/null || true)"
  say
  say "========================================"
  say " ${APP_TITLE} v${version}"
  say " Сервис: ${status}; health: ${health}"
  [[ -n "${username}" ]] && say " Telegram: @${username}"
  say " Auth: ${domain:-не настроен}"
  say "========================================"
}

command_menu() {
  require_root
  lock_operations
  ensure_base_tools
  local choice
  if [[ ! -f "${CURRENT_LINK}/VERSION" ]]; then
    while true; do
      say
      say "${APP_TITLE} пока не установлен."
      say "1) Установить"
      say "0) Выход"
      read_prompt choice "Выберите пункт"
      case "${choice}" in
        1) command_install; break ;;
        0) return ;;
        *) warn "неизвестный пункт" ;;
      esac
    done
  fi
  ensure_docker
  while true; do
    menu_header
    say "1) Обновить до последнего релиза"
    say "2) Запустить"
    say "3) Остановить"
    say "4) Перезапустить"
    say "5) Состояние контейнера"
    say "6) Журнал"
    say "7) Настройки Telegram"
    say "8) Аккаунт Wildberries"
    say "9) Настройки мониторинга"
    say "10) Лицензированный источник MPSTATS"
    say "11) Web-авторизация и пользователи"
    say "12) Резервные копии"
    say "13) Диагностика"
    say "14) Удаление"
    say "0) Выход"
    read_prompt choice "Выберите пункт"
    case "${choice}" in
      1) command_update ;;
      2) compose_current up -d ;;
      3) compose_current stop ;;
      4) compose_current restart ;;
      5) compose_current ps ;;
      6) compose_current logs --tail 200 -f || true ;;
      7) telegram_menu ;;
      8) wb_session_menu ;;
      9) monitoring_menu ;;
      10) licensed_provider_menu ;;
      11) web_auth_menu ;;
      12) backup_menu ;;
      13) doctor ;;
      14) uninstall_menu ;;
      0) return ;;
      *) warn "неизвестный пункт" ;;
    esac
  done
}

command_wb_session_import() {
  local telegram_id="${1:-}"
  require_root
  lock_operations
  ensure_base_tools
  ensure_docker
  [[ -f "${CURRENT_LINK}/VERSION" ]] || die "приложение не установлено"
  [[ "${telegram_id}" =~ ^[0-9]+$ ]] || die "укажите числовой Telegram ID"
  [[ ! -t 0 ]] || die "передайте wb-session.json через stdin"
  compose_current run --rm -T bot python -m wb_price_bot set-session \
    --telegram-id "${telegram_id}"
}

main() {
  local command="${1:-menu}"
  case "${command}" in
    install) command_install ;;
    update) command_update ;;
    menu) command_menu ;;
    start) require_root; lock_operations; ensure_docker; compose_current up -d ;;
    stop) require_root; lock_operations; ensure_docker; compose_current stop ;;
    restart) require_root; lock_operations; ensure_docker; compose_current restart ;;
    status) require_root; lock_operations; ensure_docker; compose_current ps ;;
    logs) require_root; lock_operations; ensure_docker; compose_current logs --tail 200 -f ;;
    backup) require_root; lock_operations; ensure_docker; create_backup ;;
    doctor) require_root; lock_operations; ensure_docker; doctor ;;
    wb-session-import) command_wb_session_import "${2:-}" ;;
    *) die "неизвестная команда: ${command}" ;;
  esac
}

main "$@"
