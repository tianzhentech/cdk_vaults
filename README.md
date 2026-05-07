# CDK Vaults

## Development

This project uses `uv` for Python dependency and virtual environment management.

```bash
cp .env.example .env
uv sync
uv run cdk-vaults --reload --port 8000
```

The `cdk-vaults` entry point also works from project subdirectories such as `server/`:

```bash
uv run cdk-vaults --reload --port 8000
```

Useful checks:

```bash
uv run python -m compileall server
node --check admin/app.js
node --check public/app.js
```

## Service Install / Update

Initialize the project, install dependencies, and register a system service:

```bash
./init_service.sh
```

If `uv` is not installed, the script installs a project-local copy at
`.local/bin/uv`.

Apply code updates, sync dependencies, and restart the service:

```bash
./update_service.sh
```

Linux installs a `systemd` service named `cdk-vaults`. macOS installs a
user `launchd` service named `local.cdk-vaults`.
