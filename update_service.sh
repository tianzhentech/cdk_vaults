#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${SCRIPT_DIR}"
SERVICE_NAME="${SERVICE_NAME:-cdk-vaults}"
SERVICE_LABEL="${SERVICE_LABEL:-local.${SERVICE_NAME}}"
SKIP_PULL="${SKIP_PULL:-0}"
UV_BIN="${UV_BIN:-}"

log() {
    printf '[update] %s\n' "$*"
}

need_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        printf 'Missing required command: %s\n' "$1" >&2
        exit 1
    fi
}

run_root() {
    if [[ "${EUID}" -eq 0 ]]; then
        "$@"
    else
        need_cmd sudo
        sudo "$@"
    fi
}

ensure_uv() {
    if [[ -n "${UV_BIN}" && -x "${UV_BIN}" ]]; then
        return
    fi

    if command -v uv >/dev/null 2>&1; then
        UV_BIN="$(command -v uv)"
        return
    fi

    if [[ -x "${PROJECT_DIR}/.local/bin/uv" ]]; then
        UV_BIN="${PROJECT_DIR}/.local/bin/uv"
        return
    fi

    local install_dir installer
    install_dir="${PROJECT_DIR}/.local/bin"
    installer="$(mktemp)"
    mkdir -p "${install_dir}"

    log "uv not found; installing uv to ${install_dir}"
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf -o "${installer}" https://astral.sh/uv/install.sh
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "${installer}" https://astral.sh/uv/install.sh
    else
        printf 'Missing uv and also missing curl/wget to install it.\n' >&2
        rm -f "${installer}"
        exit 1
    fi

    UV_INSTALL_DIR="${install_dir}" sh "${installer}"
    rm -f "${installer}"
    if [[ ! -x "${install_dir}/uv" ]]; then
        printf 'uv installation failed: %s not found\n' "${install_dir}/uv" >&2
        exit 1
    fi
    UV_BIN="${install_dir}/uv"
}

pull_updates() {
    if [[ "${SKIP_PULL}" == "1" ]]; then
        log "Skipping git pull because SKIP_PULL=1"
        return
    fi

    if [[ ! -d "${PROJECT_DIR}/.git" ]]; then
        log "No .git directory found; skipping git pull"
        return
    fi

    need_cmd git
    log "Pulling latest code"
    (cd "${PROJECT_DIR}" && git pull --ff-only)
}

sync_dependencies() {
    ensure_uv
    log "Syncing dependencies"
    (cd "${PROJECT_DIR}" && "${UV_BIN}" sync --locked)
}

restart_systemd() {
    need_cmd systemctl
    log "Restarting systemd service: ${SERVICE_NAME}"
    run_root systemctl restart "${SERVICE_NAME}"
    run_root systemctl status "${SERVICE_NAME}" --no-pager || true
}

restart_launchd() {
    need_cmd launchctl
    local plist="${HOME}/Library/LaunchAgents/${SERVICE_LABEL}.plist"

    if [[ ! -f "${plist}" ]]; then
        printf 'Missing launchd plist: %s\nRun ./init_service.sh first.\n' "${plist}" >&2
        exit 1
    fi

    log "Restarting launchd service: ${SERVICE_LABEL}"
    launchctl kickstart -k "gui/$(id -u)/${SERVICE_LABEL}" >/dev/null 2>&1 || {
        launchctl unload "${plist}" >/dev/null 2>&1 || true
        launchctl load "${plist}"
        launchctl start "${SERVICE_LABEL}" >/dev/null 2>&1 || true
    }
}

main() {
    pull_updates
    sync_dependencies

    case "$(uname -s)" in
        Linux)
            restart_systemd
            ;;
        Darwin)
            restart_launchd
            ;;
        *)
            printf 'Unsupported OS: %s\n' "$(uname -s)" >&2
            exit 1
            ;;
    esac

    log "Done"
}

main "$@"
