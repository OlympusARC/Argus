-- Where an LLM agent's output lands.
--
-- Never the core tables. An agent proposes and a deterministic gate disposes,
-- and that separation is expressed as a table rather than a convention so it
-- cannot be argued away inside a prompt. Two kinds can never auto-apply
-- regardless of score -- anything touching the reconciler's close logic, and
-- anything that would mark a board dead -- and they are prevented by having
-- no gate rather than by a flag someone can flip.

CREATE TABLE IF NOT EXISTS proposals (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    agent      text   NOT NULL,
    kind       text   NOT NULL,
    payload    text   NOT NULL,
    evidence   text,
    status     text   NOT NULL DEFAULT 'drafted',
    score      double precision,
    created_at bigint NOT NULL,
    decided_at bigint,
    decided_by text
);

CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals (status, created_at DESC);
