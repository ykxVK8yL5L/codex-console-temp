#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

VENV_DIR="${VENV_DIR:-$PROJECT_ROOT/.venv}"
PYTHON_BIN=""

log() {
  printf '[start_server] %s\n' "$1"
}

has_cmd() {
  command -v "$1" >/dev/null 2>&1
}

load_env_file() {
  local env_file="$PROJECT_ROOT/.env"
  if [[ -f "$env_file" ]]; then
    log "加载 .env"
    set -a
    # shellcheck disable=SC1090
    source "$env_file"
    set +a
  fi
}

ensure_python() {
  if [[ -x "$VENV_DIR/bin/python" ]]; then
    PYTHON_BIN="$VENV_DIR/bin/python"
    return
  fi

  local python_cmd=""
  if has_cmd python3; then
    python_cmd="python3"
  elif has_cmd python; then
    python_cmd="python"
  else
    log "未找到 python3/python，请先安装 Python 3.10+"
    exit 1
  fi

  log "创建虚拟环境: $VENV_DIR"
  "$python_cmd" -m venv "$VENV_DIR"
  PYTHON_BIN="$VENV_DIR/bin/python"
}

install_dependencies() {
  if has_cmd uv; then
    log "使用 uv sync 安装依赖"
    uv sync
  else
    log "使用 pip 安装依赖"
    "$PYTHON_BIN" -m pip install --upgrade pip
    "$PYTHON_BIN" -m pip install -r requirements.txt
  fi
}

maybe_run_migrations() {
  local database_url="${APP_DATABASE_URL:-${DATABASE_URL:-}}"
  if [[ -z "$database_url" ]]; then
    log "未配置 PostgreSQL，跳过 alembic，应用将按自身逻辑使用 SQLite"
    return
  fi

  if [[ "$database_url" =~ ^postgres(ql)?(\+psycopg)?:// ]]; then
    log "检测到 PostgreSQL，执行 alembic upgrade head"
    if has_cmd uv; then
      uv run alembic upgrade head
    else
      "$PYTHON_BIN" -m alembic upgrade head
    fi
  else
    log "数据库不是 PostgreSQL，跳过 alembic"
  fi
}

main() {
  export APP_HOST="${APP_HOST:-0.0.0.0}"
  export APP_PORT="${APP_PORT:-8000}"

  load_env_file
  ensure_python
  install_dependencies
  maybe_run_migrations

  log "启动 Web UI: ${APP_HOST}:${APP_PORT}"
  exec "$PYTHON_BIN" webui.py --host "$APP_HOST" --port "$APP_PORT"
}

main "$@"
