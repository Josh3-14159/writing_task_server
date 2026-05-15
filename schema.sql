-- =============================================================
-- Handwriting Study — PostgreSQL Schema
-- Run once against a fresh database:
--   psql $DATABASE_URL -f schema.sql
-- =============================================================

-- ── device_tokens ────────────────────────────────────────────
-- One row per browser/device. Created on first visit.
CREATE TABLE IF NOT EXISTS device_tokens (
    token            CHAR(32) PRIMARY KEY,
    first_seen       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen        TIMESTAMPTZ NOT NULL DEFAULT now(),
    submission_count INT NOT NULL DEFAULT 0
);

-- ── sessions ─────────────────────────────────────────────────
-- One row per study attempt.
CREATE TABLE IF NOT EXISTS sessions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    token           CHAR(32) NOT NULL REFERENCES device_tokens(token) ON DELETE CASCADE,
    status          VARCHAR(20) NOT NULL DEFAULT 'fresh',
    completed       BOOLEAN NOT NULL DEFAULT false,
    abandoned       BOOLEAN NOT NULL DEFAULT false,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ,

    -- Intake form fields
    age                     INT,
    gender                  VARCHAR(50),
    handedness              VARCHAR(20),
    writing_hand            VARCHAR(10),
    input_device            VARCHAR(30),
    parkinsons_diagnosis    BOOLEAN,
    parkinsons_stage        VARCHAR(20),
    other_conditions        TEXT,
    motor_medication        VARCHAR(30),
    hand_steadiness         VARCHAR(30),
    writing_hours_per_day   FLOAT,
    writing_style           VARCHAR(20),
    consent                 BOOLEAN NOT NULL DEFAULT false,
    consent_at              TIMESTAMPTZ
);

-- ── sentences ─────────────────────────────────────────────────
-- Corpus of sentences shown during the 'sentence' writing task.
-- Rows should never be deleted once referenced by session_sentences;
-- use active = false to retire a sentence from future assignments.
CREATE TABLE IF NOT EXISTS sentences (
    id      SMALLSERIAL PRIMARY KEY,
    text    TEXT        NOT NULL,
    active  BOOLEAN     NOT NULL DEFAULT true
);

CREATE INDEX IF NOT EXISTS idx_sentences_active ON sentences (active);

-- ── session_sentences ─────────────────────────────────────────
-- Records which sentences were assigned to each session, and in
-- what order. task_index here is the same value stored in
-- strokes.task_index for task_name = 'sentence', allowing a direct
-- join from any stroke point back to the sentence that produced it.
CREATE TABLE IF NOT EXISTS session_sentences (
    session_id  UUID     NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    task_index  INT      NOT NULL,   -- 0-based display order for this session
    sentence_id SMALLINT NOT NULL REFERENCES sentences(id),
    PRIMARY KEY (session_id, task_index)
);

CREATE INDEX IF NOT EXISTS idx_session_sentences_session_id
    ON session_sentences (session_id);

CREATE INDEX IF NOT EXISTS idx_session_sentences_sentence_id
    ON session_sentences (sentence_id);

-- ── strokes (partitioned by task_name) ───────────────────────
-- One row per captured sample point. Partition key must be included
-- in any unique/primary key constraint on a partitioned table.
-- For task_name = 'sentence', join to session_sentences on
-- (session_id, task_index) to recover the sentence text.
CREATE TABLE IF NOT EXISTS strokes (
    id              BIGSERIAL,
    session_id      UUID NOT NULL,
    task_type       VARCHAR(10) NOT NULL,
    task_name       VARCHAR(30) NOT NULL,
    orientation     VARCHAR(12),
    task_index      INT NOT NULL DEFAULT 0,
    stroke_index    INT NOT NULL,
    point_index     INT NOT NULL,
    x               NUMERIC(8,2) NOT NULL,   -- mm from start dot, X+ right
    y               NUMERIC(8,2) NOT NULL,   -- mm from start dot, Y+ up
    time_ms         NUMERIC(10,2) NOT NULL,
    pressure        NUMERIC(6,4),
    tilt_x_deg      NUMERIC(6,2),
    tilt_y_deg      NUMERIC(6,2),
    pointer_type    VARCHAR(10)
) PARTITION BY LIST (task_name);

