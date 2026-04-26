# Handwriting Study — System Documentation

A browser-based handwriting data collection tool for neurological research. Participants complete a structured series of shape-tracing and writing tasks using an Apple Pencil or other input device. Stroke data is stored in a PostgreSQL database. Tasks are configured via a JSON file; each task type has a corresponding background HTML fragment served from a `tasks/` directory. A separate researcher export tool allows filtered CSV downloads of the collected data.

---

## File Structure

```
project/
├── pencil-capture.html        Participant-facing application
├── researcher-export.html     Researcher data export tool (served at /export)
├── server.py                  HTTP server (Python 3, psycopg2)
├── schema.sql                 PostgreSQL schema — run once to initialise
└── tasks/
    ├── tasks.json             Master task configuration
    ├── trace_line_horizontal.html
    ├── trace_line_vertical.html
    ├── trace_arc_horizontal.html
    ├── trace_arc_vertical.html
    ├── trace_wave_horizontal.html
    ├── trace_wave_vertical.html
    ├── trace_spiral_round.html
    ├── trace_spiral_square.html
    └── trace_writing_lines.html
```

---

## Requirements

- Python 3.8+
- PostgreSQL 14+ (list partitioning, `gen_random_uuid()`)
- `psycopg2-binary`: `pip install psycopg2-binary`
- iPad with Apple Pencil (preferred), Safari on iPadOS
- Both server machine and iPad on the same network

---

## Setup

### 1. Create and initialise the database

```bash
sudo -u postgres psql
```
```sql
CREATE ROLE muon WITH SUPERUSER LOGIN;
CREATE DATABASE handwriting OWNER muon;
\q
```
```bash
psql -d handwriting -f schema.sql
```

### 2. Create the read-only export user

The researcher export tool connects using a separate database user that has SELECT-only privileges. This limits the blast radius if the export endpoint is ever misused.

```sql
CREATE USER remote_readonly WITH PASSWORD '<your-password-here>';
GRANT CONNECT ON DATABASE handwriting TO remote_readonly;
GRANT USAGE ON SCHEMA public TO remote_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO remote_readonly;
```

### 3. Set environment variables and start the server

```bash
export DATABASE_URL="postgresql://muon@/handwriting"
export READONLY_DATABASE_URL="host=localhost dbname=handwriting user=remote_readonly password=<your-password-here>"
python3 server.py              # default port 8080
python3 server.py --port 3000  # custom port
```

> **Note on `READONLY_DATABASE_URL` format:** Use the keyword format (`host=... dbname=... user=... password=...`) rather than a URL (`postgresql://...`) if your password contains special characters such as `@`, `#`, or `!`. The keyword format does not require percent-encoding.

> **Warning:** If `READONLY_DATABASE_URL` is not set, the export endpoints fall back to `DATABASE_URL` and the server prints a warning at startup. This is not recommended in production.

### 4. Access from iPad

Open Safari on the iPad and navigate to:
```
http://<server-ip>:<port>
```

The researcher export tool is available at:
```
http://<server-ip>:<port>/export
```

### Run detached

```bash
nohup python3 server.py --port 8080 > server.log 2>&1 &
echo $! > server.pid

tail -f server.log          # monitor
kill $(cat server.pid)      # stop
```

### Reset the database (testing only)

```bash
psql -d handwriting -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public; GRANT ALL ON SCHEMA public TO muon;"
psql -d handwriting -f schema.sql
```

---

## Task Configuration (`tasks/tasks.json`)

The task list is loaded fresh on each server startup. To add, remove, or reorder tasks, edit `tasks.json` — no application code needs to change.

### Field reference

