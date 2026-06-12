"""
OKX 新币做空策略 v2 — 核心逻辑
纯标准库实现，无需安装额外依赖
"""

import time
import json
import math
import logging
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone
from typing import Optional, Dict, List, Any

from config import (
    API_KEY, SECRET_KEY, PASSPHRASE,
    USE_DEMO, DEMO_API_KEY, DEMO_SECRET_KEY, DEMO_PASSPHRASE,
    LISTING_DAYS_MIN, LISTING_DAYS_MAX,
    ATR_THRESHOLD, PRICE_95_PERCENTILE, FUNDING_RATE_POSITIVE,
    TP1_PCT, TP2_PCT, SL_PCT, MAX_HOLD_DAYS,
    POSITION_SIZE, LEVERAGE, MAX_POSITIONS,
    STATE_FILE, LOG_FILE
)

# ============ 日志配置 =============

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("okx_short")

# ============ OKX API 封装（urllib版）============

BASE_URL = "https://www.okx.com"
PROXY_HOST = "127.0.0.1"
PROXY_PORT = 7898  # 你的 Clash HTTP 代理端口


class OKXClient:
    def __init__(self, api_key: str, secret_key: str, passphrase: str, demo: bool = False):
        self.api_key = api_key
        self.secret_key = secret_key
        self.passphrase = passphrase
        self.demo = demo
        self.base_url = BASE_URL

    def _sign(self, timestamp: str, method: str, path: str, body: str = "") -> str:
        import hmac, base64
        message = timestamp + method + path + body
        mac = hmac.new(
            self.secret_key.encode(),
            message.encode(),
            digestmod="sha256"
        )
        return base64.b64encode(mac.digest()).decode()

    def _request_raw(self, method: str, path: str, params: dict = None, body: dict = None) -> dict:
        """发请求，返回解析后的JSON dict"""
        # Python 3.11 不支持 timespec="millis"，手动拼接毫秒
        ts = datetime.now(timezone.utc)
        timestamp = ts.strftime("%Y-%m-%dT%H:%M:%S") + ".{:03d}Z".format(int(ts.microsecond / 1000))
        body_str = json.dumps(body) if body else ""
        sign = self._sign(timestamp, method, path, body_str)

        headers = {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
        }

        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)

        req = urllib.request.Request(url, data=body_str.encode() if body_str else None,
                                      headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body_err = e.read().decode() if e.fp else ""
            logger.error(f"HTTP {e.code}: {body_err}")
            raise Exception(f"HTTP {e.code}: {body_err}")
        except Exception as e:
            logger.error(f"请求异常: {e}")
            raise

    def _request(self, method: str, path: str, params: dict = None, body: dict = None) -> list:
        data = self._request_raw(method, path, params, body)
        if isinstance(data, dict):
            code = data.get("code", "")
            msg = data.get("msg", "")
            if code != "0":
                logger.warning(f"API {code}: {msg}")
            return data.get("data", []) if code == "0" else []
        return data if isinstance(data, list) else []

    # ---- 公开接口 ----

    def get_instruments(self, inst_type: str = "SWAP") -> list:
        return self._request("GET", "/api/v5/public/instruments",
                              params={"instType": inst_type})

    def get_candles(self, inst_id: str, bar: str = "1D", limit: int = 100) -> list:
        """获取K线（最新在前，返回时反转成旧到新）"""
        params = {"instId": inst_id, "bar": bar, "limit": str(limit)}
        try:
            data = self._request("GET", "/api/v5/market/history-candles", params=params)
            # data格式: [ts, open, high, low, close, vol, volCcy]  最新在前
            return list(reversed(data))  # 转为旧到新
        except Exception as e:
            logger.warning(f"获取K线失败 {inst_id}: {e}")
            return []

    def get_ticker(self, inst_id: str) -> Optional[dict]:
        params = {"instId": inst_id}
        try:
            data = self._request("GET", "/api/v5/market/ticker", params=params)
            return data[0] if data else None
        except:
            return None

    def get_funding_rate(self, inst_id: str) -> Optional[float]:
        params = {"instId": inst_id}
        try:
            data = self._request("GET", "/api/v5/public/funding-rate", params=params)
            if data:
                return float(data[0].get("fundingRate", 0))
        except:
            pass
        return None

    def get_instrument_info(self, inst_id: str) -> Optional[dict]:
        params = {"instId": inst_id}
        try:
            data = self._request("GET", "/api/v5/public/instrument", params=params)
            return data[0] if data else None
        except:
            return None

    # ---- 私有接口 ----

    def set_leverage(self, inst_id: str, mgn_mode: str = "isolated", lever: int = 3) -> list:
        body = {
            "instId": inst_id,
            "lever": str(lever),
            "mgnMode": mgn_mode
        }
        return self._request("POST", "/api/v5/account/set-leverage", body=body)

    def get_account_balance(self) -> float:
        try:
            data = self._request("GET", "/api/v5/account/balance", params={"ccy": "USDT"})
            if not data:
                return 0.0
            for item in data:
                for bal in item.get("details", []):
                    if bal.get("ccy") == "USDT":
                        return float(bal.get("availBal", 0))
        except Exception as e:
            logger.error(f"获取余额失败: {e}")
        return 0.0

    def get_positions(self) -> list:
        try:
            data = self._request("GET", "/api/v5/account/positions",
                                  params={"instType": "SWAP"})
            return [p for p in data if p.get("instId", "").endswith("-USDT-SWAP")
                    and p.get("posSide") == "short"]
        except:
            return []

    def place_order(self, inst_id: str, side: str, sz: str,
                    tdMode: str = "isolated", ordType: str = "market",
                    slTriggerPx: str = "", slOrdPx: str = "-1",
                    tpTriggerPx: str = "", tpOrdPx: str = "-2") -> Optional[str]:
        body: Dict[str, Any] = {
            "instId": inst_id,
            "tdMode": tdMode,
            "side": side,
            "ordType": ordType,
            "sz": sz,
        }
        if slTriggerPx:
            body["slTriggerPx"] = slTriggerPx
            body["slOrdPx"] = slOrdPx
        if tpTriggerPx:
            body["tpTriggerPx"] = tpTriggerPx
            body["tpOrdPx"] = tpOrdPx

        try:
            result = self._request("POST", "/api/v5/trade/order", body=body)
            if result:
                ord_id = result[0].get("ordId", "")
                if ord_id:
                    return ord_id
                sCode = result[0].get("sCode", "")
                sMsg = result[0].get("sMsg", "")
                logger.warning(f"下单响应异常 {inst_id}: code={sCode} msg={sMsg}")
        except Exception as e:
            logger.error(f"下单失败 {inst_id}: {e}")
        return None

    def close_position(self, inst_id: str, mgn_mode: str = "isolated") -> bool:
        body = {
            "instId": inst_id,
            "mgnMode": mgn_mode,
            "posSide": "short",
            "side": "buy",
            "ordType": "market",
            "sz": "0"  # 全平
        }
        try:
            self._request("POST", "/api/v5/trade/close-position", body=body)
            return True
        except Exception as e:
            logger.error(f"平仓失败 {inst_id}: {e}")
            return False

    def get_open_orders(self, inst_id: str) -> list:
        try:
            return self._request("GET", "/api/v5/trade/orders-pending",
                                  params={"instId": inst_id, "instType": "SWAP"})
        except:
            return []


# ============ 策略工具函数 =============

def calc_atr(candles: List[list]) -> Optional[float]:
    """计算ATR（取平均TrueRange）"""
    if len(candles) < 2:
        return None
    trs = []
    for i in range(1, len(candles)):
        high = float(candles[i][2])
        low = float(candles[i][3])
        prev_close = float(candles[i - 1][4])
        tr = max(high - low,
                 abs(high - prev_close),
                 abs(low - prev_close))
        trs.append(tr)
    return sum(trs) / len(trs) if trs else None


def check_entry_filters(client: OKXClient, inst_id: str, listing_ts_ms: int) -> dict:
    """
    入场四滤检查
    返回 pass + reason + details
    """
    now_ms = time.time() * 1000
    listing_days = (now_ms - listing_ts_ms) / (86400 * 1000)

    # 第一关：上市期限
    if listing_days < LISTING_DAYS_MIN:
        return {"pass": False, "reason": f"上市{listing_days:.1f}天 < {LISTING_DAYS_MIN}天（太新）"}
    if listing_days > LISTING_DAYS_MAX:
        return {"pass": False, "reason": f"上市{listing_days:.1f}天 > {LISTING_DAYS_MAX}天（已过期）"}

    # 第二关：波动率过滤
    candles = client.get_candles(inst_id, bar="1D", limit=30)
    if len(candles) < 5:
        return {"pass": False, "reason": "K线数据不足（<5根）"}

    ticker = client.get_ticker(inst_id)
    if not ticker:
        return {"pass": False, "reason": "无法获取当前价格"}
    last_price = float(ticker.get("last", 0))
    if last_price <= 0:
        return {"pass": False, "reason": "价格异常"}

    atr = calc_atr(candles)
    if atr is None:
        return {"pass": False, "reason": "无法计算ATR"}
    atr_ratio = atr / last_price
    if atr_ratio > ATR_THRESHOLD:
        return {"pass": False, "reason": f"ATR比率{atr_ratio:.2%} > {ATR_THRESHOLD:.2%}（波动过大）"}

    # 第三关：阴线确认（今日）
    today = candles[-1]
    today_open = float(today[1])
    today_close = float(today[4])
    if today_close >= today_open:
        return {"pass": False, "reason": f"今日阳线（开{today_open:.4g} vs 收{today_close:.4g}），等待阴线"}

    # 第四关：高位 + 资金费率
    closes = [float(c[4]) for c in candles]
    p95 = sorted(closes)[int(len(closes) * 0.95)] if len(closes) >= 20 else max(closes)

    if PRICE_95_PERCENTILE and last_price < p95:
        return {"pass": False, "reason": f"价格{last_price:.4g} < 3日95%分位{p95:.4g}（未冲高）"}

    funding_rate = None
    if FUNDING_RATE_POSITIVE:
        funding_rate = client.get_funding_rate(inst_id)
        if funding_rate is None:
            return {"pass": False, "reason": "无法获取资金费率"}
        if funding_rate <= 0:
            return {"pass": False, "reason": f"资金费率{funding_rate:.4%} ≤ 0，不适合做空"}

    return {
        "pass": True,
        "reason": "全部通过",
        "details": {
            "listing_days": round(listing_days, 1),
            "atr_ratio": round(atr_ratio, 4),
            "today_return": round((today_close - today_open) / today_open, 4),
            "price_vs_p95": round((last_price - p95) / p95, 4),
            "funding_rate": funding_rate if funding_rate else 0,
        }
    }


def calc_exit_prices(entry_price: float) -> dict:
    return {
        "sl": entry_price * (1 + SL_PCT),
        "tp1": entry_price * (1 - TP1_PCT),
        "tp2": entry_price * (1 - TP2_PCT),
    }


# ============ 状态管理 =============

def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def update_state(inst_id: str, entry_data: dict):
    state = load_state()
    state[inst_id] = {**state.get(inst_id, {}), **entry_data}
    save_state(state)


def remove_from_state(inst_id: str):
    state = load_state()
    state.pop(inst_id, None)
    save_state(state)


# ============ 交易操作 =============

def place_short(client: OKXClient, inst_id: str) -> bool:
    """开空单"""
    try:
        client.set_leverage(inst_id, mgn_mode="isolated", lever=LEVERAGE)

        ticker = client.get_ticker(inst_id)
        if not ticker:
            logger.error(f"无法获取 {inst_id} 价格")
            return False

        last_price = float(ticker.get("last", 0))
        if last_price <= 0:
            logger.error(f"价格异常 {inst_id}: {last_price}")
            return False

        sz = int(math.floor(POSITION_SIZE * LEVERAGE / last_price))
        if sz < 1:
            logger.warning(f"仓位太小，跳过 {inst_id}")
            return False

        exits = calc_exit_prices(last_price)

        ord_id = client.place_order(
            inst_id=inst_id,
            side="sell",
            sz=str(sz),
            tdMode="isolated",
            ordType="market",
            slTriggerPx=str(exits["sl"]),
            slOrdPx="-1",
            tpTriggerPx=str(exits["tp1"]),
            tpOrdPx="-2",
        )

        if ord_id:
            logger.info(f"✅ 做空成功: {inst_id} @ {last_price:.4g}, "
                        f"数量:{sz}张, SL:{exits['sl']:.4g}, TP1:{exits['tp1']:.4g}")
            update_state(inst_id, {
                "entry_price": last_price,
                "sz": sz,
                "open_time": datetime.now(timezone.utc).isoformat(),
                "sl": exits["sl"],
                "tp1": exits["tp1"],
                "tp2": exits["tp2"],
                "ord_id": ord_id,
            })
            return True
        else:
            logger.error(f"下单未返回ordId: {inst_id}")
            return False

    except Exception as e:
        logger.error(f"开空异常 {inst_id}: {e}")
        return False


# ============ 持仓检查 =============

def check_and_close_positions(client: OKXClient):
    """检查持仓：止盈/止损/超时"""
    positions = client.get_positions()
    if not positions:
        return

    logger.info(f"检查 {len(positions)} 个持仓...")
    now = datetime.now(timezone.utc)

    for pos in positions:
        inst_id = pos.get("instId", "")
        pos_sz = int(float(pos.get("pos", 0)))
        if pos_sz <= 0:
            continue

        entry_price = float(pos.get("avgPx", 0))
        if entry_price <= 0:
            continue

        state = load_state()
        entry = state.get(inst_id, {})
        open_time_str = entry.get("open_time", "")
        exits = {
            "sl": entry.get("sl", entry_price * (1 + SL_PCT)),
            "tp1": entry.get("tp1", entry_price * (1 - TP1_PCT)),
            "tp2": entry.get("tp2", entry_price * (1 - TP2_PCT)),
        }

        ticker = client.get_ticker(inst_id)
        if not ticker:
            continue
        last_price = float(ticker.get("last", 0))

        # 超时
        if open_time_str:
            try:
                open_time = datetime.fromisoformat(open_time_str.replace("Z", "+00:00"))
                hold_days = (now - open_time).total_seconds() / 86400
                if hold_days >= MAX_HOLD_DAYS:
                    logger.info(f"⏰ 超时平仓: {inst_id}，持仓{hold_days:.1f}天")
                    if client.close_position(inst_id):
                        remove_from_state(inst_id)
                    continue
            except Exception:
                pass

        # 止损（做空方向：价格涨）
        if last_price >= exits["sl"]:
            logger.info(f"🛑 止损: {inst_id} {last_price:.4g} >= SL {exits['sl']:.4g}")
            if client.close_position(inst_id):
                remove_from_state(inst_id)
            continue

        # 止盈（做空方向：价格跌）
        if last_price <= exits["tp1"]:
            logger.info(f"🎯 止盈(全平): {inst_id} {last_price:.4g} <= TP1 {exits['tp1']:.4g}")
            if client.close_position(inst_id):
                remove_from_state(inst_id)
            continue

        # 浮盈
        pnl_pct = (entry_price - last_price) / entry_price * LEVERAGE
        logger.info(f"  {inst_id}: 成本{entry_price:.4g} 当前{last_price:.4g} 浮盈{pnl_pct:.1f}% "
                    f"({entry.get('open_time','')[:10]})")


# ============ 主扫描 =============

def run_scan(client: OKXClient):
    """全市场扫描"""
    logger.info("=" * 50)
    logger.info("开始扫描 OKX USDT永续 新币做空机会...")

    instruments = client.get_instruments(inst_type="SWAP")
    usdt_swaps = [i for i in instruments if i.get("settleCcy") == "USDT"]
    logger.info(f"共 {len(usdt_swaps)} 个 USDT永续合约")

    positions = client.get_positions()
    open_inst_ids = {p["instId"] for p in positions}
    current_count = len(positions)
    logger.info(f"当前持仓: {current_count}/{MAX_POSITIONS}，剩余可开: {MAX_POSITIONS - current_count}")

    for inst in usdt_swaps:
        if current_count >= MAX_POSITIONS:
            logger.info("已达最大仓位上限，停止扫描")
            break

        inst_id = inst.get("instId", "")
        if inst_id in open_inst_ids:
            continue

        # 获取上市时间
        listing_ts = int(inst.get("listTime", 0))
        if listing_ts <= 0:
            info = client.get_instrument_info(inst_id)
            if info:
                listing_ts = int(info.get("listTime", 0))

        if listing_ts <= 0:
            continue

        result = check_entry_filters(client, inst_id, listing_ts)
        tag = "✅" if result["pass"] else "❌"
        if result["pass"]:
            logger.info(f"  {tag} {inst_id}: {result['reason']}")
            logger.info(f"      {result['details']}")
            place_short(client, inst_id)
            current_count += 1
            time.sleep(0.3)
        else:
            logger.debug(f"  {tag} {inst_id}: {result['reason']}")


# ============ 入口 =============

def main():
    if USE_DEMO and all([DEMO_API_KEY, DEMO_SECRET_KEY, DEMO_PASSPHRASE]):
        client = OKXClient(DEMO_API_KEY, DEMO_SECRET_KEY, DEMO_PASSPHRASE, demo=True)
        logger.info("🧪 模式: 模拟盘")
    else:
        client = OKXClient(API_KEY, SECRET_KEY, PASSPHRASE)
        logger.info("💰 模式: 实盘")

    logger.info(f"账户余额: {client.get_account_balance():.4f} USDT")

    # 先检查持仓（止盈/止损/超时）
    check_and_close_positions(client)

    # 再扫新机会
    run_scan(client)

    logger.info("本轮扫描完成\n")


if __name__ == "__main__":
    main()
