from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _env(key: str, default, cast=str):
    v = os.getenv(key)
    if v is None or str(v).strip() == "":
        return default
    if cast is bool:
        return str(v).strip().lower() in ("1", "true", "yes", "on")
    return cast(str(v).strip())


@dataclass
class Config:
    telegram_bot_token: str
    etherscan_api_key: str
    dune_api_key: str
    dune_query_id: str
    dune_min_interval_min: int
    owner_id: int | None
    scan_interval_min: int
    track_interval_min: int
    digest_hour: int
    digest_minute: int
    max_daily_calls: int
    max_calls_per_scan: int
    max_leverage: int
    max_target_pct: float
    min_confidence: float
    top_n_tokens: int
    min_24h_usd_volume: float
    call_expiry_hours: float
    mover_24h_pct: float
    mover_vol_ratio: float
    pulse_interval_min: int
    db_path: str
    wallets_path: str
    charts_dir: str


def load_config(root: Path) -> Config:
    load_dotenv(root / ".env")
    data_dir = Path(root) / "data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "charts").mkdir(exist_ok=True)
    owner = os.getenv("TELEGRAM_OWNER_ID", "").strip()
    return Config(
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        etherscan_api_key=os.getenv("ETHERSCAN_API_KEY", "").strip(),
        dune_api_key=os.getenv("DUNE_API_KEY", "").strip(),
        dune_query_id=os.getenv("DUNE_QUERY_ID", "").strip(),
        dune_min_interval_min=_env("DUNE_MIN_INTERVAL_MIN", 30, int),
        owner_id=int(owner) if owner.isdigit() else None,
        scan_interval_min=_env("SCAN_INTERVAL_MIN", 30, int),
        track_interval_min=_env("TRACK_INTERVAL_MIN", 10, int),
        digest_hour=_env("DIGEST_HOUR", 20, int),
        digest_minute=_env("DIGEST_MINUTE", 0, int),
        max_daily_calls=_env("MAX_DAILY_CALLS", 15, int),
        max_calls_per_scan=_env("MAX_CALLS_PER_SCAN", 4, int),
        max_leverage=_env("MAX_LEVERAGE", 30, int),
        max_target_pct=_env("MAX_TARGET_PCT", 500, float),
        min_confidence=_env("MIN_CONFIDENCE", 45, float),
        top_n_tokens=_env("TOP_N_TOKENS", 120, int),
        min_24h_usd_volume=_env("MIN_24H_USD_VOLUME", 1_000_000, float),
        call_expiry_hours=_env("CALL_EXPIRY_HOURS", 24, float),
        mover_24h_pct=_env("MOVER_24H_PCT", 12, float),
        mover_vol_ratio=_env("MOVER_VOL_RATIO", 2.0, float),
        pulse_interval_min=_env("PULSE_INTERVAL_MIN", 15, int),
        db_path=str(data_dir / "agent.db"),
        wallets_path=str(Path(root) / "tracked_wallets.json"),
        charts_dir=str(data_dir / "charts"),
    )
