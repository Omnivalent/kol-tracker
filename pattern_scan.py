#!/usr/bin/env python3
"""
Find the "deployer hands out tokens / bundled launch / dump" pattern across coins.

  python3 pattern_scan.py group  --wallets FILE --out DIR [--txs 300]
      Reads the recent own txs of a group of wallets (e.g. a deployer and the wallets it gave tokens to),
      finds the coins two or more of them traded, and checks every such coin for:
        - launch-block buyers: wallets that bought in the same block as the create tx (bundle)
        - deployer handouts: tokens sent by the deployer straight to other wallets
        - each group wallet's profit on the coin (average cost, from its token-account history)

  python3 pattern_scan.py saved  --watchlist private/user_wallets_raw.tsv --out DIR [--txs 60]
      For every saved wallet: tokens it received from a coin's creator (pump.fun creator field),
      and whether it then sold them. Results cached per wallet (resumable).

Full addresses and signatures everywhere. Free RPC: slow, resumable.
"""
import argparse
import csv
import json
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import solana_tracer as T  # noqa: E402

STABLE = {T.WSOL, T.USDC, "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}


def own_mint_deltas(tx, w):
    """mint -> change of w's balance in this tx (non-stable tokens only)."""
    meta = tx.get("meta") or {}
    d = defaultdict(float)
    for key, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for b in meta.get(key) or []:
            if b.get("owner") == w and b.get("mint") not in STABLE:
                d[b["mint"]] += sign * float(b["uiTokenAmount"].get("uiAmountString") or 0)
    return {m: v for m, v in d.items() if abs(v) > 1e-9}


def wallet_coins(w, n):
    """Coins in w's last n own txs: mint -> list of events."""
    sigs, before, kept = [], None, 0
    while len(sigs) < n:
        opts = {"limit": 1000}
        if before:
            opts["before"] = before
        page = T.rpc("getSignaturesForAddress", [w, opts])
        sigs += [s for s in page if not s.get("err")]
        if len(page) < 1000:
            break
        before = page[-1]["signature"]
    sigs = sigs[:n]
    coins = defaultdict(list)
    for s, tx in zip(sigs, T.pmap(lambda s: T.get_tx(s["signature"]), sigs)):
        if not tx:
            continue
        for m, tok in own_mint_deltas(tx, w).items():
            sol = T.lamport_delta(tx, w) + T.token_deltas(tx, T.WSOL).get(w, 0.0)
            td = T.token_deltas(tx, m)
            cp = [o for o, v in td.items() if o != w and v * tok < 0 and T.on_curve(o)]
            if tok > 0:
                kind = "BUY" if sol < -0.003 else "XFER_IN"
            else:
                kind = "SELL" if sol > 0.003 else "XFER_OUT"
            coins[m].append({"kind": kind, "tokens": abs(tok), "sol": sol, "sig": s["signature"],
                             "time": tx.get("blockTime"), "counterparty": cp[0] if kind.startswith("XFER") and cp else None})
    return {"coins": dict(coins), "txs_read": len(sigs),
            "oldest": min((s.get("blockTime") or 0) for s in sigs) if sigs else None}


def launch_info(mint, pages):
    """Deployer and launch-block buyers of a pump.fun coin (from its bonding-curve history)."""
    pump = T.pump_curve(mint)
    if not pump:
        return {"pump": False}
    sigs, reached = T.all_sigs(pump["curve"], pages)
    out = {"pump": True, "curve": pump["curve"], "graduated": pump["graduated"], "creator": pump["creator"],
           "launch_reached": reached}
    if not reached or not sigs:
        return out
    ok = [s for s in sigs if not s.get("err")][::-1]
    first = ok[0]
    create = T.get_tx(first["signature"])
    if not create:
        return out
    out["create_sig"], out["launch_time"], out["launch_slot"] = first["signature"], create.get("blockTime"), create["slot"]
    out["deployer"] = T.signers_of(create)[0]
    early = [s for s in ok if s.get("slot", 0) <= create["slot"] + 1][:80]
    buyers = []
    for s, tx in zip(early, T.pmap(lambda s: T.get_tx(s["signature"]), early)):
        if not tx:
            continue
        sg = set(T.signers_of(tx))
        for o, v in T.token_deltas(tx, mint).items():
            if v > 0 and o in sg and T.on_curve(o):
                buyers.append({"wallet": o, "tokens": v, "slot": tx["slot"], "sig": s["signature"],
                               "same_block": tx["slot"] == create["slot"],
                               "sol": -(T.lamport_delta(tx, o) + T.token_deltas(tx, T.WSOL).get(o, 0.0))})
    out["launch_buyers"] = buyers
    return out


def coin_trades(w, mint):
    acc = T.rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}])["value"]
    trades, done, _ = T.wallet_trades(w, mint, acc["owner"])
    st = T.realized(trades)
    st.update({"trades": trades, "complete": done})
    return st


