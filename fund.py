import requests
import re
import os
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S")

# --- HTTP ---
session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0"})
adapter = HTTPAdapter(max_retries=Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504]))
session.mount("https://", adapter)
session.mount("http://", adapter)
TIMEOUT = 10
JSONP_RE = re.compile(r"jsonpgz\((.*?)\);")


# --- Server酱 推送 ---
def push(sendkey, title, content):
    if sendkey.startswith("sctp"):
        m = re.match(r"sctp(\d+)t", sendkey)
        url = f"https://{m.group(1)}.push.ft07.com/send/{sendkey}.send" if m else None
    else:
        url = f"https://sctapi.ftqq.com/{sendkey}.send"
    if not url:
        return logging.error("无效 sendkey")
    try:
        r = session.post(url, json={"title": title, "desp": content}, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logging.error(f"推送失败: {e}")


# --- 基金数据获取（天天基金 JSONP） ---
def fetch_one(code):
    try:
        r = session.get(f"https://fundgz.1234567.com.cn/js/{code}.js", timeout=TIMEOUT)
        r.raise_for_status()
        m = JSONP_RE.search(r.text)
        if not m:
            return {"code": code, "error": "数据格式异常"}
        d = json.loads(m.group(1))
        return {"code": code, "name": d["name"], "gszzl": float(d.get("gszzl", 0))}
    except Exception as e:
        return {"code": code, "error": str(e)}


def fetch_funds(codes):
    ok, err = [], []
    with ThreadPoolExecutor(max_workers=5) as pool:
        for r in pool.map(fetch_one, list(set(codes))):
            (err if "error" in r else ok).append(r)
    return {f["code"]: f for f in ok}, err


# --- 格式化 ---
def fmt_change(v):
    if v > 0:   return f"🔴 +{v:.2f}%"
    if v < 0:   return f"🟢 {v:.2f}%"
    return f"⚪ {v:.2f}%"


def fmt_table(title, funds):
    rows = sorted(funds, key=lambda x: x["gszzl"], reverse=True)
    lines = [f"### {title}", "| 名称 | 代码 | 涨跌幅 |", "|---|---|---|"]
    for f in rows:
        lines.append(f"| **{f['name']}** | `{f['code']}` | {fmt_change(f['gszzl'])} |")
    return "\n".join(lines)


# --- 配置 ---
FUNDS = {
    '黄金': ["159934"],
    '指数': ["011609"],
    '板块': ["018412", "005693", "026265"],
    '债券': ["017763", "011555"],
}


# --- 主程序 ---
if __name__ == "__main__":
    all_codes = list({c for cs in FUNDS.values() for c in cs})
    logging.info(f"获取 {len(all_codes)} 只基金...")

    data_map, errors = fetch_funds(all_codes)

    parts = []
    for cat, codes in FUNDS.items():
        items = [data_map[c] for c in codes if c in data_map]
        if items:
            parts.append(fmt_table(cat, items))

    if errors:
        parts.append("**获取失败：**\n" + "\n".join(f"❌ `{e['code']}`: {e['error']}" for e in errors))

    content = "\n\n".join(parts)
    print(content)

    key = os.environ.get("SERVER_KEY")
    if not key:
        logging.info("未设置 SERVER_KEY，跳过推送（本地模式）")
    elif not content.strip():
        logging.warning("无数据，跳过推送")
    else:
        r = push(key, "基金每日估值推送", content)
        logging.info("推送成功！" if r and r.get("code") == 0 else f"推送失败: {r}")
