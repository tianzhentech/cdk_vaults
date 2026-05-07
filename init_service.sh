#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${SCRIPT_DIR}"
SERVICE_NAME="${SERVICE_NAME:-cdk-vaults}"
SERVICE_LABEL="${SERVICE_LABEL:-local.${SERVICE_NAME}}"
SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-${USER}}}"

log() {
    printf '[init] %s\n' "$*"
}

need_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        printf 'Missing required command: %s\n' "$1" >&2
        exit 1
    fi
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
    need_cmd uv
    log "Installing dependencies with uv"
    (cd "${PROJECT_DIR}" && uv sync --locked)
}

install_systemd() {
    local uv_bin
    uv_bin="$(command -v uv)"
    local service_file="/etc/systemd/system/${SERVICE_NAME}.service"

    log "Installing systemd service: ${service_file}"
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
ExecStart=${uv_bin} run cdk-vaults
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    sudo systemctl enable --now "${SERVICE_NAME}"
    sudo systemctl status "${SERVICE_NAME}" --no-pager || true
}

install_launchd() {
    local uv_bin plist log_dir
    uv_bin="$(command -v uv)"
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
        <string>${uv_bin}</string>
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
