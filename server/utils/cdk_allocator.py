"""
Helpers for attaching newly-added assets to CDKs that were generated before
their full asset quota was available.
"""


def _category_match_expr() -> str:
    return """
        (
            COALESCE(
                c.category_id,
                (SELECT a.category_id FROM assets a WHERE a.id = c.asset_id)
            ) = ?
            OR (
                COALESCE(
                    c.category_id,
                    (SELECT a.category_id FROM assets a WHERE a.id = c.asset_id)
                ) IS NULL
                AND ? IS NULL
            )
        )
    """


def assign_asset_to_pending_cdk(db, asset_id: int, category_id: int | None) -> int | None:
    """Assign one unbound asset to the oldest CDK in the same category with free quota."""
    already_bound = db.execute(
        "SELECT 1 FROM cdk_assets WHERE asset_id = ? LIMIT 1",
        (asset_id,),
    ).fetchone()
    if already_bound:
        return None

    cdk = db.execute(
        f"""
        SELECT c.id, c.asset_id
        FROM cdk_codes c
        WHERE {_category_match_expr()}
          AND c.status IN ('active', 'disabled')
          AND c.used_count < c.max_uses
          AND (
              SELECT COUNT(*)
              FROM cdk_assets ca
              WHERE ca.cdk_id = c.id
          ) < c.max_uses
        ORDER BY c.created_at ASC, c.id ASC
        LIMIT 1
        """,
        (category_id, category_id),
    ).fetchone()
    if not cdk:
        return None

    db.execute(
        "INSERT OR IGNORE INTO cdk_assets (cdk_id, asset_id) VALUES (?, ?)",
        (cdk["id"], asset_id),
    )
    if not cdk["asset_id"]:
        db.execute(
            "UPDATE cdk_codes SET asset_id = ? WHERE id = ? AND asset_id IS NULL",
            (asset_id, cdk["id"]),
        )
    return cdk["id"]
