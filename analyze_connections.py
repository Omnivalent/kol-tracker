#!/usr/bin/env python3
"""
Turn connect_wallets.py results into links between watched wallets, an Excel workbook and a PDF report.

Link rules (each link lists the exact transaction(s) behind it):
  STRONG  direct transfer between two watched wallets (SOL/USDC/USDT, $20+), one funded the other,
          co-signed a transaction, same first funder when that funder is a private wallet (<1000 txs),
          funding chains meet at a private wallet
  MEDIUM  first funding of both came with the same amount from the same busy wallet within 15 minutes;
          both moved $500+ with the same private wallet
"Possible connected wallets" of a wallet: private wallets (<1000 txs) it moved $500+ with, its funders,
its co-signers. Busy wallets (exchanges, bots, services) are listed separately and never link wallets.

Usage: python3 analyze_connections.py --out private
Writes private/wallets.xlsx, private/connections_report.html and private/connections_report.pdf
"""
import argparse
import html
import json
import os
import subprocess
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import solana_tracer as T  # noqa: E402

DIRECT_MIN_USD = 20
PARTNER_MIN_USD = 500


class Graph:
    def __init__(self):
        self.links = []

    def add(self, a, b, strength, kind, text, sigs=()):
        if a == b:
            return
        x, y = sorted([a, b])
        self.links.append({"a": x, "b": y, "strength": strength, "kind": kind, "text": text,
                           "sigs": [s for s in sigs if s][:3]})


