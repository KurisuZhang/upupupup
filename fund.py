import json
import logging
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor

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

TIMEOUT = 15

# 兼容：
# jsonpgz({...});
# jsonpgz ( {...} )
JSONP_RE = re.compile(
    r"jsonpgz\s*\(\s*(\{.*?\})\s*\)\s*;?",
    re.DOTALL,
)


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

def create_fund_session():
    """
    创建基金数据请求 Session。

    每个线程单独创建 Session，避免多个线程共享同一个 Session
    可能产生的线程安全问题。
    """
    session = requests.Session()

    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/150.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://fund.eastmoney.com/",
        "Connection": "keep-alive",
    })

    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET"]),
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


# ============================================================
# Server 酱推送
# ============================================================

def push(sendkey, title, content):
    """
    推送消息到 Server 酱。
    """
    if sendkey.startswith("sctp"):
        match = re.match(r"sctp(\d+)t", sendkey)

        if not match:
            logging.error("无效的 Turbo sendkey: %s", sendkey)
            return None

        server_number = match.group(1)
        url = (
            f"https://{server_number}.push.ft07.com/"
            f"send/{sendkey}.send"
        )
    else:
        url = f"https://sctapi.ftqq.com/{sendkey}.send"

    try:
        response = requests.post(
            url,
            json={
                "title": title,
                "desp": content,
            },
            headers={
                "User-Agent": "Mozilla/5.0",
                "Content-Type": "application/json",
            },
            timeout=TIMEOUT,
        )

        response.raise_for_status()

        try:
            result = response.json()
        except ValueError:
            logging.error(
                "Server酱返回的不是 JSON，响应内容: %r",
                response.text[:300],
            )
            return None

        return result

    except requests.RequestException as exc:
        logging.error("Server酱推送请求失败: %s", exc)
        return None

    except Exception as exc:
        logging.exception("Server酱推送出现未知异常: %s", exc)
        return None


# ============================================================
# 基金数据获取
# ============================================================

def fetch_one(code):
    """
    获取单只基金的实时估值数据。

    对 HTTP 200 但正文不是 JSONP 的情况额外重试。
    """
    url = f"https://fundgz.1234567.com.cn/js/{code}.js"
    last_error = "未知错误"

    for attempt in range(1, 4):
        session = None

        try:
            session = create_fund_session()

            response = session.get(
                url,
                params={
                    # 防止 CDN 缓存
                    "rt": int(time.time() * 1000),
                },
                headers={
                    "Referer": f"https://fund.eastmoney.com/{code}.html",
                },
                timeout=TIMEOUT,
                allow_redirects=True,
            )

            response.raise_for_status()

            text = response.text.strip()
            content_type = response.headers.get("Content-Type", "")

            if not text:
                last_error = (
                    f"接口返回空内容，status={response.status_code}, "
                    f"type={content_type}"
                )

                logging.warning(
                    "基金 %s 第 %d 次获取失败：%s",
                    code,
                    attempt,
                    last_error,
                )

            else:
                match = JSONP_RE.search(text)

                if not match:
                    body_preview = (
                        text[:300]
                        .replace("\r", " ")
                        .replace("\n", " ")
                    )

                    last_error = (
                        f"非JSONP响应，status={response.status_code}, "
                        f"type={content_type}, "
                        f"body={body_preview!r}"
                    )

                    logging.warning(
                        "基金 %s 第 %d 次响应格式异常：%s",
                        code,
                        attempt,
                        last_error,
                    )

                else:
                    data = json.loads(match.group(1))

                    if not isinstance(data, dict):
                        raise ValueError(
                            f"JSONP 内部数据不是对象: {type(data).__name__}"
                        )

                    name = data.get("name")
                    gszzl = data.get("gszzl")

                    if not name:
                        raise ValueError(
                            f"响应缺少基金名称，原始数据: {data}"
                        )

                    if gszzl in (None, ""):
                        raise ValueError(
                            f"响应缺少估算涨跌幅，原始数据: {data}"
                        )

                    try:
                        change = float(gszzl)
                    except (TypeError, ValueError) as exc:
                        raise ValueError(
                            f"涨跌幅无法转换成数字: {gszzl!r}"
                        ) from exc

                    logging.info(
                        "基金获取成功：%s %s，估算涨跌幅 %.2f%%",
                        code,
                        name,
                        change,
                    )

                    return {
                        "code": code,
                        "name": name,
                        "gszzl": change,
                        "gztime": data.get("gztime"),
                        "dwjz": data.get("dwjz"),
                        "gsz": data.get("gsz"),
                    }

        except requests.Timeout:
            last_error = f"请求超时，timeout={TIMEOUT}s"

            logging.warning(
                "基金 %s 第 %d 次请求超时",
                code,
                attempt,
            )

        except requests.RequestException as exc:
            last_error = (
                f"{type(exc).__name__}: {exc}"
            )

            logging.warning(
                "基金 %s 第 %d 次网络请求失败：%s",
                code,
                attempt,
                last_error,
            )

        except json.JSONDecodeError as exc:
            last_error = (
                f"JSON解析失败: {exc}"
            )

            logging.warning(
                "基金 %s 第 %d 次 JSON 解析失败：%s",
                code,
                attempt,
                exc,
            )

        except Exception as exc:
            last_error = (
                f"{type(exc).__name__}: {exc}"
            )

            logging.warning(
                "基金 %s 第 %d 次处理失败：%s",
                code,
                attempt,
                last_error,
            )

        finally:
            if session is not None:
                session.close()

        if attempt < 3:
            # 避免所有线程在同一时刻重新请求
            sleep_seconds = random.uniform(1.5, 3.0) * attempt

            logging.info(
                "基金 %s 将在 %.1f 秒后重试",
                code,
                sleep_seconds,
            )

            time.sleep(sleep_seconds)

    return {
        "code": code,
        "error": last_error,
    }


