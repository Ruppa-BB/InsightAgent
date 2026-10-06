"""Single-user local analysis history, independent of the read-only business DB."""
from datetime import datetime, timezone
import json
import sqlite3
from contextlib import contextmanager
from collections.abc import Iterator
from uuid import UUID

from backend.app.agent.schemas import AnalysisFeedback
from backend.app.config import settings
from backend.app.errors import ServiceError
from backend.app.serialization import json_ready


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    settings.analysis_store.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(settings.analysis_store, timeout=5)
    connection.execute('PRAGMA foreign_keys = ON')
    try:
        connection.execute('''CREATE TABLE IF NOT EXISTS analyses (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL)''')
        connection.execute('CREATE INDEX IF NOT EXISTS analysis_session ON analyses(session_id, created_at)')
        connection.execute('''CREATE TABLE IF NOT EXISTS analysis_feedback (
            analysis_id TEXT PRIMARY KEY REFERENCES analyses(id),
            rating TEXT NOT NULL CHECK(rating IN ('helpful','not_helpful')),
            reason TEXT NOT NULL, correction TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL)''')
        connection.execute('CREATE TABLE IF NOT EXISTS monitor_reports (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        connection.execute('CREATE TABLE IF NOT EXISTS agent_runs (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL)')
        connection.execute('CREATE TABLE IF NOT EXISTS business_reports (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, month TEXT NOT NULL, payload TEXT NOT NULL)')
        yield connection
        connection.commit()
    finally:
        connection.close()


def save(result: dict) -> None:
    payload = json_ready(result)
    with connect() as connection:
        connection.execute('INSERT INTO analyses VALUES (?, ?, ?, ?)',
            (payload['id'], payload['session_id'], payload['created_at'], json.dumps(payload, ensure_ascii=False)))
        from backend.app.data_management.lineage import synchronize
        synchronize(connection)


def get(analysis_id: UUID) -> dict | None:
    with connect() as connection:
        row = connection.execute('SELECT payload FROM analyses WHERE id = ?', (str(analysis_id),)).fetchone()
        feedback = _read_feedback(connection, analysis_id) if row else None
    if row is None:
        return None
    return json.loads(row[0]) | {'feedback': feedback}


def latest_intent(session_id: UUID) -> dict | None:
    with connect() as connection:
        row = connection.execute('SELECT payload FROM analyses WHERE session_id = ? ORDER BY created_at DESC LIMIT 1', (str(session_id),)).fetchone()
    return json.loads(row[0])['intent'] if row else None


def recent() -> list[dict]:
    with connect() as connection:
        rows = connection.execute('SELECT payload FROM analyses ORDER BY created_at DESC LIMIT 30').fetchall()
    return [{key: data[key] for key in ('id', 'session_id', 'created_at', 'question', 'mode')}
            for data in (json.loads(row[0]) for row in rows)]


def markdown(result: dict) -> str:
    lines = ['# InsightAgent 分析记录', '', result['question'], '', result['answer'], '',
             '## 范围与来源', '', f"- 模式：{result['mode']}", '- 来源：PostgreSQL / Online Retail II',
             '- 日期区间为左闭右开；仅 completed 订单；金额单位 GBP。', '']
    lines.extend('- ' + warning for warning in result['warnings'])
    lines += ['', '## 可复算的执行记录', '', '```json', json.dumps(json_ready(result), ensure_ascii=False, indent=2), '```', '']
    return '\n'.join(lines)


def _read_feedback(connection: sqlite3.Connection, analysis_id: UUID) -> dict | None:
    row = connection.execute(
        'SELECT rating,reason,correction,created_at,updated_at,revision FROM analysis_feedback WHERE analysis_id=?',
        (str(analysis_id),)).fetchone()
    if row is None:
        return None
    return {'analysis_id':str(analysis_id), **dict(zip(
        ('rating','reason','correction','created_at','updated_at','revision'),row))}


def feedback(analysis_id: UUID) -> dict | None:
    with connect() as connection:
        if connection.execute('SELECT 1 FROM analyses WHERE id=?',(str(analysis_id),)).fetchone() is None:
            raise ServiceError('not_found','分析记录不存在。',404)
        return _read_feedback(connection,analysis_id)


def save_feedback(analysis_id: UUID, request: AnalysisFeedback) -> dict:
    with connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        if connection.execute('SELECT 1 FROM analyses WHERE id=?',(str(analysis_id),)).fetchone() is None:
            raise ServiceError('not_found','分析记录不存在。',404)
        previous = _read_feedback(connection,analysis_id)
        values = request.model_dump()
        if previous and all(previous[key] == value for key,value in values.items()):
            return previous
        now = datetime.now(timezone.utc).isoformat()
        connection.execute('''INSERT INTO analysis_feedback VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(analysis_id) DO UPDATE SET rating=excluded.rating,reason=excluded.reason,
            correction=excluded.correction,updated_at=excluded.updated_at,revision=excluded.revision''',
            (str(analysis_id),request.rating,request.reason,request.correction,
             previous['created_at'] if previous else now,now,previous['revision']+1 if previous else 1))
        return _read_feedback(connection,analysis_id)


def save_monitor_report(report_id: UUID, report: dict) -> None:
    with connect() as connection:
        connection.execute('INSERT INTO monitor_reports VALUES (?,?)',
                           (str(report_id),json.dumps(json_ready(report),ensure_ascii=False)))


def get_monitor_report(report_id: UUID) -> dict | None:
    with connect() as connection:
        row=connection.execute('SELECT payload FROM monitor_reports WHERE id=?',(str(report_id),)).fetchone()
    return json.loads(row[0]) if row else None


def save_run(run: dict) -> None:
    payload = json_ready(run)
    with connect() as connection:
        connection.execute('INSERT INTO agent_runs VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
                           (payload['id'],payload['created_at'],json.dumps(payload,ensure_ascii=False)))


def get_run(run_id: UUID) -> dict | None:
    with connect() as connection:
        row=connection.execute('SELECT payload FROM agent_runs WHERE id=?',(str(run_id),)).fetchone()
    return json.loads(row[0]) if row else None


def save_business_report(report: dict) -> None:
    with connect() as connection:
        connection.execute('INSERT INTO business_reports VALUES (?,?,?,?)',
                           (report['id'],report['created_at'],report['month'],json.dumps(json_ready(report),ensure_ascii=False)))
        from backend.app.data_management.lineage import synchronize
        synchronize(connection)


def get_business_report(report_id: UUID) -> dict | None:
    with connect() as connection:
        row=connection.execute('SELECT payload FROM business_reports WHERE id=?',(str(report_id),)).fetchone()
    return json.loads(row[0]) if row else None


def business_reports() -> list[dict]:
    with connect() as connection:
        rows=connection.execute('SELECT payload FROM business_reports ORDER BY created_at DESC LIMIT 30').fetchall()
    return [{key:report[key] for key in ('id','title','month','created_at','snapshot_sha256')}
            for report in (json.loads(row[0]) for row in rows)]