def group_mode(a):
    lines = [l.split()[0] for l in open(a.wallets) if l.strip() and not l.startswith("#")]
    group = list(dict.fromkeys(lines))
    os.makedirs(os.path.join(a.out, "wallets"), exist_ok=True)
    data = {}
    for i, w in enumerate(group, 1):
        p = os.path.join(a.out, "wallets", w + ".json")
        if os.path.exists(p):
            data[w] = json.load(open(p))
            continue
        T.log(f"[{i}/{len(group)}] coins of {w}")
        try:
            data[w] = wallet_coins(w, a.txs)
        except Exception as e:
            T.log(f"  ! {type(e).__name__}: {e}")
            continue
        json.dump(data[w], open(p, "w"))
    by_coin = defaultdict(set)
    for w, d in data.items():
        for m in d["coins"]:
            by_coin[m].add(w)
    shared = sorted((m for m, ws in by_coin.items() if len(ws) >= 2), key=lambda m: -len(by_coin[m]))[: a.max_coins]
    T.log(f"{len(shared)} coins traded by 2+ group wallets")
    coins = {}
    cpath = os.path.join(a.out, "coins.json")
    if os.path.exists(cpath):
        coins = json.load(open(cpath))
    for i, m in enumerate(shared, 1):
        if m in coins:
            continue
        T.log(f"[coin {i}/{len(shared)}] {m} ({len(by_coin[m])} group wallets)")
        try:
            info = launch_info(m, a.pages)
            dep = info.get("deployer") or info.get("creator")
            info["deployer_handouts"] = []
            if dep:
                try:
                    dt = coin_trades(dep, m)
                    info["deployer_handouts"] = [t for t in dt["trades"] if t["kind"] == "XFER_OUT"]
                    info["deployer_pnl"] = {k: dt[k] for k in ("realized_sol", "spent_sol", "received_sol", "held_tokens")}
                except Exception as e:
                    T.log(f"  ! deployer history: {e}")
            info["group"] = {}
            for w in sorted(by_coin[m]):
                try:
                    st = coin_trades(w, m)
                    info["group"][w] = {k: st[k] for k in ("realized_sol", "spent_sol", "received_sol", "held_tokens",
                                                           "free_tokens_in", "complete")}
                    info["group"][w]["trades"] = st["trades"]
                except Exception as e:
                    T.log(f"  ! {w}: {e}")
            coins[m] = info
        except Exception as e:
            T.log(f"  ! {type(e).__name__}: {e}")
            continue
        json.dump(coins, open(cpath, "w"))
    T.log(f"Saved {cpath}")


def saved_mode(a):
    rows = list(csv.DictReader(open(a.watchlist, encoding="utf-8"), delimiter="\t"))
    ws = list(dict.fromkeys(r["address"].strip() for r in rows))
    d = os.path.join(a.out, "saved")
    os.makedirs(d, exist_ok=True)
    creators = {}
    cp = os.path.join(a.out, "creators.json")
    if os.path.exists(cp):
        creators = json.load(open(cp))
    for i, w in enumerate(ws, 1):
        p = os.path.join(d, w + ".json")
        if os.path.exists(p):
            continue
        T.log(f"[{i}/{len(ws)}] {w}")
        try:
            res = wallet_coins(w, a.txs)
            hits = []
            for m, ev in res["coins"].items():
                ins = [e for e in ev if e["kind"] == "XFER_IN" and e["counterparty"]]
                if not ins:
                    continue
                if m not in creators:
                    pc = T.pump_curve(m)
                    creators[m] = (pc or {}).get("creator") if pc else None
                for e in ins:
                    if creators[m] and e["counterparty"] == creators[m]:
                        sold = [x for x in ev if x["kind"] == "SELL" and (x["time"] or 0) >= (e["time"] or 0)]
                        hits.append({"mint": m, "from_creator": creators[m], "tokens": e["tokens"], "sig": e["sig"],
                                     "time": e["time"], "sold_after": sum(x["sol"] for x in sold),
                                     "sell_sigs": [x["sig"] for x in sold]})
            res["creator_hits"] = hits
            json.dump(res, open(p, "w"))
            json.dump(creators, open(cp, "w"))
        except Exception as e:
            T.log(f"  ! {type(e).__name__}: {e} (retried next run)")
    T.log("done")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="mode", required=True)
    g = sub.add_parser("group")
    g.add_argument("--wallets", required=True)
    g.add_argument("--out", required=True)
    g.add_argument("--txs", type=int, default=300)
    g.add_argument("--max-coins", type=int, default=30)
    g.add_argument("--pages", type=int, default=8, help="curve history pages to reach the launch")
    s = sub.add_parser("saved")
    s.add_argument("--watchlist", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--txs", type=int, default=60)
    a = p.parse_args()
    group_mode(a) if a.mode == "group" else saved_mode(a)


if __name__ == "__main__":
    main()
