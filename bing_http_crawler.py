#!/usr/bin/env python3
# Bing 纯 HTTP 爬虫 (CN/EN)
# 使用 requests + BeautifulSoup 实现，不依赖浏览器
# 注意：国际版需要海外代理，否则会被 302 重定向到 cn.bing.com

import requests
import urllib.parse
import base64
import re
import time
from datetime import datetime, timedelta
from bs4 import BeautifulSoup


def extract_real_url(bing_url):
    try:
        parsed = urllib.parse.urlparse(bing_url)
        params = urllib.parse.parse_qs(parsed.query)
        if 'u' in params:
            encoded_url = params['u'][0]
            if encoded_url.startswith('a1'):
                encoded_url = encoded_url[2:]
            encoded_url += '=' * ((4 - len(encoded_url) % 4) % 4)
            decoded_url = base64.urlsafe_b64decode(encoded_url).decode('utf-8')
            return decoded_url
    except Exception:
        pass
    return bing_url


def parse_english_date(date_str):
    months = {
        'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4,
        'may': 5, 'jun': 6, 'jul': 7, 'aug': 8,
        'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12
    }
    date_str = date_str.strip()
    
    match = re.search(r'(\w+)\s+(\d+),\s+(\d{4})', date_str, re.IGNORECASE)
    if match:
        month_name = match.group(1).lower()
        day = int(match.group(2))
        year = int(match.group(3))
        month = months.get(month_name, 1)
        return f"{year}-{month:02d}-{day:02d}"
    
    match = re.search(r'(\d+)\s+(\w+)\s+ago', date_str, re.IGNORECASE)
    if match:
        num = int(match.group(1))
        unit = match.group(2).lower()
        current = datetime.now()
        if 'hour' in unit:
            delta = timedelta(hours=num)
        elif 'min' in unit:
            delta = timedelta(minutes=num)
        else:
            delta = timedelta(days=num)
        return (current - delta).strftime('%Y-%m-%d')
    
    return None


def _build_session(proxy=None):
    session = requests.Session()
    if proxy:
        session.proxies = {
            "http": f"http://{proxy}",
            "https": f"http://{proxy}",
        }
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        # 关键：不要声明支持 br (brotli)，否则 requests 无法解压
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    })
    return session


def _parse_search_results(soup, base_url, K):
    result = []
    items = soup.find_all(class_='b_algo')
    
    for item in items:
        if len(result) >= K:
            break
        
        title_text = ''
        url_text = ''
        
        h2 = item.find('h2')
        if h2:
            a_in_h2 = h2.find('a')
            if a_in_h2:
                title_text = a_in_h2.get_text(strip=True)
                url_text = a_in_h2.get('href', '')
            else:
                title_text = h2.get_text(strip=True)
        
        if not title_text:
            all_a = item.find_all('a', href=True)
            if all_a:
                best_a = max(all_a, key=lambda a: len(a.get_text(strip=True)))
                title_text = best_a.get_text(strip=True)
                url_text = best_a.get('href', '')
        
        if not title_text:
            continue
        
        if '哔哩哔哩' in title_text:
            continue
        
        if url_text:
            url_text = extract_real_url(url_text)
        
        author_elem = item.find('a', class_='tilk')
        author_text = ''
        if author_elem:
            author_text = author_elem.get('aria-label', '')
            if not author_text:
                author_text = author_elem.get_text(strip=True)
        
        abstract_elem = item.find('p')
        abstract_text = ''
        if abstract_elem:
            abstract_text = abstract_elem.get_text(strip=True).replace('\u2002', ' ').replace('\xa0', ' ')
        
        date = None
        if abstract_text:
            date_match = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', abstract_text)
            if date_match:
                date = f"{date_match.group(1)}-{date_match.group(2)}-{date_match.group(3)}"
        if not date:
            date = parse_english_date(abstract_text)
        if not date:
            date = datetime.now().strftime('%Y-%m-%d')
        
        result.append({
            "title": title_text,
            "parsed_date": date,
            "url": url_text,
            "author": author_text,
            "description": abstract_text,
            "source": "Bing"
        })
    
    next_link = soup.find('a', class_='sb_pagN')
    next_url = None
    if next_link:
        next_href = next_link.get('href', '')
        if next_href:
            next_url = next_href if next_href.startswith('http') else base_url + next_href
    
    return result, next_url


def crawl_news_http(news, K=20, proxy=None, international=False, exact_match=False):
    if proxy is None:
        proxy = "127.0.0.1:7890"
    
    if proxy:
        print(f"[info] 使用代理: {proxy}")
    
    if exact_match:
        print("[info] 使用精确匹配模式（关键词用双引号括起）")
        quoted_news = f'"{news}"'
        encoded_news = urllib.parse.quote(quoted_news)
    else:
        encoded_news = urllib.parse.quote(news)
    
    session = _build_session(proxy)
    
    # 关键方案：所有搜索都使用 cn.bing.com
    # - 对于英文关键词使用 &ensearch=1
    # - 对于中文关键词直接搜索
    base_url = "https://cn.bing.com"
    
    # 检查是否是英文关键词
    has_chinese = any('\u4e00' <= c <= '\u9fff' for c in news)
    
    if international or not has_chinese:
        # 英文关键词或国际版模式，使用 &ensearch=1
        search_url = f"{base_url}/search?q={encoded_news}&ensearch=1"
        print("[info] 使用 cn.bing.com + &ensearch=1 (优化英文搜索)")
    else:
        # 中文关键词，直接搜索
        search_url = f"{base_url}/search?q={encoded_news}"
        print("[info] 使用 cn.bing.com 直接搜索 (中文关键词)")
    
    print(f"[debug] 搜索 URL: {search_url}")
    
    result = []
    current_url = search_url
    page_num = 0
    
    while len(result) < K:
        page_num += 1
        print(f"[info] 获取第 {page_num} 页...")
        
        try:
            response = session.get(current_url, timeout=30, allow_redirects=True)
            response.raise_for_status()
            response.encoding = response.apparent_encoding
            
            final_url = response.url
            print(f"[debug] 状态码: {response.status_code}, 最终URL: {final_url}")
            
            soup = BeautifulSoup(response.text, 'html.parser')
            page_results, next_url = _parse_search_results(soup, base_url, K - len(result))
            
            print(f"[info] 本页找到 {len(page_results)} 条结果")
            result.extend(page_results)
            
            if next_url and len(result) < K:
                current_url = next_url
                time.sleep(1.5)
            else:
                break
        
        except requests.exceptions.RequestException as e:
            print(f"[error] 请求失败: {e}")
            break
        except Exception as e:
            print(f"[error] 解析失败: {e}")
            import traceback
            traceback.print_exc()
            break
    
    print(f"[info] 爬取完成，共获取 {len(result)} 条新闻")
    return result


def crawl_news(news, K=20, proxy=None, exact_match=False):
    return crawl_news_http(news, K, proxy, international=False, exact_match=exact_match)


def crawl_news_en(news, K=20, proxy=None, exact_match=False):
    return crawl_news_http(news, K, proxy, international=True, exact_match=exact_match)


if __name__ == "__main__":
    print("=" * 60)
    print("测试 Bing 国内版 (HTTP)")
    print("=" * 60)
    result = crawl_news_http("乌克兰和平谈判", K=5, international=False)
    for i, item in enumerate(result):
        print(f"  {i+1}. {item['title'][:60]}")
        print(f"     日期: {item['parsed_date']}  来源: {item['author']}")
        print(f"     URL: {item['url'][:80]}")
