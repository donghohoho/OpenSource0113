#!/usr/bin/env bash
# Blue-green switch behind an nginx proxy. Called by .github/workflows/deploy.yml after
# the image is pulled.
#
# Any failure before the new version is verified through the proxy restores the
# previous state: the new container is removed and the old one keeps serving.
#
# The API key only ever reaches containers through --env-file. Do not add `set -x`
# here, and do not print `docker inspect` output, which includes the environment.
set -euo pipefail

: "${IMAGE:?IMAGE is required}"
: "${COMMIT_SHA:?COMMIT_SHA is required}"
: "${SPRITE_ENV_FILE:?SPRITE_ENV_FILE is required}"
: "${PROXY_IMAGE:?PROXY_IMAGE is required}"

PREFIX="${DEPLOY_PREFIX:-sprite-maker}"
PROXY_PORT="${PROXY_PORT:-8000}"
APP_URL="${APP_URL:-http://127.0.0.1:$PROXY_PORT}"
HEALTH_ATTEMPTS="${HEALTH_ATTEMPTS:-30}"
# How long the old container may finish in-flight requests. uvicorn exits as soon as it
# is idle, so this only costs time when a generation is running (up to 2 x 240 s).
DRAIN_SECONDS="${DRAIN_SECONDS:-540}"
# The single-container setup has to release the port, so it gets a short limit.
LEGACY_STOP_SECONDS="${LEGACY_STOP_SECONDS:-10}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROXY_CONF="$SCRIPT_DIR/nginx-proxy.conf"
PROXY="$PREFIX-proxy"
PROXY_CHECK="$PREFIX-proxy-check"
LEGACY="$PREFIX"
NETWORK="$PREFIX-net"
UPSTREAM_FILE=/etc/nginx/sprite-maker-upstream.conf
SITE_FILE=/etc/nginx/conf.d/default.conf

mode=""
new_app=""
old_app=""
switched=false
proxy_created=false
legacy_stopped=false
committed=false
failure_reason=""
work_dir="$(mktemp -d)"

log() { echo "[bluegreen] $*"; }

output() {
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    echo "$1=$2" >> "$GITHUB_OUTPUT"
  fi
}

fail() {
  failure_reason="$1"
  echo "::error::$1"
  exit 1
}

container_exists() { docker container inspect "$1" >/dev/null 2>&1; }

health_matches() {
  [[ "$1" == *'"status":"ok"'* && "$1" == *"\"version\":\"$COMMIT_SHA\""* ]]
}

# ----- proxy configuration ---------------------------------------------------

write_upstream_file() {
  echo "set \$app_upstream http://$1:8000;" > "$work_dir/upstream.conf"
  chmod 644 "$work_dir/upstream.conf"
}

# Prepares a proxy container that has not been started yet.
create_proxy() {
  local name="$1"
  shift
  docker create --name "$name" --network "$NETWORK" --restart unless-stopped "$@" "$PROXY_IMAGE" >/dev/null
  docker cp --quiet "$work_dir/upstream.conf" "$name:$UPSTREAM_FILE"
  docker cp --quiet "$PROXY_CONF" "$name:$SITE_FILE"
}

# Points the running proxy at new_app. `nginx -s reload` starts new workers on the same
# listening socket and lets the old workers finish their requests, so no connection drops.
switch_proxy() {
  docker exec "$PROXY" sh -c "cp $UPSTREAM_FILE $UPSTREAM_FILE.bak && cp $SITE_FILE $SITE_FILE.bak"
  write_upstream_file "$new_app"
  docker cp --quiet "$work_dir/upstream.conf" "$PROXY:$UPSTREAM_FILE.new"
  docker cp --quiet "$PROXY_CONF" "$PROXY:$SITE_FILE.new"
  switched=true
  docker exec "$PROXY" sh -c "mv $UPSTREAM_FILE.new $UPSTREAM_FILE && mv $SITE_FILE.new $SITE_FILE"
  docker exec "$PROXY" nginx -t -q || fail "새 프록시 설정 검사(nginx -t)에 실패했습니다."
  docker exec "$PROXY" nginx -s reload
}

# ----- rollback --------------------------------------------------------------

# Container output never goes to the Actions log or artifacts: they are public in this
# repository, and a process that prints its environment would publish the API key. It
# is kept in a private file next to the key file, because rollback removes the container.
save_new_app_logs() {
  if [ -n "$new_app" ] && container_exists "$new_app"; then
    local log_file
    log_file="$(dirname "$SPRITE_ENV_FILE")/failed-deploy.log"
    (umask 077; docker logs --tail 200 "$new_app" > "$log_file" 2>&1; chmod 600 "$log_file")
    log "container output saved on this machine: ~/.sprite-maker/failed-deploy.log"
  fi
}

