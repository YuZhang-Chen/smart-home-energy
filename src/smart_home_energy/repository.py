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
from smart_home_energy.ac_domain import ACConstraints, ACPlan, ACApproval, validate_ac

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
    device_id: str = "washer-1"
    kind: str = "washer"
    ac_constraints: ACConstraints | None = None
    ac_plan: ACPlan | None = None
    ac_approval: ACApproval | None = None


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
    parent_id: str | None = None
    device_id: str | None = None
    role: str = "main"


class Repository:
    def __init__(self, path: str | Path):
        self.path = str(Path(path).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.local = threading.local()
        with self.transaction() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] > 2:
                raise ValueError("資料庫版本較新，拒絕降版")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, version INTEGER NOT NULL,
                    active INTEGER NOT NULL, data TEXT NOT NULL);
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
            if not db.in_transaction:
                db.execute("BEGIN IMMEDIATE")
            columns = {r[1] for r in db.execute("PRAGMA table_info(tasks)")}
            if "device_id" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN device_id TEXT NOT NULL DEFAULT 'washer-1'")
            db.execute("DROP INDEX IF EXISTS single_active")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS active_per_device ON tasks(device_id) WHERE active=1")
            db.execute("PRAGMA user_version=2")
            db.execute("CREATE TABLE IF NOT EXISTS delegations(id TEXT PRIMARY KEY, root_id TEXT NOT NULL, device_id TEXT NOT NULL, task_id TEXT, data TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS energy_segments(device_id TEXT, boot_id TEXT, sequence INTEGER, task_id TEXT, run_id TEXT, start TEXT, end TEXT, power_w REAL, PRIMARY KEY(device_id,boot_id,sequence))")

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
            if self.active_tasks():
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

    def active_task(self, device_id: str = "washer-1") -> Task | None:
        with self.transaction() as db:
            row = db.execute("SELECT data FROM tasks WHERE active=1 AND device_id=?", (device_id,)).fetchone()
            return decode(Task, json.loads(row[0])) if row else None

    def active_tasks(self) -> list[Task]:
        with self.transaction() as db:
            return [decode(Task, json.loads(r[0])) for r in db.execute("SELECT data FROM tasks WHERE active=1 ORDER BY rowid")]

    def recent_tasks(self) -> list[dict]:
        with self.transaction() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT data FROM tasks ORDER BY rowid DESC LIMIT 20")]

    def create_task(self, constraints: TaskConstraints | None, program: WashProgram, tariff: Tariff,
                    message: str = "") -> Task:
        with self.transaction() as db:
            if self.active_task():
                raise ValueError("已有未完成任務（故障或未知狀態須先人工處理）")
            task = Task(uuid4().hex, 1, "needs_details" if constraints is None else "pending", constraints, program, tariff, message=message)
            db.execute("INSERT INTO tasks(id,version,active,data,device_id) VALUES (?,?,1,?,?)", (task.task_id, 1, json.dumps(encode(task)), task.device_id))
            self.audit("task_created", task)
            return task

    def update_task(self, task_id: str, expected: int, **changes) -> Task:
        from dataclasses import replace
        if type(expected) is not int or expected < 1:
            raise ValueError("預期版本必須為正整數")
        with self.transaction() as db:
            current = self.task(task_id)
            if current.version != expected:
                raise ValueError("任務版本已改變，請刷新後重試")
            if {"task_id", "version", "device_id", "kind"} & changes.keys():
                raise ValueError("不可修改任務識別碼／版本")
            updated = replace(current, version=expected + 1, **changes)
            db.execute("UPDATE tasks SET version=?,active=?,data=? WHERE id=? AND version=?",
                       (updated.version, int(updated.status not in TERMINAL), json.dumps(encode(updated)), task_id, expected))
            if "plan" in changes and updated.plan:
                db.execute("INSERT INTO plans(task_id,version,data) VALUES (?,?,?)",
                           (task_id, updated.version, json.dumps(encode(updated.plan))))
            self.audit("task_updated", updated)
            return updated

    def create_ac_task(self, constraints: ACConstraints | None, tariff: Tariff, message: str = "") -> Task:
        with self.transaction() as db:
            if self.active_task("ac-1"):
                raise ValueError("冷氣已有未完成任務")
            task = Task(uuid4().hex, 1, "needs_details" if constraints is None else "pending", tariff=tariff,
                        message=message, device_id="ac-1", kind="ac", ac_constraints=constraints)
            db.execute("INSERT INTO tasks(id,version,active,data,device_id) VALUES (?,?,1,?,?)", (task.task_id, 1, json.dumps(encode(task)), task.device_id))
            self.audit("task_created", task)
            return task

    def propose_ac(self, task_id: str, expected: int, plan: ACPlan) -> Task:
        with self.transaction() as db:
            task = self.task(task_id)
            if task.kind != "ac" or task.status not in {"needs_details", "pending", "scheduled"} or task.tariff != plan.tariff:
                raise ValueError("冷氣目前狀態或費率快照不符")
            clock = self.clock()
            validate_ac(plan, clock.now(), clock.clock_id)
            approval, status = None if plan.start_on_approval else task.ac_approval, "pending"
            if approval:
                try:
                    validate_ac(plan, clock.now(), clock.clock_id, approval)
                    status = "scheduled"
                except ValueError:
                    approval = None
            updated = self.update_task(task_id, expected, ac_constraints=plan.constraints, ac_plan=plan,
                                       ac_approval=approval, status=status, message="冷氣計畫已驗證；費用為估算，舒適優先")
            db.execute("INSERT INTO plans(task_id,version,data) VALUES (?,?,?)", (task_id, updated.version, json.dumps(encode(plan))))
            return updated

    def approve_ac(self, task_id: str, expected: int) -> Task:
        with self.transaction():
            task = self.task(task_id)
            if task.kind != "ac" or task.status != "pending" or not task.ac_plan:
                raise ValueError("只能核准完整冷氣提案")
            p, clock = task.ac_plan, self.clock()
            if p.start_on_approval:
                from dataclasses import replace
                from datetime import datetime, timedelta
                from smart_home_energy.ac_domain import make_ac_plan
                state = self.device("ac-1")
                if not state or not -2 <= (utcnow() - datetime.fromisoformat(state["observed_at"])).total_seconds() <= 10:
                    raise ValueError("冷氣回報過期，請等待重新連線後確認")
                if state["status"] not in {"idle", "completed", "aborted"} or state["clock_id"] != clock.clock_id:
                    raise ValueError("冷氣目前無法啟動，請先查核設備狀態")
                start = clock.now() + timedelta(seconds=2 * clock.speed)
                duration = p.constraints.end - p.constraints.start
                p = make_ac_plan(replace(p.constraints, start=start, end=start + duration), state["temperature_c"], p.tariff, clock.clock_id)
            validate_ac(p, clock.now(), clock.clock_id)
            self.check_quote(task)
            if task.ac_plan.start_on_approval:
                with self.transaction() as db:
                    db.execute("INSERT INTO plans(task_id,version,data) VALUES (?,?,?)", (task_id, expected + 1, json.dumps(encode(p))))
            return self.update_task(task_id, expected, ac_plan=p, ac_constraints=p.constraints,
                                    ac_approval=ACApproval(p.constraints, p.clock_id, p.tariff), status="scheduled")

    def check_quote(self, task: Task):
        if task.tariff.official:
            from smart_home_energy.tariffs import selected_tariff
            current = selected_tariff(self, exclude_task=task.task_id)
            if current != task.tariff:
                raise ValueError("用電基準、費率或已核准負載已改變，請重新計算計畫再核准")

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
            self.check_quote(task)
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
            from datetime import datetime
            previous = self.device(state.device_id)
            if state.task_id and state.run_id and state.energy_kwh > 0:
                same = previous and previous.get("run_id") == state.run_id and previous.get("boot_id") == state.boot_id
                before = previous["energy_kwh"] if same else 0
                start = datetime.fromisoformat(previous["logical_at"]) if same else None
                if start is None:
                    row = db.execute("SELECT data FROM tasks WHERE id=?", (state.task_id,)).fetchone()
                    task = decode(Task, json.loads(row[0])) if row else None
                    start = task.plan.start if task and task.plan else task.ac_plan.constraints.start if task and task.ac_plan else state.logical_at
                elapsed = (state.logical_at - start).total_seconds()
                delta = state.energy_kwh - before
                if elapsed > 0 and delta > 0:
                    db.execute("INSERT OR IGNORE INTO energy_segments VALUES (?,?,?,?,?,?,?,?)", (state.device_id, state.boot_id,
                        state.sequence, state.task_id, state.run_id, start.isoformat(), state.logical_at.isoformat(), delta * 3_600_000 / elapsed))
            payload = json.dumps(encode(state))
            db.execute("INSERT OR REPLACE INTO devices VALUES (?,?)", (state.device_id, payload))
            db.execute("INSERT INTO samples(device_id,data) VALUES (?,?)", (state.device_id, payload))
            db.execute("DELETE FROM samples WHERE device_id=? AND id NOT IN (SELECT id FROM samples WHERE device_id=? ORDER BY id DESC LIMIT 900)", (state.device_id, state.device_id))

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
        if table not in {"agent_runs", "tool_logs", "samples", "audit", "requests", "commands", "plans", "delegations", "energy_segments"}:
            raise ValueError("不允許的紀錄查詢")
        with self.transaction() as db:
            rows = db.execute(f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]
