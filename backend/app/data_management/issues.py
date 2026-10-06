"""Quality issues record observations; only a fresh successful recheck permits closure."""
import json
from uuid import UUID, uuid4

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

from backend.app.agent.store import connect
from backend.app.data_management import quality
from backend.app.data_management.versions import REQUEST_VERSION, source_info
from backend.app.errors import ServiceError

router = APIRouter(prefix='/api/data/issues', tags=['质量问题处理'])


class Action(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=1)
    action: Literal['start', 'note', 'waive', 'reopen', 'close', 'cancel_recheck']
    note: str = Field(min_length=1, max_length=2000)


class Recheck(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=1)


def init(db) -> None:
    quality.init(db)
    db.execute('''CREATE TABLE IF NOT EXISTS dm_quality_issues (
        id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL, version_id TEXT NOT NULL,
        rule_code TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL)''')
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS dm_active_issue ON dm_quality_issues
        (dataset_id,version_id,rule_code) WHERE status IN ('open','in_progress')""")
    db.execute('''CREATE TABLE IF NOT EXISTS dm_issue_checks (
        check_id TEXT PRIMARY KEY REFERENCES dm_quality_checks(id))''')
    db.execute('''CREATE TABLE IF NOT EXISTS dm_issue_events (
        id TEXT PRIMARY KEY, issue_id TEXT NOT NULL REFERENCES dm_quality_issues(id),
        created_at TEXT NOT NULL, payload TEXT NOT NULL)''')
    db.execute('INSERT OR IGNORE INTO dm_schema_versions VALUES (6,?)', (quality.now(),))


def save(db, issue: dict) -> None:
    db.execute('''INSERT INTO dm_quality_issues VALUES (?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET status=excluded.status,payload=excluded.payload''',
        (issue['id'], issue['dataset_id'], issue['version_id'], issue['rule_code'],
         issue['status'], json.dumps(issue, ensure_ascii=False)))


def event(db, issue: dict, kind: str, **details) -> None:
    stamp = quality.now()
    db.execute('INSERT INTO dm_issue_events VALUES (?,?,?,?)',
               (str(uuid4()), issue['id'], stamp,
                json.dumps({'kind': kind, 'created_at': stamp, **details}, ensure_ascii=False)))


def synchronize(db) -> None:
    """Consume each completed immutable check once, including existing history."""
    init(db)
    rows = db.execute("""SELECT c.payload FROM dm_quality_checks c
        LEFT JOIN dm_issue_checks p ON p.check_id=c.id
        WHERE c.status='completed' AND p.check_id IS NULL ORDER BY c.created_at,c.id""").fetchall()
    for (raw,) in rows:
        check = json.loads(raw)
        for rule in check['current']['rules']:
            code = rule['rule']
            if code not in quality.RULES:
                continue
            row = db.execute("""SELECT payload FROM dm_quality_issues WHERE
                dataset_id=? AND version_id=? AND rule_code=? AND status IN ('open','in_progress')""",
                (check['dataset_id'], check['version_id'], code)).fetchone()
            issue = json.loads(row[0]) if row else None
            if issue is None and rule['count'] == 0:
                continue
            if issue is None:
                issue = {'id': str(uuid4()), 'dataset_id': check['dataset_id'],
                         'version_id': check['version_id'], 'rule_code': code,
                         'name': quality.RULES[code][0], 'status': 'open', 'revision': 0,
                         'created_at': check['created_at'], 'observations': 0,
                         'candidate': None, 'pending': None}
            issue.update(revision=issue['revision'] + 1, latest_check_id=check['id'],
                         latest_count=rule['count'], updated_at=quality.now(), candidate=None,
                         observations=issue['observations'] + 1)
            save(db, issue)
            event(db, issue, 'observation', check_id=check['id'], count=rule['count'],
                  samples=rule.get('samples', []))
        db.execute('INSERT INTO dm_issue_checks VALUES (?)', (check['id'],))


def read(db, ident: UUID | str) -> dict:
    row = db.execute('SELECT payload FROM dm_quality_issues WHERE id=?', (str(ident),)).fetchone()
    if not row:
        raise ServiceError('not_found', '质量问题不存在。', 404)
    return json.loads(row[0])


def revision(issue: dict, expected: int) -> None:
    if issue['revision'] != expected:
        raise ServiceError('revision_conflict', '问题记录已更新，请刷新后重试。', 409)


@router.get('')
def listing() -> dict:
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        synchronize(db)
        rows = db.execute('SELECT payload FROM dm_quality_issues ORDER BY rowid DESC LIMIT 200').fetchall()
    return {'issues': [json.loads(row[0]) for row in rows],
            'note': '人工豁免只登记接受风险；不会解除 Agent 硬质量门禁。最多显示最近200张问题单。'}


@router.get('/{issue_id}')
def detail(issue_id: UUID) -> dict:
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        synchronize(db)
        issue = read(db, issue_id)
        events = db.execute('SELECT payload FROM dm_issue_events WHERE issue_id=? ORDER BY rowid',
                            (str(issue_id),)).fetchall()
    return {**issue, 'events': [json.loads(row[0]) for row in events]}


@router.post('/{issue_id}/actions')
def act(issue_id: UUID, request: Action) -> dict:
    note = request.note.strip()
    if not note:
        raise ServiceError('invalid_note', '请填写处理说明。', 422)
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        synchronize(db)
        issue = read(db, issue_id)
        revision(issue, request.expected_revision)
        action = request.action
        if issue['pending'] and action != 'cancel_recheck':
            raise ServiceError('recheck_pending', '复检进行中，请等待或登记取消。', 409)
        active = issue['status'] in ('open', 'in_progress')
        if action == 'cancel_recheck':
            if not issue['pending']:
                raise ServiceError('invalid_transition', '没有待完成的复检。', 409)
            issue.update(pending=None, candidate=None)
        elif action == 'reopen':
            if active:
                raise ServiceError('invalid_transition', '问题已经处于处理中。', 409)
            existing = db.execute("SELECT id FROM dm_quality_issues WHERE dataset_id=? AND version_id=? AND rule_code=? AND status IN ('open','in_progress')",
                                  (issue['dataset_id'], issue['version_id'], issue['rule_code'])).fetchone()
            if existing:
                raise ServiceError('active_issue_exists', '已有同规则未关闭问题，请处理该问题单。', 409)
            issue.update(status='open', candidate=None)
        elif not active:
            raise ServiceError('invalid_transition', '请先重新打开问题。', 409)
        elif action == 'start':
            issue['status'] = 'in_progress'
        elif action == 'waive':
            issue.update(status='waived', candidate=None)
        elif action == 'close':
            candidate = issue['candidate']
            if not candidate or not candidate['passed'] or candidate['check_id'] != issue['latest_check_id']:
                raise ServiceError('recheck_required', '必须由本版本最新复检证明八项硬规则全部通过。', 409)
            issue.update(status='resolved', resolution=candidate)
        issue.update(revision=issue['revision'] + 1, updated_at=quality.now())
        save(db, issue)
        event(db, issue, action, note=note)
    return issue


@router.post('/{issue_id}/recheck')
def recheck(issue_id: UUID, request: Recheck) -> dict:
    # Resolve the issue version, ignoring any unrelated caller-selected version.
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        synchronize(db)
        issue = read(db, issue_id)
        revision(issue, request.expected_revision)
        if issue['status'] not in ('open', 'in_progress') or issue['pending']:
            raise ServiceError('invalid_transition', '仅可复检未关闭且没有进行中复检的问题。', 409)
    context = REQUEST_VERSION.set(issue['version_id'])
    try:
        source_info()
        token = str(uuid4())
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            issue = read(db, issue_id)
            revision(issue, request.expected_revision)
            issue.update(pending=token, candidate=None, status='in_progress', revision=issue['revision'] + 1)
            save(db, issue)
            event(db, issue, 'recheck_started')
        try:
            check = quality.run_check()
        except Exception:
            with connect() as db:
                db.execute('BEGIN IMMEDIATE')
                issue = read(db, issue_id)
                if issue['pending'] == token:
                    issue.update(pending=None, candidate=None, revision=issue['revision'] + 1)
                    save(db, issue)
                    event(db, issue, 'recheck_error', note='复检未完成；未证明问题已修复。')
            raise
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            synchronize(db)
            issue = read(db, issue_id)
            if issue['pending'] != token:
                raise ServiceError('recheck_cancelled', '复检已取消，其结果不能用于关闭。', 409)
            passed = not check['current']['blocking'] and issue['latest_check_id'] == check['id']
            issue.update(pending=None, candidate={'check_id': check['id'], 'passed': passed,
                         'version_id': check['version_id'], 'completed_at': check['completed_at']},
                         revision=issue['revision'] + 1, updated_at=quality.now())
            save(db, issue)
            event(db, issue, 'recheck_passed' if passed else 'recheck_failed', check_id=check['id'])
        return issue
    finally:
        REQUEST_VERSION.reset(context)