| Field | Type | Description |
|---|---|---|
| `order` | int | Execution order (sorted ascending at startup) |
| `task_name` | string | Must match a partition value in `schema.sql` and `VALID_TASK_NAMES` in `server.py` |
| `task_type` | string | `"shape"` or `"writing"` |
| `orientation` | string\|null | `"horizontal"`, `"vertical"`, or `null` |
| `repetitions` | int | Number of times the participant repeats this task before advancing |
| `canvas_html` | string | Filename inside `tasks/` — provides the background guide drawing |
| `instruction` | string | Shown in the task header above the canvas |
| `prompt` | string\|null | Shown in the yellow banner (writing tasks). `null` hides the banner |
| `label` | string | Short name shown in the progress bar |

### Current task list

| # | `task_name` | `task_type` | `orientation` | Reps |
|---|---|---|---|---|
| 1 | `straight_line` | `shape` | `horizontal` | 3 |
| 2 | `straight_line` | `shape` | `vertical` | 3 |
| 3 | `arc` | `shape` | `horizontal` | 3 |
| 4 | `arc` | `shape` | `vertical` | 3 |
| 5 | `wave` | `shape` | `horizontal` | 3 |
| 6 | `wave` | `shape` | `vertical` | 3 |
| 7 | `spiral_round` | `shape` | `null` | 6 |
| 8 | `spiral_square` | `shape` | `null` | 6 |
| 9 | `healthy_control` | `writing` | `null` | 1 |
| 10 | `parkinsons_disease` | `writing` | `null` | 1 |
| 11 | `sentence` | `writing` | `null` | 1 |

---

## Architecture

### Participant flow

```
Page load
  → fetch /tasks            (task list)
  → GET /session/check      (cookie check → mint or look up device token)
      ├── fresh             → intake form
      ├── restart           → warning interstitial → abandon old session → intake form
      └── warn_completed    → soft notice → intake form

Intake form submitted
  → POST /session/create    → session_id returned and stored client-side

For each task group (one entry in tasks.json):
  For each repetition:
    → host fetches /tasks/<canvas_html>, extracts <script>, evals in page scope
    → drawGuide() draws background onto baseline canvas, returns origin {x, y}
    → participant draws on task canvas (host handles all input)
    → taps Save & Continue
    → POST /strokes  { session_id, task_name, task_index, points: [...] }
    → canvas clears, rep counter advances

After final repetition of final task:
  → POST /session/complete
  → completion screen shown
```

### Canvas architecture

There is no iframe. Two `<canvas>` elements live directly in `pencil-capture.html`:

- `#baseline-canvas` — draws the reference guide shape (pointer events disabled)
- `#task-canvas` — captures participant input

When a task loads, the host fetches the corresponding `trace_*.html` file, extracts its `<script>` block, and injects it into the page. The script contains only a `drawGuide()` function which draws onto `bctx` (the baseline canvas context, available from host scope) and returns the CSS-pixel position of the start dot. All input handling, stroke storage, and data export live entirely in `pencil-capture.html`.

### Trace file contract

Each `trace_*.html` file must:

- Contain exactly one `<script>` block
- Define a single `drawGuide()` function
- Draw the reference guide onto `bctx` using `cssW()`, `cssH()`, and `mmToPx()` — all provided by the host
- Return `{ x, y }` in CSS pixels — the centre of the start dot — which becomes `(0, 0)` in saved data

No event listeners, no stroke arrays, no resize handlers, no canvas variable declarations. All of those live in the host.

---

## Researcher Export Tool

The export tool is served at `/export` and provides a browser-based interface for researchers to download filtered subsets of the dataset as CSV.

### Features

- **Column selection** — choose any combination of columns grouped by category: Identifiers, Position (x/y), Timing, Pressure & Tilt, and Input Device
- **Task filter** — include or exclude individual tasks; quick-filter by shape or writing type
- **Session quality filter** — completed sessions only toggle; include/exclude abandoned sessions
- **Date/time range filter** — filter by `started_at` or `completed_at` with optional from/to bounds. From-only applies a lower bound (after); to-only applies an upper bound (before)
- **Demographic filters** — Parkinson's diagnosis, H&Y stage, gender, dominant hand, other neurological conditions, hand steadiness, writing style, and age range
- **Live SQL preview** — the generated query is shown and can be copied before downloading
- **Estimated row count** — the preview bar shows the number of sessions and points matching the current filter before the download is triggered

