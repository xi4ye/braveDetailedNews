#!/usr/bin/env python3
import asyncio
import urllib.parse
import base64
import os
from pydoll.browser import Edge
from pydoll.constants import By
from pydoll.browser.options import ChromiumOptions
from pydoll.commands import PageCommands
from datetime import datetime, timedelta
import re
import random


def extract_real_url(bing_url):
    """从 Bing 跳转链接中提取真实 URL"""
    try:
        parsed = urllib.parse.urlparse(bing_url)
        params = urllib.parse.parse_qs(parsed.query)
        if 'u' in params:
            # u 参数是 base64 编码的真实 URL
            encoded_url = params['u'][0]
            # 移除开头的 "a1" 前缀
            if encoded_url.startswith('a1'):
                encoded_url = encoded_url[2:]
            # 移除可能的 padding
            encoded_url += '=' * ((4 - len(encoded_url) % 4) % 4)
            decoded_url = base64.urlsafe_b64decode(encoded_url).decode('utf-8')
            return decoded_url
    except Exception:
        pass
    return bing_url  # 解析失败返回原 URL

def get_english_date(gap):
    """
    获取当前时间减去指定时间间隔后的时间
    gap为时间间隔
    1小时、1分钟、1天
    返回绝对时间格式
    YYYY-MM-DD
    """
    # 当前时间
    current_time = datetime.now()

    if 'hour' in gap or '小时' in gap:
        time_diff = timedelta(hours=int(re.search(r'(\d+)', gap).group(1)))

    elif 'min' in gap or '分钟' in gap:
        time_diff = timedelta(minutes=int(re.search(r'(\d+)', gap).group(1)))    

    elif 'day' in gap or '天' in gap:
        time_diff = timedelta(days=int(re.search(r'(\d+)', gap).group(1))) 

    one_hour_ago = current_time - time_diff

    # 格式化为 "年月日 小时" 格式
    formatted_time = one_hour_ago.strftime('%Y-%m-%d')

    return formatted_time


def parse_english_date(date_str):
    """
    解析英文日期格式如 "Oct 10, 2018" 或 "Mar 17, 2026"
    返回 YYYY-MM-DD 格式
    """
    months = {
        'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4,
        'may': 5, 'jun': 6, 'jul': 7, 'aug': 8,
        'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12
    }
    
    date_str = date_str.strip()
    
    pattern1 = r'(\w+)\s+(\d+),\s+(\d{4})'
    match = re.search(pattern1, date_str, re.IGNORECASE)
    if match:
        month_name = match.group(1).lower()
        day = int(match.group(2))
        year = int(match.group(3))
        month = months.get(month_name, 1)
        return f"{year}-{month:02d}-{day:02d}"
    
    pattern2 = r'(\d+)\s+(\w+)\s+ago'
    match = re.search(pattern2, date_str, re.IGNORECASE)
    if match:
        num = int(match.group(1))
        unit = match.group(2).lower()
        current = datetime.now()
        if 'hour' in unit or 'min' in unit:
            if 'hour' in unit:
                delta = timedelta(hours=num)
            else:
                delta = timedelta(minutes=num)
        else:
            delta = timedelta(days=num)
        return (current - delta).strftime('%Y-%m-%d')
    
    return None
        
