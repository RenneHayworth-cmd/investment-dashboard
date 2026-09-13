"""Independent validation gates and explicit alignment-aware diagnostics.

No thresholds are fitted to observed membership or strategy performance.
Evidence statuses must come from reviewed, scope-matched source validation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from services.microcap_pit import has_evidence


def _ranking(frame: pd.DataFrame) -> pd.DataFrame:
    required = {'security', 'rank', 'market_cap'}
    if required - set(frame.columns):
        raise ValueError('对照需要security、rank、market_cap')
    result = frame[list(sorted(required))].copy()
    if result['security'].isna().any() or result['security'].duplicated().any():
        raise ValueError('对照证券缺失或重复')
    ranks = pd.to_numeric(result['rank'], errors='coerce')
    if (ranks.isna().any() or not np.isfinite(ranks).all() or ranks.le(0).any()
            or ranks.mod(1).ne(0).any() or ranks.duplicated().any()):
        raise ValueError('对照排名必须为唯一正整数')
    result['rank'] = ranks.astype(int)
    caps = pd.to_numeric(result['market_cap'], errors='coerce')
    if caps.isna().any() or not np.isfinite(caps).all() or caps.le(0).any():
        raise ValueError('缺失或无效市值不能进入正式对照')
    result['market_cap'] = caps
    return result.sort_values('rank').set_index('security')


def _require_date(frame: pd.DataFrame, column: str, expected: date):
    if column not in frame or frame.empty or not frame[column].eq(expected).all():
        raise ValueError(f'对照数据{column}与已验证日期不一致')


def _top(frame: pd.DataFrame, n: int) -> set | None:
    sub = frame[frame['rank'].le(n)]
    # Missing ranks are not treated as a smaller but complete TopN list.
    return set(sub.index) if set(sub['rank']) == set(range(1,n+1)) else None


def _overlap(left: pd.DataFrame, right: pd.DataFrame, n: int) -> dict:
    a, b = _top(left,n), _top(right,n)
    if a is None or b is None:
        return {'status':'Unknown', 'intersection_count':None, 'overlap_rate':None}
    return {'status':'Fact', 'intersection_count':len(a & b), 'overlap_rate':len(a & b)/n}


@dataclass(frozen=True)
class AlignmentDecision:
    observed_date: date
    reconstructed_date: date
    lag_sessions: int
    classification: str
    evidence: str
    # Must include both tested dates, not a best-match score in isolation.
    tested_dates: frozenset[date]


def alignment_test(observed: dict[date,pd.DataFrame], reconstructed: dict[date,pd.DataFrame],
                   sessions: tuple[date,...]) -> list[dict]:
    if sessions != tuple(sorted(set(sessions))):
        raise ValueError('交易日历必须严格升序')
    results = []
    for day, frame in sorted(observed.items()):
        if day not in sessions:
            raise ValueError('快照日期不在验证交易日历')
        _require_date(frame,'snapshot_date',day)
        left = _ranking(frame)
        pos = sessions.index(day)
        for lag in (0,1):
            target = sessions[pos-lag] if pos >= lag else None
            right = reconstructed.get(target)
            if right is not None:
                _require_date(right,'selection_date',target)
            comparison = ({'status':'Unknown','intersection_count':None,'overlap_rate':None}
                          if right is None else _overlap(left,_ranking(right),20))
            results.append({'observed_date':day, 'reconstructed_date':target,
                            'lag_sessions':lag,'purpose':'alignment_diagnostic_only',
                            'alignment_classification':'Unknown', **comparison})
    return results


def formal_comparison(observed: pd.DataFrame, reconstructed: pd.DataFrame,
                      alignment: AlignmentDecision, sessions: tuple[date,...], *,
                      observed_provenance_evidence: str, same_valuation_time_evidence: str,
                      missing_eligible_securities: list[str],
                      uncertain_boundary_securities: list[str]) -> dict:
    if sessions != tuple(sorted(set(sessions))) or alignment.observed_date not in sessions:
        raise ValueError('需要严格升序且覆盖对照日期的交易日历')
    index = sessions.index(alignment.observed_date)
    if index == 0 or alignment.lag_sessions not in (0,1):
        raise ValueError('需要T及上一交易日供对齐测试')
    tested = {sessions[index], sessions[index-1]}
    if (alignment.classification != 'Fact' or not has_evidence(alignment.evidence)
            or not tested.issubset(alignment.tested_dates)
            or alignment.reconstructed_date != sessions[index-alignment.lag_sessions]
            or not has_evidence(observed_provenance_evidence)):
        raise ValueError('来源或effective-date alignment未验证，禁止正式重合率')
    _require_date(observed,'snapshot_date',alignment.observed_date)
    _require_date(reconstructed,'selection_date',alignment.reconstructed_date)
    if 'quality' not in reconstructed or not reconstructed['quality'].eq('complete').all():
        raise ValueError('incomplete研究池不得进入正式重合率')
    left, right = _ranking(observed), _ranking(reconstructed)
    overlaps = {f'top{n}_overlap':_overlap(left,right,n) for n in (20,50,200,400)}
    a,b = _top(left,20),_top(right,20)
    shared = sorted(a & b) if a is not None and b is not None else []
    rank_mae = (float((left.loc[shared,'rank']-right.loc[shared,'rank']).abs().mean())
                if shared else None)
    cap_mape = (float(((left.loc[shared,'market_cap']-right.loc[shared,'market_cap']).abs()
                       /left.loc[shared,'market_cap']).mean()*100)
                if shared and has_evidence(same_valuation_time_evidence) else None)
    boundary_codes = sorted(set(left[left['rank'].between(15,25)].index)
                            | set(right[right['rank'].between(15,25)].index))
    boundary = left.add_prefix('observed_').join(right.add_prefix('reconstructed_'),how='outer')
    boundary = boundary.reindex(boundary_codes).reset_index()
    boundary['uncertain'] = boundary['security'].isin(uncertain_boundary_securities)
    return {
        'observed_date':alignment.observed_date,
        'reconstructed_date':alignment.reconstructed_date,
        'alignment_evidence':alignment.evidence,
        **overlaps,
        'top20_exact_match':a == b if a is not None and b is not None else None,
        'top20_jaccard':len(a & b)/len(a | b) if a is not None and b is not None else None,
        'top20_rank_mae':rank_mae, 'top20_rank_mae_sample_count':len(shared),
        'top20_market_cap_mape_pct':cap_mape,
        'market_cap_time_aligned':has_evidence(same_valuation_time_evidence),
        'rank_15_25_boundary':boundary.to_dict('records'),
        'missing_eligible_securities':sorted(set(missing_eligible_securities)),
        'uncertain_boundary_securities':sorted(set(uncertain_boundary_securities)),
    }


GATE_1A_REQUIREMENTS = frozenset({
    'pit_universe_complete','raw_price_basis','effective_shares',
    'st_suspension_listing_delisting_boundaries','no_future_dependency',
    'share_change_sample_validation',
})
GATE_1B_REQUIREMENTS = frozenset({
    'observed_provenance','effective_date_alignment','top20_replication',
    'major_differences_attributed',
})


@dataclass(frozen=True)
class EvidenceCheck:
    status: str  # passed / failed / Unknown
    evidence: str
    validation_dates: frozenset[date]


def evaluate_gate(checks: dict[str,EvidenceCheck], required: frozenset[str],
                  validation_dates: frozenset[date]) -> dict:
    failed, unknown = [], []
    for name in sorted(required):
        check = checks.get(name)
        if check and check.status == 'failed':
            failed.append(name)
        elif (check is None or check.status != 'passed' or not has_evidence(check.evidence)
              or not validation_dates or not validation_dates.issubset(check.validation_dates)):
            unknown.append(name)
    return {'status':'failed' if failed else ('Unknown' if unknown else 'passed'),
            'failed_checks':failed, 'unknown_checks':unknown}


def evaluate_gates(checks_1a: dict[str,EvidenceCheck], checks_1b: dict[str,EvidenceCheck],
                   validation_dates: frozenset[date], validated_share_securities: frozenset[str]) -> dict:
    a = dict(checks_1a)
    if len(validated_share_securities) < 20:
        a.pop('share_change_sample_validation',None)
    gate_a = evaluate_gate(a,GATE_1A_REQUIREMENTS,validation_dates)
    gate_b = evaluate_gate(checks_1b,GATE_1B_REQUIREMENTS,validation_dates)
    return {'gate_1a':gate_a, 'gate_1b':gate_b,
            'historical_expansion_allowed':gate_a['status'] == 'passed',
            'validated_share_security_count':len(validated_share_securities)}
