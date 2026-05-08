"""
CDK Vaults — 数据库初始化与连接管理
使用 SQLite + WAL 模式，零配置，单文件存储
"""

import sqlite3
import os
from contextlib import contextmanager

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "vaults.db")


def get_db() -> sqlite3.Connection:
    """获取数据库连接，启用 WAL 模式和外键约束"""
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_db_context(transaction_mode: str | None = None):
    """数据库连接上下文管理器，自动提交/回滚"""
    conn = get_db()
    try:
        if transaction_mode:
            mode = transaction_mode.upper()
            if mode not in {"DEFERRED", "IMMEDIATE", "EXCLUSIVE"}:
                raise ValueError(f"unsupported SQLite transaction mode: {transaction_mode}")
            conn.execute(f"BEGIN {mode}")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row["name"] == column for row in conn.execute(f"PRAGMA table_info({table})").fetchall())


def _migrate_cdk_codes_schema(conn: sqlite3.Connection):
    """Allow CDKs to exist before assets are assigned and store their category directly."""
    columns = conn.execute("PRAGMA table_info(cdk_codes)").fetchall()
    if not columns:
        return

    column_names = {row["name"] for row in columns}
    asset_id_notnull = any(row["name"] == "asset_id" and row["notnull"] for row in columns)
    if "category_id" in column_names and not asset_id_notnull:
        conn.execute("""
            UPDATE cdk_codes
            SET category_id = COALESCE(
                category_id,
                (SELECT a.category_id FROM assets a WHERE a.id = cdk_codes.asset_id)
            )
        """)
        return

    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("PRAGMA legacy_alter_table=ON")
    conn.execute("ALTER TABLE cdk_codes RENAME TO cdk_codes_old")
    conn.execute("""
        CREATE TABLE cdk_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            asset_id INTEGER,
            category_id INTEGER,
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'used', 'disabled', 'expired')),
            max_uses INTEGER DEFAULT 1,
            used_count INTEGER DEFAULT 0,
            note TEXT DEFAULT '',
            expires_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (asset_id) REFERENCES assets(id) ON DELETE SET NULL,
            FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE SET NULL
        )
    """)

    old_columns = {row["name"] for row in conn.execute("PRAGMA table_info(cdk_codes_old)").fetchall()}
    category_expr = (
        "category_id"
        if "category_id" in old_columns
        else "(SELECT a.category_id FROM assets a WHERE a.id = cdk_codes_old.asset_id)"
    )
    conn.execute(f"""
        INSERT INTO cdk_codes (
            id, code, asset_id, category_id, status, max_uses,
            used_count, note, expires_at, created_at
        )
        SELECT
            id, code, asset_id, {category_expr}, status, max_uses,
            used_count, note, expires_at, created_at
        FROM cdk_codes_old
    """)
    conn.execute("DROP TABLE cdk_codes_old")
    conn.execute("PRAGMA legacy_alter_table=OFF")
    conn.execute("PRAGMA foreign_keys=ON")


