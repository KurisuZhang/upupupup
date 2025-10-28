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
    # 使用 set 去除重复的基金代码，避免重复请求
    unique_codes = list(set(fund_codes))
    with ThreadPoolExecutor(max_workers=5) as executor:
        future_to_code = {executor.submit(fetch_single_fund, code): code for code in unique_codes}
        for future in as_completed(future_to_code):
            results.append(future.result())
    return results


# ===== 【修改后】的格式化函数：为单个分类生成表格 =====
def format_category_table(category_title, funds_in_category):
    """
    为指定的基金类别生成一个Markdown表格。

    :param category_title: 基金类别的名称 (e.g., '黄金', '指数')
    :param funds_in_category: 该类别下已获取到的基金数据列表
    :return: 格式化后的Markdown字符串
    """
    # 按涨跌幅降序
    funds_sorted = sorted(funds_in_category, key=lambda x: x.get("gszzl", 0), reverse=True)

    # 表格标题和头部
    table_header = f"### {category_title}\n| 基金名称 | 代码 | 涨跌幅 |\n|---|---|---|"
    table_rows = []

    for f in funds_sorted:
        gszzl = f["gszzl"]

        # 用 Emoji 来模拟颜色：🟢 绿色跌，🔴 红色涨，⚪ 灰色平
        if gszzl > 0:
            gszzl_text = f"🔴 +{gszzl:.2f}%"
        elif gszzl < 0:
            gszzl_text = f"🟢 {gszzl:.2f}%"
        else:
            gszzl_text = f"⚪ {gszzl:.2f}%"

        table_rows.append(f"| **{f['name']}** | `{f['code']}` | {gszzl_text} |")

    # 拼接完整的分类表格
    return f"{table_header}\n" + "\n".join(table_rows)


# ===== 【修改后】的主程序 =====
if __name__ == "__main__":
    server_key = os.environ.get("SERVER_KEY")
    if not server_key:
        logging.error("环境变量 SERVER_KEY 未设置！")
        exit(1)

    # 1. 使用字典来定义基金列表
    fund_dict = {
        '黄金': ["159934"],
        '指数': ["011609", "510210", "016942"],  # 示例中 016942 重复，代码会自动处理
        '板块': ["018412", "005693"],
        '债券': ["017763", "011555"],
        '笨狗': ["400015", "001951", "519150"]
    }

    # 2. 从字典中提取所有需要查询的基金代码
    all_fund_codes = [code for codes in fund_dict.values() for code in codes]

    logging.info("开始获取基金数据...")
    all_results = fetch_fund_data(all_fund_codes)

    # 3. 将获取结果分类，方便后续查找
    results_map = {res['code']: res for res in all_results if 'error' not in res}
    error_results = [res for res in all_results if 'error' in res]

    # 4. 循环处理每个分类，生成各自的表格
    final_message_parts = []
    for category, codes in fund_dict.items():
        # 找出当前分类下成功获取到数据的基金
        # 使用 set(codes) 来处理 fund_dict 中可能存在的重复代码
        category_data = [results_map[code] for code in set(codes) if code in results_map]

        # 如果该分类下有数据，则为其生成表格
        if category_data:
            table_str = format_category_table(category, category_data)
            final_message_parts.append(table_str)

    # 5. 如果有获取失败的基金，统一添加到末尾
    if error_results:
        error_rows = ["\n**获取失败的基金：**"]
        for f in error_results:
            error_rows.append(f"❌ **基金 {f['code']}**: {f['error']}")
        final_message_parts.append("\n".join(error_rows))

    # 6. 拼接所有内容，并推送
    # 使用 "\n\n" 分隔每个表格，使显示效果更佳
    push_content = "\n\n".join(final_message_parts)

    print(push_content)

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
