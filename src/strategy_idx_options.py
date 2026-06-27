"""Build and backtest the OI-momentum strategy on NIFTY/BANKNIFTY index options.

Workflow:
  1. Fetch NIFTY (sec 13) and NIFTY Jun-26 future (62329, with OI) — signal substrate.
  2. (Optional) Same for BANKNIFTY (25, 62326).
  3. Compute v4c signals on the futures (using the same Params/generate_signals).
  4. For each signal, identify front-week expiry with >= 1 DTE and ATM strike.
  5. Pre-fetch ATM ± 5 strikes for the needed (expiry, type) combos.
  6. Run the options backtester with:
       - BE @ 0.4% favorable + trail @ 0.3% behind (v4c rules)
       - Spot-based SL/TGT (0.4% / 0.8%)
       - Equity-based 25% sizing per trade
"""
from __future__ import annotations

import os, sys, json
from datetime import datetime, timedelta, date
from dataclasses import asdict
import pandas as pd, numpy as np
from tqdm import tqdm

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from src.dhan_client import intraday_history
from src.strategy import Params, add_features, generate_signals, _parse_hhmm
from src.strategy_options import OptParams, OptionUniverse, OptTrade, _pick_executable_strike, summarize_opt
from src.option_costs import option_net_pnl

INDEX_FUT = {
    "NIFTY":     {"spot_sid": 13, "fut_sid": 62329, "exchange": "NSE_FNO", "instrument": "FUTIDX"},
    "BANKNIFTY": {"spot_sid": 25, "fut_sid": 62326, "exchange": "NSE_FNO", "instrument": "FUTIDX"},
}

FROM_STR = "2026-03-30 09:15:00"
TO_STR   = "2026-06-26 15:30:00"

DATA_RAW = os.path.join(ROOT, "data", "raw")
DATA_OPT = os.path.join(ROOT, "data", "opt_idx")
RESULTS  = os.path.join(ROOT, "results")
os.makedirs(DATA_RAW, exist_ok=True); os.makedirs(DATA_OPT, exist_ok=True)
os.makedirs(RESULTS, exist_ok=True)


def _to_df(raw: dict) -> pd.DataFrame:
    if not raw or not raw.get("timestamp"):
        return pd.DataFrame()
    df = pd.DataFrame({
        "ts": pd.to_datetime(raw["timestamp"], unit="s") + pd.Timedelta(hours=5, minutes=30),
        "open": raw["open"], "high": raw["high"], "low": raw["low"],
        "close": raw["close"], "volume": raw["volume"],
    })
    if "open_interest" in raw and raw["open_interest"]:
        df["oi"] = raw["open_interest"]
    return df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)


def fetch_index_signal(idx: str) -> pd.DataFrame:
    """Fetch NIFTY spot + future, merge into the same format as load_pair."""
    info = INDEX_FUT[idx]
    spot_path = os.path.join(DATA_RAW, f"{idx}_spot.parquet")
    fut_path  = os.path.join(DATA_RAW, f"{idx}_fut.parquet")

    if not os.path.exists(spot_path):
        raw = intraday_history(security_id=info["spot_sid"], exchange_segment="IDX_I",
                               instrument="INDEX", interval="5",
                               from_date=FROM_STR, to_date=TO_STR, oi=False)
        _to_df(raw).to_parquet(spot_path, index=False)
    if not os.path.exists(fut_path):
        raw = intraday_history(security_id=info["fut_sid"], exchange_segment=info["exchange"],
                               instrument=info["instrument"], interval="5",
                               from_date=FROM_STR, to_date=TO_STR, oi=True)
        _to_df(raw).to_parquet(fut_path, index=False)

    spot = pd.read_parquet(spot_path).rename(columns={
        "open":"s_open","high":"s_high","low":"s_low","close":"s_close","volume":"s_vol"})
    fut = pd.read_parquet(fut_path).rename(columns={
        "open":"f_open","high":"f_high","low":"f_low","close":"f_close","volume":"f_vol"})
    df = pd.merge(spot, fut, on="ts", how="inner").sort_values("ts").reset_index(drop=True)
    df["date"] = df["ts"].dt.date
    df["tod"] = df["ts"].dt.time
    return df