def ts(t):
    return time.strftime("%Y-%m-%d", time.gmtime(t)) if t else ""


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "private"))
    p.add_argument("--no-pdf", action="store_true")
    a = p.parse_args()
    a.out = os.path.abspath(a.out)

    meta = json.load(open(os.path.join(a.out, "watch_meta.json")))
    watch, kols, sol_usd = meta["watch"], meta["kols"], meta["sol_usd"]
    scan = {}
    for w in watch:
        f = os.path.join(a.out, "scan", w + ".json")
        if os.path.exists(f):
            scan[w] = json.load(open(f))
    every = set(watch) | set(kols)  # all named wallets

    def label(w):
        parts = []
        if w in kols:
            parts.append(f"KOL {kols[w]}")
        if w in watch:
            parts.append(" / ".join(watch[w]["names"][:3]) if watch[w]["names"] else "your list, no name")
        return " · ".join(parts) or "(unnamed)"

    # busy status for wallets we need to judge
    busy = {w: s.get("busy") for w, s in scan.items()}

    def is_busy(x):
        if x not in busy:
            try:
                busy[x] = T.is_hub(x, 1000)
            except Exception:
                busy[x] = None
        return busy[x]

    g = Graph()
    partners = defaultdict(dict)  # w -> counterparty -> stats
    for w, s in scan.items():
        for t in s.get("transfers", []):
            o = t["to"] if t["from"] == w else t["from"]
            st = partners[w].setdefault(o, {"sent": 0.0, "recv": 0.0, "n": 0, "sigs": [], "first": None, "last": None,
                                            "assets": set()})
            st["sent" if t["from"] == w else "recv"] += t["usd"]
            st["n"] += 1
            st["assets"].add(t["asset"])
            if len(st["sigs"]) < 3:
                st["sigs"].append(t["sig"])
            tt = t.get("time")
            if tt:
                st["first"] = tt if st["first"] is None else min(st["first"], tt)
                st["last"] = tt if st["last"] is None else max(st["last"], tt)

    # STRONG: direct transfers between named wallets
    for w, ps in partners.items():
        for o, st in ps.items():
            if o in every and st["sent"] + st["recv"] >= DIRECT_MIN_USD:
                g.add(w, o, "strong", "direct transfer",
                      f"{label(w)} sent ${st['sent']:,.0f} / received ${st['recv']:,.0f} with {label(o)} "
                      f"({st['n']} transfers, {', '.join(sorted(st['assets']))})", st["sigs"])
    # STRONG: funding and co-signing
    for w, s in scan.items():
        ch = s.get("chain") or []
        for hop, c in enumerate(ch):
            if c.get("addr") in every:
                g.add(w, c["addr"], "strong", "funding",
                      f"{label(c['addr'])} is in the funding chain of {label(w)} (hop {hop + 1}, "
                      f"{c['sol']:.3f} SOL on {ts(c.get('time'))})", [c.get("sig")])
        for k, sigs in (s.get("cosigners") or {}).items():
            if k in every:
                g.add(w, k, "strong", "co-signed", f"{label(w)} and {label(k)} signed the same transaction", sigs)
    # shared funding
    at = defaultdict(list)
    for w, s in scan.items():
        for hop, c in enumerate(s.get("chain") or []):
            if c.get("addr"):
                at[c["addr"]].append((w, hop, c))
    for F, hits in at.items():
        ws = {}
        for w, hop, c in hits:
            if w not in ws or hop < ws[w][0]:
                ws[w] = (hop, c)
        if len(ws) < 2:
            continue
        F_busy = any(c.get("busy") for _, _, c in hits)
        names = sorted(ws)
        if not F_busy:
            for i, x in enumerate(names):
                for y in names[i + 1:]:
                    g.add(x, y, "strong", "shared funder",
                          f"funding chains of {label(x)} (hop {ws[x][0] + 1}) and {label(y)} (hop {ws[y][0] + 1}) "
                          f"meet at private wallet {F}", [ws[x][1].get("sig"), ws[y][1].get("sig")])
        else:
            by_amt = defaultdict(list)
            for w in names:
                by_amt[round(ws[w][1]["sol"], 2)].append(w)
            for amt, grp in by_amt.items():
                grp.sort(key=lambda w: ws[w][1].get("time") or 0)
                for x, y in zip(grp, grp[1:]):
                    dt = abs((ws[x][1].get("time") or 0) - (ws[y][1].get("time") or 0))
                    if dt <= 900:
                        g.add(x, y, "medium", "same busy funder",
                              f"{label(x)} and {label(y)}: funding chains start with exactly {amt} SOL from busy "
                              f"wallet {F}, {int(dt)}s apart", [ws[x][1].get("sig"), ws[y][1].get("sig")])
    # MEDIUM: shared private counterparty ($500+ each)
    seen_by = defaultdict(list)
    for w, ps in partners.items():
        for o, st in ps.items():
            if o not in every and st["sent"] + st["recv"] >= PARTNER_MIN_USD:
                seen_by[o].append(w)
    for o, ws in seen_by.items():
        if len(ws) >= 2 and is_busy(o) is False:
            ws = sorted(ws)
            for i, x in enumerate(ws):
                for y in ws[i + 1:]:
                    g.add(x, y, "medium", "shared counterparty",
                          f"{label(x)} and {label(y)} both moved $500+ with private wallet {o}",
                          partners[x][o]["sigs"][:1] + partners[y][o]["sigs"][:1])

    # dedupe links (same pair+kind): keep the first text, merge sigs
    merged = {}
    for l in g.links:
        k = (l["a"], l["b"], l["kind"])
        if k not in merged:
            merged[k] = l
    links = sorted(merged.values(), key=lambda l: (l["strength"] != "strong", l["kind"], label(l["a"])))

    # clusters from strong links
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for l in links:
        if l["strength"] == "strong":
            parent[find(l["a"])] = find(l["b"])
    comps = defaultdict(set)
    for l in links:
        if l["strength"] == "strong":
            comps[find(l["a"])].update([l["a"], l["b"]])
    clusters = sorted((sorted(c) for c in comps.values()), key=lambda c: -len(c))

    # possible connected wallets per wallet
    possible = {}
    for w in watch:
        s = scan.get(w)
        if not s:
            continue
        rows = []
        for o, st in sorted(partners.get(w, {}).items(), key=lambda kv: -(kv[1]["sent"] + kv[1]["recv"])):
            if st["sent"] + st["recv"] < PARTNER_MIN_USD:
                continue
            b = is_busy(o)
            rows.append({"wallet": o, "role": "KOL / watched" if o in every else
                         ("busy (exchange/bot/service)" if b else "private wallet" if b is False else "unknown"),
                         "sent": st["sent"], "recv": st["recv"], "n": st["n"], "sigs": st["sigs"],
                         "first": st["first"], "last": st["last"]})
        for hop, c in enumerate(s.get("chain") or []):
            if c.get("addr"):
                rows.append({"wallet": c["addr"], "role": f"funder hop {hop + 1}" + (" (busy)" if c.get("busy") else ""),
                             "sent": 0, "recv": c["sol"] * sol_usd, "n": 1, "sigs": [c.get("sig")],
                             "first": c.get("time"), "last": c.get("time")})
        for k, sigs in (s.get("cosigners") or {}).items():
            rows.append({"wallet": k, "role": "co-signer", "sent": 0, "recv": 0, "n": len(sigs), "sigs": sigs[:3],
                         "first": None, "last": None})
        possible[w] = rows

    result = {"generated": int(time.time()), "sol_usd": sol_usd, "watched": len(watch), "scanned": len(scan),
              "kols": len(kols), "links": links, "clusters": clusters,
              "possible": possible, "labels": {w: label(w) for w in every | {x for r in possible.values() for x in
                                                                             (y["wallet"] for y in r)}}}
    json.dump(result, open(os.path.join(a.out, "connections.json"), "w"))
    write_xlsx(a.out, watch, kols, scan, links, possible, label, sol_usd)
    html_path = write_html(a.out, result, watch, kols, scan, label)
    if not a.no_pdf:
        pdf = os.path.join(a.out, "connections_report.pdf")
        js = ("const {chromium}=require('playwright');(async()=>{const b=await chromium.launch();const p=await b.newPage();"
              f"await p.goto('file://{html_path}');await p.waitForTimeout(500);"
              f"await p.pdf({{path:'{pdf}',format:'A4',printBackground:true,margin:{{top:'14mm',bottom:'14mm',left:'12mm',right:'12mm'}},"
              "displayHeaderFooter:true,headerTemplate:'<span></span>',footerTemplate:'<div style=\"font-size:8px;width:100%;text-align:center;color:#888\">"
              "Wallet connections · page <span class=pageNumber></span> of <span class=totalPages></span></div>'});await b.close();})();")
        subprocess.run(["node", "-e", js], check=True)
    print(f"{len(scan)}/{len(watch)} scanned, {len(links)} links, {len(clusters)} clusters")


