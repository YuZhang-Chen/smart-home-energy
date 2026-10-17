"""SQLite persistence. One connection per operation/thread; nested work shares a transaction."""

from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any
from uuid import uuid4

from smart_home_energy.domain import (
    ApprovalEnvelope, LogicalClock, Plan, TaskConstraints, WashProgram, Tariff,
    decode, encode, utcnow, validate_plan, number,
)

TERMINAL = {"completed", "cancelled", "aborted", "resolved"}


@dataclass(frozen=True)
class Task:
    task_id: str
    version: int
    status: str
    constraints: TaskConstraints | None = None
    program: WashProgram = WashProgram()
    tariff: Tariff = Tariff()
    plan: Plan | None = None
    approval: ApprovalEnvelope | None = None
    message: str = ""
    run_id: str | None = None


@dataclass(frozen=True)
class AgentEvent:
    event_id: str
    key: str
    task_id: str | None
    payload: dict


@dataclass(frozen=True)
class AgentRun:
    run_id: str
    event_id: str
    status: str
    provider: str
    requests: int
    elapsed: float
    message: str


class Repository:
    def __init__(self, path: str | Path):
        self.path = str(Path(path).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.local = threading.local()
        with self.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, version INTEGER NOT NULL,
                    active INTEGER NOT NULL, data TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS single_active ON tasks(active) WHERE active=1;
                CREATE TABLE IF NOT EXISTS plans(id INTEGER PRIMARY KEY, task_id TEXT, version INTEGER, data TEXT);
                CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY, kind TEXT, payload TEXT,
                    status TEXT DEFAULT 'pending', result TEXT, created TEXT);
                CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, key TEXT, task_id TEXT,
                    payload TEXT, status TEXT DEFAULT 'pending');
                CREATE UNIQUE INDEX IF NOT EXISTS open_event ON events(key) WHERE status IN ('pending','running');
                CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY, data TEXT, result TEXT, created TEXT);
                CREATE TABLE IF NOT EXISTS devices(id TEXT PRIMARY KEY, data TEXT);
                CREATE TABLE IF NOT EXISTS samples(id INTEGER PRIMARY KEY, device_id TEXT, data TEXT);
                CREATE TABLE IF NOT EXISTS agent_runs(id TEXT PRIMARY KEY, data TEXT);
                CREATE TABLE IF NOT EXISTS tool_logs(id INTEGER PRIMARY KEY, run_id TEXT, data TEXT);
                CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, time TEXT, kind TEXT, data TEXT);
            """)

    @contextmanager
    def transaction(self):
        existing = getattr(self.local, "db", None)
        if existing is not None:
            yield existing
            return
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=15000")
        self.local.db = db
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            self.local.db = None
            db.close()

    def meta(self, key: str, default: Any = None) -> Any:
        with self.transaction() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set_meta(self, key: str, value: Any) -> None:
        with self.transaction() as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(encode(value), allow_nan=False)))

    def clock(self) -> LogicalClock:
        with self.transaction():
            value = self.meta("clock")
            if value is None:
                clock = LogicalClock.create()
                self.set_meta("clock", clock)
                return clock
            return decode(LogicalClock, value)

    def switch_clock(self, speed: float) -> LogicalClock:
        with self.transaction():
            if self.active_task():
                raise ValueError("有未完成任務時不能切換時間模式")
            clock = LogicalClock.create(speed)
            self.set_meta("clock", clock)
            self.audit("clock_changed", clock)
            return clock

    def task(self, task_id: str) -> Task:
        with self.transaction() as db:
            row = db.execute("SELECT data FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise ValueError("找不到任務")
            return decode(Task, json.loads(row[0]))

    def active_task(self) -> Task | None:
        with self.transaction() as db:
            row = db.execute("SELECT data FROM tasks WHERE active=1").fetchone()
            return decode(Task, json.loads(row[0])) if row else None

    def recent_tasks(self) -> list[dict]:
        with self.transaction() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT data FROM tasks ORDER BY rowid DESC LIMIT 20")]

    def create_task(self, constraints: TaskConstraints | None, program: WashProgram, tariff: Tariff,
                    message: str = "") -> Task:
        with self.transaction() as db:
            if self.active_task():
                raise ValueError("已有未完成任務（故障或未知狀態須先人工處理）")
            task = Task(uuid4().hex, 1, "needs_details" if constraints is None else "pending", constraints, program, tariff, message=message)
            db.execute("INSERT INTO tasks VALUES (?,?,1,?)", (task.task_id, 1, json.dumps(encode(task))))
            self.audit("task_created", task)
            return task

    def update_task(self, task_id: str, expected: int, **changes) -> Task:
        from dataclasses import replace
        with self.transaction() as db:
            current = self.task(task_id)
            if current.version != expected:
                raise ValueError("任務版本已改變，請刷新後重試")
            if "task_id" in changes or "version" in changes:
                raise ValueError("不可修改任務識別碼／版本")
            updated = replace(current, version=expected + 1, **changes)
            db.execute("UPDATE tasks SET version=?,active=?,data=? WHERE id=? AND version=?",
                       (updated.version, int(updated.status not in TERMINAL), json.dumps(encode(updated)), task_id, expected))
            if "plan" in changes and updated.plan:
                db.execute("INSERT INTO plans(task_id,version,data) VALUES (?,?,?)",
                           (task_id, updated.version, json.dumps(encode(updated.plan))))
            self.audit("task_updated", updated)
            return updated

    def propose(self, task_id: str, expected: int, constraints: TaskConstraints, plan: Plan,
                reschedule: bool = False, reason: str = "") -> Task:
        with self.transaction():
            task = self.task(task_id)
            if task.status not in {"needs_details", "pending", "scheduled"}:
                raise ValueError("目前狀態不可提案或改排")
            if plan.program != task.program or plan.tariff != task.tariff:
                raise ValueError("計畫必須使用任務行程與費率快照")
            clock = self.clock()
            validate_plan(plan, constraints, clock.now(), clock.clock_id)
            status = "pending"
            approval = task.approval
            if reschedule and approval:
                try:
                    validate_plan(plan, constraints, clock.now(), clock.clock_id, approval)
                    status = "scheduled"
                except ValueError:
                    approval = None
                    reason = f"{reason}；超出原核准範圍，等待重新核准"
            else:
                approval = None
            return self.update_task(task_id, expected, constraints=constraints, plan=plan, approval=approval,
                                    status=status, message=reason)

    def approve(self, task_id: str, expected: int, max_cost: float) -> Task:
        with self.transaction():
            task = self.task(task_id)
            if task.status != "pending" or task.plan is None or task.constraints is None:
                raise ValueError("只能核准完整的待核准計畫")
            number(max_cost, "費用上限")
            envelope = ApprovalEnvelope(task.constraints, task.plan.program, task.plan.tariff, task.plan.clock_id, max_cost)
            clock = self.clock()
            validate_plan(task.plan, task.constraints, clock.now(), clock.clock_id, envelope)
            return self.update_task(task_id, expected, approval=envelope, status="scheduled", message="使用者已核准")

    def submit(self, request_id: str, kind: str, payload: dict) -> None:
        with self.transaction() as db:
            previous = db.execute("SELECT kind,payload FROM requests WHERE id=?", (request_id,)).fetchone()
            serialized = json.dumps(payload, sort_keys=True, allow_nan=False)
            if previous and (previous[0] != kind or previous[1] != serialized):
                raise ValueError("請求識別碼已用於其他內容")
            db.execute("INSERT OR IGNORE INTO requests(id,kind,payload,created) VALUES (?,?,?,?)",
                       (request_id, kind, serialized, utcnow().isoformat()))

    def pending_requests(self) -> list[dict]:
        with self.transaction() as db:
            return [dict(r) for r in db.execute("SELECT * FROM requests WHERE status='pending' ORDER BY rowid")]

    def finish_request(self, request_id: str, status: str, result: str) -> None:
        with self.transaction() as db:
            db.execute("UPDATE requests SET status=?,result=? WHERE id=?", (status, result, request_id))

    def event(self, key: str, task_id: str | None, payload: dict) -> None:
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO events(id,key,task_id,payload) VALUES (?,?,?,?)",
                       (uuid4().hex, key, task_id, json.dumps(payload)))

    def claim_event(self) -> AgentEvent | None:
        with self.transaction() as db:
            row = db.execute("SELECT * FROM events WHERE status='pending' ORDER BY rowid LIMIT 1").fetchone()
            if not row:
                return None
            db.execute("UPDATE events SET status='running' WHERE id=?", (row["id"],))
            return AgentEvent(row["id"], row["key"], row["task_id"], json.loads(row["payload"]))

    def finish_event(self, event_id: str) -> None:
        with self.transaction() as db:
            db.execute("UPDATE events SET status='done' WHERE id=?", (event_id,))

    def recover_events(self) -> None:
        with self.transaction() as db:
            db.execute("UPDATE events SET status='pending' WHERE status='running'")

    def audit(self, kind: str, data: Any) -> None:
        with self.transaction() as db:
            db.execute("INSERT INTO audit(time,kind,data) VALUES (?,?,?)", (utcnow().isoformat(), kind, json.dumps(encode(data))))

    def save_device(self, state: Any) -> None:
        with self.transaction() as db:
            payload = json.dumps(encode(state))
            db.execute("INSERT OR REPLACE INTO devices VALUES (?,?)", (state.device_id, payload))
            db.execute("INSERT INTO samples(device_id,data) VALUES (?,?)", (state.device_id, payload))
            db.execute("DELETE FROM samples WHERE id NOT IN (SELECT id FROM samples ORDER BY id DESC LIMIT 900)")

    def device(self, device_id: str = "washer-1") -> dict | None:
        with self.transaction() as db:
            row = db.execute("SELECT data FROM devices WHERE id=?", (device_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def save_command(self, command: Any) -> None:
        with self.transaction() as db:
            db.execute("INSERT INTO commands(id,data,created) VALUES (?,?,?)",
                       (command.command_id, json.dumps(encode(command)), utcnow().isoformat()))

    def command_result(self, result: Any) -> None:
        with self.transaction() as db:
            db.execute("UPDATE commands SET result=? WHERE id=?", (json.dumps(encode(result)), result.command_id))

    def record_run(self, run: AgentRun) -> None:
        with self.transaction() as db:
            db.execute("INSERT OR REPLACE INTO agent_runs VALUES (?,?)", (run.run_id, json.dumps(encode(run))))

    def log_tool(self, run_id: str, data: dict) -> None:
        with self.transaction() as db:
            db.execute("INSERT INTO tool_logs(run_id,data) VALUES (?,?)", (run_id, json.dumps(encode(data), allow_nan=False)))

    def records(self, table: str, limit: int = 50) -> list[dict]:
        if table not in {"agent_runs", "tool_logs", "samples", "audit", "requests", "commands", "plans"}:
            raise ValueError("不允許的紀錄查詢")
        with self.transaction() as db:
            rows = db.execute(f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]