def fetch_funds(codes):
    """
    并发获取多只基金。

    降低到两个并发，减少触发接口风控的概率。
    """
    success = []
    errors = []

    # 去重并保持原始顺序
    unique_codes = list(dict.fromkeys(codes))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = pool.map(fetch_one, unique_codes)

        for result in results:
            if "error" in result:
                errors.append(result)
            else:
                success.append(result)

    data_map = {
        fund["code"]: fund
        for fund in success
    }

    return data_map, errors


# ============================================================
# 消息格式化
# ============================================================

def fmt_change(value):
    """
    按国内基金习惯：
    红色表示上涨，绿色表示下跌。
    """
    if value > 0:
        return f"🔴 +{value:.2f}%"

    if value < 0:
        return f"🟢 {value:.2f}%"

    return f"⚪ {value:.2f}%"


def fmt_table(title, funds):
    """
    将基金列表格式化为 Markdown 表格。
    """
    rows = sorted(
        funds,
        key=lambda item: item["gszzl"],
        reverse=True,
    )

    lines = [
        f"### {title}",
        "",
        "| 名称 | 代码 | 估算涨跌幅 |",
        "|---|---|---:|",
    ]

    for fund in rows:
        lines.append(
            f"| **{fund['name']}** "
            f"| `{fund['code']}` "
            f"| {fmt_change(fund['gszzl'])} |"
        )

    return "\n".join(lines)


def build_content(data_map, errors):
    """
    生成最终推送内容。
    """
    parts = []

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

    if not parts:
        return "**未获取到任何基金数据。**"

    return "\n\n".join(parts)


def build_push_title(success_count, error_count):
    """
    根据获取结果生成更准确的推送标题。
    """
    if success_count == 0:
        return "基金估值获取失败"

    if error_count > 0:
        return (
            f"基金每日估值："
            f"{success_count}只成功，{error_count}只失败"
        )

    return f"基金每日估值：{success_count}只"


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

    # server_key = os.environ.get("SERVER_KEY", "").strip()

    # if not server_key:
    #     logging.info(
    #         "未设置 SERVER_KEY，跳过推送（本地模式）"
    #     )
    #     return

    # title = build_push_title(
    #     success_count=success_count,
    #     error_count=error_count,
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
    #         "推送失败，Server酱返回: %s",
    #         result,
    #     )


if __name__ == "__main__":
    main()
