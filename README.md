# Alpaca Autonomous Trader

Candlestick Trading Bible strategies running on paper account.

## Local Setup

- Install Alpaca CLI (see `alpaca-skills/README.md`)
- Run: `python3 bot/trader.py`

## GitHub Actions Setup (cloud, free)

1. Push this repo to GitHub
2. Go to Settings > Secrets > Actions > New repository secret
3. Name: `APCA_ACCESS_TOKEN`
4. Value: get your token from `~/.config/alpaca/profiles/paper.yaml` (access_token field)
   - Or use API keys: `APCA_API_KEY_ID` + `APCA_API_SECRET_KEY` from https://alpaca.markets

The bot will run automatically:
- Mon-Fri, every 30 min during US market hours (9:30 AM - 4:00 PM ET)
- Daily journal at 4:30 PM ET

## Files

| Path | Purpose |
|------|---------|
| `bot/trader.py` | Main trading engine + journal |
| `bot/rest_client.py` | REST API fallback (no CLI binary needed) |
| `config.json` | Symbols, thresholds, strategy config |
| `runs/` | Journals and signal logs |
| `.github/workflows/` | GitHub Actions schedules |