### Security

The export endpoints (`/export/preview` and `/export/csv`) connect exclusively via `READONLY_DATABASE_URL`. This user has `SELECT` privileges only and cannot modify data. All filter values are passed as parameterised query arguments; column names are validated against a fixed allowlist before interpolation.

---

## Server API

### Participant endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Serve `pencil-capture.html` |
| `GET` | `/tasks` | Serve `tasks/tasks.json` |
| `GET` | `/tasks/<file>.html` | Serve a trace background fragment |
| `GET` | `/session/check` | Mint or look up device token; return participant status |
| `POST` | `/session/create` | Create session from intake form data |
| `POST` | `/session/abandon` | Mark an incomplete session as abandoned |
| `POST` | `/session/complete` | Mark session complete, increment submission count |
| `POST` | `/strokes` | Insert one repetition's worth of stroke points |

### Researcher export endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/export` | Serve `researcher-export.html` |
| `POST` | `/export/preview` | Return estimated row and session counts for a filter set |
| `POST` | `/export/csv` | Execute the filtered query and return result as a CSV download |

### `POST /strokes` payload

```json
{
  "session_id":  "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "task_name":   "spiral_round",
  "task_type":   "shape",
  "orientation": null,
  "task_index":  2,
  "points": [
    {
      "stroke_index": 0,
      "point_index":  0,
      "x":            -12.34,
      "y":             8.71,
      "time_ms":      1234.56,
      "pressure":     0.72,
      "tilt_x_deg":   12.0,
      "tilt_y_deg":   -5.0,
      "pointer_type": "pen"
    }
  ]
}
```

`x` and `y` are in millimetres relative to the start dot, Y+ up. Values can be negative.

### `POST /export/preview` payload

```json
{
  "columns":           ["session_id", "x", "y", "time_ms"],
  "tasks":             ["spiral_round", "spiral_square"],
  "completed_only":    true,
  "include_abandoned": false,
  "device":            "any",
  "pd_diagnosis":      "yes",
  "pd_stage":          "any",
  "gender":            "any",
  "handedness":        "any",
  "other_conditions":  "any",
  "hand_steadiness":   "any",
  "writing_style":     "any",
  "age_min":           null,
  "age_max":           null,
  "date_col":          "started_at",
  "date_from":         "2025-01-01T00:00",
  "date_to":           null
}
```

`/export/csv` accepts the same payload and returns `text/csv` with a `Content-Disposition: attachment` header.

---

## Database Schema

### `device_tokens`

One row per browser/device, created on first visit.

| Column | Type | Description |
|---|---|---|
| `token` | `CHAR(32)` | Random 32-char hex string, set as a long-lived cookie |
| `first_seen` | `TIMESTAMPTZ` | Timestamp of first visit |
| `last_seen` | `TIMESTAMPTZ` | Updated on every visit |
| `submission_count` | `INT` | Incremented each time a session is completed |

### `sessions`

One row per study attempt.

| Column | Type | Description |
|---|---|---|
| `id` | `UUID` | Primary key |
| `token` | `CHAR(32)` | FK → `device_tokens` (ON DELETE CASCADE) |
| `status` | `VARCHAR(20)` | `fresh`, `restart`, or `warn_completed` |
| `completed` | `BOOLEAN` | True when all tasks saved |
| `abandoned` | `BOOLEAN` | True when superseded by a new session |
| `started_at` | `TIMESTAMPTZ` | Session creation time |
| `completed_at` | `TIMESTAMPTZ` | Time of final task save |
| `age` … `consent_at` | various | Intake form fields |

### `strokes`

One row per captured sample point. Partitioned by `task_name`.