rollback() {
  log "rolling back"
  save_new_app_logs

  if [ "$switched" = true ]; then
    docker exec "$PROXY" sh -c "mv $UPSTREAM_FILE.bak $UPSTREAM_FILE && mv $SITE_FILE.bak $SITE_FILE" \
      && docker exec "$PROXY" nginx -t -q \
      && docker exec "$PROXY" nginx -s reload \
      || echo "::error::프록시 설정을 되돌리지 못했습니다. 맥북에서 $PROXY 컨테이너를 확인하세요."
    # The reload signal returns at once. Removing the new container before the workers
    # have turned over would fail the requests still going to it.
    sleep 3
  fi
  if [ "$proxy_created" = true ]; then
    docker rm -f "$PROXY" >/dev/null 2>&1
  fi
  if [ "$legacy_stopped" = true ]; then
    docker start "$LEGACY" >/dev/null || echo "::error::기존 $LEGACY 컨테이너를 다시 시작하지 못했습니다."
  fi
  if [ -n "$new_app" ]; then
    docker rm -f "$new_app" >/dev/null 2>&1
  fi
  docker rm -f "$PROXY_CHECK" >/dev/null 2>&1

  local body=""
  for _ in $(seq 1 15); do
    body="$(curl --silent --fail --max-time 2 "$APP_URL/health")" && break
    sleep 1
  done
  log "service after rollback: ${body:-no response}"
}

on_exit() {
  local status=$?
  trap - EXIT INT TERM
  set +e
  if [ "$status" -ne 0 ] && [ "$committed" != true ]; then
    rollback
    output failure_reason "${failure_reason:-배포 단계에서 오류가 발생했습니다 (로그 참고).}"
  fi
  rm -rf "$work_dir"
  exit "$status"
}
trap on_exit EXIT
trap 'failure_reason="배포가 중단되었습니다."; exit 1' INT TERM

# ----- deployment steps ------------------------------------------------------

start_new_app() {
  docker rm -f "$new_app" >/dev/null 2>&1 || true
  log "starting $new_app (no host port)"
  docker run --detach \
    --name "$new_app" \
    --network "$NETWORK" \
    --restart unless-stopped \
    --env-file "$SPRITE_ENV_FILE" \
    --env "APP_VERSION=$COMMIT_SHA" \
    "$IMAGE" >/dev/null
}

wait_for_new_app() {
  local probe="import urllib.request as u; print(u.urlopen('http://127.0.0.1:8000/health', timeout=2).read().decode())"
  local status body=""
  for attempt in $(seq 1 "$HEALTH_ATTEMPTS"); do
    status="$(docker inspect --format '{{.State.Status}}' "$new_app")"
    case "$status" in
      exited | dead | restarting) fail "새 컨테이너가 시작 직후 중단되었습니다 (상태: $status). 이전 버전을 유지합니다." ;;
    esac
    if body="$(docker exec "$new_app" python -c "$probe" 2>/dev/null)" && health_matches "$body"; then
      log "$new_app is healthy after $attempt attempt(s)"
      return 0
    fi
    sleep 2
  done
  fail "새 컨테이너가 $((HEALTH_ATTEMPTS * 2))초 안에 정상 응답하지 않았습니다. 기대 버전: $COMMIT_SHA, 응답: ${body:-없음}. 이전 버전을 유지합니다."
}

# Traffic check through the fixed address. The last old worker can still answer right
# after a reload, so this waits for several new-version answers in a row.
verify_through_proxy() {
  local matches=0 body=""
  for _ in $(seq 1 15); do
    body="$(curl --silent --fail --max-time 3 "$APP_URL/health")" || body=""
    if health_matches "$body"; then
      matches=$((matches + 1))
      if [ "$matches" -ge 3 ]; then
        local page
        page="$(curl --silent --fail --max-time 3 "$APP_URL/")" || page=""
        grep -qi '<html' <<<"$page" || fail "프록시를 통한 메인 페이지 확인에 실패했습니다. 이전 버전으로 되돌립니다."
        log "proxy serves the new version: $body"
        return 0
      fi
    else
      matches=0
    fi
    sleep 1
  done
  fail "프록시를 통한 /health가 새 버전을 반환하지 않습니다. 기대 버전: $COMMIT_SHA, 응답: ${body:-없음}. 이전 버전으로 되돌립니다."
}

