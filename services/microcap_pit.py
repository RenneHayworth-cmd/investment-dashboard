"""Pure PIT research calculations; no cache, fetch, observed writes or trading.

Inputs must be adjudicated evidence, not raw source adapters. Fact labels and
evidence references are a contract with the source-validation layer, not an
automatic certificate that the upstream information is correct.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import pandas as pd

SHANGHAI = ZoneInfo("Asia/Shanghai")
RULE_VERSION = "microcap_pit_v1"


def has_evidence(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("必须提供带时区的时间")
    return value.astimezone(SHANGHAI)


@dataclass(frozen=True)
class DecisionClock:
    selection_date: date
    information_available_at: datetime
    effective_date: date
    # Explicit validated calendar avoids guessing holidays outside our coverage.
    trading_sessions: tuple[date, ...]
    calendar_evidence: str
    market_data_asof: str = "EOD"

    def validate(self):
        available = aware(self.information_available_at)
        sessions = self.trading_sessions
        if not has_evidence(self.calendar_evidence) or sessions != tuple(sorted(set(sessions))):
            raise ValueError("需要已核验且严格升序的交易日历")
        if self.selection_date not in sessions or self.effective_date not in sessions:
            raise ValueError("决策日期不在已核验交易日历中")
        if self.effective_date <= self.selection_date or self.market_data_asof != "EOD":
            raise ValueError("正式EOD数据只能供后续交易日使用")
        close = datetime.combine(self.selection_date, time(15), SHANGHAI)
        recheck = datetime.combine(self.effective_date, time(9,25), SHANGHAI)
        if not close <= available <= recheck:
            raise ValueError("EOD数据可用时间必须在收盘后且不晚于资格复核")


@dataclass(frozen=True)
class UniverseEvidence:
    selection_date: date
    securities: frozenset[str]
    classification: str
    historical_roster_evidence: str
    independent_reconciliation_evidence: str


@dataclass(frozen=True)
class ShareInterval:
    security: str
    shares: Decimal
    valid_from: date
    valid_through: date
    information_available_at: datetime
    evidence: str
    coverage_evidence: str
    classification: str = "Unknown"


@dataclass(frozen=True)
class EligibilityEvent:
    security: str
    field: str
    value: bool
    effective_at: datetime
    information_available_at: datetime
    evidence: str
    classification: str = 'Unknown'


def effective_shares(security: str, day: date, available: datetime,
                     intervals: list[ShareInterval]) -> Decimal | None:
    """No unlimited carry forward; overlapping conflicting evidence is Unknown."""
    cutoff = aware(available)
    candidates = [row for row in intervals if row.security == security
                  and row.valid_from <= day <= row.valid_through
                  and aware(row.information_available_at) <= cutoff]
    if not candidates or any(row.classification != 'Fact' or not has_evidence(row.evidence)
                             or not has_evidence(row.coverage_evidence) for row in candidates):
        return None
    values = {row.shares for row in candidates}
    if len(values) != 1:
        return None
    value = next(iter(values))
    return value if value.is_finite() and value > 0 else None


def rank_day(rows: pd.DataFrame, clock: DecisionClock,
             universe: UniverseEvidence) -> tuple[pd.DataFrame, dict]:
    """Rank a fully resolved EOD input without losing excluded securities.

    Required per-row evidence refers to price, effective share interval and
    status coverage. Unknown status remains NA. Any unproved universe or row
    makes the entire result incomplete, even when the missing stock seems big.
    """
    clock.validate()
    columns = {'security','raw_unadjusted_close','effective_total_shares',
               'price_available_at','shares_available_at','status_available_at',
               'price_evidence','shares_evidence','status_evidence',
               'rule_pool_member','index_member','strategy_eligible','tradable',
               'classification','price_date','price_adjustment',
               'shares_valid_from','shares_valid_through','shares_coverage_evidence','status_asof'}
    if missing := columns - set(rows.columns):
        raise ValueError(f"缺少PIT字段：{sorted(missing)}")
    out = rows.copy(deep=True)
    if out['security'].duplicated().any():
        raise ValueError("同日证券重复，不能静默去重")
    for field in ['rule_pool_member','index_member','strategy_eligible','tradable']:
        # Strings such as 'False' must not become truthy booleans.
        if any(not pd.isna(v) and not isinstance(v, bool) for v in out[field]):
            raise ValueError(f"{field}必须为布尔值或Unknown")
        out[field] = pd.array(out[field], dtype='boolean')
    actual = set(out['security'])
    missing_securities = sorted(universe.securities - actual)
    unexpected = sorted(actual - universe.securities)
    universe_complete = (universe.selection_date == clock.selection_date
                         and universe.classification == 'Fact'
                         and has_evidence(universe.historical_roster_evidence)
                         and has_evidence(universe.independent_reconciliation_evidence)
                         and bool(universe.securities)
                         and not missing_securities and not unexpected)
    cutoff = aware(clock.information_available_at)
    selection_close = datetime.combine(clock.selection_date,time(15),SHANGHAI)
    caps, reasons = [], []
    for _, row in out.iterrows():
        problems = []
        if pd.isna(row['status_asof']) or aware(row['status_asof']) != selection_close:
            problems.append('status_unknown')
        if row['price_date'] != clock.selection_date or row['price_adjustment'] != 'none':
            problems.append('price_basis_or_date')
        if (pd.isna(row['shares_valid_from']) or pd.isna(row['shares_valid_through'])
                or not row['shares_valid_from'] <= clock.selection_date <= row['shares_valid_through']
                or not has_evidence(row['shares_coverage_evidence'])):
            problems.append('shares_coverage_unknown')
        for prefix in ['price','shares','status']:
            if not has_evidence(row[prefix+'_evidence']) or pd.isna(row[prefix+'_available_at']):
                problems.append(prefix+'_unknown')
            elif aware(row[prefix+'_available_at']) > cutoff:
                problems.append(prefix+'_future')
        if row['classification'] != 'Fact':
            problems.append('unverified')
        if any(pd.isna(row[f]) for f in ['rule_pool_member','strategy_eligible','tradable']):
            problems.append('eligibility_unknown')
        try:
            price = Decimal(str(row['raw_unadjusted_close']))
            shares = Decimal(str(row['effective_total_shares']))
            valid = price.is_finite() and shares.is_finite() and price > 0 and shares > 0
        except InvalidOperation:
            valid = False
        price_or_shares_unknown = any(p.startswith(('price_', 'shares_')) for p in problems)
        caps.append(price*shares if valid and not price_or_shares_unknown else None)
        if not valid:
            problems.append('invalid_market_cap')
        reasons.append('|'.join(problems))
    out['market_cap'] = caps
    out['quality_reason'] = reasons
    status_unknown = out['quality_reason'].str.contains('status_unknown|status_future')
    out.loc[status_unknown, ['rule_pool_member','strategy_eligible','tradable']] = pd.NA
    out = out.sort_values(['market_cap','security'],na_position='last').reset_index(drop=True)
    out['raw_market_cap_rank'] = pd.array([pd.NA]*len(out),dtype='Int64')
    valid_cap = out['market_cap'].notna()
    out.loc[valid_cap,'raw_market_cap_rank'] = range(1,int(valid_cap.sum())+1)
    eligible = (out['rule_pool_member'] & out['strategy_eligible'] & out['tradable']).fillna(False) & valid_cap
    out['strategy_rank'] = pd.array([pd.NA]*len(out),dtype='Int64')
    out.loc[eligible,'strategy_rank'] = range(1,int(eligible.sum())+1)
    complete = universe_complete and not any(reasons)
    out['dataset'] = 'microcap_rule_reconstructed'
    out['rule_version'] = RULE_VERSION
    out['selection_date'] = clock.selection_date
    out['market_data_asof'] = clock.market_data_asof
    out['information_available_at'] = clock.information_available_at
    out['effective_date'] = clock.effective_date
    out['quality'] = 'complete' if complete else 'incomplete'
    return out, {'quality':'complete' if complete else 'incomplete',
                 'trusted_top20':complete and int(eligible.sum()) >= 20,
                 'missing_eligible_securities':missing_securities,
                 'unexpected_securities':unexpected,
                 'universe_complete':universe_complete}


def recheck_next_session(ranking: pd.DataFrame, clock: DecisionClock,
                        events: list[EligibilityEvent],
                        coverage_evidence: dict[str, str], count: int = 20) -> tuple[pd.DataFrame, dict]:
    """Apply disclosed effective events at 09:25 to the unchanged raw rank queue.

    coverage_evidence must independently establish the complete event feed for
    each security through the recheck instant, including evidence of no event.
    Events after 09:25 have no effect on this result. No T+1 price is accepted.
    """
    clock.validate()
    if count < 1:
        raise ValueError('名单数量必须为正数')
    if ranking.empty or ranking['security'].duplicated().any():
        raise ValueError('需要非空且无重复证券的排名')
    if not (ranking['selection_date'].eq(clock.selection_date).all()
            and ranking['effective_date'].eq(clock.effective_date).all()
            and ranking['information_available_at'].eq(clock.information_available_at).all()):
        raise ValueError('排名与复核时间不一致')
    instant = datetime.combine(clock.effective_date,time(9,25),SHANGHAI)
    selection_close = datetime.combine(clock.selection_date,time(15),SHANGHAI)
    out = ranking.copy(deep=True).set_index('security',drop=False)
    out.index.name = '_security_key'
    out['preopen_strategy_eligible'] = out['strategy_eligible'].copy()
    out['preopen_tradable'] = out['tradable'].copy()
    problems = []
    if not out['quality'].eq('complete').all():
        problems.append('selection_incomplete')
    if any(not has_evidence(coverage_evidence.get(code)) for code in out.index):
        problems.append('event_coverage_incomplete')
    applicable = []
    for event in events:
        # Future events are ignored before any validation of their values.
        if aware(event.information_available_at) > instant or aware(event.effective_at) > instant:
            continue
        if aware(event.effective_at) <= selection_close:
            # A late announcement of an earlier event is applicable now; an
            # already-known event belongs in the selection status baseline.
            if aware(event.information_available_at) <= clock.information_available_at:
                continue
        if event.security not in out.index:
            problems.append('event_outside_universe')
            continue
        if (event.field not in {'strategy_eligible','tradable'} or not isinstance(event.value,bool)
                or event.classification != 'Fact' or not has_evidence(event.evidence)):
            problems.append('event_unknown')
            continue
        applicable.append(event)
    grouped = {}
    for event in applicable:
        grouped.setdefault((event.security,event.field),[]).append(event)
    for (code,field), changes in grouped.items():
        changes.sort(key=lambda e:(aware(e.effective_at),aware(e.information_available_at)))
        latest = changes[-1]
        tied = [e for e in changes if e.effective_at == latest.effective_at
                and e.information_available_at == latest.information_available_at]
        if len({e.value for e in tied}) != 1:
            problems.append('conflicting_events')
            continue
        out.at[code,'preopen_'+field] = latest.value
    queue = out.sort_values(['raw_market_cap_rank','security'],na_position='last')
    eligible = (queue['rule_pool_member'] & queue['preopen_strategy_eligible']
                & queue['preopen_tradable']).fillna(False) & queue['raw_market_cap_rank'].notna()
    for field in ['preopen_strategy_eligible','preopen_tradable']:
        if queue[field].isna().any():problems.append('preopen_status_unknown')
    queue['preopen_rank'] = pd.array([pd.NA]*len(queue),dtype='Int64')
    queue.loc[eligible,'preopen_rank'] = range(1,int(eligible.sum())+1)
    queue['recheck_information_available_at'] = instant
    queue['selected_for_next_session'] = False
    if not problems:
        queue.loc[eligible,'selected_for_next_session'] = queue.loc[eligible,'preopen_rank'].le(count)
    return queue.reset_index(drop=True), {'quality':'incomplete' if problems else 'complete',
                                         'blockers':sorted(set(problems)),
                                         'unfilled_slots':count if problems else max(0,count-int(eligible.sum()))}
