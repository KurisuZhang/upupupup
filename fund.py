import logging
import os
import re
from datetime import datetime

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ============================================================
# 日志配置
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)

TIMEOUT = 20


# ============================================================
# 基金配置
# ============================================================

FUNDS = {
    "黄金": ["159934"],
    "指数": ["011609", "024619"],
    "板块": ["018412", "005693", "026265"],
    "债券": ["017763", "011555"],
}


# ============================================================
# HTTP Session
# ============================================================

def create_session():
    session = requests.Session()

    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 13) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/150.0.0.0 Mobile Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://fund.eastmoney.com/",
    })

    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )

    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=5,
        pool_maxsize=5,
    )

    session.mount("https://", adapter)
    session.mount("http://", adapter)

    return session


session = create_session()


# ============================================================
# Server 酱推送
# ============================================================

def push(sendkey, title, content):
    if sendkey.startswith("sctp"):
        match = re.match(r"sctp(\d+)t", sendkey)

        if not match:
            logging.error("无效的 Server酱 Turbo SendKey")
            return None

        server_number = match.group(1)

        url = (
            f"https://{server_number}.push.ft07.com/"
            f"send/{sendkey}.send"
        )
    else:
        url = f"https://sctapi.ftqq.com/{sendkey}.send"

    try:
        response = session.post(
            url,
            json={
                "title": title,
                "desp": content,
            },
            timeout=TIMEOUT,
        )

        response.raise_for_status()

        try:
            return response.json()
        except ValueError:
            logging.error(
                "Server酱返回非 JSON 内容：%r",
                response.text[:300],
            )
            return None

    except requests.RequestException as exc:
        logging.error("Server酱推送请求失败：%s", exc)
        return None

    except Exception as exc:
        logging.exception("Server酱推送异常：%s", exc)
        return None


# ============================================================
# 基金净值获取
# ============================================================

def fetch_funds(codes):
    """
    批量获取基金最新已公布净值。

    注意：
    这里获取的是基金公司已经公布的净值，
    不是交易时段内的实时估算净值。
    """
    unique_codes = list(dict.fromkeys(codes))

    url = (
        "https://fundmobapi.eastmoney.com/"
        "FundMNewApi/FundMNFInfo"
    )

    params = {
        "pageIndex": 1,
        "pageSize": max(len(unique_codes), 20),
        "plat": "Android",
        "appType": "ttjj",
        "product": "EFund",
        "Version": "1",
        "deviceid": "github-actions-fund-notifier",
        "Fcodes": ",".join(unique_codes),
    }

    try:
        response = session.get(
            url,
            params=params,
            timeout=TIMEOUT,
        )

        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "")

        logging.info(
            "基金接口响应：status=%s，type=%s",
            response.status_code,
            content_type,
        )

        try:
            result = response.json()
        except ValueError as exc:
            preview = response.text[:500].replace("\n", " ")

            logging.error(
                "基金接口返回非 JSON 内容：%r",
                preview,
            )

            return {}, [
                {
                    "code": code,
                    "error": f"接口返回非JSON内容: {exc}",
                }
                for code in unique_codes
            ]

        raw_funds = result.get("Datas")

        if not isinstance(raw_funds, list):
            logging.error(
                "基金接口数据异常：%s",
                result,
            )

            message = (
                result.get("ErrMsg")
                or result.get("ErrorMessage")
                or result.get("Message")
                or "接口未返回 Datas"
            )

            return {}, [
                {
                    "code": code,
                    "error": str(message),
                }
                for code in unique_codes
            ]

        data_map = {}

        for item in raw_funds:
            code = str(
                item.get("FCODE")
                or item.get("CODE")
                or ""
            ).strip()

            if not code:
                continue

            name = (
                item.get("SHORTNAME")
                or item.get("NAME")
                or code
            )

            nav = (
                item.get("NAV")
                or item.get("DWJZ")
                or item.get("UNITNAV")
            )

            change = (
                item.get("NAVCHGRT")
                or item.get("JZZZL")
                or item.get("RZDF")
            )

            nav_date = (
                item.get("PDATE")
                or item.get("FSRQ")
                or item.get("NAVDATE")
                or ""
            )

            try:
                change_value = (
                    float(change)
                    if change not in (None, "", "--")
                    else 0.0
                )
            except (TypeError, ValueError):
                logging.warning(
                    "基金 %s 涨跌幅格式异常：%r",
                    code,
                    change,
                )
                change_value = 0.0

            try:
                nav_value = (
                    float(nav)
                    if nav not in (None, "", "--")
                    else None
                )
            except (TypeError, ValueError):
                logging.warning(
                    "基金 %s 单位净值格式异常：%r",
                    code,
                    nav,
                )
                nav_value = None

            data_map[code] = {
                "code": code,
                "name": name,
                "nav": nav_value,
                "change": change_value,
                "date": nav_date,
            }

            logging.info(
                "基金获取成功：%s %s，净值=%s，涨跌幅=%.2f%%，日期=%s",
                code,
                name,
                nav_value if nav_value is not None else "--",
                change_value,
                nav_date or "--",
            )

        errors = []

        for code in unique_codes:
            if code not in data_map:
                errors.append({
                    "code": code,
                    "error": "接口未返回该基金数据",
                })

        return data_map, errors

    except requests.Timeout:
        error_message = f"接口请求超时，timeout={TIMEOUT}s"
        logging.error(error_message)

    except requests.RequestException as exc:
        error_message = f"{type(exc).__name__}: {exc}"
        logging.error("基金接口请求失败：%s", error_message)

    except Exception as exc:
        error_message = f"{type(exc).__name__}: {exc}"
        logging.exception("处理基金数据时发生异常：%s", exc)

    return {}, [
        {
            "code": code,
            "error": error_message,
        }
        for code in unique_codes
    ]