def write_xlsx(out, watch, kols, scan, links, possible, label, sol_usd):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    head = Font(bold=True, color="FFFFFF")
    fill = PatternFill("solid", fgColor="1F3A4D")

    def sheet(ws, title, cols, rows, widths):
        ws.title = title
        ws.append(cols)
        for c in ws[1]:
            c.font, c.fill = head, fill
            c.alignment = Alignment(vertical="center")
        for r in rows:
            ws.append(r)
        for i, wd in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = wd
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions

    nlinks = defaultdict(int)
    for l in links:
        nlinks[l["a"]] += 1
        nlinks[l["b"]] += 1
    rows = []
    for w in sorted(set(watch) | set(kols), key=lambda x: (x not in kols, label(x).lower())):
        s = scan.get(w, {})
        v = watch.get(w, {"names": [], "groups": [], "lists": []})
        ch = s.get("chain") or []
        f = ch[0] if ch else {}
        rows.append([w, kols.get(w, ""), " / ".join(v["names"]), ", ".join(v["groups"]), ", ".join(v["lists"]),
                     "kolscan KOL" + (" + your list" if w in watch else "") if w in kols else "your list (new)",
                     ("yes" if s.get("busy") else "no") if s else "not scanned",
                     f.get("addr") or f.get("note", ""), round(f.get("sol", 0), 4) if f.get("sol") else "",
                     ts(f.get("time")), " <- ".join(c.get("addr") or "?" for c in ch[1:]),
                     s.get("own_txs", ""), sum(1 for r in possible.get(w, []) if r["role"] == "private wallet"),
                     nlinks.get(w, 0), f"https://solscan.io/account/{w}"])
    sheet(wb.active, "All wallets",
          ["Wallet", "kolscan name", "Your name(s)", "Your groups", "Your lists", "Status", "Busy (1000+ txs)",
           "First funder", "Funded SOL", "Funded on", "Funder's funders", "Own txs read",
           "Private $500+ partners", "Links to named wallets", "Solscan"],
          rows, [46, 16, 34, 18, 10, 24, 14, 46, 11, 12, 60, 12, 14, 14, 60])
    sheet(wb.create_sheet(), "Links",
          ["Strength", "Type", "Wallet A", "A name", "Wallet B", "B name", "Evidence", "Tx 1", "Tx 2", "Tx 3"],
          [[l["strength"].upper(), l["kind"], l["a"], label(l["a"]), l["b"], label(l["b"]), l["text"],
            *(l["sigs"] + ["", "", ""])[:3]] for l in links],
          [10, 18, 46, 30, 46, 30, 90, 90, 90, 90])
    prow = []
    for w, rs in possible.items():
        for r in rs:
            prow.append([w, label(w), r["wallet"], label(r["wallet"]) if r["role"] == "KOL / watched" else "",
                         r["role"], round(r["sent"], 2), round(r["recv"], 2), r["n"], ts(r["first"]), ts(r["last"]),
                         *(r["sigs"] + ["", "", ""])[:3]])
    sheet(wb.create_sheet(), "Possible connected wallets",
          ["Wallet", "Wallet name", "Connected wallet", "Connected name", "Role", "Sent to it $", "Received from it $",
           "Transfers", "First", "Last", "Tx 1", "Tx 2", "Tx 3"],
          prow, [46, 30, 46, 26, 26, 12, 14, 10, 12, 12, 90, 90, 90])
    ws = wb.create_sheet("Read me")
    for line in [
        "Wallet connections workbook",
        f"Generated {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}. SOL valued at ${sol_usd:.2f}.",
        "Sheets: All wallets (every kolscan KOL and every wallet in your lists), Links (between named wallets),",
        "Possible connected wallets (per wallet: private $500+ partners, funders, co-signers).",
        "Only plain transfers count: transactions touching a DEX, pool, bonding curve or other program are excluded.",
        "Busy = 1000+ transactions (exchange, bot or service): never used to link wallets.",
        "Coverage: each wallet's most recent own transactions (see 'Own txs read') plus its funding chain.",
        "Older transfers outside that window are not included. Links are leads, not proof.",
    ]:
        ws.append([line])
    ws.column_dimensions["A"].width = 120
    wb.save(os.path.join(out, "wallets.xlsx"))


