# Market Agent (GitHub Actions deployment)

24/7 crypto market-analysis agent. Runs as a free GitHub Actions workflow —
no server, no card, no cost. Posts token calls (with charts), target/stop
alerts and a daily breakdown to Telegram.

## How it works
- `.github/workflows/market-agent.yml` fires every 5 minutes
- `run.py headless dispatch` runs whichever tasks are due:
  scan (45m) · track (10m) · mover pulse (15m) · daily digest (20:00 WAT) · command poll
- State lives in `state/agent.db` (committed after every run)
- Data sources: Binance + Bitget (Bybit is geo-blocked from US runners),
  Etherscan, Dune (query 8688378), public RPC, keyless news/sentiment feeds

## Required GitHub secrets (Settings → Secrets and variables → Actions)
| Secret | What |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Your numeric Telegram chat id |
| `TELEGRAM_OWNER_ID` | Same number as chat id |
| `ETHERSCAN_API_KEY` | Etherscan v2 API key (free) |
| `DUNE_API_KEY` | Dune API key |
| `DUNE_QUERY_ID` | `8688378` |

## Telegram commands (answered within ~5 min)
`/start` · `/status` · `/movers` · `/cancel [id|TICKER]` · `/dune_setup`

## Notes
- The repo must stay **public** — public repos get unlimited free Actions
  minutes; private repos are capped at 2,000/month (not enough for this bot).
- No secrets are stored in this repo; keys live only in GitHub's encrypted
  secrets. `state/agent.db` holds call/flow history only.
