import requests
import re
import os
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

# ===== 日志配置 =====
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S"
)

# ===== HTTP Session 配置 =====
def get_session():
    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "POST", "OPTIONS"]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    s = requests.Session()
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s

session = get_session()
REQUEST_TIMEOUT = 10

# ===== Server酱 推送函数 =====
def sc_send(sendkey, title, desp='', options=None):
    if options is None:
        options = {}

    if sendkey.startswith('sctp'):
        match = re.match(r'sctp(\d+)t', sendkey)
        if match:
            num = match.group(1)
            url = f'https://{num}.push.ft07.com/send/{sendkey}.send'
        else:
            logging.error("无效的 sctp sendkey 格式")
            return {"code": -1, "message": "无效的 sctp sendkey 格式"}
    else:
        url = f'https://sctapi.ftqq.com/{sendkey}.send'

    params = {'title': title, 'desp': desp, **options}
    headers = {'Content-Type': 'application/json;charset=utf-8'}

    try:
        logging.info(f"推送消息到 {url.split('/send/')[0]}/send/...")
        response = session.post(url, json=params, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logging.error(f"推送失败: {e}")
        return {"code": -3, "message": str(e)}

# ===== 单个基金数据获取 =====
def fetch_single_fund(code):
    base_url = f"https://fundgz.1234567.com.cn/js/{code}.js"
    pattern = re.compile(r'jsonpgz\((.*?)\);')

    try:
        resp = session.get(base_url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        match = pattern.search(resp.text)
        if not match:
            return {"code": code, "error": "数据格式不匹配"}

        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError as e:
            return {"code": code, "error": f"JSON解析失败: {e}"}

        gszzl = float(data.get('gszzl', 0))
        return {
            "code": code,
            "name": data.get('name', f"基金{code}"),
            "gszzl": gszzl
        }
    except requests.exceptions.RequestException as e:
        return {"code": code, "error": f"请求失败: {e}"}

# ===== 并发获取基金数据 =====
def fetch_fund_data(fund_codes):
    results = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        future_to_code = {executor.submit(fetch_single_fund, code): code for code in fund_codes}
        for future in as_completed(future_to_code):
            results.append(future.result())
    return results

# ===== 格式化推送消息 =====
def format_messages(funds):
    # 按涨跌幅降序
    funds_sorted = sorted(funds, key=lambda x: x.get("gszzl", 0) or 0, reverse=True)
    messages = []
    for f in funds_sorted:
        if "error" in f:
            messages.append(f"- 基金 {f['code']}: ❌ {f['error']}")
        else:
            emoji = "📈" if f["gszzl"] > 0 else ("📉" if f["gszzl"] < 0 else "➖")
            gszzl_text = f"**{f['gszzl']}**" if abs(f['gszzl']) > 1 else f"{f['gszzl']}"
            messages.append(f"- **{f['name']}** ({f['code']}): {emoji} {gszzl_text}%")
    return "  \n".join(messages)

# ===== 主程序 =====
if __name__ == "__main__":
    server_key = os.environ.get("SERVER_KEY")
    if not server_key:
        logging.error("环境变量 SERVER_KEY 未设置！")
        exit(1)

    fund_list = ["020670", "016942", "159934", "011609", "005693"]

    logging.info("开始获取基金数据...")
    all_results = fetch_fund_data(fund_list)
    push_content = format_messages(all_results)

    if not push_content.strip():
        logging.warning("未获取到任何基金数据，跳过推送。")
        exit(0)

    logging.info("开始推送基金数据...")
    push_title = "基金每日估值推送"
    result = sc_send(server_key, push_title, push_content)

    if result.get("code") == 0:
        logging.info("推送成功！")
    else:
        logging.error(f"推送失败: {result}")
