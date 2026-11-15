#!/usr/bin/env python3
"""
Scan a watchlist of wallets and find how they connect to each other and to the KOLs in kols.csv.

Per wallet (results cached per wallet in <out>/scan/, so an interrupted run resumes):
  - funding chain: who first funded it, and who funded that (up to --hops, stops at busy wallets)
  - plain transfers of SOL/USDC/USDT with real wallets (txs touching a DEX, pool or other program are
    excluded, same rule as kol_network.py), from its most recent --txs own transactions
  - co-signers: other keys that signed a transaction together with it
  - busy: 1000+ transactions (exchange, bot or very active trader)

Links reported between two watched wallets (watchlist + KOLs):
  STRONG  direct transfer, one funded the other, co-signed a tx, same private first funder,
          funding chains meet at a private wallet
  MEDIUM  funding chains start with the same amount from the same busy wallet within 15 minutes,
          both moved $500+ with the same private wallet
Plus, per wallet, its "possible connected wallets": private wallets it moved $500+ with, its funders
and co-signers.

Usage: python3 connect_wallets.py private/user_wallets_raw.tsv --out private [--txs 150] [--hops 3]
The input is a TSV with columns list, address, name, groups.
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
import kol_network as K  # noqa: E402

MIN_LINK_USD = 500


def own_transfers(w, n, sol_usd):
    """Plain transfers (any size) and co-signers from w's own recent txs."""
    kept, fetched, before, pages = [], 0, None, 0
    while len(kept) < n and pages < 4 and fetched < n * 2:
        opts = {"limit": 1000}
        if before:
            opts["before"] = before
        page = T.rpc("getSignaturesForAddress", [w, opts])
        pages += 1
        ok = [s for s in page if not s.get("err")]
        for i in range(0, len(ok), 100):
            chunk = ok[i:i + 100]
            for s, tx in zip(chunk, T.pmap(lambda s: T.get_tx(s["signature"]), chunk)):
                fetched += 1
                if not tx:
                    continue
                own = w in T.signers_of(tx) or abs(T.lamport_delta(tx, w)) > 0 or any(
                    b.get("owner") == w for b in (tx.get("meta") or {}).get("postTokenBalances") or [])
                if own:
                    kept.append((s["signature"], tx))
            if len(kept) >= n or fetched >= n * 2:
                break
        if len(page) < 1000:
            break
        before = page[-1]["signature"]
    transfers, cosig = [], defaultdict(list)
    for sig, tx in kept[:n]:
        sg = T.signers_of(tx)
        if w in sg:
            for k in sg:
                if k != w and T.on_curve(k):
                    cosig[k].append(sig)
        for t in K.transfers_of(tx, w, sol_usd, 0):  # any size; filtered when reporting
            t["sig"], t["time"] = sig, tx.get("blockTime")
            transfers.append(t)
    times = [tx.get("blockTime") for _, tx in kept[:n] if tx.get("blockTime")]
    return {"transfers": transfers, "cosigners": dict(cosig), "own_txs": len(kept[:n]),
            "span": [min(times), max(times)] if times else [None, None]}


def scan_one(w, a, sol_usd, cache_dir):
    path = os.path.join(cache_dir, w + ".json")
    if os.path.exists(path):
        return json.load(open(path))
    res = {"wallet": w, "scanned": int(time.time())}
    res["busy"] = T.is_hub(w, 1000)
    chain, cur = [], w
    if res["busy"]:  # 1000+ txs: its first funder is out of reach on free RPC, don't page through it
        chain.append({"addr": None, "note": "busy wallet (1000+ txs): first funder not searched"})
    for _ in range(0 if res["busy"] else a.hops):
        f = T.find_funder(cur)
        if not f:
            if cur in T.DEEP:
                chain.append({"addr": None, "note": "history too deep to find the first funder"})
            break
        F = f["funder"]
        busy = T.is_hub(F, 1000)
        chain.append({"addr": F, "sol": f["sol"], "time": f["time"], "sig": f["sig"], "busy": busy,
                      "first_in_history": f["oldest_reached"]})
        if busy:
            break
        cur = F
    res["chain"] = chain
    res.update(own_transfers(w, a.txs, sol_usd))
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(res, fh)
    os.replace(tmp, path)
    return res


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("watchlist")
    p.add_argument("--out", default=os.path.join(here, "private"))
    p.add_argument("--kols", nargs="*", default=[os.path.join(here, "kols.csv"), os.path.join(here, "kols_gmgn.csv")])
    p.add_argument("--txs", type=int, default=60, help="own recent txs read per wallet")
    p.add_argument("--hops", type=int, default=3)
    p.add_argument("--sol-usd", type=float)
    a = p.parse_args()

    rows = list(csv.DictReader(open(a.watchlist, encoding="utf-8"), delimiter="\t"))
    watch = {}
    for r in rows:
        w = r["address"].strip()
        e = watch.setdefault(w, {"names": [], "groups": set(), "lists": set()})
        if r["name"].strip() and r["name"].strip() not in e["names"]:
            e["names"].append(r["name"].strip())
        e["groups"].update(g for g in (r.get("groups") or "").split(",") if g)
        e["lists"].add(r["list"])
    kols = K.load_kols(a.kols)
    cache_dir = os.path.join(a.out, "scan")
    os.makedirs(cache_dir, exist_ok=True)
    sol_usd = a.sol_usd
    if not sol_usd:
        try:
            sol_usd = K.current_sol_usd()
        except Exception:
            sol_usd = None
    if not sol_usd:  # RPC busy: reuse the price from the previous run
        prev = os.path.join(a.out, "watch_meta.json")
        sol_usd = json.load(open(prev)).get("sol_usd") if os.path.exists(prev) else None
    if not sol_usd:
        sys.exit("Could not read the SOL price; pass --sol-usd")
    T.log(f"{len(watch)} watched wallets, {len(kols)} KOLs, SOL ${sol_usd:.2f}")

    meta = {"sol_usd": sol_usd, "generated": int(time.time()),
            "watch": {w: {"names": v["names"], "groups": sorted(v["groups"]), "lists": sorted(v["lists"])}
                      for w, v in watch.items()},
            "kols": {w: k["name"] for w, k in kols.items()}}
    with open(os.path.join(a.out, "watch_meta.json"), "w") as fh:
        json.dump(meta, fh)
    data = {}
    for i, w in enumerate(watch, 1):
        nm = (watch[w]["names"] or [kols.get(w, {}).get("name", "")])[0]
        T.log(f"[{i}/{len(watch)}] {nm} {w}")
        try:
            data[w] = scan_one(w, a, sol_usd, cache_dir)
        except Exception as e:
            T.log(f"  ! {type(e).__name__}: {e} (will retry on the next run)")
    with open(os.path.join(a.out, "watch_meta.json"), "w") as fh:
        json.dump({"sol_usd": sol_usd, "generated": int(time.time()),
                   "watch": {w: {"names": v["names"], "groups": sorted(v["groups"]), "lists": sorted(v["lists"])}
                             for w, v in watch.items()},
                   "kols": {w: k["name"] for w, k in kols.items()}}, fh)
    T.log(f"Scanned {len(data)} of {len(watch)}. Run analyze_connections.py next.")


if __name__ == "__main__":
    main()
