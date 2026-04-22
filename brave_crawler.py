#!/usr/bin/env python3
import asyncio
import os
import urllib.parse
from pydoll.browser import Edge
from pydoll.constants import By
from pydoll.browser.options import ChromiumOptions
from datetime import datetime, timedelta
import re
from pydoll.elements.mixins.find_elements_mixin import FindElementsMixin
from brave_captcha_solver import solve_brave_captcha

SCREENSHOT_DIR = "screenshots"
TAKE_SCREENSHOT = True

os.makedirs(SCREENSHOT_DIR, exist_ok=True)


original_execute_command = FindElementsMixin._execute_command

async def _patched_execute_command(self, command):
    handler, session_id = self._resolve_routing()
    if session_id:
        command['sessionId'] = session_id
    return await handler.execute_command(command, timeout=60)

FindElementsMixin._execute_command = _patched_execute_command

def extract_and_convert_date(text):
    """
    从文本开头提取日期（相对/中文绝对/英文），转换为YYYY-MM-DD格式的绝对日期
    :param text: 待处理文本
    :return: (格式化绝对日期字符串, 日期来源标记)，匹配失败日期来源为None
    """
    text_stripped = text.strip()

    # 月份映射
    month_map = {
        'january': 1, 'jan': 1,
        'february': 2, 'feb': 2,
        'march': 3, 'mar': 3,
        'april': 4, 'apr': 4,
        'may': 5,
        'june': 6, 'jun': 6,
        'july': 7, 'jul': 7,
        'august': 8, 'aug': 8,
        'september': 9, 'sep': 9,
        'october': 10, 'oct': 10,
        'november': 11, 'nov': 11,
        'december': 12, 'dec': 12
    }

    # -------------------------- 1. 匹配英文绝对日期 (January 21, 2026) --------------------------
    en_absolute_pattern = r'^([A-Za-z]+)\s+(\d{1,2}),?\s*(\d{4})\s*-'
    en_absolute_match = re.match(en_absolute_pattern, text_stripped, re.IGNORECASE)
    if en_absolute_match:
        month_name = en_absolute_match.group(1).lower()
        day = en_absolute_match.group(2)
        year = en_absolute_match.group(3)

        if month_name in month_map:
            month = str(month_map[month_name]).zfill(2)
            day = day.zfill(2)
            return f'{year}-{month}-{day}', 'en_absolute'

    # -------------------------- 2. 匹配英文相对日期 (3 weeks ago, 2 days ago) --------------------------
    en_relative_pattern = r'^(\d+)\s*(day|days|week|weeks|month|months|year|years)\s+ago\s*-'
    en_relative_match = re.match(en_relative_pattern, text_stripped, re.IGNORECASE)
    if en_relative_match:
        num = int(en_relative_match.group(1))
        unit = en_relative_match.group(2).lower()

        today = datetime.now()
        if 'day' in unit:
            target_date = today - timedelta(days=num)
        elif 'week' in unit:
            target_date = today - timedelta(weeks=num)
        elif 'month' in unit:
            target_date = today - timedelta(days=num*30)
        elif 'year' in unit:
            target_date = today - timedelta(days=num*365)
        else:
            target_date = today

        return target_date.strftime('%Y-%m-%d'), 'en_relative'

    # -------------------------- 3. 匹配中文相对日期（如1周前、2天前、3个月前、1年前） --------------------------
    cn_relative_pattern = r'^(\d+)(周|天|月|年)前\s*-'
    cn_relative_match = re.match(cn_relative_pattern, text_stripped)
    if cn_relative_match:
        num = int(cn_relative_match.group(1))
        unit = cn_relative_match.group(2)

        today = datetime.now()
        if unit == '天':
            target_date = today - timedelta(days=num)
        elif unit == '周':
            target_date = today - timedelta(weeks=num)
        elif unit == '月':
            target_date = today - timedelta(days=num*30)
        elif unit == '年':
            target_date = today - timedelta(days=num*365)
        else:
            target_date = today

        return target_date.strftime('%Y-%m-%d'), 'cn_relative'

    # -------------------------- 4. 匹配中文绝对日期（如2025年8月8日、2026年12月31日） --------------------------
    cn_absolute_pattern = r'^(\d{4})年(\d{1,2})月(\d{1,2})日\s*-'
    cn_absolute_match = re.match(cn_absolute_pattern, text_stripped)
    if cn_absolute_match:
        year = cn_absolute_match.group(1)
        month = cn_absolute_match.group(2).zfill(2)
        day = cn_absolute_match.group(3).zfill(2)
        return f'{year}-{month}-{day}', 'cn_absolute'

    # -------------------------- 5. 匹配中文简短日期（如1月21日，没有年份） --------------------------
    cn_short_pattern = r'^(\d{1,2})月(\d{1,2})日'
    cn_short_match = re.match(cn_short_pattern, text_stripped)
    if cn_short_match:
        today = datetime.now()
        year = today.year
        month = cn_short_match.group(1).zfill(2)
        day = cn_short_match.group(2).zfill(2)

        # 如果这个日期在今天之后，说明是去年的
        parsed_date = datetime(year, int(month), int(day))
        if parsed_date > today:
            year -= 1

        return f'{year}-{month}-{day}', 'cn_short'

    # 无匹配结果
    return None, None

