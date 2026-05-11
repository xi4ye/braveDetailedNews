#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Scrapy 提取器模块 - 替代 Playwright
功能：使用 Scrapy + parsel 进行页面获取和内容提取，支持反反爬机制
"""

import random
import time
import json
import re
from typing import Optional, Dict, Tuple, Any
from urllib.parse import urlparse
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from fake_useragent import UserAgent
    UA_AVAILABLE = True
except ImportError:
    UA_AVAILABLE = False
    print("[Warning] fake_useragent 未安装，使用默认 User-Agent")

try:
    from parsel import Selector
    PARSEL_AVAILABLE = True
except ImportError:
    PARSEL_AVAILABLE = False
    print("[Warning] parsel 未安装，请安装: pip install parsel")


DEFAULT_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edge/122.0.0.0 Safari/537.36",
]

DEFAULT_HEADERS = {
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
    'Cache-Control': 'max-age=0',
    'Sec-Ch-Ua': '"Chromium";v="122", "Google Chrome";v="122", "Not(A:Brand";v="24"',
    'Sec-Ch-Ua-Mobile': '?0',
    'Sec-Ch-Ua-Platform': '"Windows"',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Sec-Fetch-User': '?1',
    'Priority': 'u=0, i',
}


class ScrapyExtractor:
    """Scrapy 风格的提取器 - 使用 requests + parsel 替代 Playwright"""
    
    def __init__(self, proxy: str = None, timeout: int = 30):
        self.proxy = proxy
        self.timeout = timeout
        self.session = None
        self.ua = None
        
        if UA_AVAILABLE:
            try:
                self.ua = UserAgent()
            except Exception:
                self.ua = None
        
        self._init_session()
    
    def _init_session(self):
        """初始化 requests session"""
        self.session = requests.Session()
        
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        
        if self.proxy:
            self.session.proxies = {
                'http': self.proxy,
                'https': self.proxy
            }
    
    def _get_random_ua(self) -> str:
        """获取随机 User-Agent"""
        if self.ua:
            try:
                return self.ua.random
            except Exception:
                pass
        return random.choice(DEFAULT_USER_AGENTS)
    
    def _get_headers(self) -> Dict[str, str]:
        """获取请求头（包含随机 UA）"""
        headers = DEFAULT_HEADERS.copy()
        headers['User-Agent'] = self._get_random_ua()
        return headers
    
    def _random_delay(self):
        """随机延迟，模拟人类行为"""
        delay = random.uniform(0.5, 2.0)
        time.sleep(delay)
    
    def fetch_page(self, url: str) -> Tuple[Optional[str], str, int]:
        """
        获取页面内容
        
        Args:
            url: 目标 URL
            
        Returns:
            (html_content, final_url, status_code)
        """
        if not url.startswith(('http://', 'https://')):
            if url.startswith('//'):
                url = 'https:' + url
            else:
                url = 'https://' + url
        
        print(f"[ScrapyExtractor] 正在请求: {url}")
        self._random_delay()
        
        try:
            headers = self._get_headers()
            print(f"[ScrapyExtractor] 发送 HTTP 请求...")
            
            response = self.session.get(
                url,
                headers=headers,
                timeout=self.timeout,
                allow_redirects=True,
                verify=True
            )
            
            final_url = response.url
            status_code = response.status_code
            
            print(f"[ScrapyExtractor] 收到响应: HTTP {status_code}, URL: {final_url}")
            
            if status_code == 200:
                encoding = response.apparent_encoding or response.encoding or 'utf-8'
                try:
                    html_content = response.content.decode(encoding)
                except UnicodeDecodeError:
                    try:
                        html_content = response.content.decode('utf-8', errors='ignore')
                    except:
                        html_content = response.content.decode('gbk', errors='ignore')
                
                return html_content, final_url, status_code
            else:
                return None, final_url, status_code
                
        except requests.exceptions.Timeout:
            return None, url, 408
        except requests.exceptions.ConnectionError:
            return None, url, 503
        except Exception as e:
            print(f"[ScrapyExtractor] 请求异常: {str(e)}")
            return None, url, 500
    
    def extract_by_selector(self, html: str, locator_type: str, locator_value: str) -> Optional[str]:
        """
        根据选择器提取内容
        
        Args:
            html: HTML 内容
            locator_type: 定位类型 (css_selector, xpath, id, class)
            locator_value: 定位值
            
        Returns:
            提取的文本内容
        """
        if not PARSEL_AVAILABLE:
            print("[ScrapyExtractor] parsel 未安装，无法提取")
            return None
        
        if not html:
            return None
        
        try:
            selector = Selector(text=html)
            
            if locator_type == "css_selector":
                elements = selector.css(locator_value)
            elif locator_type == "xpath":
                elements = selector.xpath(locator_value)
            elif locator_type == "id":
                elements = selector.css(f"#{locator_value}")
            elif locator_type == "class":
                elements = selector.css(f".{locator_value}")
            else:
                print(f"[ScrapyExtractor] 无效的定位类型: {locator_type}")
                return None
            
            if not elements:
                return None
            
            content = elements[0].get()
            
            text = re.sub(r'<[^>]+>', '', content)
            text = re.sub(r'\s+', ' ', text).strip()
            
            return text
            
        except Exception as e:
            print(f"[ScrapyExtractor] 提取异常: {str(e)}")
            return None
    
    def extract_text_by_selector(self, html: str, locator_type: str, locator_value: str) -> Optional[str]:
        """
        根据选择器提取纯文本内容
        
        Args:
            html: HTML 内容
            locator_type: 定位类型
            locator_value: 定位值
            
        Returns:
            提取的纯文本内容
        """
        if not PARSEL_AVAILABLE:
            return None
        
        if not html:
            return None
        
        try:
            selector = Selector(text=html)
            
            if locator_type == "css_selector":
                text = selector.css(locator_value + " ::text").getall()
            elif locator_type == "xpath":
                if "text()" in locator_value:
                    text = selector.xpath(locator_value).getall()
                else:
                    text = selector.xpath(locator_value + "//text()").getall()
            elif locator_type == "id":
                text = selector.css(f"#{locator_value} ::text").getall()
            elif locator_type == "class":
                text = selector.css(f".{locator_value} ::text").getall()
            else:
                return None
            
            if not text:
                return None
            
            content = ' '.join(text)
            content = re.sub(r'\s+', ' ', content).strip()
            
            return content
            
        except Exception as e:
            print(f"[ScrapyExtractor] 提取文本异常: {str(e)}")
            return None
    
    def get_dom_preview(self, html: str, max_length: int = 10000) -> str:
        """
        获取 DOM 预览
        
        Args:
            html: HTML 内容
            max_length: 最大长度
            
        Returns:
            DOM 预览字符串
        """
        if not html:
            return ""
        
        cleaned_html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
        cleaned_html = re.sub(r'<style[^>]*>.*?</style>', '', cleaned_html, flags=re.DOTALL | re.IGNORECASE)
        cleaned_html = re.sub(r'<!--.*?-->', '', cleaned_html, flags=re.DOTALL)
        
        if len(cleaned_html) > max_length:
            return cleaned_html[:max_length]
        return cleaned_html
    
    def close(self):
        """关闭 session"""
        if self.session:
            self.session.close()
            self.session = None


class ScrapyManager:
    """Scrapy 管理器 - 替代 BrowserManager
    
    注意：不使用单例模式，每个线程创建自己的实例
    """
    
    def __init__(self):
        self._extractor = None
    
    def start(self, proxy: str = None):
        """启动提取器"""
        if self._extractor is None:
            self._extractor = ScrapyExtractor(proxy=proxy)
            print("Scrapy 提取器初始化成功")
        return self
    
    def stop(self):
        """停止提取器"""
        if self._extractor:
            self._extractor.close()
            self._extractor = None
    
    @property
    def extractor(self) -> Optional[ScrapyExtractor]:
        return self._extractor


def extract_domain(url: str) -> str:
    """从 URL 中提取域名"""
    if url.startswith('//'):
        url = 'https:' + url
    elif not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    
    try:
        parsed = urlparse(url)
        domain = parsed.netloc or parsed.path.split('/')[0]
        return domain.lower()
    except:
        return ""


if __name__ == "__main__":
    print("测试 ScrapyExtractor...")
    
    extractor = ScrapyExtractor()
    
    test_url = "https://www.example.com"
    html, final_url, status = extractor.fetch_page(test_url)
    
    print(f"状态码: {status}")
    print(f"最终 URL: {final_url}")
    print(f"HTML 长度: {len(html) if html else 0}")
    
    if html:
        title = extractor.extract_by_selector(html, "css_selector", "title")
        print(f"页面标题: {title}")
    
    extractor.close()
    print("测试完成")
