#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${SCRIPT_DIR}"
SERVICE_NAME="${SERVICE_NAME:-cdk-vaults}"
SERVICE_LABEL="${SERVICE_LABEL:-local.${SERVICE_NAME}}"
SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-${USER}}}"
UV_BIN="${UV_BIN:-}"

log() {
    printf '[init] %s\n' "$*"
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
    export PATH="${install_dir}:${PATH}"

    if [[ ! -x "${install_dir}/uv" ]]; then
        printf 'uv installation failed: %s not found\n' "${install_dir}/uv" >&2
        exit 1
    fi
    UV_BIN="${install_dir}/uv"
}

ensure_env() {
    if [[ ! -f "${PROJECT_DIR}/.env" ]]; then
        if [[ ! -f "${PROJECT_DIR}/.env.example" ]]; then
            printf 'Missing .env.example; cannot create .env\n' >&2
            exit 1
        fi
        cp "${PROJECT_DIR}/.env.example" "${PROJECT_DIR}/.env"
        log "Created .env from .env.example. Edit it before exposing the service."
    fi
}

install_dependencies() {
    ensure_uv
    log "Installing dependencies with uv"
    (cd "${PROJECT_DIR}" && "${UV_BIN}" sync --locked)
}

install_systemd() {
    local service_file="/etc/systemd/system/${SERVICE_NAME}.service"

    log "Installing systemd service: ${service_file}"
    if [[ "${EUID}" -eq 0 ]]; then
        cat > "${service_file}" <<EOF
[Unit]
Description=CDK Vaults
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${PROJECT_DIR}
EnvironmentFile=-${PROJECT_DIR}/.env
ExecStart=${UV_BIN} run cdk-vaults
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    else
        need_cmd sudo
        sudo tee "${service_file}" >/dev/null <<EOF
[Unit]
Description=CDK Vaults
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${PROJECT_DIR}
EnvironmentFile=-${PROJECT_DIR}/.env
ExecStart=${UV_BIN} run cdk-vaults
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    fi

    run_root systemctl daemon-reload
    run_root systemctl enable --now "${SERVICE_NAME}"
    run_root systemctl status "${SERVICE_NAME}" --no-pager || true
}

install_launchd() {
    local plist log_dir
    plist="${HOME}/Library/LaunchAgents/${SERVICE_LABEL}.plist"
    log_dir="${PROJECT_DIR}/logs"

    mkdir -p "${HOME}/Library/LaunchAgents" "${log_dir}"

    log "Installing launchd user service: ${plist}"
    tee "${plist}" >/dev/null <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${SERVICE_LABEL}</string>
    <key>WorkingDirectory</key>
    <string>${PROJECT_DIR}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${UV_BIN}</string>
        <string>run</string>
        <string>cdk-vaults</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${log_dir}/${SERVICE_NAME}.out.log</string>
    <key>StandardErrorPath</key>
    <string>${log_dir}/${SERVICE_NAME}.err.log</string>
</dict>
</plist>
EOF

    launchctl unload "${plist}" >/dev/null 2>&1 || true
    launchctl load "${plist}"
    launchctl start "${SERVICE_LABEL}" >/dev/null 2>&1 || true
    log "launchd service loaded: ${SERVICE_LABEL}"
}

main() {
    ensure_env
    install_dependencies

    case "$(uname -s)" in
        Linux)
            need_cmd systemctl
            install_systemd
            ;;
        Darwin)
            need_cmd launchctl
            install_launchd
            ;;
        *)
            printf 'Unsupported OS: %s\n' "$(uname -s)" >&2
            exit 1
            ;;
    esac

    log "Done"
}

main "$@"