def _ensure_cdk_indexes(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_cdk_code ON cdk_codes(code);
        CREATE INDEX IF NOT EXISTS idx_cdk_status ON cdk_codes(status);
        CREATE INDEX IF NOT EXISTS idx_cdk_asset ON cdk_codes(asset_id);
        CREATE INDEX IF NOT EXISTS idx_cdk_category ON cdk_codes(category_id);
    """)


def _dedupe_cdk_asset_bindings(conn: sqlite3.Connection):
    """
    Keep exactly one cdk_assets row per asset before adding the unique index.
    Prefer rows that already have redemption evidence, then consumed rows, then oldest binding.
    """
    conn.execute("DROP TABLE IF EXISTS temp.cdk_asset_keep")
    conn.execute("""
        CREATE TEMP TABLE cdk_asset_keep AS
        SELECT
            ca.asset_id,
            (
                SELECT ca2.id
                FROM cdk_assets ca2
                LEFT JOIN assets a2 ON a2.id = ca2.asset_id
                WHERE ca2.asset_id = ca.asset_id
                ORDER BY
                    CASE
                        WHEN EXISTS (
                            SELECT 1
                            FROM redemption_logs rl
                            WHERE rl.cdk_id = ca2.cdk_id AND rl.asset_id = ca2.asset_id
                        ) THEN 0
                        WHEN a2.consumed_by_cdk_id = ca2.cdk_id THEN 1
                        WHEN ca2.consumed_at IS NOT NULL THEN 2
                        ELSE 3
                    END,
                    ca2.id ASC
                LIMIT 1
            ) AS keep_id
        FROM cdk_assets ca
        GROUP BY ca.asset_id
    """)
    conn.execute("""
        DELETE FROM cdk_assets
        WHERE id NOT IN (SELECT keep_id FROM cdk_asset_keep)
    """)
    conn.execute("DROP TABLE IF EXISTS temp.cdk_asset_keep")

    # Keep the legacy primary asset pointer aligned with the surviving binding.
    conn.execute("""
        UPDATE cdk_codes
        SET asset_id = (
            SELECT ca.asset_id
            FROM cdk_assets ca
            WHERE ca.cdk_id = cdk_codes.id
            ORDER BY ca.id ASC
            LIMIT 1
        )
        WHERE asset_id IS NULL
          OR NOT EXISTS (
              SELECT 1
              FROM cdk_assets ca
              WHERE ca.cdk_id = cdk_codes.id AND ca.asset_id = cdk_codes.asset_id
          )
    """)
    conn.execute("""
        UPDATE cdk_codes
        SET used_count = (
            SELECT COUNT(*)
            FROM cdk_assets ca
            WHERE ca.cdk_id = cdk_codes.id AND ca.consumed_at IS NOT NULL
        )
    """)
    conn.execute("""
        UPDATE cdk_codes
        SET status = CASE
            WHEN status IN ('disabled', 'expired') THEN status
            WHEN used_count >= max_uses THEN 'used'
            ELSE 'active'
        END
    """)


def _ensure_unique_asset_binding(conn: sqlite3.Connection):
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_cdk_assets_asset_unique ON cdk_assets(asset_id)")


def init_db():
    """初始化数据库表结构"""
    os.makedirs(os.path.join(BASE_DIR, "uploads"), exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)

    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            description TEXT DEFAULT '',
            color TEXT DEFAULT '#8b5cf6',
            sort_order INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            type TEXT NOT NULL CHECK(type IN ('file', 'text', 'link')),
            description TEXT DEFAULT '',
            file_path TEXT,
            content TEXT,
            category_id INTEGER,
            thumbnail TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS cdk_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            asset_id INTEGER,
            category_id INTEGER,
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'used', 'disabled', 'expired')),
            max_uses INTEGER DEFAULT 1,
            used_count INTEGER DEFAULT 0,
            note TEXT DEFAULT '',
            expires_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (asset_id) REFERENCES assets(id) ON DELETE SET NULL,
            FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS redemption_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cdk_id INTEGER NOT NULL,
            asset_id INTEGER NOT NULL,
            ip_address TEXT,
            user_agent TEXT,
            redeemed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (cdk_id) REFERENCES cdk_codes(id),
            FOREIGN KEY (asset_id) REFERENCES assets(id)
        );

        CREATE TABLE IF NOT EXISTS file_download_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT NOT NULL UNIQUE,
            cdk_id INTEGER NOT NULL,
            asset_id INTEGER NOT NULL,
            used_at TIMESTAMP,
            expires_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (cdk_id) REFERENCES cdk_codes(id) ON DELETE CASCADE,
            FOREIGN KEY (asset_id) REFERENCES assets(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS cdk_assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cdk_id INTEGER NOT NULL,
            asset_id INTEGER NOT NULL,
            consumed_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (cdk_id) REFERENCES cdk_codes(id) ON DELETE CASCADE,
            FOREIGN KEY (asset_id) REFERENCES assets(id) ON DELETE CASCADE,
            UNIQUE (cdk_id, asset_id)
        );

        CREATE INDEX IF NOT EXISTS idx_log_cdk ON redemption_logs(cdk_id);
        CREATE INDEX IF NOT EXISTS idx_log_time ON redemption_logs(redeemed_at);
        CREATE INDEX IF NOT EXISTS idx_asset_category ON assets(category_id);
        CREATE INDEX IF NOT EXISTS idx_file_download_token ON file_download_tokens(token);
        CREATE INDEX IF NOT EXISTS idx_file_download_expires ON file_download_tokens(expires_at);
        CREATE INDEX IF NOT EXISTS idx_cdk_assets_cdk ON cdk_assets(cdk_id);
        CREATE INDEX IF NOT EXISTS idx_cdk_assets_asset ON cdk_assets(asset_id);
        CREATE INDEX IF NOT EXISTS idx_cdk_assets_consumed ON cdk_assets(consumed_at);
    """)

    _migrate_cdk_codes_schema(conn)
    _ensure_cdk_indexes(conn)

    if not _column_exists(conn, "assets", "consumed_at"):
        conn.execute("ALTER TABLE assets ADD COLUMN consumed_at TIMESTAMP")
    if not _column_exists(conn, "assets", "consumed_by_cdk_id"):
        conn.execute("ALTER TABLE assets ADD COLUMN consumed_by_cdk_id INTEGER")

    # 兼容旧 CDK：旧的一码一资产关系迁移成资产额度明细。
    conn.execute("""
        INSERT OR IGNORE INTO cdk_assets (cdk_id, asset_id, consumed_at)
        SELECT id, asset_id, CASE WHEN used_count > 0 THEN CURRENT_TIMESTAMP ELSE NULL END
        FROM cdk_codes
        WHERE asset_id IS NOT NULL
    """)
    conn.execute("""
        UPDATE cdk_codes
        SET category_id = COALESCE(
            category_id,
            (SELECT a.category_id FROM assets a WHERE a.id = cdk_codes.asset_id)
        )
    """)
    conn.execute("""
        UPDATE assets
        SET consumed_at = COALESCE(consumed_at, CURRENT_TIMESTAMP),
            consumed_by_cdk_id = COALESCE(
                consumed_by_cdk_id,
                (SELECT c.id FROM cdk_codes c WHERE c.asset_id = assets.id AND c.used_count > 0 LIMIT 1)
            )
        WHERE consumed_at IS NULL
          AND id IN (SELECT asset_id FROM cdk_codes WHERE used_count > 0)
    """)
    _dedupe_cdk_asset_bindings(conn)
    _ensure_unique_asset_binding(conn)

    # ── 内置 Codex 分类 (不可删除) ─────────────────────
    existing = conn.execute("SELECT id FROM categories WHERE name = 'Codex'").fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO categories (name, description, color, sort_order) VALUES (?, ?, ?, ?)",
            ("Codex", "OpenAI OAuth 凭据管理", "#f59e0b", -1),
        )

    conn.commit()
    conn.close()
    print("✅ 数据库初始化完成")