stop_old_container() {
  local name="$1" limit="$2"
  log "stopping $name (up to ${limit}s for in-flight requests)"
  if ! docker stop -t "$limit" "$name" >/dev/null || ! docker rm "$name" >/dev/null; then
    echo "::warning::$name 컨테이너를 정리하지 못했습니다. 새 버전은 정상 서비스 중입니다."
  fi
}

# First run: the single container owns the port, so it must stop before the proxy can
# bind it. Everything that can be checked in advance is checked first to keep this short.
migrate_from_single_container() {
  mode=migration
  old_app="$LEGACY"
  new_app="$PREFIX-blue"
  output mode migration
  output to_color blue
  start_new_app
  wait_for_new_app
  write_upstream_file "$new_app"

  log "checking the proxy configuration against $new_app without a host port"
  docker rm -f "$PROXY_CHECK" >/dev/null 2>&1 || true
  create_proxy "$PROXY_CHECK"
  docker start "$PROXY_CHECK" >/dev/null
  local body="" passed=false
  for _ in $(seq 1 10); do
    if body="$(docker exec "$PROXY_CHECK" wget -qO- -T 3 http://127.0.0.1/health 2>/dev/null)" && health_matches "$body"; then
      passed=true
      break
    fi
    sleep 1
  done
  [ "$passed" = true ] || fail "프록시에서 새 컨테이너로 연결되지 않습니다. 이전 버전을 유지합니다."
  docker rm -f "$PROXY_CHECK" >/dev/null

  create_proxy "$PROXY" --publish "127.0.0.1:$PROXY_PORT:80"
  proxy_created=true
  if container_exists "$LEGACY"; then
    log "cutover: stopping $LEGACY and binding the port to $PROXY"
    legacy_stopped=true
    docker stop -t "$LEGACY_STOP_SECONDS" "$LEGACY" >/dev/null
  fi
  docker start "$PROXY" >/dev/null || fail "프록시 컨테이너를 시작하지 못했습니다 (포트 $PROXY_PORT 확인). 이전 컨테이너로 되돌립니다."
  verify_through_proxy
  committed=true

  if container_exists "$LEGACY"; then
    docker rm "$LEGACY" >/dev/null || echo "::warning::$LEGACY 컨테이너를 삭제하지 못했습니다."
  fi
}

deploy_blue_green() {
  mode=bluegreen
  local running conf from to current_image
  running="$(docker inspect --format '{{.State.Running}}' "$PROXY")"
  [ "$running" = true ] || fail "$PROXY 컨테이너가 실행 중이 아닙니다. 맥북에서 Docker 상태를 확인하세요."

  current_image="$(docker inspect --format '{{.Config.Image}}' "$PROXY")"
  if [ "$current_image" != "$PROXY_IMAGE" ]; then
    echo "::warning::실행 중인 프록시 이미지가 PROXY_IMAGE와 다릅니다. 프록시 교체는 포트를 비워야 해서 자동으로 하지 않습니다."
  fi

  conf="$(docker exec "$PROXY" cat "$UPSTREAM_FILE")"
  case "$conf" in
    *"http://$PREFIX-blue:8000"*) from=blue; to=green ;;
    *"http://$PREFIX-green:8000"*) from=green; to=blue ;;
    *) fail "프록시 설정에서 현재 활성 색을 알 수 없습니다." ;;
  esac
  old_app="$PREFIX-$from"
  new_app="$PREFIX-$to"
  log "active: $from, deploying to: $to"
  output mode bluegreen
  output from_color "$from"
  output to_color "$to"

  start_new_app
  wait_for_new_app

  local body=""
  if ! body="$(docker exec "$PROXY" wget -qO- -T 3 "http://$new_app:8000/health" 2>/dev/null)" || ! health_matches "$body"; then
    fail "프록시에서 새 컨테이너로 연결되지 않습니다. 이전 버전을 유지합니다."
  fi

  log "switching the proxy to $new_app"
  switch_proxy
  verify_through_proxy
  committed=true

  if container_exists "$old_app"; then
    stop_old_container "$old_app" "$DRAIN_SECONDS"
  fi
}

docker network inspect "$NETWORK" >/dev/null 2>&1 || docker network create "$NETWORK" >/dev/null

if container_exists "$PROXY"; then
  deploy_blue_green
else
  migrate_from_single_container
fi
log "done"
