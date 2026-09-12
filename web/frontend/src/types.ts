export type Value = string | number | null | boolean | Value[]
export type Row = Record<string, Value>
export interface Instrument { code: string; name: string; category: string; status: string; source: string; latest_date: string; cache_time: string; metrics: Row; error: string; formal_history_valid: boolean }
export interface Dashboard {
 schema_version: number;
 generated_at: string; expected_formal_date: string; session: string; formal_dates: Record<string,string>; formal_updated_at: string; quote_time: string; preview_codes: string[]; missing_formal_codes: string[]; missing_quote_codes: string[]; missing_index_codes: string[];
 items: Instrument[]; etf_formal: Row[]; etf_preview: Row[]; index_formal: Row[]; index_preview: Row[]; guidance: Row[];
 strategy_parameters: Row;
 strategy_live: { mode: 'intraday' | 'pending_close' | 'formal' | 'unavailable'; available: boolean; complete: boolean; valuation_date: string; formal_date: string; quote_time: string; missing_codes: string[]; daily_pnl: number | null; estimated_assets: number | null; daily_return_pct?: number | null; warnings: string[]; by_symbol: Row[] };
 strategy: { summary: Row; daily: Row[]; positions: Row[]; trades: Row[]; components: Row[]; warnings: string[]; errors: string[]; daily_by_symbol: Row[] };
 trade_preview: { actions: Row[]; formal_date: string; preview_date: string; quote_time: string; errors: string[]; warnings: string[] };
 derivatives: Instrument[]; spreads: Instrument[]; refreshing: boolean; refresh_error: string; refresh_stage: string; last_refresh_at: string;
}