CREATE TABLE IF NOT EXISTS strokes_straight_line PARTITION OF strokes
    FOR VALUES IN ('straight_line');

CREATE TABLE IF NOT EXISTS strokes_arc PARTITION OF strokes
    FOR VALUES IN ('arc');

CREATE TABLE IF NOT EXISTS strokes_wave PARTITION OF strokes
    FOR VALUES IN ('wave');

CREATE TABLE IF NOT EXISTS strokes_spiral_round PARTITION OF strokes
    FOR VALUES IN ('spiral_round');

CREATE TABLE IF NOT EXISTS strokes_spiral_square PARTITION OF strokes
    FOR VALUES IN ('spiral_square');

-- Writing tasks share one partition — add new task_names here as needed
CREATE TABLE IF NOT EXISTS strokes_writing PARTITION OF strokes
    FOR VALUES IN ('healthy_control', 'parkinsons_disease', 'sentence');

-- Per-partition unique indexes on id (Postgres cannot create a global
-- unique index on a non-partition-key column of a partitioned table)
CREATE UNIQUE INDEX IF NOT EXISTS strokes_straight_line_id_idx ON strokes_straight_line (id);
CREATE UNIQUE INDEX IF NOT EXISTS strokes_arc_id_idx           ON strokes_arc (id);
CREATE UNIQUE INDEX IF NOT EXISTS strokes_wave_id_idx          ON strokes_wave (id);
CREATE UNIQUE INDEX IF NOT EXISTS strokes_spiral_round_id_idx  ON strokes_spiral_round (id);
CREATE UNIQUE INDEX IF NOT EXISTS strokes_spiral_square_id_idx ON strokes_spiral_square (id);
CREATE UNIQUE INDEX IF NOT EXISTS strokes_writing_id_idx       ON strokes_writing (id);

-- ── Indexes ──────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_strokes_session_id  ON strokes (session_id);
CREATE INDEX IF NOT EXISTS idx_strokes_task_name   ON strokes (task_name);
CREATE INDEX IF NOT EXISTS idx_strokes_task_type   ON strokes (task_type);
CREATE INDEX IF NOT EXISTS idx_strokes_orientation ON strokes (orientation);
CREATE INDEX IF NOT EXISTS idx_sessions_token      ON sessions (token);
CREATE INDEX IF NOT EXISTS idx_sessions_completed  ON sessions (completed, abandoned);

-- ── Views ────────────────────────────────────────────────────
CREATE OR REPLACE VIEW task_summary AS
SELECT
    s.id                              AS session_id,
    s.token,
    st.task_type,
    st.task_name,
    st.orientation,
    st.task_index,
    COUNT(*)                          AS total_points,
    COUNT(DISTINCT st.stroke_index)   AS total_strokes,
    AVG(st.pressure)                  AS mean_pressure,
    MIN(st.time_ms)                   AS start_ms,
    MAX(st.time_ms)                   AS end_ms,
    MAX(st.time_ms) - MIN(st.time_ms) AS duration_ms
FROM strokes st
JOIN sessions s ON st.session_id = s.id
GROUP BY s.id, s.token, st.task_type, st.task_name, st.orientation, st.task_index;

-- Extends task_summary for sentence tasks only, joining in the
-- assigned sentence text for each (session, task_index) pair.
CREATE OR REPLACE VIEW sentence_task_summary AS
SELECT
    ts.*,
    ss.sentence_id,
    sen.text AS sentence_text
FROM task_summary ts
JOIN session_sentences ss
    ON ss.session_id = ts.session_id
   AND ss.task_index = ts.task_index
JOIN sentences sen ON sen.id = ss.sentence_id
WHERE ts.task_name = 'sentence';
