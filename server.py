#!/usr/bin/env python3
"""
Handwriting Study — Server
==========================
Serves the participant app and canvas fragments, manages device tokens
and sessions, and persists stroke data to a PostgreSQL database.
Also serves the researcher export tool and provides CSV export endpoints.

Usage
-----
  export DATABASE_URL="postgresql://user:pass@localhost/handwriting"
  export READONLY_DATABASE_URL="postgresql://readonly:pass@localhost/handwriting"
  python3 server.py                  # default port 8080
  python3 server.py --port 3000

Environment variables
---------------------
  DATABASE_URL          (required) psycopg2-compatible connection string.
                        Used for all participant-facing read/write operations.
  READONLY_DATABASE_URL (strongly recommended) A separate connection string
                        for a database user granted SELECT-only privileges.
                        Used exclusively by the researcher export endpoints.
                        Falls back to DATABASE_URL with a startup warning if
                        not set, but this is not recommended in production.
  PORT                  Optional fallback if --port is not passed.

Dependencies
------------
  pip install psycopg2-binary
"""

import argparse
import csv
import datetime
import io
import json
import os
import secrets
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from http.cookies import SimpleCookie
from urllib.parse import urlparse

import psycopg2
import psycopg2.extras

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ROOT_DIR    = os.path.dirname(os.path.abspath(__file__))
TASKS_DIR   = os.path.join(ROOT_DIR, 'tasks')
TASKS_JSON  = os.path.join(TASKS_DIR, 'tasks.json')
MAIN_HTML   = os.path.join(ROOT_DIR, 'pencil-capture.html')
EXPORT_HTML = os.path.join(ROOT_DIR, 'researcher-export.html')

# Valid task_name values must match the strokes partition list in schema.sql.
# The server validates every /strokes POST against this set so a misconfigured
# tasks.json cannot insert into an unmapped partition.
VALID_TASK_NAMES = {
    'straight_line', 'arc', 'wave',
    'spiral_round', 'spiral_square',
    'healthy_control', 'parkinsons_disease', 'sentence',
}

# Allowed stroke column names for export — validated before interpolation into
# the SELECT list, since column names cannot be passed as psycopg2 parameters.
EXPORT_STROKE_COLS = {
    'task_type', 'task_name', 'orientation', 'task_index',
    'stroke_index', 'point_index', 'x', 'y', 'time_ms',
    'pressure', 'tilt_x_deg', 'tilt_y_deg', 'pointer_type',
}

VALID_TASK_TYPES   = {'shape', 'writing'}
VALID_ORIENTATIONS = {'horizontal', 'vertical', None}

DATABASE_URL = os.environ.get('DATABASE_URL')
if not DATABASE_URL:
    sys.exit('ERROR: DATABASE_URL environment variable is not set.\n'
             'Example: export DATABASE_URL="postgresql://user:pass@localhost/handwriting"')

READONLY_DATABASE_URL = os.environ.get('READONLY_DATABASE_URL')
# Checked at startup — see __main__ block.

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_conn():
    """Return a new psycopg2 connection with autocommit off (read/write)."""
    return psycopg2.connect(DATABASE_URL)


def get_readonly_conn():
    """
    Return a connection using the read-only database user.
    Used exclusively by the researcher export endpoints so that
    even a compromised or malformed query cannot mutate data.
    Falls back to the main DATABASE_URL if READONLY_DATABASE_URL is unset
    (the startup warning makes this visible to the operator).
    """
    return psycopg2.connect(READONLY_DATABASE_URL or DATABASE_URL)


