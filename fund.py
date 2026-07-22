import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)

# hzm0321/real-time-fund 目前使用的新版天天基金批量估值接口。
# 旧接口 https://fundgz.1234567.com.cn/js/{code}.js 已不再使用。
FUND_VALUATION_URL = (
    "https://fundcomapi.tiantianfunds.com/mm/newCore/FundValuationLast"
)
FUND_VALUATION_FIELDS = "FCODE,SHORTNAME,GSZZL,GZTIME,GSZ,NAV,PDATE"
FUND_VALUATION_BATCH_SIZE = 50

# real-time-fund 还支持新浪的两种估算口径。天天基金不提供某只基金的
# 盘中估值时，默认使用新浪口径 2（growthrate/pre_nav）作为备用源。
SINA_VALUATION_URL = (
    "https://stock.finance.sina.com.cn/fundInfo/api/openapi.php/"
    "FdFundService.getEstimateNetworthPic"
)
ENABLE_SINA_FALLBACK = os.environ.get("FUND_SINA_FALLBACK", "1").lower() not in {
    "0",
    "false",
    "no",
}
SINA_SOURCE = int(os.environ.get("FUND_SINA_SOURCE", "2"))
if SINA_SOURCE not in (2, 3):
    raise ValueError("FUND_SINA_SOURCE 只能是 2 或 3")

TIMEOUT = 10
MAX_WORKERS = 5


def build_session():
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0 Safari/537.36"
            ),
            "Accept": "application/json,text/plain,*/*",
        }
    )
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(("GET", "POST")),
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


session = build_session()


# --- Server酱 推送 ---
def push(sendkey, title, content):
    if sendkey.startswith("sctp"):
        match = re.match(r"sctp(\d+)t", sendkey)
        url = (
            f"https://{match.group(1)}.push.ft07.com/send/{sendkey}.send"
            if match
            else None
        )
    else:
        url = f"https://sctapi.ftqq.com/{sendkey}.send"

    if not url:
        logging.error("无效 sendkey")
        return None

    try:
        response = session.post(
            url, json={"title": title, "desp": content}, timeout=TIMEOUT
        )
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError) as exc:
        logging.error("推送失败: %s", exc)
        return None


def normalize_codes(codes):
    """验证并去重，同时保留原有顺序。"""
    result = []
    seen = set()
    for raw in codes:
        code = str(raw).strip()
        if not (len(code) == 6 and code.isdigit()):
            raise ValueError(f"无效基金代码: {raw!r}")
        if code not in seen:
            result.append(code)
            seen.add(code)
    return result


def to_float(value):
    if value is None or value == "":
        return None
    try:
        number = float(value)
        return number if number == number else None  # 排除 NaN
    except (TypeError, ValueError):
        return None


def batched(items, size):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def parse_tiantian_item(item):
    code = str(item.get("FCODE") or "").strip()
    if not code:
        return None
    return {
        "code": code,
        "name": str(item.get("SHORTNAME") or f"基金({code})"),
        "gszzl": to_float(item.get("GSZZL")),
        "gsz": to_float(item.get("GSZ")),
        "dwjz": to_float(item.get("NAV")),
        "gztime": str(item["GZTIME"]) if item.get("GZTIME") else None,
        "jzrq": str(item["PDATE"]) if item.get("PDATE") else None,
        "source": "天天基金",
    }


def fetch_tiantian_batch(codes):
    """
    调用 FundValuationLast，每次最多查询 50 只基金。

    返回 (code -> data, errors)。正常基金即使 GSZZL/GSZ 为 null
    也会保留，不会被误报成 0% 或“获取失败”。
    """
    data_map = {}
    errors = []

    for chunk in batched(codes, FUND_VALUATION_BATCH_SIZE):
        try:
            response = session.get(
                FUND_VALUATION_URL,
                params={
                    "FCODES": ",".join(chunk),
                    "FIELDS": FUND_VALUATION_FIELDS,
                },
                timeout=TIMEOUT,
            )
            response.raise_for_status()
            payload = response.json()
            if not payload.get("success"):
                raise ValueError(
                    payload.get("firstError")
                    or payload.get("errorCode")
                    or "API success=false"
                )
            items = payload.get("data")
            if not isinstance(items, list):
                raise ValueError("接口 data 不是列表")

            for item in items:
                if not isinstance(item, dict):
                    continue
                fund = parse_tiantian_item(item)
                if fund:
                    data_map[fund["code"]] = fund

            for code in chunk:
                if code not in data_map:
                    errors.append({"code": code, "error": "新接口未返回该基金"})
        except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
            for code in chunk:
                errors.append({"code": code, "error": str(exc)})

    return data_map, errors