| Column | Type | Description |
|---|---|---|
| `session_id` | `UUID` | FK → `sessions` (enforced at application layer) |
| `task_type` | `VARCHAR(10)` | `shape` or `writing` |
| `task_name` | `VARCHAR(30)` | e.g. `spiral_round`, `straight_line` |
| `orientation` | `VARCHAR(12)` | `horizontal`, `vertical`, or `NULL` |
| `task_index` | `INT` | 0-based repetition index within the task group |
| `stroke_index` | `INT` | Index of each pen-down to pen-up stroke |
| `point_index` | `INT` | Sample index within the stroke |
| `x` | `NUMERIC(8,2)` | mm from start dot, X+ right |
| `y` | `NUMERIC(8,2)` | mm from start dot, Y+ up |
| `time_ms` | `NUMERIC(10,2)` | ms since page load |
| `pressure` | `NUMERIC(6,4)` | Normalised tip force 0.0–1.0, nullable |
| `tilt_x_deg` | `NUMERIC(6,2)` | Pen tilt X in degrees, nullable |
| `tilt_y_deg` | `NUMERIC(6,2)` | Pen tilt Y in degrees, nullable |
| `pointer_type` | `VARCHAR(10)` | `pen`, `touch`, or `mouse` |

### Partitions

```
strokes_straight_line  → ('straight_line')
strokes_arc            → ('arc')
strokes_wave           → ('wave')
strokes_spiral_round   → ('spiral_round')
strokes_spiral_square  → ('spiral_square')
strokes_writing        → ('healthy_control', 'parkinsons_disease', 'sentence')
```

### `task_summary` view

Aggregates stroke-level data into one row per task/orientation/repetition per session. Columns: `session_id`, `token`, `task_type`, `task_name`, `orientation`, `task_index`, `total_points`, `total_strokes`, `mean_pressure`, `start_ms`, `end_ms`, `duration_ms`.

---

## Coordinate System and Data Export

### Transform pipeline

Raw canvas pixel coordinates are transformed at save time before being sent to the database:

```
raw canvas px
  → subtract originPx (CSS-pixel position of start dot returned by drawGuide())
  → flip Y  (canvas Y increases downward; saved Y+ is up)
  → divide by PX_PER_MM_EXPORT
  → round to 2 decimal places
  → stored as mm
```

The origin `(0, 0)` in saved data corresponds to the centre of the start dot for that task. Positive X is right, positive Y is up.

### Calibration constants

Both constants are near the top of the JS section in `pencil-capture.html`:

```javascript
const RENDER_SCALE_CORRECTION = 160 / 117;   // visual guide sizing correction
const PX_PER_MM_EXPORT        = 5.197;        // px/mm for data export
```

**`RENDER_SCALE_CORRECTION`** compensates for a discrepancy between the browser's CSS `mm` unit and physical screen millimetres on this device. A guide commanded at 160 mm was measured at 117 mm on screen, giving a correction of 160/117 ≈ 1.368. This constant only affects how guide shapes are drawn — not the exported data. Adjust if guide sizes look wrong after changing device, browser zoom, or OS display scaling.

**`PX_PER_MM_EXPORT`** is the calibrated pixels-per-millimetre used when converting collected coordinates to millimetres for storage. Measured at 5.197 px/mm for this device. This has no effect on how guides appear on screen.

### `mmToPx` argument values in trace files

Because `RENDER_SCALE_CORRECTION` lives inside the host's `mmToPx()` function, all `mmToPx()` calls in the trace files use pre-scaled arguments (physical mm × 110/160 = physical mm × 0.6875). This means the values written in the trace files are not the physical targets — they are inputs that, after `RENDER_SCALE_CORRECTION` is applied inside `mmToPx()`, produce the correct physical size on screen.

