import requests
import re
import os
import time
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

# --- 配置重试策略 ---
# 定义一个 Retry 对象来配置重试行为
retry_strategy = Retry(
    total=3,  # 总重试次数
    backoff_factor=1,  # 重试之间的等待时间因子 (例如, 第一次重试等 1s, 第二次 2s, 第三次 4s)
    status_forcelist=[429, 500, 502, 503, 504], # 对哪些状态码进行重试
    allowed_methods=["HEAD", "GET", "POST", "OPTIONS"] # 对哪些请求方法进行重试
    # 注意: 对于 POST 请求重试需要谨慎，确保服务端操作是幂等的，或者能够处理重复请求
)

# 创建一个 HTTPAdapter 并挂载 Retry 策略
adapter = HTTPAdapter(max_retries=retry_strategy)

# 创建一个 Session 对象
session = requests.Session()

# 将适配器挂载到 session 上，针对 http 和 https
session.mount("https://", adapter)
session.mount("http://", adapter)

# --- 定义全局超时时间 (秒) ---
REQUEST_TIMEOUT = 10 # 例如，设置超时时间为 10 秒

def sc_send(sendkey, title, desp='', options=None):
    """
    使用 Server酱 推送消息，增加了超时和重试机制。
    """
    if options is None:
        options = {}
    # 判断 sendkey 是否以 'sctp' 开头，并提取数字构造 URL
    if sendkey.startswith('sctp'):
        match = re.match(r'sctp(\d+)t', sendkey)
        if match:
            num = match.group(1)
            url = f'https://{num}.push.ft07.com/send/{sendkey}.send'
        else:
            # 可以选择返回错误信息或抛出异常
            print('错误: 无效的 sctp sendkey 格式')
            return {"code": -1, "message": "无效的 sctp sendkey 格式"}
            # raise ValueError('Invalid sendkey format for sctp')
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

    try:
        # 使用带有重试和超时的 session 发送 POST 请求
        print(f"开始推送消息到: {url.split('/send/')[0]}/send/...") # 隐藏部分 URL
        response = session.post(url, json=params, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()  # 如果发生 HTTP 错误 (4xx or 5xx)，抛出异常
        print("推送成功")
        result = response.json()
        return result
    except requests.exceptions.Timeout as e:
        print(f"错误: 推送消息超时 - {e}")
        return {"code": -2, "message": f"请求超时: {e}"}
    except requests.exceptions.RequestException as e:
        # RequestException 是所有 requests 异常的基类，包括连接错误、超时、HTTP错误等
        print(f"错误: 推送消息失败 (尝试 {retry_strategy.total + 1} 次后) - {e}")
        return {"code": -3, "message": f"请求失败: {e}"}
    except Exception as e:
        # 捕获其他可能的错误，例如 JSON 解析错误
        print(f"错误: 推送过程中发生未知异常 - {e}")
        return {"code": -99, "message": f"未知错误: {e}"}


def fetch_fund_data(fund_codes):
    """
    获取基金数据，增加了超时和重试机制。
    """
    base_url = "https://fundgz.1234567.com.cn/js/{}.js"
    pattern = re.compile(r'jsonpgz\((.*?)\);')
    # 用列表收集所有要推送的消息
    messages = []

    for code in fund_codes:
        url = base_url.format(code)
        try:
            print(f"开始请求基金数据: {url}")
            # 使用带有重试和超时的 session 发送 GET 请求
            response = session.get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status() # 检查 HTTP 错误
            print(f"请求成功: {url}")

            match = pattern.search(response.text)
            if match:
                # 注意：eval() 有安全风险，如果可能，最好使用 json.loads()
                # 但天天基金接口返回的是 JS 代码，eval() 是常用方式，确保来源可信
                try:
                    data = eval(match.group(1)) # 将 JSON 字符串转换为字典
                    gszzl = float(data.get('gszzl', 0)) # 使用 .get() 避免 KeyError
                    fund_name = data.get('name', f'基金{code}')
                    emoji = "📈" if gszzl > 0 else ("📉" if gszzl < 0 else "➖")
                    # 对估值绝对值大于 1 的进行加粗
                    gszzl_text = f"**{data.get('gszzl', 'N/A')}**" if abs(gszzl) > 1 else data.get('gszzl', 'N/A')
                    messages.append(f"- **基金**: {fund_name}, **估值**: {emoji} {gszzl_text}% ")
                except (SyntaxError, ValueError, TypeError) as e:
                    print(f"警告: 解析基金 {code} 数据时出错: {e} - 原始文本: {match.group(1)}")
                    messages.append(f"- 基金 {code}: 数据解析失败 (内容异常)")
                except Exception as e: # 捕获其他eval可能引发的错误
                     print(f"警告: 处理基金 {code} 数据时发生未知错误: {e}")
                     messages.append(f"- 基金 {code}: 处理数据时发生未知错误")

            else:
                print(f"警告: 无法从基金 {code} 的响应中解析数据: {response.text[:100]}...") # 打印部分响应内容帮助调试
                messages.append(f"- 基金 {code}: 数据格式不匹配")

        except requests.exceptions.Timeout as e:
             print(f"错误: 请求基金 {code} 超时 - {e}")
             messages.append(f"- 基金 {code}: 请求超时")
        except requests.exceptions.RequestException as e:
             print(f"错误: 请求基金 {code} 失败 (尝试 {retry_strategy.total + 1} 次后) - {e}")
             messages.append(f"- 基金 {code}: 请求失败 ({e})")
        except Exception as e: # 捕获其他可能的异常
            print(f"错误: 处理基金 {code} 时发生未知异常: {e}")
            messages.append(f"- 基金 {code}: 发生未知错误")
        
        # 可以选择在每次请求后稍微暂停一下，防止请求过于频繁
        # time.sleep(0.1) # 暂停 0.1 秒

    # 将所有消息合并成一个字符串，使用 Markdown 的换行符
    return "  \n".join(messages)


# --- 主程序逻辑 ---

# 读取环境变量 SERVER_KEY
server_key = os.environ.get("SERVER_KEY")
if not server_key:
    print("错误: 环境变量 SERVER_KEY 未设置!")
    # 可以选择退出程序或使用默认值
    exit(1) # 或者 server_key = "YOUR_DEFAULT_KEY"

# 示例基金代码列表
fund_list = ["020670", "016942", "159934"] # 你的基金代码

print("--- 开始获取基金数据 ---")
all_messages = fetch_fund_data(fund_list)

if not all_messages:
    print("未能获取到任何基金数据，跳过推送。")
else:
    print("\n--- 开始推送基金数据 ---")
    # 使用 sendkey 推送所有的内容
    push_title = "基金每日估值推送" # 可以自定义推送标题
    result = sc_send(server_key, push_title, all_messages)

    print("\n--- 推送结果 ---")
    print(result)

    # 可以根据推送结果进行判断
    if result and result.get("code") == 0:
        print("推送成功！")
    else:
        print("推送失败或发生错误。")

print("\n--- 程序结束 ---")