def fetch_sina_fallback(fund):
    """获取新浪估算曲线的最后一个点，字段解析与参考项目一致。"""
    code = fund["code"]
    try:
        response = session.get(
            SINA_VALUATION_URL,
            params={"symbol": code},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
        points = payload.get("result", {}).get("data", {}).get("networth")
        if not isinstance(points, list) or not points:
            return fund

        point = points[-1]
        rate_key = "growthrate" if SINA_SOURCE == 2 else "growthrate2"
        nav_key = "pre_nav" if SINA_SOURCE == 2 else "pre_nav2"
        rate = to_float(point.get(rate_key))
        nav = to_float(point.get(nav_key))
        if rate is None and nav is None:
            return fund

        date = point.get("pre_date")
        time = point.get("min_time")
        gztime = " ".join(str(v) for v in (date, time) if v) or None
        return {
            **fund,
            "gszzl": rate * 100 if rate is not None else None,
            "gsz": nav,
            "gztime": gztime,
            "source": f"新浪口径{SINA_SOURCE}",
        }
    except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
        logging.warning("新浪备用估值失败 %s: %s", code, exc)
        return fund


def fetch_funds(codes):
    codes = normalize_codes(codes)
    data_map, errors = fetch_tiantian_batch(codes)

    if ENABLE_SINA_FALLBACK:
        missing = [fund for fund in data_map.values() if fund["gszzl"] is None]
        if missing:
            logging.info("天天基金有 %d 只无盘中估值，尝试新浪备用源...", len(missing))
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                for fund in pool.map(fetch_sina_fallback, missing):
                    data_map[fund["code"]] = fund

    return data_map, errors


# --- 格式化 ---
def fmt_change(value):
    if value is None:
        return "⚪ 暂无估值"
    if value > 0:
        return f"🔴 +{value:.2f}%"
    if value < 0:
        return f"🟢 {value:.2f}%"
    return f"⚪ {value:.2f}%"


def fmt_table(title, funds):
    # 有估值的排在前面，再按估算涨跌幅降序排列。
    rows = sorted(
        funds,
        key=lambda item: (
            item["gszzl"] is not None,
            item["gszzl"] if item["gszzl"] is not None else float("-inf"),
        ),
        reverse=True,
    )
    lines = [
        f"### {title}",
        "| 名称 | 代码 | 估值涨跌 | 估值时间 |",
        "|---|---|---:|---|",
    ]
    for fund in rows:
        lines.append(
            "| **{name}** | `{code}` | {change} | {time} |".format(
                name=fund["name"],
                code=fund["code"],
                change=fmt_change(fund["gszzl"]),
                time=fund["gztime"] or fund["jzrq"] or "—",
            )
        )
    return "\n".join(lines)


# --- 配置 ---
FUNDS = {
    "黄金": ["159934"],
    "指数": ["011609", "024619"],
    "板块": ["018412", "005693", "026265"],
    "债券": ["017763", "011555"],
}


def main():
    all_codes = normalize_codes(code for codes in FUNDS.values() for code in codes)
    logging.info("批量获取 %d 只基金...", len(all_codes))

    data_map, errors = fetch_funds(all_codes)

    parts = []
    for category, codes in FUNDS.items():
        items = [data_map[code] for code in codes if code in data_map]
        if items:
            parts.append(fmt_table(category, items))

    if errors:
        parts.append(
            "**获取失败：**\n"
            + "\n".join(f"❌ `{item['code']}`: {item['error']}" for item in errors)
        )

    content = "\n\n".join(parts)
    print(content)

    key = os.environ.get("SERVER_KEY")
    if not key:
        logging.info("未设置 SERVER_KEY，跳过推送（本地模式）")
    elif not content.strip():
        logging.warning("无数据，跳过推送")
    else:
        result = push(key, "基金每日估值推送", content)
        if result and result.get("code") == 0:
            logging.info("推送成功！")
        else:
            logging.info("推送失败: %s", result)


if __name__ == "__main__":
    main()
