from __future__ import annotations

from contextlib import closing
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path

import pandas as pd

from core.db import get_conn, init_db
from services.futures_spread import completed_futures_daily_cutoff
from services.futures_live_models import (
    CASH_FLOW_TYPES,
    DAILY_PNL_RESOLUTIONS,
    StatementPayload,
    StatementSyncResult,
    discover_statement_files,
)
from services.futures_live_statement_parser import _date_text, parse_statement

def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_list(values: list[str]) -> str:
    return json.dumps(values, ensure_ascii=False)


def _insert_statement_payload(conn, path: Path, payload: StatementPayload, file_hash: str) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    source_file = str(path.resolve())
    conn.execute("DELETE FROM futures_account_monthly WHERE source_file=?", (source_file,))
    conn.execute("DELETE FROM futures_month_end_positions WHERE source_file=?", (source_file,))
    conn.execute(
        "DELETE FROM futures_live_trades WHERE source='月结单' AND source_file=?",
        (source_file,),
    )
    conn.execute(
        "DELETE FROM futures_cash_flows WHERE source='月结单' AND source_file=?",
        (source_file,),
    )
    account = payload.account
    conn.execute(
        """
        INSERT OR REPLACE INTO futures_account_monthly (
            statement_month, statement_end_date, previous_balance,
            deposits_withdrawals, monthly_pnl, total_premium, monthly_fee,
            declaration_fee, ending_balance, customer_equity, cash_equity, frozen_funds, margin,
            floating_pnl, available_funds, risk_ratio, additional_margin,
            source_file, imported_at, warnings
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload.statement_month,
            payload.statement_end_date,
            account.get("previous_balance"),
            account.get("deposits_withdrawals"),
            account.get("monthly_pnl"),
            account.get("total_premium"),
            account.get("monthly_fee"),
            account.get("declaration_fee"),
            account.get("ending_balance"),
            account.get("customer_equity"),
            account.get("cash_equity"),
            account.get("frozen_funds"),
            account.get("margin"),
            account.get("floating_pnl"),
            account.get("available_funds"),
            account.get("risk_ratio"),
            account.get("additional_margin"),
            source_file,
            now,
            _json_list(payload.warnings),
        ),
    )
    for record in payload.positions.to_dict("records"):
        conn.execute(
            """
            INSERT INTO futures_month_end_positions (
                statement_month, statement_end_date, asset_type, contract, side,
                quantity, average_price, previous_settlement, settlement_price,
                floating_pnl, margin, multiplier, trade_code, source_file
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload.statement_month,
                payload.statement_end_date,
                record.get("asset_type"),
                record.get("contract"),
                record.get("side"),
                record.get("quantity"),
                record.get("average_price"),
                record.get("previous_settlement"),
                record.get("settlement_price"),
                record.get("floating_pnl"),
                record.get("margin"),
                record.get("multiplier"),
                record.get("trade_code"),
                source_file,
            ),
        )
    for record in payload.trades.to_dict("records"):
        conn.execute(
            """
            INSERT INTO futures_live_trades (
                source, statement_month, trade_date, trade_time, asset_type,
                contract, broker_trade_id, buy_sell, open_close, price, quantity,
                turnover, multiplier, fee, close_pnl, strategy, notes,
                reconciliation_status, source_file, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.get("source"),
                record.get("statement_month"),
                record.get("trade_date"),
                record.get("trade_time"),
                record.get("asset_type"),
                record.get("contract"),
                record.get("broker_trade_id"),
                record.get("buy_sell"),
                record.get("open_close"),
                record.get("price"),
                record.get("quantity"),
                record.get("turnover"),
                record.get("multiplier"),
                record.get("fee"),
                record.get("close_pnl"),
                record.get("strategy"),
                record.get("notes"),
                record.get("reconciliation_status"),
                source_file,
                now,
            ),
        )
    for record in payload.cash_flows.to_dict("records"):
        source_key = f"{source_file}|{record.get('source_row')}"
        conn.execute(
            """
            INSERT INTO futures_cash_flows (
                source, statement_month, flow_date, entry_type, amount, notes,
                reconciliation_status, matched_statement_flow_id, source_key,
                source_file, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
            """,
            (
                record.get("source"),
                record.get("statement_month"),
                record.get("flow_date"),
                record.get("entry_type"),
                record.get("amount"),
                record.get("notes"),
                record.get("reconciliation_status"),
                source_key,
                source_file,
                now,
            ),
        )
    stat = path.stat()
    conn.execute(
        """
        INSERT INTO futures_statement_imports (
            file_path, file_name, file_size, file_mtime_ns, file_hash,
            statement_month, imported_at, status, warnings, error_message
        ) VALUES (?, ?, ?, ?, ?, ?, ?, '成功', ?, '')
        ON CONFLICT(file_path) DO UPDATE SET
            file_name=excluded.file_name,
            file_size=excluded.file_size,
            file_mtime_ns=excluded.file_mtime_ns,
            file_hash=excluded.file_hash,
            statement_month=excluded.statement_month,
            imported_at=excluded.imported_at,
            status='成功',
            warnings=excluded.warnings,
            error_message=''
        """,
        (
            source_file,
            path.name,
            stat.st_size,
            stat.st_mtime_ns,
            file_hash,
            payload.statement_month,
            now,
            _json_list(payload.warnings),
        ),
    )


def _backfill_position_multipliers(conn) -> None:
    rows = conn.execute(
        """
        SELECT asset_type, contract, multiplier
        FROM futures_live_trades
        WHERE multiplier IS NOT NULL AND multiplier > 0
        """
    ).fetchall()
    grouped: dict[tuple[str, str], list[float]] = {}
    for asset_type, contract, multiplier in rows:
        grouped.setdefault((asset_type, contract), []).append(float(multiplier))
    for (asset_type, contract), values in grouped.items():
        median = float(pd.Series(values).median())
        conn.execute(
            """
            UPDATE futures_month_end_positions
            SET multiplier=?
            WHERE asset_type=? AND contract=? AND multiplier IS NULL
            """,
            (median, asset_type, contract),
        )


def _manual_match_candidates(conn, manual) -> list[int]:
    if manual["broker_trade_id"]:
        rows = conn.execute(
            """
            SELECT id FROM futures_live_trades
            WHERE source='月结单' AND asset_type=? AND broker_trade_id=?
            """,
            (manual["asset_type"], manual["broker_trade_id"]),
        ).fetchall()
        if rows:
            return [int(row[0]) for row in rows]
    params: list[object] = [
        manual["trade_date"],
        manual["asset_type"],
        manual["contract"],
        manual["buy_sell"],
        manual["price"],
        manual["quantity"],
    ]
    time_clause = ""
    if manual["trade_time"]:
        time_clause = " AND trade_time=?"
        params.append(manual["trade_time"])
    rows = conn.execute(
        f"""
        SELECT id FROM futures_live_trades
        WHERE source='月结单' AND trade_date=? AND asset_type=? AND contract=?
          AND buy_sell=? AND ABS(price - ?) < 0.0000001 AND quantity=?
          {time_clause}
        """,
        tuple(params),
    ).fetchall()
    return [int(row[0]) for row in rows]


def _reconcile_manual_trades(conn) -> None:
    conn.execute(
        """
        UPDATE futures_live_trades
        SET reconciliation_status='手工', matched_statement_trade_id=NULL
        WHERE source='手工'
        """
    )
    conn.row_factory = __import__("sqlite3").Row
    manual_rows = conn.execute(
        "SELECT * FROM futures_live_trades WHERE source='手工' ORDER BY id"
    ).fetchall()
    for manual in manual_rows:
        candidates = _manual_match_candidates(conn, manual)
        if len(candidates) == 1:
            conn.execute(
                """
                UPDATE futures_live_trades
                SET reconciliation_status='已接管', matched_statement_trade_id=?
                WHERE id=?
                """,
                (candidates[0], manual["id"]),
            )
        elif len(candidates) > 1:
            conn.execute(
                """
                UPDATE futures_live_trades
                SET reconciliation_status='待核对', matched_statement_trade_id=NULL
                WHERE id=?
                """,
                (manual["id"],),
            )
    conn.row_factory = None


def _reconcile_manual_cash_flows(conn) -> None:
    conn.execute(
        """
        UPDATE futures_cash_flows
        SET reconciliation_status='手工', matched_statement_flow_id=NULL
        WHERE source='手工'
        """
    )
    latest_row = conn.execute(
        "SELECT MAX(statement_end_date) FROM futures_account_monthly"
    ).fetchone()
    latest_end = str(latest_row[0] or "") if latest_row else ""
    conn.row_factory = __import__("sqlite3").Row
    manual_rows = conn.execute(
        "SELECT * FROM futures_cash_flows WHERE source='手工' ORDER BY id"
    ).fetchall()
    for manual in manual_rows:
        if latest_end and str(manual["flow_date"]) <= latest_end:
            candidates = conn.execute(
                """
                SELECT id FROM futures_cash_flows
                WHERE source='月结单' AND flow_date=? AND entry_type=?
                  AND ABS(amount - ?) < 0.0000001
                """,
                (manual["flow_date"], manual["entry_type"], manual["amount"]),
            ).fetchall()
            if len(candidates) == 1:
                conn.execute(
                    """
                    UPDATE futures_cash_flows
                    SET reconciliation_status='已接管', matched_statement_flow_id=?
                    WHERE id=?
                    """,
                    (int(candidates[0][0]), int(manual["id"])),
                )
            else:
                conn.execute(
                    """
                    UPDATE futures_cash_flows
                    SET reconciliation_status='待核对', matched_statement_flow_id=NULL
                    WHERE id=?
                    """,
                    (int(manual["id"]),),
                )
    conn.row_factory = None


def _reconcile_option_expiry_events(conn) -> None:
    latest_row = conn.execute(
        "SELECT MAX(statement_end_date) FROM futures_account_monthly"
    ).fetchone()
    latest_end = str(latest_row[0] or "") if latest_row else ""
    if not latest_end:
        return
    conn.row_factory = __import__("sqlite3").Row
    events = conn.execute(
        """
        SELECT * FROM futures_option_expiry_events
        WHERE source='手工' AND event_date <= ?
        ORDER BY id
        """,
        (latest_end,),
    ).fetchall()
    for event in events:
        month = str(event["event_date"])[:7]
        option_quantity = conn.execute(
            """
            SELECT COALESCE(SUM(quantity), 0)
            FROM futures_month_end_positions
            WHERE statement_month=? AND asset_type='期权' AND contract=?
            """,
            (month, event["option_contract"]),
        ).fetchone()[0]
        reconciled = int(option_quantity or 0) == 0
        if reconciled and event["outcome"] == "履约":
            futures_quantity = conn.execute(
                """
                SELECT COALESCE(SUM(quantity), 0)
                FROM futures_month_end_positions
                WHERE statement_month=? AND asset_type='期货'
                  AND contract=? AND side=?
                """,
                (month, event["underlying_contract"], event["futures_side"]),
            ).fetchone()[0]
            reconciled = int(futures_quantity or 0) >= int(event["quantity"])
        conn.execute(
            """
            UPDATE futures_option_expiry_events
            SET reconciliation_status=?
            WHERE id=?
            """,
            ("已接管" if reconciled else "待核对", int(event["id"])),
        )
    conn.row_factory = None


def sync_statements(
    directory: str | os.PathLike[str] | None = None,
    *,
    force: bool = False,
) -> StatementSyncResult:
    init_db()
    files = discover_statement_files(directory)
    imported = skipped = failed = 0
    all_warnings: list[str] = []
    errors: list[str] = []
    with closing(get_conn()) as conn:
        for path in files:
            source_file = str(path.resolve())
            current_hash = _file_hash(path)
            existing = conn.execute(
                "SELECT file_hash, status FROM futures_statement_imports WHERE file_path=?",
                (source_file,),
            ).fetchone()
            if not force and existing and existing[0] == current_hash and existing[1] == "成功":
                skipped += 1
                continue
            try:
                payload = parse_statement(path)
                conn.execute("SAVEPOINT import_statement")
                _insert_statement_payload(conn, path, payload, current_hash)
                conn.execute("RELEASE SAVEPOINT import_statement")
                imported += 1
                all_warnings.extend(f"{path.name}：{item}" for item in payload.warnings)
            except Exception as exc:
                try:
                    conn.execute("ROLLBACK TO SAVEPOINT import_statement")
                    conn.execute("RELEASE SAVEPOINT import_statement")
                except Exception:
                    pass
                failed += 1
                errors.append(f"{path.name}：{exc}")
                if existing is None:
                    stat = path.stat()
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO futures_statement_imports (
                            file_path, file_name, file_size, file_mtime_ns, file_hash,
                            statement_month, imported_at, status, warnings, error_message
                        ) VALUES (?, ?, ?, ?, ?, '', ?, '失败', '[]', ?)
                        """,
                        (
                            source_file,
                            path.name,
                            stat.st_size,
                            stat.st_mtime_ns,
                            current_hash,
                            datetime.now().isoformat(timespec="seconds"),
                            str(exc),
                        ),
                    )
            conn.commit()
        _backfill_position_multipliers(conn)
        _reconcile_manual_trades(conn)
        _reconcile_manual_cash_flows(conn)
        _reconcile_option_expiry_events(conn)
        conn.commit()
    return StatementSyncResult(
        scanned=len(files),
        imported=imported,
        skipped=skipped,
        failed=failed,
        warnings=all_warnings,
        errors=errors,
    )


def list_statement_imports() -> pd.DataFrame:
    init_db()
    with closing(get_conn()) as conn:
        return pd.read_sql_query(
            """
            SELECT file_name, statement_month, imported_at, status, warnings, error_message
            FROM futures_statement_imports
            ORDER BY statement_month DESC, file_name DESC
            """,
            conn,
        )


def list_monthly_accounts() -> pd.DataFrame:
    init_db()
    with closing(get_conn()) as conn:
        return pd.read_sql_query(
            "SELECT * FROM futures_account_monthly ORDER BY statement_month",
            conn,
        )


def latest_monthly_account() -> dict[str, object] | None:
    accounts = list_monthly_accounts()
    return None if accounts.empty else accounts.iloc[-1].to_dict()


def list_futures_cash_flows(*, include_taken_over: bool = True) -> pd.DataFrame:
    init_db()
    where = (
        ""
        if include_taken_over
        else "WHERE NOT (source='手工' AND reconciliation_status='已接管')"
    )
    with closing(get_conn()) as conn:
        return pd.read_sql_query(
            f"""
            SELECT * FROM futures_cash_flows
            {where}
            ORDER BY flow_date DESC, id DESC
            """,
            conn,
        )


def _effective_cash_flows(*, as_of: object = None) -> pd.DataFrame:
    rows = list_futures_cash_flows(include_taken_over=False)
    if rows.empty:
        return rows
    account = latest_monthly_account()
    latest_end = str(account["statement_end_date"]) if account else ""
    official = rows[rows["source"].eq("月结单")].copy()
    manual = rows[
        rows["source"].eq("手工")
        & ~rows["reconciliation_status"].eq("已接管")
        & (rows["flow_date"].astype(str) > latest_end)
    ].copy()
    result = pd.concat([official, manual], ignore_index=True)
    cutoff = _date_text(as_of) if as_of is not None else ""
    if cutoff:
        result = result[result["flow_date"].astype(str) <= cutoff]
    return result.sort_values(["flow_date", "id"]).reset_index(drop=True)


def add_manual_cash_flow(
    *,
    flow_date: object,
    entry_type: str,
    amount: float,
    notes: str = "",
) -> int:
    init_db()
    account = latest_monthly_account()
    if account is None:
        raise ValueError("请先导入月结单。")
    normalized_date = _date_text(flow_date)
    if not normalized_date:
        raise ValueError("资金流水日期无效。")
    if pd.Timestamp(normalized_date) <= pd.Timestamp(account["statement_end_date"]):
        raise ValueError(
            f"手工资金流水日期必须晚于最新月结单截止日 {account['statement_end_date']}。"
        )
    if entry_type not in CASH_FLOW_TYPES:
        raise ValueError("资金流水类型只能是入金或出金。")
    normalized_amount = float(amount)
    if normalized_amount <= 0:
        raise ValueError("资金流水金额必须大于0。")
    now = datetime.now().isoformat(timespec="seconds")
    with closing(get_conn()) as conn:
        cursor = conn.execute(
            """
            INSERT INTO futures_cash_flows (
                source, statement_month, flow_date, entry_type, amount, notes,
                reconciliation_status, matched_statement_flow_id, source_key,
                source_file, created_at
            ) VALUES ('手工', NULL, ?, ?, ?, ?, '手工', NULL, NULL, NULL, ?)
            """,
            (
                normalized_date,
                entry_type,
                normalized_amount,
                str(notes or "").strip(),
                now,
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)


def delete_manual_cash_flow(flow_id: int) -> bool:
    init_db()
    with closing(get_conn()) as conn:
        row = conn.execute(
            "SELECT source FROM futures_cash_flows WHERE id=?", (int(flow_id),)
        ).fetchone()
        if row is None:
            return False
        if row[0] != "手工":
            raise ValueError("月结单资金流水为只读记录，不能删除。")
        conn.execute("DELETE FROM futures_cash_flows WHERE id=?", (int(flow_id),))
        conn.commit()
    return True


def list_futures_daily_pnl_overrides() -> pd.DataFrame:
    init_db()
    with closing(get_conn()) as conn:
        return pd.read_sql_query(
            """
            SELECT * FROM futures_daily_pnl_overrides
            ORDER BY trade_date DESC, id DESC
            """,
            conn,
        )


def add_manual_daily_pnl(
    *,
    trade_date: object,
    pnl_amount: float,
    notes: str = "",
) -> int:
    init_db()
    if latest_monthly_account() is None:
        raise ValueError("请先导入月结单。")
    normalized_date = _date_text(trade_date)
    if not normalized_date:
        raise ValueError("交易日期无效。")
    latest_completed = completed_futures_daily_cutoff().strftime("%Y-%m-%d")
    if normalized_date > latest_completed:
        raise ValueError(f"只能补录不晚于 {latest_completed} 的已完成交易日。")
    normalized_pnl = pd.to_numeric(pnl_amount, errors="coerce")
    if pd.isna(normalized_pnl):
        raise ValueError("当日盈亏金额无效。")
    now = datetime.now().isoformat(timespec="seconds")
    with closing(get_conn()) as conn:
        conn.execute(
            """
            INSERT INTO futures_daily_pnl_overrides (
                trade_date, pnl_amount, source, notes,
                reconciliation_status, formal_pnl, difference, resolution,
                created_at, updated_at
            ) VALUES (?, ?, '同花顺手工', ?, '待确认', NULL, NULL, NULL, ?, ?)
            ON CONFLICT(trade_date) DO UPDATE SET
                pnl_amount=excluded.pnl_amount,
                notes=excluded.notes,
                reconciliation_status='待确认',
                formal_pnl=NULL,
                difference=NULL,
                resolution=NULL,
                updated_at=excluded.updated_at
            """,
            (
                normalized_date,
                float(normalized_pnl),
                str(notes or "").strip(),
                now,
                now,
            ),
        )
        row = conn.execute(
            "SELECT id FROM futures_daily_pnl_overrides WHERE trade_date=?",
            (normalized_date,),
        ).fetchone()
        conn.commit()
    return int(row[0])


def delete_manual_daily_pnl(record_id: int) -> bool:
    init_db()
    with closing(get_conn()) as conn:
        cursor = conn.execute(
            "DELETE FROM futures_daily_pnl_overrides WHERE id=?",
            (int(record_id),),
        )
        conn.commit()
        return cursor.rowcount > 0


def resolve_manual_daily_pnl(record_id: int, resolution: str) -> bool:
    if resolution not in DAILY_PNL_RESOLUTIONS:
        raise ValueError("核对结果只能是采用手工或采用正式。")
    init_db()
    with closing(get_conn()) as conn:
        row = conn.execute(
            """
            SELECT formal_pnl FROM futures_daily_pnl_overrides
            WHERE id=?
            """,
            (int(record_id),),
        ).fetchone()
        if row is None:
            return False
        if row[0] is None:
            raise ValueError("正式结算盈亏尚未生成，暂不能确认差异。")
        conn.execute(
            """
            UPDATE futures_daily_pnl_overrides
            SET reconciliation_status=?, resolution=?, updated_at=?
            WHERE id=?
            """,
            (
                resolution,
                resolution,
                datetime.now().isoformat(timespec="seconds"),
                int(record_id),
            ),
        )
        conn.commit()
    return True


def list_month_end_positions(statement_month: str | None = None) -> pd.DataFrame:
    init_db()
    with closing(get_conn()) as conn:
        if statement_month is None:
            row = conn.execute("SELECT MAX(statement_month) FROM futures_account_monthly").fetchone()
            statement_month = row[0] if row else None
        if not statement_month:
            return pd.DataFrame()
        return pd.read_sql_query(
            """
            SELECT * FROM futures_month_end_positions
            WHERE statement_month=?
            ORDER BY asset_type, contract, side
            """,
            conn,
            params=(statement_month,),
        )


def list_futures_live_trades(*, include_taken_over: bool = True) -> pd.DataFrame:
    init_db()
    where = "" if include_taken_over else "WHERE NOT (source='手工' AND reconciliation_status='已接管')"
    with closing(get_conn()) as conn:
        return pd.read_sql_query(
            f"""
            SELECT * FROM futures_live_trades
            {where}
            ORDER BY trade_date DESC, COALESCE(trade_time, '') DESC, id DESC
            """,
            conn,
        )


def _effective_manual_trades(trades: pd.DataFrame, statement_end: str) -> pd.DataFrame:
    if trades.empty:
        return trades
    result = trades[
        trades["source"].eq("手工")
        & ~trades["reconciliation_status"].eq("已接管")
        & (pd.to_datetime(trades["trade_date"], errors="coerce") > pd.Timestamp(statement_end))
    ].copy()
    return result.sort_values(["trade_date", "trade_time", "id"], na_position="last")


def load_daily_closes(asset_type: str | None = None, contract: str | None = None) -> pd.DataFrame:
    init_db()
    clauses: list[str] = []
    params: list[object] = []
    if asset_type:
        clauses.append("asset_type=?")
        params.append(asset_type)
    if contract:
        clauses.append("contract=?")
        params.append(normalize_contract(contract, asset_type))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with closing(get_conn()) as conn:
        return pd.read_sql_query(
            f"""
            SELECT asset_type, contract, trade_date, close_price,
                   settlement_price, source, settlement_source, updated_at
            FROM futures_daily_closes {where}
            ORDER BY trade_date, asset_type, contract
            """,
            conn,
            params=tuple(params),
        )