def build_index_option_universe(idx: str, master: pd.DataFrame):
    """Returns (chain_dict, expiries_list_sorted) keyed by (expiry, opt_type)."""
    opts = master[(master["EXCH_ID"]=="NSE") & (master["INSTRUMENT"]=="OPTIDX")
                  & (master["UNDERLYING_SYMBOL"]==idx)].copy()
    opts["SM_EXPIRY_DATE"] = pd.to_datetime(opts["SM_EXPIRY_DATE"], errors="coerce")
    opts = opts.dropna(subset=["SM_EXPIRY_DATE","STRIKE_PRICE","OPTION_TYPE","SECURITY_ID","LOT_SIZE"])
    opts["STRIKE_PRICE"] = opts["STRIKE_PRICE"].astype(float)
    opts["SECURITY_ID"] = opts["SECURITY_ID"].astype(int)
    opts["LOT_SIZE"] = opts["LOT_SIZE"].astype(int)
    chain = {}
    for (exp, ot), grp in opts.groupby(["SM_EXPIRY_DATE","OPTION_TYPE"]):
        chain[(exp.date(), ot)] = grp.sort_values("STRIKE_PRICE")[
            ["STRIKE_PRICE","SECURITY_ID","LOT_SIZE"]].reset_index(drop=True)
    expiries = sorted(opts["SM_EXPIRY_DATE"].dt.date.unique())
    return chain, expiries


def pick_expiry_weekly(expiries: list, on_date: date, min_dte: int = 1) -> date | None:
    """For NIFTY: pick front weekly with >= min_dte. For BANKNIFTY (monthly only): same logic."""
    for e in expiries:
        if (e - on_date).days >= min_dte:
            return e
    return expiries[-1] if expiries else None


def plan_and_fetch_options(idx: str, df_sig: pd.DataFrame, params: OptParams,
                            chain: dict, expiries: list) -> dict:
    """Compute signals, identify (expiry, strike, type) needed, fetch all unique."""
    df_sig = add_features(df_sig, params)
    df_sig["sig"] = generate_signals(df_sig, params)
    sigs = df_sig[df_sig["sig"]!=0].copy()
    sigs["opt_type"] = np.where(sigs["sig"]==1, "CE", "PE")
    print(f"{idx}: {len(sigs)} raw signals")
    if sigs.empty:
        return {}, sigs

    # Collect needed security_ids (ATM ± 3 neighbors per signal)
    NEIGHBORS = 3
    sec_ids_needed = set()
    sig_picks = []
    for _, r in sigs.iterrows():
        d = r["ts"].date()
        exp = pick_expiry_weekly(expiries, d, min_dte=params.min_dte)
        if exp is None:
            continue
        spot = float(r["s_close"])
        ch = chain.get((exp, r["opt_type"]))
        if ch is None or ch.empty:
            continue
        idx_nearest = (ch["STRIKE_PRICE"] - spot).abs().idxmin()
        lo = max(0, idx_nearest - NEIGHBORS)
        hi = min(len(ch), idx_nearest + NEIGHBORS + 1)
        for k in range(lo, hi):
            sec_ids_needed.add(int(ch.iloc[k]["SECURITY_ID"]))
        sig_picks.append({"sig_ts": r["ts"], "expiry": exp, "opt_type": r["opt_type"], "spot": spot})

    print(f"{idx}: pre-fetching {len(sec_ids_needed)} unique option contracts...")
    cache: dict = {}
    for sid in tqdm(sorted(sec_ids_needed), desc=f"{idx} opts"):
        path = os.path.join(DATA_OPT, f"{idx}_opt_{sid}.parquet")
        if os.path.exists(path):
            try:
                cache[sid] = pd.read_parquet(path)
                continue
            except Exception:
                pass
        try:
            raw = intraday_history(security_id=sid, exchange_segment="NSE_FNO",
                                   instrument="OPTIDX", interval="5",
                                   from_date=FROM_STR, to_date=TO_STR, oi=False)
        except Exception as e:
            cache[sid] = pd.DataFrame()
            continue
        if not raw or not raw.get("timestamp"):
            cache[sid] = pd.DataFrame()
            continue
        d = pd.DataFrame({
            "ts": pd.to_datetime(raw["timestamp"], unit="s") + pd.Timedelta(hours=5, minutes=30),
            "o_open": raw["open"], "o_high": raw["high"],
            "o_low": raw["low"], "o_close": raw["close"], "o_vol": raw["volume"],
        }).drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
        d.to_parquet(path, index=False)
        cache[sid] = d
    return cache, df_sig


