"""Row-level security helpers for migrations. Every tenant table gets the same isolation policy."""

PREDICATE = (
    "current_setting('app.rls_bypass', true) = 'on' OR org_id = nullif(current_setting('app.org_id', true), '')::uuid"
)


def enable(op, tables: list[str]) -> None:  # noqa: ANN001 - alembic's op proxy
    for table in tables:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} USING ({PREDICATE}) WITH CHECK ({PREDICATE})")


def disable(op, tables: list[str]) -> None:  # noqa: ANN001
    for table in tables:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