def write_html(out, R, watch, kols, scan, label):
    e = html.escape
    acct = lambda w: f'<a href="https://solscan.io/account/{w}">{w}</a>'
    txl = lambda s: f'<a href="https://solscan.io/tx/{s}">{s}</a>' if s else ""
    links, clusters, possible = R["links"], R["clusters"], R["possible"]
    strong = [l for l in links if l["strength"] == "strong"]
    by_w = defaultdict(list)
    for l in links:
        by_w[l["a"]].append(l)
        by_w[l["b"]].append(l)
    parts = [f"""<!doctype html><html><head><meta charset="utf-8"><title>Wallet Connections</title><style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');
    body{{font:10.5px/1.5 Inter,system-ui,sans-serif;color:#141922;margin:0}}
    h1{{font-size:26px;margin:0 0 4px;letter-spacing:-.02em}} h2{{font-size:15px;margin:22px 0 8px;border-bottom:1.5px solid #141922;padding-bottom:4px}}
    h3{{font-size:12px;margin:14px 0 4px}} .muted{{color:#5d6675}} .mono,a{{font-family:'JetBrains Mono',monospace;font-size:8.6px;word-break:break-all}}
    a{{color:#0d6b78;text-decoration:none}} table{{border-collapse:collapse;width:100%;margin:4px 0 10px}}
    th,td{{border-bottom:1px solid #dde2ea;padding:4px 5px;text-align:left;vertical-align:top}} th{{font-size:8.5px;text-transform:uppercase;letter-spacing:.06em;color:#5d6675}}
    .tag{{display:inline-block;font-size:8px;font-weight:600;padding:1px 6px;border-radius:9px;text-transform:uppercase;letter-spacing:.05em}}
    .strong{{background:#fde7d3;color:#9a4300}} .medium{{background:#ece4fb;color:#5b35a8}}
    .stats{{display:flex;gap:10px;margin:12px 0}} .stat{{border:1px solid #dde2ea;border-radius:6px;padding:8px 12px;flex:1}} .stat b{{font-size:17px;display:block}}
    .card{{border:1px solid #dde2ea;border-radius:6px;padding:8px 10px;margin:8px 0;page-break-inside:avoid}}
    .note{{border-left:3px solid #c25a00;padding:4px 10px;background:#fbf6f1}}
    </style></head><body>
    <h1>Wallet Connections</h1>
    <div class="muted">Generated {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(R['generated']))} · SOL ${R['sol_usd']:.2f}</div>
    <div class="stats"><div class="stat"><b>{R['watched']}</b>wallets in your lists</div><div class="stat"><b>{R['scanned']}</b>scanned</div>
    <div class="stat"><b>{R['kols']}</b>kolscan KOLs cross-checked</div><div class="stat"><b>{len(strong)}</b>strong links</div>
    <div class="stat"><b>{len(clusters)}</b>connected groups</div></div>
    <p class="note">How to read this: a <span class="tag strong">strong</span> link is a direct on-chain tie (a transfer between the two
    wallets, one funding the other, a shared private funder, or a co-signed transaction). A <span class="tag medium">medium</span> link is a
    shared pattern (identical funding from the same exchange-like wallet minutes apart, or both trading $500+ with the same private wallet).
    Only plain SOL/USDC/USDT transfers count; swaps, pools and bonding curves are excluded. Each wallet's most recent own transactions and its
    funding chain were read; older transfers outside that window are not included. Links are leads, not proof.</p>"""]

    parts.append("<h2>Connected groups</h2><p class='muted'>Wallets joined by strong links, largest first.</p>")
    if not clusters:
        parts.append("<p>No strong links found between the named wallets.</p>")
    for i, c in enumerate(clusters, 1):
        rows = "".join(f"<tr><td>{acct(w)}</td><td>{e(label(w))}</td></tr>" for w in c)
        ev = "".join(f"<tr><td><span class='tag {l['strength']}'>{e(l['kind'])}</span></td><td>{e(l['text'])}"
                     f"<div>{'<br>'.join(txl(s) for s in l['sigs'])}</div></td></tr>"
                     for l in strong if l["a"] in c and l["b"] in c)
        parts.append(f"<div class='card'><h3>Group {i} · {len(c)} wallets</h3><table><tr><th>Wallet</th><th>Name</th></tr>{rows}</table>"
                     f"<table><tr><th>Link</th><th>Evidence and transactions</th></tr>{ev}</table></div>")

    med = [l for l in links if l["strength"] == "medium"]
    parts.append(f"<h2>Medium links ({len(med)})</h2>")
    parts.append("<table><tr><th>Type</th><th>Wallets</th><th>Evidence</th></tr>" + "".join(
        f"<tr><td><span class='tag medium'>{e(l['kind'])}</span></td><td>{acct(l['a'])}<br>{acct(l['b'])}</td>"
        f"<td>{e(l['text'])}<div>{'<br>'.join(txl(s) for s in l['sigs'])}</div></td></tr>" for l in med) + "</table>"
                 if med else "<p>None.</p>")

    parts.append("<h2>Every wallet: possible connected wallets</h2><p class='muted'>Per wallet in your lists: links to other named wallets, "
                 "private wallets it moved $500+ with, its funders and co-signers. Wallets with nothing found are listed at the end.</p>")
    empty = []
    for w in sorted(watch, key=lambda x: label(x).lower()):
        s = scan.get(w)
        rs = possible.get(w, [])
        ls = by_w.get(w, [])
        if not s:
            empty.append((w, "not scanned yet"))
            continue
        if not rs and not ls:
            empty.append((w, f"nothing found in {s.get('own_txs', 0)} recent own txs"))
            continue
        ch = " ← ".join(acct(c["addr"]) + (" (busy)" if c.get("busy") else "") for c in s.get("chain") or [] if c.get("addr"))
        lrows = "".join(f"<tr><td><span class='tag {l['strength']}'>{e(l['kind'])}</span></td><td>{acct(l['b'] if l['a'] == w else l['a'])}"
                        f"<div class='muted'>{e(label(l['b'] if l['a'] == w else l['a']))}</div></td><td>{'<br>'.join(txl(x) for x in l['sigs'])}</td></tr>" for l in ls)
        prow = "".join(f"<tr><td>{e(r['role'])}</td><td>{acct(r['wallet'])}</td><td>${r['sent']:,.0f}</td><td>${r['recv']:,.0f}</td>"
                       f"<td>{r['n']}</td><td>{txl(r['sigs'][0] if r['sigs'] else '')}</td></tr>" for r in rs)
        parts.append(f"<div class='card'><h3>{e(label(w))}</h3><div>{acct(w)}{' · <b>busy wallet</b>' if s.get('busy') else ''}</div>"
                     f"<div class='muted'>Funding chain: {ch or 'not found'} · {s.get('own_txs', 0)} own txs read</div>"
                     + (f"<table><tr><th>Link</th><th>Named wallet</th><th>Transactions</th></tr>{lrows}</table>" if lrows else "")
                     + (f"<table><tr><th>Role</th><th>Wallet</th><th>Sent to it</th><th>Received</th><th>Transfers</th><th>Example tx</th></tr>{prow}</table>" if prow else "")
                     + "</div>")
    if empty:
        parts.append("<h3>No connections found</h3><table><tr><th>Wallet</th><th>Name</th><th>Status</th></tr>" + "".join(
            f"<tr><td>{acct(w)}</td><td>{e(label(w))}</td><td>{e(why)}</td></tr>" for w, why in empty) + "</table>")
    parts.append("</body></html>")
    path = os.path.join(out, "connections_report.html")
    open(path, "w").write("".join(parts))
    return path


if __name__ == "__main__":
    main()
