# KOL Tracker

Free tools for tracing Solana wallets, and a site that maps the wallets KOLs move money with.

**Site:** https://omnivalent.github.io/kol-tracker/ (published from `site/` by GitHub Actions)

## What the site shows
For every KOL wallet in `kols.csv`, the plain transfers of **$500 or more** in SOL, USDC or USDT to or
from that wallet. Any transaction that touches a DEX, AMM, bonding curve, liquidity pool or other
program is excluded, and pool or vault accounts never appear. Private counterparties are expanded one
more ring; exchanges and other busy addresses (1,000+ transactions) are marked but not expanded.
Links are leads, not proof: wallets are labeled "suspected", with the transactions behind each link.

## How it updates
`.github/workflows/kol-network.yml` runs every 6 hours (or from Actions -> Run workflow):
1. `collect_kolscan.py` adds new KOLs from kolscan.io's leaderboard and live trade feed.
2. `kol_network.py` scans for up to 100 minutes. It is incremental: each run reads new activity and then
   goes further back in time, so coverage grows run by run.
3. The data in `site/data/` is committed and the site is republished.

Set the repository secret `SOLANA_RPC` to an RPC URL with a key (for example
`https://mainnet.helius-rpc.com/?api-key=...`) for faster scans. Without it the free public endpoint is used.

## Other tools
- `solana_tracer.py token <MINT>`: earliest buyers, funders and operator clusters (pump.fun aware).
- `solana_tracer.py pnl <MINT> --max-mc-usd 50000 --links`: top realized profit among early buyers.
- `solana_tracer.py sides <WALLET>`: side-wallet candidates for one wallet.
- `solana_tracer.py wallet <ADDRESS>`: funding chain and counterparties.
- `kol_network.py --only <name or wallet> ...`: deep-scan chosen KOLs.
- `import_kols.py pasted.txt`: merge a KOL list copied from GMGN or any site into `kols_gmgn.csv`.
- `report.py pnl_<mint>.json`: turn a pnl scan into a standalone HTML page.

Python 3.8+, standard library only. Tests: `for t in tests/test_*.py; do python3 -I $t; done`.
See `HANDOFF.md` for how it works, research notes and known limits.