async def crawl_news(news,K=20):
    """
    使用 Pydoll 库的 edge 浏览器爬取新闻
    不会打开浏览器界面
    K为需要爬取的新闻数量
    news为新闻标题
    pip install pydoll-python
    """
    options = ChromiumOptions()

    # 无头模式 (可以注释掉看浏览器窗口)
    options.headless = True

    # 反检测选项
    options.add_argument('--disable-blink-features=AutomationControlled')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--disable-infobars')
    options.add_argument('--disable-extensions')
    options.add_argument('--disable-popup-blocking')
    options.add_argument('--disable-notifications')
    options.add_argument('--ignore-certificate-errors')
    options.add_argument('--ignore-ssl-errors')
    options.add_argument('--disable-gpu')
    options.add_argument('--window-size=1920,1080')

    # 自定义 User-Agent
    custom_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    options.add_argument(f'--user-agent={custom_ua}')
    
    # 语言和地区设置
    options.add_argument('--accept-lang=zh-CN,zh;q=0.9,en;q=0.8')

    # WebRTC 泄漏防护
    options.webrtc_leak_protection = True

    # 代理 (如果不需要可以注释掉)
    options.add_argument('--proxy-server=127.0.0.1:7890')

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    screenshot_prefix = f"{SCREENSHOT_DIR}/{timestamp}"

    async with Edge(options=options) as browser:
        page = await browser.start()
        
        # 加载并注入完整的 stealth.min.js 反检测脚本
        import os
        stealth_js_path = os.path.join(os.path.dirname(__file__), 'stealth.min.js')
        if os.path.exists(stealth_js_path):
            with open(stealth_js_path, 'r', encoding='utf-8') as f:
                stealth_script = f.read()
            await page.execute_script(stealth_script)
            print("[info] 已注入完整的 stealth.min.js 反检测脚本")
        else:
            print("[warning] 未找到 stealth.min.js，使用简化版本")
            # 简化备用方案
            await page.execute_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                window.chrome = { runtime: {} };
                Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
                Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
            """)
        
        i = 0
        j = 0
        result = []

        while i < K:
            # 用英文引号包裹搜索词，并进行 URL 编码
            quoted_news = f'"{news}"'
            encoded_news = urllib.parse.quote(quoted_news)
            search_url = f'https://search.brave.com/search?q={encoded_news}&source=web&offset={j}'
            await page.go_to(search_url)

            await asyncio.sleep(3)

            max_wait = 60
            waited = 0
            found = False
            captcha_clicked = False
            captcha_solver_tried = False

            print(f"[页面 {j}] 等待搜索结果加载...")

            while waited < max_wait and not found:
                await asyncio.sleep(2)
                waited += 2

                # 检查是否有搜索结果
                try:
                    test_elem = await page.find_element(By.CLASS_NAME, "result-wrapper")
                    if test_elem:
                        found = True
                        print(f"[页面 {j}] ✅ 找到搜索结果！")
                        break
                except:
                    pass

                try:
                    test_elem = await page.find_element(By.CLASS_NAME, "search-snippet-title")
                    if test_elem:
                        found = True
                        print(f"[页面 {j}] ✅ 找到搜索结果！")
                        break
                except:
                    pass

                # 检查是否有验证码
                try:
                    turnstile_elem = await page.find_element(By.CSS_SELECTOR, 'iframe[title*="captcha"], iframe[title*="challenge"]')
                    if turnstile_elem and not captcha_clicked:
                        print(f"[页面 {j}] 发现验证码，尝试点击...")
                        try:
                            selectors = [
                                'button[class*="size--medium"]',
                                '.size--medium',
                                'input[type="checkbox"]',
                                'button',
                                'div[role="button"]'
                            ]
                            for selector in selectors:
                                try:
                                    elem = await page.find_element(By.CSS_SELECTOR, selector)
                                    if elem:
                                        await elem.click()
                                        print(f"[页面 {j}] ✅ 验证码点击成功: {selector}")
                                        captcha_clicked = True
                                        await asyncio.sleep(5)
                                        waited += 5
                                        break
                                except:
                                    continue
                        except Exception as e:
                            print(f"[页面 {j}] ⚠️ 验证码点击失败: {e}")
                    elif captcha_clicked:
                        print(f"[页面 {j}] 等待验证完成... ({waited}s)")
                        await asyncio.sleep(3)
                        waited += 3
                except:
                    pass

                # 如果等待超过30秒且还没有找到结果，尝试调用验证码解决器
                if waited >= 30 and not found and not captcha_solver_tried:
                    print(f"[页面 {j}] 等待超时，尝试调用验证码解决器...")
                    try:
                        success = await solve_brave_captcha(page, timeout=15)
                        captcha_solver_tried = True
                        if success:
                            print(f"[页面 {j}] ✅ 验证码解决器处理成功！")
                            await asyncio.sleep(5)
                            waited += 5
                        else:
                            print(f"[页面 {j}] ⚠️ 验证码解决器处理失败")
                    except Exception as e:
                        print(f"[页面 {j}] ⚠️ 调用验证码解决器出错: {e}")
                        captcha_solver_tried = True

            if TAKE_SCREENSHOT:
                screenshot_path = f"{screenshot_prefix}_page{j}.png"
                try:
                    await page.take_screenshot(path=screenshot_path)
                    print(f"[页面 {j}] [截图] 已保存: {screenshot_path}")
                except Exception as e:
                    print(f"[页面 {j}] [截图] 失败: {e}")

            try:
                news_items = await page.find_or_wait_element(By.CLASS_NAME, "result-wrapper", find_all=True, timeout=20)
            except Exception as e:
                print(f"[页面 {j}] [警告] 未找到搜索结果: {e}")
                if TAKE_SCREENSHOT:
                    error_screenshot = f"{screenshot_prefix}_error_{j}.png"
                    try:
                        await page.take_screenshot(path=error_screenshot)
                        print(f"[页面 {j}] [截图] 错误页面已保存: {error_screenshot}")
                    except:
                        pass
                j += 1
                continue

            for item in news_items:
                if i >= K:
                    break
                try:
                    await item.scroll_into_view()
                    author = await item.find_or_wait_element(By.CLASS_NAME, "desktop-small-semibold")
                    author_text = await author.text

                    title = await item.find_or_wait_element(By.CLASS_NAME, "search-snippet-title")
                    title_text = await title.text
                    url = await item.find_or_wait_element(By.XPATH, "//a")
                    url_text = url._attributes['href']

                    abstract = await item.find_or_wait_element(By.CLASS_NAME, "content")
                    abstract_text = await abstract.text

                    date, date_source = extract_and_convert_date(abstract_text)
                    if not date:
                        date = datetime.now().strftime('%Y-%m-%d')
                        date_source = "default_today"
                    i += 1

                    result.append({
                        "title": title_text,
                        "parsed_date": date,
                        "date_source": date_source,
                        "url": url_text,
                        "author": author_text,
                        "description": abstract_text,
                        "source": "Brave Search"
                    })
                    print(f"[页面 {j}] [新闻 {i}] 日期来源: {date_source} -> {date}")
                except Exception as e:
                    print(f"[页面 {j}] [警告] 解析新闻条目失败: {e}")
                    continue

            j += 1

        if TAKE_SCREENSHOT and result:
            final_screenshot = f"{screenshot_prefix}_final.png"
            try:
                await page.take_screenshot(path=final_screenshot)
                print(f"[截图] 最终页面已保存: {final_screenshot}")
            except:
                pass

        await browser.stop()
        print(f"[完成] 爬取完成，共获取 {len(result)} 条新闻")
        return result
if __name__ == "__main__":
    news = "近日，特朗普声称将与中国的关税提高到80%"
    K = 5
    # query_date = "2025-07-30"
    asyncio.run(crawl_news(news, K))