# ============================================================
# 消息格式化
# ============================================================

def fmt_change(value):
    if value > 0:
        return f"🔴 +{value:.2f}%"

    if value < 0:
        return f"🟢 {value:.2f}%"

    return f"⚪ {value:.2f}%"


def fmt_nav(value):
    if value is None:
        return "--"

    return f"{value:.4f}"


def fmt_table(title, funds):
    rows = sorted(
        funds,
        key=lambda item: item["change"],
        reverse=True,
    )

    lines = [
        f"### {title}",
        "",
        "| 名称 | 代码 | 单位净值 | 日涨跌幅 | 净值日期 |",
        "|---|---|---:|---:|---|",
    ]

    for fund in rows:
        lines.append(
            f"| **{fund['name']}** "
            f"| `{fund['code']}` "
            f"| {fmt_nav(fund['nav'])} "
            f"| {fmt_change(fund['change'])} "
            f"| {fund['date'] or '--'} |"
        )

    return "\n".join(lines)


def build_content(data_map, errors):
    parts = []

    parts.append(
        "> 数据为基金公司最新公布净值，不是盘中实时估值。"
    )

    for category, codes in FUNDS.items():
        items = [
            data_map[code]
            for code in codes
            if code in data_map
        ]

        if items:
            parts.append(
                fmt_table(category, items)
            )

    if errors:
        error_lines = [
            "**获取失败：**",
            "",
        ]

        for error in errors:
            error_lines.append(
                f"❌ `{error['code']}`: {error['error']}"
            )

        parts.append("\n".join(error_lines))

    if len(parts) == 1:
        parts.append("**未获取到任何基金数据。**")

    return "\n\n".join(parts)


def build_title(success_count, error_count):
    today = datetime.now().strftime("%m-%d")

    if success_count == 0:
        return f"基金净值获取失败｜{today}"

    if error_count > 0:
        return (
            f"基金净值｜{today}｜"
            f"{success_count}成功 {error_count}失败"
        )

    return f"基金每日净值｜{today}"


# ============================================================
# 主程序
# ============================================================

def main():
    all_codes = list(
        dict.fromkeys(
            code
            for codes in FUNDS.values()
            for code in codes
        )
    )

    logging.info(
        "开始获取 %d 只基金...",
        len(all_codes),
    )

    data_map, errors = fetch_funds(all_codes)

    success_count = len(data_map)
    error_count = len(errors)

    logging.info(
        "基金获取完成：成功=%d，失败=%d",
        success_count,
        error_count,
    )

    content = build_content(
        data_map=data_map,
        errors=errors,
    )

    print()
    print(content)
    print()

    # server_key = os.environ.get(
    #     "SERVER_KEY",
    #     "",
    # ).strip()

    # if not server_key:
    #     logging.info(
    #         "未设置 SERVER_KEY，跳过推送（本地模式）"
    #     )
    #     return

    # title = build_title(
    #     success_count,
    #     error_count,
    # )

    # result = push(
    #     sendkey=server_key,
    #     title=title,
    #     content=content,
    # )

    # if result and result.get("code") == 0:
    #     logging.info(
    #         "推送成功：基金成功=%d，失败=%d",
    #         success_count,
    #         error_count,
    #     )
    # else:
    #     logging.error(
    #         "推送失败，Server酱返回：%s",
    #         result,
    #     )


if __name__ == "__main__":
    try:
        main()
    finally:
        session.close()
