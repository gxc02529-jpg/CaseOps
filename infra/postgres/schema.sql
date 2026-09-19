CREATE TABLE IF NOT EXISTS support_tickets (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    requester_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    description TEXT NOT NULL,
    component TEXT,
    product_version TEXT,
    reproduction_steps TEXT,
    impact_scope TEXT,
    order_scope JSONB NOT NULL DEFAULT '[]'::jsonb,
    tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_support_tickets_tenant_status
    ON support_tickets (tenant_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS support_cases (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    primary_ticket_id TEXT NOT NULL REFERENCES support_tickets(id),
    ticket_ids JSONB NOT NULL,
    shared_diagnosis BOOLEAN NOT NULL DEFAULT FALSE,
    match_reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS knowledge_candidates (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    case_id TEXT NOT NULL REFERENCES support_cases(id),
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    source_ticket_ids JSONB NOT NULL,
    evidence_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    status TEXT NOT NULL,
    reviewed_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_audit_events_entity
    ON audit_events (tenant_id, entity_id, occurred_at);

