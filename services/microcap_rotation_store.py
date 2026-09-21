"""Dedicated ledger, immutable input batches and atomic four-account settlement."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
from services.microcap_rotation_policy import VERSION, POLICY, now

def dumps(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False,separators=(",",":"))

def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()

def default_db():
    from core import db
    return Path(db.DB_PATH)

@contextmanager
def process_lock(db_path=None):
    path=Path(db_path or default_db()).with_suffix(".microcap_rotation.lock")
    path.parent.mkdir(parents=True,exist_ok=True)
    stream=path.open("a+b")
    try:
        import fcntl
        try:
            fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("ABCD更新任务正在运行，请稍后重试") from None
        yield
    finally:
        stream.close()

class Store:
    def __init__(self,path=None):
        self.path=Path(path or default_db())

    @contextmanager
    def connection(self,write=False):
        if write:
            self.path.parent.mkdir(parents=True,exist_ok=True)
        conn=sqlite3.connect(str(self.path) if write else self.path.resolve().as_uri()+"?mode=ro",uri=not write,timeout=30)
        conn.row_factory=sqlite3.Row
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if write:
                conn.commit()
        except BaseException:
            if write:
                conn.rollback()
            raise
        finally:
            conn.close()

    def setup(self):
        with self.connection(True) as c:
            tables={
                "accounts":"version TEXT,strategy TEXT,enabled_at TEXT,policy TEXT,PRIMARY KEY(version,strategy)",
                "inputs":"version TEXT,date TEXT,hash TEXT,payload TEXT,PRIMARY KEY(version,date)",
                "daily":"version TEXT,strategy TEXT,date TEXT,payload TEXT,completed_at TEXT,PRIMARY KEY(version,strategy,date)",
                "plans":"version TEXT,strategy TEXT,date TEXT,payload TEXT,PRIMARY KEY(version,strategy,date)",
                "fills":"version TEXT,strategy TEXT,date TEXT,event_id TEXT,payload TEXT,PRIMARY KEY(version,strategy,date,event_id)",
                "positions":"version TEXT,strategy TEXT,date TEXT,code TEXT,payload TEXT,PRIMARY KEY(version,strategy,date,code)",
                "events":"version TEXT,strategy TEXT,event_id TEXT,date TEXT,payload TEXT,PRIMARY KEY(version,strategy,event_id)",
                "research":"id TEXT PRIMARY KEY,payload TEXT,imported_at TEXT",
                "launch":"version TEXT PRIMARY KEY,payload TEXT"}
            for name,cols in tables.items():
                c.execute(f"CREATE TABLE IF NOT EXISTS microcap_rotation_{name} ({cols})")

    def has_schema(self):
        if not self.path.exists():
            return False
        with self.connection() as c:
            return c.execute("SELECT 1 FROM sqlite_master WHERE name='microcap_rotation_accounts'").fetchone() is not None

    def accounts(self):
        if not self.has_schema():
            return []
        with self.connection() as c:
            return [dict(r) for r in c.execute("SELECT * FROM microcap_rotation_accounts WHERE version=? ORDER BY strategy",(VERSION,))]

    def enable(self,at=None):
        self.setup()
        at=at or now().isoformat(timespec="seconds")
        with self.connection(True) as c:
            for s in "ABCD":
                c.execute("INSERT OR IGNORE INTO microcap_rotation_accounts VALUES (?,?,?,?)",(VERSION,s,at,dumps(POLICY)))
            rows=c.execute("SELECT policy FROM microcap_rotation_accounts WHERE version=?",(VERSION,)).fetchall()
            expected=dumps(POLICY)
            for row in rows:
                if row[0] == expected:
                    continue
                # Existing v2 ledgers predate the explicit ETF fee field. This
                # is an accounting correction, so migrate that metadata in
                # place while still rejecting every other parameter change.
                try:
                    old_policy=json.loads(row[0])
                except (TypeError, ValueError):
                    old_policy=None
                legacy=dict(POLICY)
                legacy.pop("etf_fee_rate",None)
                if old_policy != legacy:
                    raise ValueError("参数变化必须使用新策略版本")
                c.execute("UPDATE microcap_rotation_accounts SET policy=? WHERE version=?",(expected,VERSION))
        return self.accounts()

    def days(self):
        if not self.has_schema():
            return []
        with self.connection() as c:
            return [dict(strategy=r["strategy"],completed_at=r["completed_at"],**json.loads(r["payload"]))
                    for r in c.execute("SELECT * FROM microcap_rotation_daily WHERE version=? ORDER BY date,strategy",(VERSION,))]

    def batch(self,day):
        if not self.has_schema():
            return None
        with self.connection() as c:
            r=c.execute("SELECT hash,payload FROM microcap_rotation_inputs WHERE version=? AND date=?",(VERSION,day)).fetchone()
        if r:
            payload=json.loads(r["payload"])
            if digest(payload)!=r["hash"]:
                raise ValueError("冻结输入哈希错误")
            return payload
        return None

    def commit_day(self,batch,states,revision_reason=None):
        if set(states)!=set("ABCD") or any(s["date"]!=batch["date"] for s in states.values()):
            raise ValueError("必须同时提交四账户同一日结")
        day=batch["date"]
        with self.connection(True) as c:
            existing=c.execute("SELECT strategy FROM microcap_rotation_daily WHERE version=? AND date=?",(VERSION,day)).fetchall()
            if existing:
                if len(existing)!=4:
                    raise ValueError("日结不完整，拒绝覆盖")
                if not revision_reason:
                    return False
                latest=c.execute("SELECT MAX(date) FROM microcap_rotation_daily WHERE version=?",(VERSION,)).fetchone()[0]
                if latest!=day:
                    raise ValueError("仅允许重算最新日结；不能跳过下游账户重放")
                tables=("inputs","daily","plans","fills","positions","events")
                archived={name:[dict(r) for r in c.execute(
                    f"SELECT * FROM microcap_rotation_{name} WHERE version=? AND date=?",(VERSION,day))]
                    for name in tables}
                c.execute("CREATE TABLE IF NOT EXISTS microcap_rotation_revisions (id INTEGER PRIMARY KEY,version TEXT,date TEXT,reason TEXT,old_hash TEXT,new_hash TEXT,payload TEXT,created_at TEXT)")
                c.execute("INSERT INTO microcap_rotation_revisions (version,date,reason,old_hash,new_hash,payload,created_at) VALUES (?,?,?,?,?,?,?)",
                          (VERSION,day,revision_reason,digest(archived),digest(batch),dumps(archived),now().isoformat(timespec="seconds")))
                for name in tables:
                    c.execute(f"DELETE FROM microcap_rotation_{name} WHERE version=? AND date=?",(VERSION,day))
            c.execute("INSERT INTO microcap_rotation_inputs VALUES (?,?,?,?)",(VERSION,day,digest(batch),dumps(batch)))
            for s,state in states.items():
                c.execute("INSERT INTO microcap_rotation_daily VALUES (?,?,?,?,?)",(VERSION,s,day,dumps(state),now().strftime("%Y-%m-%d %H:%M:%S")))
                if state["plan"]:
                    c.execute("INSERT INTO microcap_rotation_plans VALUES (?,?,?,?)",(VERSION,s,day,dumps(state["plan"])))
                for i,fill in enumerate(state["fills"]):
                    c.execute("INSERT INTO microcap_rotation_fills VALUES (?,?,?,?,?)",(VERSION,s,day,str(i),dumps(fill)))
                for code,pos in state["positions"].items():
                    c.execute("INSERT INTO microcap_rotation_positions VALUES (?,?,?,?,?)",(VERSION,s,day,code,dumps(pos)))
                for event in state["events"]:
                    c.execute("INSERT INTO microcap_rotation_events VALUES (?,?,?,?,?)",(VERSION,s,event["id"],day,dumps(event)))
        return True

    def save_launch(self,payload):
        self.setup()
        with self.connection(True) as c:
            c.execute("INSERT OR IGNORE INTO microcap_rotation_launch VALUES (?,?)",(VERSION,dumps(payload)))

    def launch(self):
        if not self.has_schema():
            return None
        with self.connection() as c:
            if not c.execute("SELECT 1 FROM sqlite_master WHERE name='microcap_rotation_launch'").fetchone():
                return None
            row=c.execute("SELECT payload FROM microcap_rotation_launch WHERE version=?",(VERSION,)).fetchone()
        return json.loads(row[0]) if row else None

    def input_manifest(self):
        if not self.has_schema():
            return []
        with self.connection() as c:
            return [dict(r) for r in c.execute("SELECT version,date,hash FROM microcap_rotation_inputs WHERE version=? ORDER BY date",(VERSION,))]

    def save_research(self,report):
        self.setup()
        import pandas as pd
        payload=pd.Series([report]).to_json(orient="values",force_ascii=False,double_precision=10)[1:-1]
        key=digest({"hashes":report["hashes"],"calculation_version":report["calculation_version"]})
        with self.connection(True) as c:
            c.execute("INSERT OR IGNORE INTO microcap_rotation_research VALUES (?,?,?)",(key,payload,now().strftime("%Y-%m-%d %H:%M:%S")))
        return key

    def research(self):
        if not self.has_schema():
            return None
        with self.connection() as c:
            row=c.execute("SELECT payload FROM microcap_rotation_research ORDER BY imported_at DESC LIMIT 1").fetchone()
        return json.loads(row[0]) if row else None