def backtest_index(idx: str, df_sig: pd.DataFrame, params: OptParams,
                    chain: dict, expiries: list, opt_cache: dict) -> list[OptTrade]:
    """Adapted from backtest_symbol_options but using index-specific chain."""
    df = df_sig.copy()
    df["sig"] = generate_signals(df, params)
    square_off = _parse_hhmm(params.square_off)
    trades: list[OptTrade] = []
    i, n = 0, len(df)
    last_exit_bar = -10**9
    day_pnl = {}
    while i < n - 1:
        row = df.iloc[i]
        today = row["date"]
        if params.daily_loss_stop > 0 and day_pnl.get(today,0) <= -params.daily_loss_stop:
            i+=1; continue
        if (row["sig"] != 0 and i+1 < n and df.iloc[i+1]["date"] == today
            and (i - last_exit_bar) > params.cool_off_bars):
            side = "long" if row["sig"]==1 else "short"
            opt_type = "CE" if side=="long" else "PE"
            exp = pick_expiry_weekly(expiries, today, params.min_dte)
            if exp is None: i+=1; continue
            entry_bar = df.iloc[i+1]; entry_ts = entry_bar["ts"]
            # Pick executable strike using chain
            ch = chain.get((exp, opt_type))
            if ch is None or ch.empty: i+=1; continue
            order = (ch["STRIKE_PRICE"] - float(row["s_close"])).abs().sort_values().index
            picked = None
            for ix in order:
                rch = ch.loc[ix]
                sid = int(rch["SECURITY_ID"])
                opt_df = opt_cache.get(sid)
                if opt_df is None or opt_df.empty: continue
                hit = opt_df.loc[opt_df["ts"]==entry_ts]
                if hit.empty: continue
                r0 = hit.iloc[0]
                if not np.isfinite(r0["o_open"]) or r0["o_open"]<=0: continue
                picked = {"strike":float(rch["STRIKE_PRICE"]), "sid":sid,
                          "lot_size":int(rch["LOT_SIZE"]), "opt_df":opt_df, "opt_row":r0}
                break
            if picked is None: i+=1; continue
            premium_entry = float(picked["opt_row"]["o_open"])
            spot_entry_for_signal = float(entry_bar["s_open"])
            qty = picked["lot_size"] * max(1, params.fut_lots)
            if side=="long":
                spot_sl = spot_entry_for_signal * (1 - params.sl_pct)
                spot_tgt = spot_entry_for_signal * (1 + params.tgt_pct)
            else:
                spot_sl = spot_entry_for_signal * (1 + params.sl_pct)
                spot_tgt = spot_entry_for_signal * (1 - params.tgt_pct)
            prem_sl = premium_entry * (1 - params.premium_sl_pct)
            prem_tgt = premium_entry * (1 + params.premium_tgt_pct)
            exit_reason=None; exit_ts=entry_ts; premium_exit=premium_entry; spot_exit=spot_entry_for_signal
            bars_held=0; spot_high=spot_entry_for_signal; spot_low=spot_entry_for_signal
            j = i+1; opt_df = picked["opt_df"]
            while j < n and df.iloc[j]["date"] == today:
                br = df.iloc[j]; bars_held += 1
                hit = opt_df.loc[opt_df["ts"]==br["ts"]]
                if hit.empty:
                    if br["tod"] >= square_off:
                        spot_exit = float(br["s_close"]); exit_reason="TIME"; exit_ts=br["ts"]; break
                    j+=1; continue
                opt_row = hit.iloc[0]
                bars_since = j-(i+1)
                # break-even / trailing
                if side=="long":
                    spot_high = max(spot_high, float(br["s_high"]))
                    fav_pct = (spot_high - spot_entry_for_signal)/spot_entry_for_signal
                    if params.breakeven_trigger_pct>0 and fav_pct >= params.breakeven_trigger_pct:
                        spot_sl = max(spot_sl, spot_entry_for_signal)
                    if params.trail_stop_pct>0 and fav_pct >= params.breakeven_trigger_pct:
                        spot_sl = max(spot_sl, spot_high * (1 - params.trail_stop_pct))
                else:
                    spot_low = min(spot_low, float(br["s_low"]))
                    fav_pct = (spot_entry_for_signal - spot_low)/spot_entry_for_signal
                    if params.breakeven_trigger_pct>0 and fav_pct >= params.breakeven_trigger_pct:
                        spot_sl = min(spot_sl, spot_entry_for_signal)
                    if params.trail_stop_pct>0 and fav_pct >= params.breakeven_trigger_pct:
                        spot_sl = min(spot_sl, spot_low * (1 + params.trail_stop_pct))
                s_hi, s_lo = br["s_high"], br["s_low"]
                if side=="long":
                    if s_lo <= spot_sl:
                        premium_exit = float(opt_row["o_close"]); spot_exit = spot_sl
                        exit_reason="SL"; exit_ts=br["ts"]; break
                    if s_hi >= spot_tgt:
                        premium_exit = float(opt_row["o_close"]); spot_exit = spot_tgt
                        exit_reason="TGT"; exit_ts=br["ts"]; break
                else:
                    if s_hi >= spot_sl:
                        premium_exit = float(opt_row["o_close"]); spot_exit = spot_sl
                        exit_reason="SL"; exit_ts=br["ts"]; break
                    if s_lo <= spot_tgt:
                        premium_exit = float(opt_row["o_close"]); spot_exit = spot_tgt
                        exit_reason="TGT"; exit_ts=br["ts"]; break
                if params.use_oi_flip_exit and j > i+1:
                    if side=="long" and br["f_oi_chg"] < -params.oi_pct and br["f_price_chg"] < 0:
                        premium_exit = float(opt_row["o_close"]); spot_exit = float(br["s_close"])
                        exit_reason="OI_FLIP"; exit_ts=br["ts"]; break
                    if side=="short" and br["f_oi_chg"] < -params.oi_pct and br["f_price_chg"] > 0:
                        premium_exit = float(opt_row["o_close"]); spot_exit = float(br["s_close"])
                        exit_reason="OI_FLIP"; exit_ts=br["ts"]; break
                if br["tod"] >= square_off:
                    premium_exit = float(opt_row["o_close"]); spot_exit = float(br["s_close"])
                    exit_reason="TIME"; exit_ts=br["ts"]; break
                j+=1
            else:
                if bars_held>0:
                    br = df.iloc[j-1]
                    hit = opt_df.loc[opt_df["ts"]==br["ts"]]
                    if not hit.empty:
                        premium_exit = float(hit.iloc[0]["o_close"]); spot_exit = float(br["s_close"])
                        exit_reason="EOD"; exit_ts=br["ts"]
            if exit_reason is None: i+=1; continue
            net,gross,cost = option_net_pnl(premium_entry, premium_exit, qty)
            trades.append(OptTrade(symbol=idx, side=side, opt_type=opt_type, expiry=exp,
                strike=picked["strike"], entry_ts=entry_ts, exit_ts=exit_ts,
                spot_entry=spot_entry_for_signal, spot_exit=spot_exit,
                premium_entry=premium_entry, premium_exit=premium_exit,
                qty=qty, exit_reason=exit_reason,
                gross_pnl=gross, cost=cost, net_pnl=net, bars_held=bars_held))
            day_pnl[today] = day_pnl.get(today,0) + net
            last_exit_bar = j; i = j+1
        else:
            i+=1
    return trades