async def crawl_news(news,K=20, proxy=None):
    """
    使用 Pydoll 库的 edge 浏览器爬取新闻
    不会打开浏览器界面
    K为需要爬取的新闻数量
    news为新闻标题
    proxy为代理服务器地址，默认 127.0.0.1:7890
    pip install pydoll-python
    """
    options = ChromiumOptions()

    has_chinese = any('\u4e00' <= c <= '\u9fff' for c in news)
    
    if has_chinese:
        engine_region = "zh-CN"
        accept_language = "zh-CN,zh;q=0.9,en;q=0.8"
        setlang = "zh-CN"
    else:
        engine_region = "en-US"
        accept_language = "en-US,en;q=0.9"
        setlang = "en"
    
    print(f"[info] 市场: {engine_region}")
    print(f"[info] 语言: {setlang}")

    options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')
    
    options.add_argument(f'--accept-lang={accept_language}')
    
    options.add_argument('--disable-blink-features=AutomationControlled')
    options.add_argument('--no-sandbox')
    options.add_argument('--remote-allow-origins=*') 
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--disable-infobars')
    options.add_argument('--disable-extensions')
    options.add_argument('--window-size=1920,1080')
    options.add_argument('--headless=new')# 代理设置（默认不使用代理）
    if proxy is None:
        proxy = None
    if proxy:
        options.add_argument(f'--proxy-server={proxy}')
        print(f"[info] 使用代理: {proxy}")
    else:
        print(f"[info] 不使用代理")
    async with Edge(options=options) as browser:

        page =  await browser.start()

        # 加载 stealth.min.js 并使用 CDP 持久化注入（跨导航生效）
        stealth_js_path = os.path.join(os.path.dirname(__file__), 'stealth.min.js')
        if os.path.exists(stealth_js_path):
            with open(stealth_js_path, 'r', encoding='utf-8') as f:
                stealth_script = f.read()
            await page._execute_command(
                PageCommands.add_script_to_evaluate_on_new_document(
                    source=stealth_script,
                    run_immediately=True,
                )
            )
            print("[info] 已持久化注入 stealth.min.js（跨导航生效）")
        else:
            print("[warning] 未找到 stealth.min.js，使用增强版反检测脚本")
            enhanced_stealth = """
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'automation', { get: () => undefined });
                window.chrome = { 
                    runtime: {},
                    loadTimes: function() {},
                    csi: function() {},
                    app: {}
                };
                Object.defineProperty(navigator, 'plugins', { 
                    get: () => [
                        {name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer'},
                        {name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai'},
                        {name: 'Native Client', filename: 'internal-nacl-plugin'}
                    ]
                });
                Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
                Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
                Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
                Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
                Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 0 });
                
                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications' ?
                        Promise.resolve({ state: Notification.permission }) :
                        originalQuery(parameters)
                );
                
                Object.defineProperty(HTMLCanvasElement.prototype, 'getContext', {
                    value: function() {
                        return {
                            fillStyle: '',
                            font: '',
                            getImageData: function() { return { data: new Uint8ClampedArray(4) }; },
                            measureText: function() { return { width: 0 }; },
                            strokeText: function() {},
                            fillText: function() {}
                        };
                    }
                });
                
                window.Notification = window.Notification || function() {};
            """
            await page._execute_command(
                PageCommands.add_script_to_evaluate_on_new_document(
                    source=enhanced_stealth,
                    run_immediately=True,
                )
            )

        encoded_news = urllib.parse.quote(news)
        
        query_params = {
            "q": news,
            "mkt": engine_region,
            "setlang": setlang,
            "cc": engine_region.split("-")[1] if "-" in engine_region else "CN"
        }
        
        if has_chinese:
            base_domain = "https://cn.bing.com"
        else:
            base_domain = "https://www.bing.com"
        
        search_url = f'{base_domain}/search?{urllib.parse.urlencode(query_params)}'
        print(f"[debug] 搜索 URL: {search_url}")
        
        await page.go_to(search_url)
        
        await asyncio.sleep(random.uniform(5.0, 8.0))
        i = 0
        result = []
        # intnet = await page.find_or_wait_element(By.ID, "est_en",timeout=15)
        # await page.take_screenshot("bing_news.png")
        # await intnet.click()
        await page.take_screenshot("bing_news.png")
        while i < K:
            news_items = await page.find_or_wait_element(By.CLASS_NAME, "b_algo",find_all=True,timeout=15)
            for item in news_items:
                if i >= K:
                    break
                await item.scroll_into_view()
                author = await item.find_or_wait_element(By.CLASS_NAME, "tilk")
                author_text = author._attributes["aria-label"]

                title = await item.find_or_wait_element(By.XPATH, ".//h2/a")
                title_text = await title.text
                if '哔哩哔哩' in title_text:
                    continue
                url_text = title._attributes['href']
                url_text = extract_real_url(url_text)
                abstract = await item.find_or_wait_element(By.XPATH, ".//p")
                abstract_text = (await abstract.text).replace('\u2002', ' ').replace('\xa0', ' ').strip()

                date = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', abstract_text)
                if date:
                    date = f"{date.group(1)}-{date.group(2)}-{date.group(3)}"
                else:
                    date = parse_english_date(abstract_text)
                    if not date:
                        date = datetime.now().strftime('%Y-%m-%d')
                    
                i += 1

                result.append({
                    "title": title_text,
                    "parsed_date": date,
                    "url": url_text,
                    "author": author_text,
                    "description": abstract_text,
                    "source": "Bing"
                })
            
            if i < K:
                try:
                    btn = await page.find_or_wait_element(By.CLASS_NAME, "sb_pagN", timeout=3)
                    await btn.click()
                    await asyncio.sleep(random.uniform(5.0, 8.0))
                except:
                    print("[info] 没有更多结果了")
                    break
                
        await browser.stop()
        print(result)
        return result
if __name__ == "__main__":
    # ==============================
    # Bing 国内版 (CN) - 测试入口
    # ==============================
    news = "法国兴业银行考虑将纽约基地迁出公园大道办公大楼"
    K = 5
    # query_date = "2025-07-30"
    asyncio.run(crawl_news(news, K))
