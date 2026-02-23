from __future__ import annotations

import argparse
import html
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .storage import connect_db, init_schema


def _layout(title: str, body: str) -> str:
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      --bg: #f4f6f8;
      --card: #ffffff;
      --text: #12263a;
      --muted: #526173;
      --accent: #0b7285;
      --border: #d9e2ec;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: "IBM Plex Sans", "Segoe UI", sans-serif; background: var(--bg); color: var(--text); }}
    header {{ background: linear-gradient(90deg, #0b7285, #1971c2); color: #fff; padding: 16px 20px; }}
    main {{ max-width: 1200px; margin: 18px auto; padding: 0 14px 30px; }}
    .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 14px; margin-bottom: 12px; }}
    h1, h2, h3 {{ margin: 0 0 10px; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ border: 1px solid var(--border); padding: 8px; vertical-align: top; }}
    th {{ background: #edf2f7; text-align: left; }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    textarea {{ width: 100%; min-height: 120px; }}
    .mono {{ font-family: ui-monospace, Menlo, monospace; white-space: pre-wrap; }}
    .muted {{ color: var(--muted); }}
    .badge {{ display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; font-weight: 600; }}
    .badge-pass {{ background: #d3f9d8; color: #2b8a3e; }}
    .badge-warn {{ background: #fff3bf; color: #e67700; }}
    .badge-unknown {{ background: #e9ecef; color: #495057; }}
    .row {{ display: grid; grid-template-columns: 1fr; gap: 12px; }}
    @media (min-width: 980px) {{
      .row {{ grid-template-columns: 1fr 1fr; }}
    }}
    input[type=text], select {{ width: 100%; padding: 6px 8px; }}
    button {{ background: var(--accent); color: #fff; border: 0; border-radius: 6px; padding: 8px 10px; cursor: pointer; }}
  </style>
</head>
<body>
<header><strong>Repo Requirements Analyzer DB</strong></header>
<main>{body}</main>
</body>
</html>"""


class AppHandler(BaseHTTPRequestHandler):
    db_path: Path

    def _open(self):
        conn = connect_db(self.db_path)
        init_schema(conn)
        return conn

    def _send_html(self, text: str, status: int = HTTPStatus.OK) -> None:
        payload = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_html(self.render_home())
            return
        if parsed.path == "/report":
            params = parse_qs(parsed.query)
            report_id = int(params.get("id", ["0"])[0])
            self._send_html(self.render_report(report_id))
            return
        self._send_html(_layout("Not Found", "<div class='card'>Not found</div>"), status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path not in {"/story/update", "/rec/update"}:
            self._send_html(_layout("Not Found", "<div class='card'>Not found</div>"), status=HTTPStatus.NOT_FOUND)
            return

        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        params = parse_qs(body)
        report_id = int(params.get("report_id", ["0"])[0])

        conn = self._open()
        if parsed.path == "/story/update":
            conn.execute(
                "UPDATE stories SET status=?, notes=? WHERE id=?",
                (
                    params.get("status", ["new"])[0],
                    params.get("notes", [""])[0],
                    int(params.get("id", ["0"])[0]),
                ),
            )
        else:
            conn.execute(
                "UPDATE recommendations SET status=?, notes=? WHERE id=?",
                (
                    params.get("status", ["proposed"])[0],
                    params.get("notes", [""])[0],
                    int(params.get("id", ["0"])[0]),
                ),
            )
        conn.commit()
        conn.close()

        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", f"/report?id={report_id}")
        self.end_headers()

    def render_home(self) -> str:
        conn = self._open()
        rows = conn.execute(
            """
            SELECT
                r.id, r.title, r.repo, r.model, r.created_at,
                r.validation_status, r.validation_error_count,
                (SELECT COUNT(*) FROM stories s WHERE s.report_id=r.id) AS story_count,
                (SELECT COUNT(*) FROM features f WHERE f.report_id=r.id) AS feature_count,
                (SELECT COUNT(*) FROM recommendations rc WHERE rc.report_id=r.id) AS rec_count
            FROM reports r
            ORDER BY r.id DESC
            """
        ).fetchall()
        conn.close()

        table_rows = []
        for row in rows:
            validation_status = (row["validation_status"] or "unknown").lower()
            if validation_status == "passed":
                badge_class = "badge badge-pass"
            elif validation_status == "warning":
                badge_class = "badge badge-warn"
            else:
                badge_class = "badge badge-unknown"
            table_rows.append(
                "<tr>"
                f"<td>{row['id']}</td>"
                f"<td><a href='/report?id={row['id']}'>{html.escape(row['title'])}</a></td>"
                f"<td>{html.escape(row['repo'] or '')}</td>"
                f"<td>{html.escape(row['model'] or '')}</td>"
                f"<td><span class='{badge_class}'>{html.escape(validation_status)}</span> ({row['validation_error_count']})</td>"
                f"<td>{row['story_count']}</td>"
                f"<td>{row['feature_count']}</td>"
                f"<td>{row['rec_count']}</td>"
                f"<td class='muted'>{html.escape(row['created_at'])}</td>"
                "</tr>"
            )

        rows_html = "".join(table_rows) if table_rows else "<tr><td colspan='9'>No reports yet.</td></tr>"
        body = (
            "<div class='card'>"
            "<h1>Reports</h1>"
            "<p class='muted'>Ingest markdown reports, then browse and edit stories/recommendations here.</p>"
            "<table><thead><tr><th>ID</th><th>Title</th><th>Repo</th><th>Model</th><th>Quality</th><th>Stories</th><th>Features</th><th>Recs</th><th>Created</th></tr></thead>"
            f"<tbody>{rows_html}</tbody></table>"
            "</div>"
        )
        return _layout("Reports", body)

    def render_report(self, report_id: int) -> str:
        conn = self._open()
        report = conn.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
        if not report:
            conn.close()
            return _layout("Not Found", "<div class='card'>Report not found.</div>")

        features = conn.execute(
            "SELECT domain, feature_text FROM features WHERE report_id=? ORDER BY domain, id",
            (report_id,),
        ).fetchall()
        stories = conn.execute(
            "SELECT * FROM stories WHERE report_id=? ORDER BY COALESCE(story_num, 9999), id",
            (report_id,),
        ).fetchall()
        recs = conn.execute(
            "SELECT * FROM recommendations WHERE report_id=? ORDER BY COALESCE(item_num, 9999), id",
            (report_id,),
        ).fetchall()
        conn.close()

        validation_status = (report["validation_status"] or "unknown").lower()
        if validation_status == "passed":
            badge_class = "badge badge-pass"
        elif validation_status == "warning":
            badge_class = "badge badge-warn"
        else:
            badge_class = "badge badge-unknown"

        validation_errors = [line.strip() for line in (report["validation_errors"] or "").splitlines() if line.strip()]
        validation_errors_html = (
            "<ul>" + "".join(f"<li>{html.escape(err)}</li>" for err in validation_errors) + "</ul>"
            if validation_errors
            else "<p class='muted'>No quality warnings.</p>"
        )

        feature_rows = "".join(
            f"<tr><td>{html.escape(r['domain'])}</td><td>{html.escape(r['feature_text'])}</td></tr>" for r in features
        ) or "<tr><td colspan='2'>No features parsed.</td></tr>"

        story_rows = []
        for s in stories:
            story_rows.append(
                "<tr><td colspan='6'>"
                "<form method='post' action='/story/update'>"
                f"<input type='hidden' name='id' value='{s['id']}'>"
                f"<input type='hidden' name='report_id' value='{report_id}'>"
                "<div class='row'>"
                "<div>"
                f"<strong>#{html.escape(str(s['story_num'] or ''))}</strong> "
                f"{html.escape(s['persona'])}<br>"
                f"{html.escape(s['story_text'])}<br>"
                f"<span class='muted'>Evidence: {html.escape(s['evidence'] or '')}</span>"
                "</div>"
                "<div>"
                "<label>Status</label>"
                "<select name='status'>"
                f"<option {'selected' if s['status']=='new' else ''}>new</option>"
                f"<option {'selected' if s['status']=='approved' else ''}>approved</option>"
                f"<option {'selected' if s['status']=='needs_revision' else ''}>needs_revision</option>"
                f"<option {'selected' if s['status']=='rejected' else ''}>rejected</option>"
                "</select>"
                "<label>Notes</label>"
                f"<textarea name='notes'>{html.escape(s['notes'] or '')}</textarea>"
                "<button type='submit'>Save Story</button>"
                "</div></div></form></td></tr>"
            )

        rec_rows = []
        for r in recs:
            rec_rows.append(
                "<tr><td colspan='4'>"
                "<form method='post' action='/rec/update'>"
                f"<input type='hidden' name='id' value='{r['id']}'>"
                f"<input type='hidden' name='report_id' value='{report_id}'>"
                f"<strong>{html.escape(str(r['item_num'] or ''))}.</strong> {html.escape(r['recommendation_text'])}<br>"
                "<label>Status</label>"
                "<select name='status'>"
                f"<option {'selected' if r['status']=='proposed' else ''}>proposed</option>"
                f"<option {'selected' if r['status']=='accepted' else ''}>accepted</option>"
                f"<option {'selected' if r['status']=='deferred' else ''}>deferred</option>"
                f"<option {'selected' if r['status']=='rejected' else ''}>rejected</option>"
                "</select>"
                "<label>Notes</label>"
                f"<textarea name='notes'>{html.escape(r['notes'] or '')}</textarea>"
                "<button type='submit'>Save Recommendation</button>"
                "</form></td></tr>"
            )

        body = (
            "<div class='card'>"
            "<a href='/'>Back to reports</a>"
            f"<h1>Report #{report['id']}: {html.escape(report['title'])}</h1>"
            f"<p class='muted'>Repo: {html.escape(report['repo'] or '')}<br>Model: {html.escape(report['model'] or '')}</p>"
            "</div>"
            "<div class='card'>"
            "<h2>Quality Validation</h2>"
            f"<p><span class='{badge_class}'>{html.escape(validation_status)}</span> "
            f"warnings: {report['validation_error_count']}</p>"
            f"{validation_errors_html}"
            "</div>"
            "<div class='card'><h2>Features</h2><table><thead><tr><th>Domain</th><th>Feature</th></tr></thead>"
            f"<tbody>{feature_rows}</tbody></table></div>"
            "<div class='card'><h2>User Stories</h2><table><tbody>"
            f"{''.join(story_rows) if story_rows else '<tr><td>No stories parsed.</td></tr>'}</tbody></table></div>"
            "<div class='card'><h2>Recommendations</h2><table><tbody>"
            f"{''.join(rec_rows) if rec_rows else '<tr><td>No recommendations parsed.</td></tr>'}</tbody></table></div>"
            "<div class='card'><h2>Raw Markdown</h2>"
            f"<pre class='mono'>{html.escape(report['markdown'])}</pre></div>"
        )
        return _layout(f"Report {report_id}", body)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a local web UI for stored analyzer reports.")
    parser.add_argument("--db", default="./data/specs.db", help="SQLite DB path (default: ./data/specs.db).")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000).")
    return parser.parse_args()


def entrypoint() -> None:
    args = parse_args()
    db_path = Path(args.db).expanduser().resolve()
    handler = type("BoundHandler", (AppHandler,), {"db_path": db_path})
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Serving web UI at http://{args.host}:{args.port} (db={db_path})")
    server.serve_forever()


if __name__ == "__main__":
    entrypoint()
