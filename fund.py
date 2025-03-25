import requests
import re


def sc_send(sendkey, title, desp='', options=None):
    if options is None:
        options = {}
    # 判断 sendkey 是否以 'sctp' 开头，并提取数字构造 URL
    if sendkey.startswith('sctp'):
        match = re.match(r'sctp(\d+)t', sendkey)
        if match:
            num = match.group(1)
            url = f'https://{num}.push.ft07.com/send/{sendkey}.send'
        else:
            raise ValueError('Invalid sendkey format for sctp')
    else:
        url = f'https://sctapi.ftqq.com/{sendkey}.send'
    params = {
        'title': title,
        'desp': desp,
        **options
    }
    headers = {
        'Content-Type': 'application/json;charset=utf-8'
    }
    response = requests.post(url, json=params, headers=headers)
    result = response.json()
    return result


def fetch_fund_data(fund_codes):
    base_url = "https://fundgz.1234567.com.cn/js/{}.js"
    pattern = re.compile(r'jsonpgz\((.*?)\);')
    # 用列表收集所有要推送的消息
    messages = []

    for code in fund_codes:
        url = base_url.format(code)
        try:
            response = requests.get(url)
            if response.status_code == 200:
                match = pattern.search(response.text)
                if match:
                    data = eval(match.group(1))  # 将 JSON 字符串转换为字典
                    color = "red" if float(data['gszzl']) > 0 else "green"
                    messages.append(
                        f"- **基金**: {data['name']}, **估值**: <span style='color:{color};'>{data['gszzl']}%</span>")
                else:
                    messages.append(f"基金 {code} 数据解析失败")
            else:
                messages.append(f"基金 {code} 请求失败，状态码: {response.status_code}")
        except Exception as e:
            messages.append(f"基金 {code} 请求异常: {e}")

    # 将所有消息合并成一个字符串，以换行分隔
    return "  ".join(messages)


# 示例基金代码列表
fund_list = ["020670", "016942", "159934"]
# 获取所有基金信息的内容
all_messages = fetch_fund_data(fund_list)
# 使用 sendkey "123456" 推送一次所有的内容
result = sc_send("SCT4401TgEUknsi92Gxus8cuwvzAPVDf", "基金数据推送", all_messages)
print(result)