| Physical target | Argument in trace file |
|---|---|
| 160 mm | `mmToPx(110)` |
| 80 mm | `mmToPx(55)` |
| 60 mm | `mmToPx(41.25)` |
| 40 mm | `mmToPx(27.5)` |
| 28 mm | `mmToPx(19.25)` |
| 14 mm | `mmToPx(9.625)` |

If `RENDER_SCALE_CORRECTION` is updated (e.g. after measuring on a different device), the trace file arguments must also be recalculated — see the Recalibration section below.

---

## Useful Queries

**All completed, non-abandoned sessions with participant metadata:**
```sql
SELECT * FROM sessions
WHERE completed = true AND abandoned = false;
```

**All spiral round data across participants:**
```sql
SELECT st.session_id, st.task_index, st.stroke_index, st.point_index,
       st.x, st.y, st.time_ms
FROM strokes st
JOIN sessions s ON st.session_id = s.id
WHERE st.task_name = 'spiral_round'
  AND s.completed = true AND s.abandoned = false
ORDER BY st.session_id, st.task_index, st.stroke_index, st.point_index;
```

**Per-task summary for one participant:**
```sql
SELECT * FROM task_summary WHERE session_id = '<uuid>';
```

**Pressure data for Parkinson's participants on spiral tasks:**
```sql
SELECT st.task_name, st.task_index, st.pressure, s.parkinsons_stage
FROM strokes st
JOIN sessions s ON st.session_id = s.id
WHERE s.parkinsons_diagnosis = true
  AND s.completed = true AND s.abandoned = false
  AND st.task_name IN ('spiral_round', 'spiral_square');
```

**Sessions in a date range:**
```sql
SELECT * FROM sessions
WHERE started_at >= '2025-01-01'
  AND started_at <  '2026-01-01'
  AND completed = true AND abandoned = false;
```

---

## Returning Participant Behaviour

| Status | Condition | Behaviour |
|---|---|---|
| `fresh` | No token, or token with no sessions | Proceed directly to intake form |
| `restart` | Token exists, most recent session incomplete | Warning shown; old session marked `abandoned = true`; new session starts |
| `warn_completed` | Token exists, most recent session completed | Soft notice shown; participant may continue |

---

## Input Handling

All input handling lives in `pencil-capture.html`. The primary path for Apple Pencil on Safari is Touch Events, with a passive Pointer Events sidecar listener that buffers the last 60 samples to recover `pressure`, `tiltX`, and `tiltY`. Each touch point is matched to the nearest-timestamp sidecar sample. Palm rejection uses `touch.touchType === 'direct'` (a Safari/WebKit extension) to discard finger contacts during stylus sessions. Mouse input falls back to Pointer Events only.

| Input | Stroke detection | Pressure | Tilt |
|---|---|---|---|
| Apple Pencil / stylus | Touch Events | ✓ sidecar merge | ✓ sidecar merge |
| Finger | Touch Events | ✓ `touch.force` | — |
| Mouse | Pointer Events | — | — |

---

## Extending the Study

### Adding a new task type

1. Create a new `trace_*.html` in `tasks/` following the contract above.
2. Add the new `task_name` to `VALID_TASK_NAMES` in `server.py`.
3. Add a new partition to `schema.sql` and apply it:
   ```sql
   CREATE TABLE strokes_my_task PARTITION OF strokes FOR VALUES IN ('my_task');
   ```
4. Add an entry to `tasks.json`.

### Recalibrating guide sizes (new device or display scaling)

1. Run the vertical line task and measure the rendered line length in mm.
2. Update `RENDER_SCALE_CORRECTION = 160 / <measured_mm>` in `pencil-capture.html`.
3. Recalculate all `mmToPx()` arguments in trace files: `physical_mm × (measured_mm / 160)`.

### Recalibrating export scale

1. Measure a known on-screen distance in pixels and in physical mm.
2. Compute `px / mm`.
3. Update `PX_PER_MM_EXPORT` in `pencil-capture.html`.
