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
