#!/usr/bin/env python3
import base64
import urllib.parse
from datetime import datetime
from lxml import html
import httpx
import time
import random
import uuid


def extract_bing_url(href):
    if href.startswith("https://www.bing.com/ck/a?") or href.startswith("https://cn.bing.com/ck/a?"):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
        u_values = qs.get("u")
        if u_values:
            u_val = u_values[0]
            if u_val.startswith("a1"):
                encoded = u_val[2:]
                encoded += "=" * (-len(encoded) % 4)
                href = base64.urlsafe_b64decode(encoded).decode("utf-8", errors="replace")
    return href


def extract_text(els):
    texts = []
    if not isinstance(els, list):
        els = [els]
    for el in els:
        if isinstance(el, str):
            texts.append(el.strip())
        elif el is not None and el.text_content():
            texts.append(el.text_content().strip())
    return " ".join(texts).strip()


def crawl_news_http(news, K=20, proxy=None, international=False, exact_match=False):
    if proxy is None:
        proxy = None  # 默认不使用代理
    
    has_chinese = any('\u4e00' <= c <= '\u9fff' for c in news)
    
    if international or not has_chinese:
        engine_region = "en-US"
        accept_language = "en-US,en;q=0.9"
        setlang = "en"
        base_domain = "https://www.bing.com"
    else:
        engine_region = "zh-CN"
        accept_language = "zh-CN,zh;q=0.9,en;q=0.8"
        setlang = "zh-CN"
        base_domain = "https://cn.bing.com"
    
    if proxy:
        print(f"[info] 使用代理: {proxy}")
    else:
        print(f"[info] 不使用代理")
    print(f"[info] 市场: {engine_region}")
    print(f"[info] 语言: {setlang}")
    print(f"[info] 域名: {base_domain}")
    
    q_str = news
    if exact_match:
        q_str = f'"{news}"'
    
    query_params = {"q": q_str}
    query_params["mkt"] = engine_region
    query_params["setlang"] = setlang
    
    current_url = f"{base_domain}/search?{urllib.parse.urlencode(query_params)}"
    
    print(f"[debug] 搜索 URL: {current_url}")
    
    client_id = str(uuid.uuid4())
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": accept_language,
    }
    
    proxies = None
    if proxy:
        proxies = f"http://{proxy}"
    
    result = []
    try:
        print("[info] 发送搜索请求...")
        
        with httpx.Client(http2=False, headers=headers, proxy=proxies, timeout=30.0, follow_redirects=True) as client:
            response = client.get(current_url)
            response.raise_for_status()
            
            print(f"[info] 获取到cookies: {len(client.cookies)} 个")
            
            dom = html.fromstring(response.text)
            
            items = dom.xpath('//ol[@id="b_results"]/li[contains(@class, "b_algo")]')
            print(f"[info] 本页找到 {len(items)} 条结果")
            
            for item in items:
                if len(result) >= K:
                    break
                
                link = item.xpath('.//h2/a')
                if not link or len(link) == 0:
                    continue
                link = link[0]
                
                href = link.attrib.get("href", "")
                title = extract_text(link)
                
                if not href or not title:
                    continue
                
                href = extract_bing_url(href)
                
                content_els = item.xpath('.//p')
                for p in content_els:
                    for icon in p.xpath('.//span[@class="algoSlug_icon"]'):
                        if icon.getparent() is not None:
                            icon.getparent().remove(icon)
                content = extract_text(content_els)
                
                date = datetime.now().strftime('%Y-%m-%d')
                
                result.append({
                    "title": title,
                    "parsed_date": date,
                    "url": href,
                    "author": "",
                    "description": content,
                    "source": "Bing"
                })
    except Exception as e:
        print(f"[error] 失败: {e}")
        import traceback
        traceback.print_exc()
    
    print(f"[info] 爬取完成，共获取 {len(result)} 条新闻")
    return result


def crawl_news(news, K=20, proxy=None, exact_match=False):
    return crawl_news_http(news, K, proxy, international=False, exact_match=exact_match)


def crawl_news_en(news, K=20, proxy=None, exact_match=False):
    return crawl_news_http(news, K, proxy, international=True, exact_match=exact_match)


if __name__ == "__main__":
    print("="*60)
    print("测试 Bing News (HTTP)")
    print("="*60)
    result = crawl_news_http("Ukraine peace talks", K=5, international=True)
    for i, item in enumerate(result):
        print(f"{i+1}. {item['title']}")
        print(f"   URL: {item['url']}")
