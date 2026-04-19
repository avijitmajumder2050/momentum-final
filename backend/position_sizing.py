"""
position_sizing.py
==================
deployable  = balance * DEPLOY_PCT          (default 30%)
risk        = min(MAX_LOSS, deployable * RISK_PCT)
base_qty    = floor(risk / sl_distance)
quantity    = max(1, floor(base_qty * margin_leverage))
"""
import os, math

DEPLOY_PCT = float(os.getenv("DEPLOY_PCT","0.30"))
RISK_PCT   = float(os.getenv("RISK_PCT",  "0.02"))
MAX_LOSS   = float(os.getenv("MAX_LOSS",  "1000"))


def calculate(
    available_balance: float,
    entry_price:       float,
    sl_price:          float,
    margin_leverage:   float = 1.0,
) -> dict:
    sl_dist = abs(entry_price - sl_price)
    if sl_dist == 0:
        return {"success":False,"message":"SL distance is zero"}
    if sl_price >= entry_price:
        return {"success":False,"message":"SL must be below entry"}
    if available_balance <= 0:
        return {"success":False,"message":"Zero balance"}

    deployable     = available_balance * DEPLOY_PCT
    risk_per_trade = min(MAX_LOSS, deployable * RISK_PCT)
    base_qty       = math.floor(risk_per_trade / sl_dist)
    quantity       = max(1, math.floor(base_qty * margin_leverage))

    return {
        "success":            True,
        "quantity":           quantity,
        "sl_distance":        round(sl_dist,          2),
        "deployable_capital": round(deployable,        2),
        "risk_per_trade":     round(risk_per_trade,    2),
        "actual_risk":        round(sl_dist * quantity,2),
        "margin_leverage":    margin_leverage,
    }