def _ts():
    return datetime.datetime.now().strftime('%H:%M:%S')


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):

    # ── routing ─────────────────────────────────────────────────────────────

    def do_GET(self):
        path = urlparse(self.path).path.rstrip('/')

        if path in ('', '/'):
            self._serve_file(MAIN_HTML, 'text/html; charset=utf-8')

        elif path == '/tasks':
            # Serve tasks.json so the frontend always gets the authoritative list
            self._serve_file(TASKS_JSON, 'application/json; charset=utf-8')

        elif path.startswith('/tasks/') and path.endswith('.html'):
            # Serves any .html fragment from the tasks/ directory.
            # Filenames are defined in tasks.json (canvas_html field) so no
            # allowlist is needed here — path traversal is blocked by basename().
            filename = os.path.basename(path)
            self._serve_file(os.path.join(TASKS_DIR, filename), 'text/html; charset=utf-8')

        elif path == '/session/check':
            self._handle_session_check()

        elif path == '/export':
            self._serve_file(EXPORT_HTML, 'text/html; charset=utf-8')

        else:
            self._404()

    def do_POST(self):
        path = urlparse(self.path).path.rstrip('/')

        if path == '/session/create':
            self._handle_session_create()
        elif path == '/session/abandon':
            self._handle_session_abandon()
        elif path == '/session/complete':
            self._handle_session_complete()
        elif path == '/strokes':
            self._handle_strokes()
        elif path == '/export/preview':
            self._handle_export_preview()
        elif path == '/export/csv':
            self._handle_export_csv()
        else:
            self._404()

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    # ── session / token endpoints ────────────────────────────────────────────

    def _handle_session_check(self):
        """
        Read or mint a device token. Return the participant status.

        Response JSON
        -------------
        {
          "status":     "fresh" | "restart" | "warn_completed",
          "session_id": "<uuid>" | null   (null when fresh)
        }

        Cookie
        ------
        Sets `device_token` (SameSite=Strict, 1-year expiry) on first visit.
        """
        cookie_header = self.headers.get('Cookie', '')
        sc = SimpleCookie()
        sc.load(cookie_header)

        token = sc['device_token'].value if 'device_token' in sc else None
        new_cookie = None

        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:

                if token is None:
                    # First visit — mint a new token
                    token = secrets.token_hex(16)
                    cur.execute(
                        "INSERT INTO device_tokens (token) VALUES (%s)",
                        (token,)
                    )
                    conn.commit()
                    new_cookie = token
                    self._json({'status': 'fresh', 'session_id': None},
                               cookie=new_cookie)
                    return

                # Update last_seen
                cur.execute(
                    "UPDATE device_tokens SET last_seen = now() WHERE token = %s",
                    (token,)
                )

                # Check for existing sessions
                cur.execute(
                    """
                    SELECT id, completed, abandoned
                    FROM sessions
                    WHERE token = %s
                    ORDER BY started_at DESC
                    LIMIT 1
                    """,
                    (token,)
                )
                row = cur.fetchone()
                conn.commit()

                if row is None:
                    # Token known but no session yet (edge case: token set but
                    # participant closed before submitting the intake form)
                    self._json({'status': 'fresh', 'session_id': None},
                               cookie=new_cookie)
                    return

                if not row['completed'] and not row['abandoned']:
                    status = 'restart'
                elif row['completed']:
                    status = 'warn_completed'
                else:
                    # most recent session abandoned — treat as fresh
                    status = 'fresh'

                self._json({
                    'status':     status,
                    'session_id': str(row['id']) if status == 'restart' else None,
                }, cookie=new_cookie)

    def _handle_session_abandon(self):
        """
        Mark a session as abandoned (called before creating a replacement).
        Body: { "session_id": "<uuid>" }
        """
        body = self._read_json()
        if body is None:
            return

        session_id = body.get('session_id')
        if not session_id:
            self._respond(400, b'Missing session_id')
            return

        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE sessions SET abandoned = true WHERE id = %s",
                    (session_id,)
                )
            conn.commit()

        print(f'[{_ts()}]  Abandoned  session={session_id}')
        self._json({'ok': True})

    def _handle_session_create(self):
        """
        Create a new session from the submitted intake form.

        Body (JSON) — all optional except consent
        ------------------------------------------
        {
          "token": "<32-char hex>",      // read from cookie server-side? No —
                                         // client echoes it back for simplicity
          "age": 42,
          "gender": "female",
          "handedness": "right",
          "writing_hand": "right",
          "input_device": "apple-pencil",
          "parkinsons_diagnosis": false,
          "parkinsons_stage": "n/a",
          "other_conditions": "none",
          "motor_medication": "no",
          "hand_steadiness": "very-steady",
          "writing_hours_per_day": 1.5,
          "writing_style": "mixed",
          "consent": true
        }

        Response: { "session_id": "<uuid>" }
        """
        body = self._read_json()
        if body is None:
            return

        token = body.get('token')
        if not token or len(token) != 32:
            self._respond(400, b'Invalid token')
            return

        # Ensure token row exists (it should — /session/check always creates it)
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "INSERT INTO device_tokens (token) VALUES (%s) ON CONFLICT DO NOTHING",
                    (token,)
                )
                cur.execute(
                    """
                    INSERT INTO sessions (
                        token, status, age, gender, handedness, writing_hand,
                        input_device, parkinsons_diagnosis, parkinsons_stage,
                        other_conditions, motor_medication, hand_steadiness,
                        writing_hours_per_day, writing_style, consent, consent_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s, now()
                    )
                    RETURNING id
                    """,
                    (
                        token,
                        body.get('status', 'fresh'),
                        body.get('age'),
                        body.get('gender'),
                        body.get('handedness'),
                        body.get('writing_hand'),
                        body.get('input_device'),
                        body.get('parkinsons_diagnosis'),
                        body.get('parkinsons_stage'),
                        body.get('other_conditions'),
                        body.get('motor_medication'),
                        body.get('hand_steadiness'),
                        body.get('writing_hours_per_day'),
                        body.get('writing_style'),
                        bool(body.get('consent', False)),
                    )
                )
                session_id = str(cur.fetchone()['id'])
            conn.commit()

        print(f'[{_ts()}]  Session created  id={session_id}  token={token[:8]}…')
        self._json({'session_id': session_id})

    def _handle_session_complete(self):
        """
        Mark a session complete and increment the token's submission count.
        Body: { "session_id": "<uuid>" }
        """
        body = self._read_json()
        if body is None:
            return

        session_id = body.get('session_id')
        if not session_id:
            self._respond(400, b'Missing session_id')
            return

        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE sessions
                    SET completed = true, completed_at = now()
                    WHERE id = %s
                    RETURNING token
                    """,
                    (session_id,)
                )
                row = cur.fetchone()
                if row:
                    cur.execute(
                        """
                        UPDATE device_tokens
                        SET submission_count = submission_count + 1
                        WHERE token = %s
                        """,
                        (row[0],)
                    )
            conn.commit()

        print(f'[{_ts()}]  Session complete  id={session_id}')
        self._json({'ok': True})

    # ── strokes endpoint ─────────────────────────────────────────────────────

    def _handle_strokes(self):
        """
        Persist one repetition's worth of stroke points.

        Body
        ----
        {
          "session_id":  "<uuid>",
          "task_name":   "spiral_round",
          "task_type":   "shape",
          "orientation": null,
          "task_index":  2,
          "points": [
            {
              "stroke_index": 0,
              "point_index":  0,
              "x":            320.5,
              "y":            241.1,
              "time_ms":      1234.56,
              "pressure":     0.72,
              "tilt_x_deg":   12.0,
              "tilt_y_deg":   -5.0,
              "pointer_type": "pen"
            },
            ...
          ]
        }

        Response: { "inserted": <int> }
        """
        body = self._read_json()
        if body is None:
            return

        session_id  = body.get('session_id')
        task_name   = body.get('task_name')
        task_type   = body.get('task_type')
        orientation = body.get('orientation')   # may be null / None
        task_index  = body.get('task_index', 0)
        points      = body.get('points', [])

        # ── validation ───────────────────────────────────────────────────────
        errors = []
        if not session_id:
            errors.append('Missing session_id')
        if task_name not in VALID_TASK_NAMES:
            errors.append(f'Unknown task_name: {task_name!r}')
        if task_type not in VALID_TASK_TYPES:
            errors.append(f'Unknown task_type: {task_type!r}')
        if orientation not in VALID_ORIENTATIONS:
            errors.append(f'Unknown orientation: {orientation!r}')
        if not isinstance(points, list) or len(points) == 0:
            errors.append('points must be a non-empty list')
        if errors:
            self._respond(400, json.dumps({'errors': errors}).encode())
            return

        rows = [
            (
                session_id,
                task_type,
                task_name,
                orientation,
                task_index,
                p.get('stroke_index', 0),
                p.get('point_index', 0),
                p.get('x', 0.0),
                p.get('y', 0.0),
                p.get('time_ms', 0.0),
                p.get('pressure'),       # nullable
                p.get('tilt_x_deg'),     # nullable
                p.get('tilt_y_deg'),     # nullable
                p.get('pointer_type'),
            )
            for p in points
        ]

        with get_conn() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(
                    cur,
                    """
                    INSERT INTO strokes (
                        session_id, task_type, task_name, orientation,
                        task_index, stroke_index, point_index,
                        x, y, time_ms, pressure, tilt_x_deg, tilt_y_deg,
                        pointer_type
                    ) VALUES %s
                    """,
                    rows,
                    page_size=500
                )
            conn.commit()

        count = len(rows)
        print(
            f'[{_ts()}]  Strokes  '
            f'task={task_name}  orient={orientation}  '
            f'idx={task_index}  pts={count:,}'
        )
        self._json({'inserted': count})

    # ── export endpoints ─────────────────────────────────────────────────────

    def _build_export_query(self, body, count_only=False):
        """
        Build a parameterised (sql, params) tuple from the export payload.

        Column names are validated against EXPORT_STROKE_COLS before being
        interpolated into the SELECT list — they cannot be passed as psycopg2
        parameters. All filter values travel as parameters.

        If count_only=True the SELECT returns aggregate counts only (used by
        /export/preview to estimate result size without fetching all rows).
        """
        if count_only:
            select_clause = (
                'COUNT(*) AS total_points, '
                'COUNT(DISTINCT st.session_id) AS total_sessions'
            )
        else:
            requested = body.get('columns', ['x', 'y'])
            safe_parts = []
            for col in requested:
                if col == 'session_id':
                    safe_parts.append('st.session_id')
                elif col in EXPORT_STROKE_COLS:
                    safe_parts.append(f'st.{col}')
                # silently skip any unrecognised column names
            if not safe_parts:
                safe_parts = ['st.x', 'st.y']
            select_clause = ', '.join(safe_parts)

        sql = (
            f'SELECT {select_clause}\n'
            f'FROM strokes st\n'
            f'JOIN sessions s ON st.session_id = s.id\n'
        )

        params = []
        conditions = []

        # Task filter
        tasks = [t for t in body.get('tasks', []) if t in VALID_TASK_NAMES]
        if tasks and len(tasks) < len(VALID_TASK_NAMES):
            placeholders = ', '.join(['%s'] * len(tasks))
            conditions.append(f'st.task_name IN ({placeholders})')
            params.extend(tasks)

        # Session quality
        if body.get('completed_only', True):
            conditions.append('s.completed = true')
        if not body.get('include_abandoned', False):
            conditions.append('s.abandoned = false')

        # Input device
        device = body.get('device', 'any')
        if device and device != 'any':
            conditions.append('s.input_device = %s')
            params.append(device)

        # Parkinson's diagnosis
        pd = body.get('pd_diagnosis', 'any')
        if pd == 'yes':
            conditions.append('s.parkinsons_diagnosis = true')
        elif pd == 'no':
            conditions.append('s.parkinsons_diagnosis = false')
        elif pd == 'null':
            conditions.append('s.parkinsons_diagnosis IS NULL')

        # Parkinson's stage
        stage = body.get('pd_stage', 'any')
        if stage and stage != 'any':
            conditions.append('s.parkinsons_stage = %s')
            params.append(stage)

        # Gender
        gender = body.get('gender', 'any')
        if gender and gender != 'any':
            conditions.append('s.gender = %s')
            params.append(gender)

        # Handedness
        hand = body.get('handedness', 'any')
        if hand and hand != 'any':
            conditions.append('s.handedness = %s')
            params.append(hand)

        # Other neurological conditions
        neuro = body.get('other_conditions', 'any')
        if neuro and neuro != 'any':
            conditions.append('s.other_conditions = %s')
            params.append(neuro)

        # Hand steadiness
        steady = body.get('hand_steadiness', 'any')
        if steady and steady != 'any':
            conditions.append('s.hand_steadiness = %s')
            params.append(steady)

        # Writing style
        style = body.get('writing_style', 'any')
        if style and style != 'any':
            conditions.append('s.writing_style = %s')
            params.append(style)

        # Age range
        age_min = body.get('age_min')
        age_max = body.get('age_max')
        if age_min is not None:
            conditions.append('s.age >= %s')
            params.append(int(age_min))
        if age_max is not None:
            conditions.append('s.age <= %s')
            params.append(int(age_max))

        # Date/time range filter
        # date_col is either 'started_at' or 'completed_at' (validated below).
        # date_from with no date_to  → col >= date_from  (after this moment)
        # date_to   with no date_from → col <= date_to   (before this moment)
        # Both provided              → col BETWEEN date_from AND date_to
        date_col_raw = body.get('date_col', 'started_at')
        date_col = date_col_raw if date_col_raw in ('started_at', 'completed_at') else 'started_at'
        date_from = body.get('date_from')   # ISO-8601 string or None
        date_to   = body.get('date_to')     # ISO-8601 string or None
        if date_from:
            conditions.append(f's.{date_col} >= %s')
            params.append(date_from)
        if date_to:
            conditions.append(f's.{date_col} <= %s')
            params.append(date_to)

        if conditions:
            sql += 'WHERE ' + '\n  AND '.join(conditions) + '\n'

        if not count_only:
            sql += (
                'ORDER BY st.session_id, st.task_name, st.task_index,\n'
                '         st.stroke_index, st.point_index\n'
            )

        return sql, params

    def _handle_export_preview(self):
        """
        POST /export/preview
        Returns estimated row and session counts for the current filter set.
        Used by the researcher UI to populate the preview bar without
        downloading the full dataset.

        Response: { "sessions": <int>, "points": <int> }
        """
        body = self._read_json()
        if body is None:
            return

        sql, params = self._build_export_query(body, count_only=True)

        try:
            with get_readonly_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    row = cur.fetchone()
                    points   = int(row[0]) if row else 0
                    sessions = int(row[1]) if row else 0
            self._json({'sessions': sessions, 'points': points})
        except Exception as exc:
            self._respond(500, str(exc).encode())

    def _handle_export_csv(self):
        """
        POST /export/csv
        Executes the filtered query and streams the result as a CSV download.

        The Content-Disposition header causes browsers to prompt a file save.
        Rows are fetched in full before writing — for very large datasets
        consider switching to a named server-side cursor with fetchmany().
        """
        body = self._read_json()
        if body is None:
            return

        sql, params = self._build_export_query(body, count_only=False)

        try:
            with get_readonly_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    col_names = [desc[0] for desc in cur.description]
                    rows = cur.fetchall()

            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(col_names)
            writer.writerows(rows)
            csv_bytes = buf.getvalue().encode('utf-8')

            self.send_response(200)
            self.send_header('Content-Type', 'text/csv; charset=utf-8')
            self.send_header(
                'Content-Disposition',
                'attachment; filename="handwriting_export.csv"'
            )
            self.send_header('Content-Length', str(len(csv_bytes)))
            self._cors()
            self.end_headers()
            self.wfile.write(csv_bytes)

            print(
                f'[{_ts()}]  Export CSV  '
                f'rows={len(rows):,}  cols={len(col_names)}'
            )

        except Exception as exc:
            self._respond(500, str(exc).encode())

    # ── helpers ──────────────────────────────────────────────────────────────

    def _serve_file(self, path, content_type):
        if not os.path.exists(path):
            self._404()
            return
        with open(path, 'rb') as f:
            data = f.read()
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        length = int(self.headers.get('Content-Length', 0))
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            self._respond(400, f'Invalid JSON: {e}'.encode())
            return None

    def _json(self, obj, cookie=None):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        if cookie:
            expires = datetime.datetime.utcnow() + datetime.timedelta(days=365)
            expires_str = expires.strftime('%a, %d %b %Y %H:%M:%S GMT')
            self.send_header(
                'Set-Cookie',
                f'device_token={cookie}; Path=/; Expires={expires_str}; SameSite=Strict'
            )
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _respond(self, code, body=b''):
        self.send_response(code)
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _404(self):
        self._respond(404, b'Not found')

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def log_message(self, format, *args):
        pass  # suppress default Apache-style request log


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Handwriting study server')
    parser.add_argument('--port', '-p', type=int,
                        default=int(os.environ.get('PORT', 8080)))
    args = parser.parse_args()

    # Quick connectivity check
    try:
        conn = get_conn()
        conn.close()
        print(f'Database connection OK')
    except psycopg2.OperationalError as e:
        sys.exit(f'ERROR: Cannot connect to database:\n  {e}')

    if not READONLY_DATABASE_URL:
        print('WARNING: READONLY_DATABASE_URL is not set.')
        print('         Export endpoints will use the main DATABASE_URL.')
        print('         Set READONLY_DATABASE_URL to a SELECT-only user in production.\n')
    else:
        try:
            rc = get_readonly_conn()
            rc.close()
            print(f'Read-only database connection OK')
        except psycopg2.OperationalError as e:
            sys.exit(f'ERROR: Cannot connect with READONLY_DATABASE_URL:\n  {e}')

    print(f'Handwriting study server  —  http://0.0.0.0:{args.port}')
    print(f'Tasks directory:            {TASKS_DIR}')
    print(f'Researcher export tool:     http://0.0.0.0:{args.port}/export')
    print('Ctrl+C to stop.\n')
    HTTPServer(('0.0.0.0', args.port), Handler).serve_forever()
