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
RISK_PCT   = float(os.getenv("RISK_PCT",  "0.04"))
MAX_LOSS   = float(os.getenv("MAX_LOSS",  "1000"))


def calculate(
    available_balance: float,
    entry_price:       float,
    sl_price:          float,
    margin_leverage:   float = 1.0,
) -> dict:
    sl_dist = abs(entry_price - sl_price)
    if sl_dist <= 0:
        return {"success":False,"message":"SL distance is zero"}
    if sl_price >= entry_price:
        return {"success":False,"message":"SL must be below entry"}
    if available_balance <= 0:
        return {"success":False,"message":"Zero balance"}

    
    # ─────────────────────────────
    # 1. Risk-based quantity
    # ─────────────────────────────
    risk_per_trade = min(MAX_LOSS, available_balance * RISK_PCT)
    qty_by_risk  = int(risk_per_trade / sl_dist)
    
    # ─────────────────────────────
    # 2. Fund-based quantity
    # ─────────────────────────────
    deployable     = (available_balance)/3
    qty_by_fund       = int((deployable * margin_leverage) / entry_price)

    # ─────────────────────────────
    # 3. Final quantity (SAFE)
    # ─────────────────────────────
    quantity = max(0, min(qty_by_risk, qty_by_fund))
    # 🔥 IMPORTANT: skip trade if qty < 1
    if quantity < 1:
        return {
            "success": False,
            "message": "SAFE mode → qty < 1 (insufficient funds or high SL)"
        }
    return {
        "success":            True,
        "quantity":           quantity,
        "sl_distance":        round(sl_dist,          2),
        "deployable_capital": round(deployable,        2),
        "risk_per_trade":     round(risk_per_trade,    2),
        "actual_risk":        round(sl_dist * quantity,2),
        "margin_leverage":    margin_leverage,
    }