if __name__ == "__main__":
    master = pd.read_csv("data/scrip-master.csv", low_memory=False)

    # Use v4c parameters
    params = OptParams(
        trade_segment="OPT", price_pct=0.0025, oi_pct=0.003, vol_z=2.0,
        sl_pct=0.004, tgt_pct=0.008,
        require_trend_align=True, avoid_lunch=True, use_oi_flip_exit=True,
        min_dte=1,  # NIFTY weeklies — much shorter expiry, so 1 DTE is OK
        breakeven_trigger_pct=0.004, trail_stop_pct=0.003,
        fut_lots=1,  # 1 lot of NIFTY option = 65 qty (much smaller than stock futures)
    )

    all_trades = []
    for idx in ["NIFTY", "BANKNIFTY"]:
        print(f"\n{'='*60}\nProcessing {idx}\n{'='*60}")
        df = fetch_index_signal(idx)
        print(f"{idx}: signal data {df['ts'].min()} -> {df['ts'].max()}, {len(df)} candles")
        chain, expiries = build_index_option_universe(idx, master)
        print(f"{idx}: {len(chain)} (expiry,type) groups, {len(expiries)} expiries")
        opt_cache, df_with_feats = plan_and_fetch_options(idx, df, params, chain, expiries)
        t = backtest_index(idx, df_with_feats, params, chain, expiries, opt_cache)
        print(f"{idx}: {len(t)} trades, net Rs {sum(x.net_pnl for x in t):,.0f}")
        all_trades.extend(t)

    # Save consolidated
    if all_trades:
        df_t = pd.DataFrame([asdict(t) for t in all_trades])
        df_t.to_csv(os.path.join(RESULTS, "trades_opt_idx_v4c.csv"), index=False)
        summary = summarize_opt(all_trades)
        summary["tag"] = "opt_idx_v4c"
        summary["params"] = asdict(params)
        with open(os.path.join(RESULTS, "summary_opt_idx_v4c.json"), "w") as f:
            json.dump(summary, f, indent=2, default=str)
        print(f"\n{'='*60}\nFINAL: {summary['n_trades']} trades  net Rs {summary['total_net_pnl']:,.0f}  "
              f"WR {summary['win_rate']*100:.1f}%  Sharpe {summary['sharpe_daily_annualized']:.2f}  "
              f"MDD Rs {summary['max_drawdown_rs']:,.0f}")
        print(f"exit mix: {summary['exit_reason_counts']}")
