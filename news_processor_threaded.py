#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新闻JSONL批量处理工具 - Scrapy版本
功能：使用Scrapy异步爬取 + 多线程Agent处理
"""

# 必须在所有导入之前安装 reactor
from twisted.internet import asyncioreactor
try:
    asyncioreactor.install()
except:
    pass  # 如果已经安装，忽略错误

import json
import os
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse
from tqdm import tqdm

import scrapy
from scrapy.crawler import CrawlerRunner
from scrapy.utils.log import configure_logging
from scrapy.settings import Settings
from twisted.internet import reactor, defer
from twisted.internet.threads import deferToThread

from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage

from scrapy_extractor import ScrapyManager, ScrapyExtractor, extract_domain


INPUT_JSONL_FILE = "crawled_news.jsonl"
OUTPUT_JSONL_FILE = "results_threaded.jsonl"
MAX_AGENT_STEPS = 20
MEMORY_FILE = "memory.json"
ERROR_FILE = "error_threaded.json"
BLACKLIST_THRESHOLD = 3  # 降低黑名单阈值，更快地跳过问题域名
PURE_SCRIPT_THRESHOLD = 1
MAX_WORKERS = 5  # 线程数，可以根据实际情况调整
STATS_FILE = "processor_stats.json"  # 统计信息保存文件

# API限流配置
API_RATE_LIMIT_QPS = 3.0  # 每秒最多3次API调用（专业版）
API_RATE_LIMIT_ENABLED = True  # 是否启用API限流


DEEPSEEK_CONFIG = {
    "model": "deepseek-chat",
    "base_url": "https://api.deepseek.com/v1",
    "api_key": os.environ.get("DEEPSEEK_API_KEY", "")
}


class APIRateLimiter:
    """API调用限流器 - 使用令牌桶算法
    
    防止多个Agent同时调用API导致限流
    """
    
    def __init__(self, qps: float = 3.0, enabled: bool = True):
        self.qps = qps
        self.enabled = enabled
        self.min_interval = 1.0 / qps if qps > 0 else 0
        self.last_call_time = 0.0
        self.lock = threading.Lock()
        self.call_count = 0
        self.wait_count = 0
    
    def acquire(self):
        """获取调用许可（会阻塞直到可以调用）"""
        if not self.enabled:
            return
        
        with self.lock:
            current_time = time.time()
            elapsed = current_time - self.last_call_time
            
            if elapsed < self.min_interval:
                wait_time = self.min_interval - elapsed
                time.sleep(wait_time)
                self.wait_count += 1
            
            self.last_call_time = time.time()
            self.call_count += 1
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        with self.lock:
            return {
                "total_calls": self.call_count,
                "total_waits": self.wait_count,
                "qps_limit": self.qps,
                "enabled": self.enabled
            }


# 全局API限流器实例
api_rate_limiter = APIRateLimiter(qps=API_RATE_LIMIT_QPS, enabled=API_RATE_LIMIT_ENABLED)

# 全局变量：存储 spider 的结果和统计
_spider_results = []
_spider_stats = {
    'processed_count': 0,
    'success_count': 0,
    'pure_script_count': 0,
    'blacklisted_count': 0,
    'agent_count': 0,
    'agent_success_count': 0,
}


class ThreadLocalContext:
    """线程本地上下文，避免全局变量冲突"""
    
    def __init__(self):
        self._local = threading.local()
    
    def init_context(self, browser_manager, news_info):
        """初始化线程本地上下文"""
        self._local.browser_manager = browser_manager
        self._local.current_news_info = news_info
        self._local.page_cache = {}
        self._local.tool_call_history = []
        self._local.extraction_completed = False
        
        # 生成简洁的线程标识
        thread_id = threading.current_thread().ident
        # 使用线程ID的后4位作为简短标识
        short_id = str(thread_id)[-4:]
        title = news_info.get('title', 'Unknown')[:15]
        self._local.thread_tag = f"[T{short_id}|{title}]"
    
    @property
    def browser_manager(self):
        return getattr(self._local, 'browser_manager', None)
    
    @property
    def current_news_info(self):
        return getattr(self._local, 'current_news_info', {})
    
    @property
    def page_cache(self):
        return getattr(self._local, 'page_cache', {})
    
    @property
    def tool_call_history(self):
        return getattr(self._local, 'tool_call_history', [])
    
    @property
    def extraction_completed(self):
        return getattr(self._local, 'extraction_completed', False)
    
    @extraction_completed.setter
    def extraction_completed(self, value):
        self._local.extraction_completed = value
    
    @property
    def thread_tag(self):
        """获取线程标识标签"""
        return getattr(self._local, 'thread_tag', '[Thread-?]')


_thread_local = ThreadLocalContext()


def log_print(message: str, level: str = "INFO"):
    """带线程标识的日志输出
    
    Args:
        message: 日志消息
        level: 日志级别 (INFO, TOOL, SUCCESS, ERROR)
    """
    thread_tag = _thread_local.thread_tag
    
    # 根据级别添加前缀
    if level == "TOOL":
        prefix = "🔧 "
    elif level == "SUCCESS":
        prefix = "✅ "
    elif level == "ERROR":
        prefix = "❌ "
    else:
        prefix = "   "
    
    print(f"{thread_tag} {prefix}{message}")
    sys.stdout.flush()


class ThreadSafeMemoryManager:
    """线程安全的 MemoryManager - 使用读写锁保护所有数据库操作
    
    并发规则：
    1. 读操作可以并发（多个线程同时读）
    2. 写操作必须独占（一个线程写时，其他线程不能读也不能写）
    3. 使用 threading.Lock() 实现互斥访问
    """
    
    def __init__(self, memory_file):
        self.db_file = memory_file.replace('.json', '.db')
        self._lock = threading.Lock()  # 全局锁，保护所有数据库操作
        self._init_db()
    
    def _get_conn(self):
        """获取数据库连接（已加锁保护）"""
        conn = sqlite3.connect(self.db_file, check_same_thread=False, timeout=30)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA busy_timeout=30000')  # 30秒超时
        return conn
    
    def _init_db(self):
        """初始化数据库（加锁保护）"""
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute('''
                    CREATE TABLE IF NOT EXISTS locators (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        domain TEXT NOT NULL,
                        locator_type TEXT NOT NULL,
                        locator_value TEXT NOT NULL,
                        locator_desc TEXT,
                        usage_count INTEGER DEFAULT 0,
                        success_count INTEGER DEFAULT 0,
                        create_time TEXT,
                        update_time TEXT,
                        UNIQUE(domain, locator_value)
                    )
                ''')
                conn.execute('CREATE INDEX IF NOT EXISTS idx_domain ON locators(domain)')
                conn.execute('CREATE INDEX IF NOT EXISTS idx_success ON locators(domain, success_count DESC)')
                conn.commit()
            finally:
                conn.close()
    
    def get_locator_by_domain(self, domain: str) -> Optional[Dict]:
        """获取单个定位器（读操作，加锁保护）"""
        with self._lock:
            conn = self._get_conn()
            try:
                cursor = conn.execute('''
                    SELECT * FROM locators 
                    WHERE domain = ? 
                    ORDER BY success_count DESC 
                    LIMIT 1
                ''', (domain,))
                row = cursor.fetchone()
                if row:
                    return {
                        'id': row[0],
                        'domain': row[1],
                        'locator_type': row[2],
                        'locator_value': row[3],
                        'locator_desc': row[4],
                        'usage_count': row[5],
                        'success_count': row[6],
                        'create_time': row[7],
                        'update_time': row[8]
                    }
                return None
            finally:
                conn.close()
    
    def get_all_locators_by_domain(self, domain: str) -> List[Dict]:
        """获取所有定位器（读操作，加锁保护）"""
        with self._lock:
            conn = self._get_conn()
            try:
                cursor = conn.execute('''
                    SELECT * FROM locators 
                    WHERE domain = ? 
                    ORDER BY success_count DESC
                ''', (domain,))
                rows = cursor.fetchall()
                return [{
                    'id': row[0],
                    'domain': row[1],
                    'locator_type': row[2],
                    'locator_value': row[3],
                    'locator_desc': row[4],
                    'usage_count': row[5],
                    'success_count': row[6],
                    'create_time': row[7],
                    'update_time': row[8]
                } for row in rows]
            finally:
                conn.close()
    
    def add_or_update_locator(self, domain: str, locator_type: str, locator_value: str, locator_desc: str = "") -> bool:
        """添加或更新定位器（写操作，加锁保护）
        
        写操作规则：
        - 同一时刻只能有 1 个线程写
        - 写的时候，所有线程不能读
        """
        with self._lock:
            conn = self._get_conn()
            try:
                now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                
                cursor = conn.execute('''
                    SELECT id, usage_count, success_count FROM locators 
                    WHERE domain = ? AND locator_value = ?
                ''', (domain, locator_value))
                row = cursor.fetchone()
                
                if row:
                    conn.execute('''
                        UPDATE locators 
                        SET usage_count = ?, success_count = ?, update_time = ?
                        WHERE id = ?
                    ''', (row[1] + 1, row[2] + 1, now, row[0]))
                else:
                    conn.execute('''
                        INSERT INTO locators 
                        (domain, locator_type, locator_value, locator_desc, usage_count, success_count, create_time, update_time)
                        VALUES (?, ?, ?, ?, 1, 1, ?, ?)
                    ''', (domain, locator_type, locator_value, locator_desc, now, now))
                
                conn.commit()
                return True
            except Exception as e:
                print(f"[错误] 数据库写入失败: {e}")
                return False
            finally:
                conn.close()
    
    def increment_locator_usage(self, domain: str, locator_value: str, success: bool = True):
        """增加定位器使用次数（写操作，加锁保护）
        
        写操作规则：
        - 同一时刻只能有 1 个线程写
        - 写的时候，所有线程不能读
        """
        with self._lock:
            conn = self._get_conn()
            try:
                now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                
                cursor = conn.execute('''
                    SELECT usage_count, success_count FROM locators 
                    WHERE domain = ? AND locator_value = ?
                ''', (domain, locator_value))
                row = cursor.fetchone()
                
                if row:
                    if success:
                        conn.execute('''
                            UPDATE locators 
                            SET usage_count = usage_count + 1, success_count = success_count + 1, update_time = ?
                            WHERE domain = ? AND locator_value = ?
                        ''', (now, domain, locator_value))
                    else:
                        conn.execute('''
                            UPDATE locators 
                            SET usage_count = usage_count + 1, update_time = ?
                            WHERE domain = ? AND locator_value = ?
                        ''', (now, domain, locator_value))
                    conn.commit()
            except Exception as e:
                print(f"[错误] 数据库更新失败: {e}")
            finally:
                conn.close()
    
    def close(self):
        pass


class ThreadSafeErrorManager:
    """线程安全的 ErrorManager - 使用单个锁保护所有操作"""
    
    def __init__(self, error_file):
        self.error_file = error_file
        self._lock = threading.Lock()
        self.errors = self._load_errors()
    
    def _load_errors(self):
        if not os.path.exists(self.error_file):
            return {}
        try:
            with open(self.error_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except:
            return {}
    
    def get_error_by_domain(self, domain: str) -> Optional[Dict]:
        with self._lock:
            return self.errors.get(domain)
    
    def add_error(self, domain: str, reason: str):
        with self._lock:
            if domain not in self.errors:
                self.errors[domain] = {
                    "fail_count": 0,
                    "reasons": [],
                    "blacklisted": False
                }
            
            self.errors[domain]["fail_count"] += 1
            self.errors[domain]["reasons"].append({
                "time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                "reason": reason
            })
            
            if self.errors[domain]["fail_count"] >= BLACKLIST_THRESHOLD:
                self.errors[domain]["blacklisted"] = True
            
            self._save_errors()
    
    def is_blacklisted(self, domain: str) -> bool:
        with self._lock:
            error_record = self.errors.get(domain)
            if error_record:
                return error_record.get("blacklisted", False) or error_record.get("fail_count", 0) >= BLACKLIST_THRESHOLD
            return False
    
    def _save_errors(self):
        try:
            with open(self.error_file, 'w', encoding='utf-8') as f:
                json.dump(self.errors, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"保存error.json失败: {e}")


def check_duplicate_tool_call(tool_name: str, tool_args: Dict) -> bool:
    """检查是否是重复的工具调用（线程安全）"""
    for call in _thread_local.tool_call_history:
        if call["name"] == tool_name and call["args"] == tool_args:
            print(f"  [拦截] 检测到重复工具调用: {tool_name}({tool_args})，已跳过")
            return True
    return False


def record_tool_call(tool_name: str, tool_args: Dict):
    """记录工具调用历史（线程安全）"""
    _thread_local.tool_call_history.append({
        "name": tool_name,
        "args": tool_args,
        "time": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    })


def check_extraction_completed() -> bool:
    """检查提取是否已完成（线程安全）"""
    return _thread_local.extraction_completed


def mark_extraction_completed():
    """标记提取已完成（线程安全）"""
    _thread_local.extraction_completed = True


@tool
def get_page_dom(url: str) -> str:
    """获取网页的DOM结构"""
    browser_manager = _thread_local.browser_manager
    current_news_info = _thread_local.current_news_info
    page_cache = _thread_local.page_cache
    
    log_print(f"获取DOM: {url}", level="TOOL")
    
    if check_extraction_completed():
        return json.dumps({"success": False, "error": "提取已完成，无需继续操作"}, ensure_ascii=False)
    
    if check_duplicate_tool_call("get_page_dom", {"url": url}):
        return json.dumps({"success": False, "error": "重复调用，已从缓存返回"}, ensure_ascii=False)
    
    if browser_manager is None or browser_manager.extractor is None:
        return "错误：Scrapy 提取器未初始化"
    
    try:
        if url in page_cache:
            log_print(f"使用缓存的页面内容", level="INFO")
            html_content, final_url, status_code = page_cache[url]
        else:
            html_content, final_url, status_code = browser_manager.extractor.fetch_page(url)
            page_cache[url] = (html_content, final_url, status_code)
        
        if status_code == 404:
            return "错误：页面不存在（404错误）"
        
        if status_code == 403:
            return "错误：访问被拒绝（403错误），可能需要登录或IP被封"
        
        if status_code >= 400:
            return f"错误：页面加载失败（HTTP {status_code}）"
        
        if not html_content:
            return "错误：页面内容为空"
        
        final_domain = extract_domain(final_url)
        current_news_info['final_domain'] = final_domain
        log_print(f"最终域名: {final_domain}", level="INFO")
        
        if '<title>404' in html_content or 'class="error-404"' in html_content or 'id="error"' in html_content:
            return "错误：页面内容显示404错误"
        
        dom_preview = browser_manager.extractor.get_dom_preview(html_content, max_length=10000)
        
        return json.dumps({
            "dom": dom_preview,
            "final_domain": final_domain,
            "final_url": final_url
        }, ensure_ascii=False)
        
    except Exception as e:
        return f"错误：页面加载异常 - {str(e)}"


def check_locator_is_generic(locator_value: str, title: str) -> Dict[str, Any]:
    """检查定位表达式是否为通用型"""
    issues = []
    
    title_keywords = re.findall(r'[\u4e00-\u9fa5]{2,}', title)
    for keyword in title_keywords:
        if len(keyword) >= 4 and keyword in locator_value:
            issues.append(f"表达式包含文章标题关键词: {keyword}")
    
    date_patterns = [
        r'\d{4}年\d{1,2}月\d{1,2}日',
        r'\d{4}-\d{2}-\d{2}',
        r'\d{4}/\d{2}/\d{2}',
        r'\d{1,2}月\d{1,2}日',
    ]
    for pattern in date_patterns:
        if re.search(pattern, locator_value):
            issues.append(f"表达式包含日期: {re.search(pattern, locator_value).group()}")
            break
    
    specific_patterns = [
        r'公司仅用\d+天',
        r'\d+天.*过会',
        r'张建中',
        r'摩尔线程',
    ]
    for pattern in specific_patterns:
        if re.search(pattern, locator_value):
            issues.append(f"表达式包含文章特定内容: {re.search(pattern, locator_value).group()}")
            break
    
    is_generic = len(issues) == 0
    return {
        "is_generic": is_generic,
        "issues": issues
    }


@tool
def validate_locator(locator_type: str, locator_value: str) -> str:
    """验证定位器"""
    browser_manager = _thread_local.browser_manager
    current_news_info = _thread_local.current_news_info
    page_cache = _thread_local.page_cache
    
    url = current_news_info.get('url', '')
    title = current_news_info.get('title', '')
    
    log_print(f"验证定位器: {locator_type}={locator_value}", level="TOOL")
    
    generic_check = check_locator_is_generic(locator_value, title)
    if not generic_check["is_generic"]:
        return json.dumps({
            "success": False,
            "error": "定位表达式不是通用型",
            "issues": generic_check["issues"],
            "hint": "请生成通用的定位表达式，不要包含文章标题、日期、人名等特定内容。"
        }, ensure_ascii=False)
    
    if browser_manager is None or browser_manager.extractor is None:
        return json.dumps({"success": False, "error": "Scrapy 提取器未初始化"}, ensure_ascii=False)
    
    try:
        if url in page_cache:
            log_print(f"使用缓存的页面内容", level="INFO")
            html_content, final_url, status_code = page_cache[url]
        else:
            html_content, final_url, status_code = browser_manager.extractor.fetch_page(url)
            page_cache[url] = (html_content, final_url, status_code)
        
        if status_code >= 400:
            return json.dumps({"success": False, "error": f"页面加载失败（HTTP {status_code}）"}, ensure_ascii=False)
        
        if not html_content:
            return json.dumps({"success": False, "error": "页面内容为空"}, ensure_ascii=False)
        
        if '<title>404' in html_content or 'class="error-404"' in html_content or 'id="error"' in html_content:
            return json.dumps({"success": False, "error": "页面内容显示404错误"}, ensure_ascii=False)
        
        content = browser_manager.extractor.extract_text_by_selector(
            html_content, locator_type, locator_value
        )
        
        if not content:
            return json.dumps({"success": False, "error": "未找到匹配元素，请尝试其他定位方式"}, ensure_ascii=False)
        
        if len(content.strip()) < 100:
            return json.dumps({
                "success": False, 
                "error": f"提取的内容过短（{len(content.strip())}字符），正文内容应至少100字符",
                "content_preview": content[:200] if content else ""
            }, ensure_ascii=False)
        
        return json.dumps({
            "success": True,
            "content_length": len(content),
            "content_preview": content[:500],
            "similarity": 1.0,
            "is_generic": True,
            "message": "定位验证成功，表达式为通用型"
        }, ensure_ascii=False)
        
    except Exception as e:
        return json.dumps({"success": False, "error": f"验证过程异常: {str(e)}"}, ensure_ascii=False)


@tool
def extract_content(locator_type: str, locator_value: str) -> str:
    """提取正文内容"""
    browser_manager = _thread_local.browser_manager
    current_news_info = _thread_local.current_news_info
    page_cache = _thread_local.page_cache
    
    url = current_news_info.get('url', '')
    
    log_print(f"提取正文: {locator_type}={locator_value}", level="TOOL")
    
    if browser_manager is None or browser_manager.extractor is None:
        return "错误：Scrapy 提取器未初始化"
    
    try:
        if url in page_cache:
            log_print(f"使用缓存的页面内容", level="INFO")
            html_content, final_url, status_code = page_cache[url]
        else:
            html_content, final_url, status_code = browser_manager.extractor.fetch_page(url)
            page_cache[url] = (html_content, final_url, status_code)
        
        if status_code >= 400:
            return f"错误：页面加载失败（HTTP {status_code}）"
        
        if not html_content:
            return "错误：页面内容为空"
        
        if '<title>404' in html_content or 'class="error-404"' in html_content or 'id="error"' in html_content:
            return "错误：页面内容显示404错误"
        
        content = browser_manager.extractor.extract_text_by_selector(
            html_content, locator_type, locator_value
        )
        
        if not content:
            return "错误：未找到匹配元素"
        
        content = re.sub(r'\s+', ' ', content).strip()
        
        return content
        
    except Exception as e:
        return f"错误：提取过程异常 - {str(e)}"


@tool
def validate_date_locator(locator_type: str, locator_value: str) -> str:
    """验证日期定位器是否能正确找到日期元素

    Args:
        locator_type: 定位类型，可选值：css_selector, xpath, id, class
        locator_value: 具体的定位值（CSS选择器、XPath表达式、ID或class名）

    Returns:
        验证结果JSON字符串，包含是否成功、提取的内容片段、是否为通用型等信息
    """
    browser_manager = _thread_local.browser_manager
    current_news_info = _thread_local.current_news_info
    page_cache = _thread_local.page_cache
    
    url = current_news_info.get('url', '')
    title = current_news_info.get('title', '')
    
    log_print(f"[Tool] 验证日期定位器: {locator_type}={locator_value}")
    
    generic_check = check_locator_is_generic(locator_value, title)
    if not generic_check["is_generic"]:
        return json.dumps({
            "success": False,
            "error": "定位表达式不是通用型",
            "issues": generic_check["issues"],
            "hint": "请生成通用的日期定位表达式，不要包含文章标题、日期、人名等特定内容。"
        }, ensure_ascii=False)
    
    if browser_manager is None or browser_manager.extractor is None:
        return json.dumps({"success": False, "error": "Scrapy 提取器未初始化"}, ensure_ascii=False)
    
    try:
        if url in page_cache:
            log_print(f"[日期验证缓存] 使用缓存的页面内容")
            html_content, final_url, status_code = page_cache[url]
        else:
            html_content, final_url, status_code = browser_manager.extractor.fetch_page(url)
            page_cache[url] = (html_content, final_url, status_code)
        
        if status_code >= 400:
            return json.dumps({"success": False, "error": f"页面加载失败（HTTP {status_code}）"}, ensure_ascii=False)
        
        if not html_content:
            return json.dumps({"success": False, "error": "页面内容为空"}, ensure_ascii=False)
        
        if '<title>404' in html_content or 'class="error-404"' in html_content or 'id="error"' in html_content:
            return json.dumps({"success": False, "error": "页面内容显示404错误"}, ensure_ascii=False)
        
        content = browser_manager.extractor.extract_text_by_selector(
            html_content, locator_type, locator_value
        )
        
        if not content or not content.strip():
            return json.dumps({"success": False, "error": "未找到日期元素，请尝试其他定位方式"}, ensure_ascii=False)
        
        date_patterns = [
            r'\d{4}-\d{2}-\d{2}',
            r'\d{4}年\d{1,2}月\d{1,2}日',
            r'[A-Za-z]+\s+\d{1,2},?\s*\d{4}',
            r'\d{1,2}[/-]\d{1,2}[/-]\d{2,4}'
        ]
        
        has_date = any(re.search(pattern, content) for pattern in date_patterns)
        
        return json.dumps({
            "success": True,
            "date_found": has_date,
            "content_preview": content[:500] if content else "",
            "is_generic": True,
            "message": "日期定位验证成功，表达式为通用型"
        }, ensure_ascii=False)
        
    except Exception as e:
        return json.dumps({"success": False, "error": f"验证过程异常: {str(e)}"}, ensure_ascii=False)


@tool
def extract_date(locator_type: str, locator_value: str) -> str:
    """使用定位器提取网页中的日期信息

    Args:
        locator_type: 定位类型，可选值：css_selector, xpath, id, class
        locator_value: 具体的定位值

    Returns:
        提取的日期文本，如果失败返回错误信息
    """
    browser_manager = _thread_local.browser_manager
    current_news_info = _thread_local.current_news_info
    page_cache = _thread_local.page_cache
    
    url = current_news_info.get('url', '')
    
    log_print(f"[Tool] 提取日期: {locator_type}={locator_value}")
    
    if browser_manager is None or browser_manager.extractor is None:
        return "错误：Scrapy 提取器未初始化"
    
    try:
        if url in page_cache:
            log_print(f"[日期缓存] 使用缓存的页面内容")
            html_content, final_url, status_code = page_cache[url]
        else:
            html_content, final_url, status_code = browser_manager.extractor.fetch_page(url)
            page_cache[url] = (html_content, final_url, status_code)
        
        if status_code >= 400:
            log_print(f"[日期失败] 页面加载失败（HTTP {status_code}）")
            return f"错误：页面加载失败（HTTP {status_code}）"
        
        if not html_content:
            log_print(f"[日期失败] 页面内容为空")
            return "错误：页面内容为空"
        
        if '<title>404' in html_content or 'class="error-404"' in html_content or 'id="error"' in html_content:
            log_print(f"[日期失败] 页面返回404错误")
            return "错误：页面内容显示404错误"
        
        log_print(f"[日期提取] 使用 {locator_type}={locator_value} 提取日期")
        date_text = browser_manager.extractor.extract_text_by_selector(
            html_content, locator_type, locator_value
        )
        
        if not date_text:
            log_print(f"[日期失败] 未找到日期元素")
            return "错误：未找到日期元素"
        
        date_text = re.sub(r'\s+', ' ', date_text).strip()
        
        date_patterns = [
            r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})',
            r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})',
            r'(\d{4}-\d{2}-\d{2})',
        ]
        
        for pattern in date_patterns:
            match = re.search(pattern, date_text)
            if match:
                date_text = match.group(1)
                break
        
        print(f"  [日期成功] 提取到日期文本: {date_text}")
        
        return date_text
        
    except Exception as e:
        print(f"  [日期异常] 提取日期异常: {str(e)}")
        return f"错误：提取日期异常 - {str(e)}"


@tool
def get_existing_locator(domain: str) -> str:
    """查询已有定位规则"""
    return json.dumps({
        "exists": False,
        "message": f"域名 {domain} 暂无定位规则，需要生成新的"
    }, ensure_ascii=False)


@tool
def save_locator(locator_type: str, locator_value: str, locator_desc: str, locator_category: str = "content") -> str:
    """保存验证通过的通用型定位规则到记忆库

    Args:
        locator_type: 定位类型
        locator_value: 定位值（必须是通用型表达式）
        locator_desc: 定位方式描述
        locator_category: 定位器类别，'content' 或 'date'

    Returns:
        保存结果
    """
    current_news_info = _thread_local.current_news_info
    
    domain = current_news_info.get('final_domain', current_news_info.get('domain', ''))
    
    locator_json = {
        "domain": domain,
        "locator_type": locator_type,
        "locator_value": locator_value,
        "locator_desc": locator_desc,
        "locator_category": locator_category,
        "create_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "is_valid": True,
        "usage_count": 0,
        "success_count": 0
    }
    
    if locator_category == 'date':
        current_news_info['_saved_date_locator'] = locator_json
    else:
        current_news_info['_saved_locator'] = locator_json
    
    return json.dumps({
        "success": True,
        "message": f"通用型定位规则已保存，域名: {domain}，类别: {locator_category}",
        "locator": locator_json
    }, ensure_ascii=False)


@tool
def save_date_locator(locator_type: str, locator_value: str, locator_desc: str) -> str:
    """保存验证通过的日期定位规则到记忆库

    Args:
        locator_type: 定位类型
        locator_value: 定位值（必须是通用型表达式）
        locator_desc: 定位方式描述

    Returns:
        保存结果
    """
    return save_locator(locator_type, locator_value, locator_desc, locator_category="date")


@tool
def give_up(reason: str) -> str:
    """放弃处理"""
    return json.dumps({
        "success": False,
        "action": "give_up",
        "reason": reason,
        "message": f"已放弃处理当前新闻，原因: {reason}。"
    }, ensure_ascii=False)


class DeepSeekAgentWithTools:
    """使用Tools的DeepSeek Agent"""
    
    def __init__(self, config):
        self.llm = ChatOpenAI(
            model=config["model"],
            base_url=config["base_url"],
            api_key=config["api_key"],
            temperature=0
        )
        self.tools = [get_page_dom, validate_locator, validate_date_locator, extract_content, extract_date, get_existing_locator, save_locator, save_date_locator, give_up]
        self.llm_with_tools = self.llm.bind_tools(self.tools)
    
    def process_news(self, news_item: Dict[str, Any]) -> Dict[str, Any]:
        """处理单条新闻"""
        url = news_item['url']
        if url.startswith('//'):
            url = 'https:' + url
        elif not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        
        system_prompt = f"""你是一个专业的网页正文定位专家。你的任务有两项：
1. 找到网页中新闻正文的位置并提取正文内容
2. 找到网页中发布日期的位置并提取日期表达式

【核心要求】生成的定位表达式必须是【通用型】：
- 同一域名下的所有文章都能使用该表达式
- 禁止包含文章标题、日期、人名、公司名等特定内容
- 禁止使用 contains(text(), '某具体内容') 这种特定内容的定位方式
- 应该使用页面结构特征：如 class名、id名、标签层级关系等

【重要】必须排除以下标签内的内容：
- <script> 标签：包含 JavaScript 代码，不是正文
- <style> 标签：包含 CSS 样式，不是正文
- <noscript> 标签：包含备用内容，不是正文
- <svg> 标签：包含图标/图形，不是正文

【正文定位正确示例】：
- css_selector: "div.article-content", "#js_content", ".rich_media_content", "div.content article"
- xpath: "//article//div[@class='content']", "//div[contains(@class, 'article-body')]", "//div[@class='main']//p[not(ancestor::script or ancestor::style)]"
- id: "artibody", "js_content", "article-content"

【正文定位错误示例】（禁止使用）：
- "//*[text()]" - 会匹配所有文本，包括 script/style 内的代码！
- "//p[contains(text(), '摩尔线程')]" - 包含文章特定内容
- "//div[contains(., '9月26日')]" - 包含日期
- "//*[contains(text(), '公司仅用88天')]" - 包含文章特定内容

【日期定位要求】：
- 日期通常位于文章标题下方、作者信息附近、或网页头部 meta 标签中
- 常见位置：class="date"、class="time"、class="publish-time"、id="pubdate" 等
- 定位器应能匹配该域名下所有文章的日期元素

【日期格式返回要求】：
- 如果找到完整日期时间，返回标准格式: YYYY-MM-DD hh:mm:ss
- 如果只有日期部分 (YYYY-MM-DD)，请只返回该部分
- 如果无法找到日期，返回空字符串

当前新闻信息：
- 标题: {news_item['title']}
- 来源: {news_item['author']}
- URL: {news_item['url']}

你可以使用以下工具：
1. get_page_dom(url) - 获取网页DOM结构（会返回最终跳转后的域名）
2. get_existing_locator(domain) - 查询该域名是否已有定位规则（返回 content 和 date 两类）
3. validate_locator(locator_type, locator_value) - 验证定位器是否有效（会检查是否为通用型）
4. extract_content(locator_type, locator_value) - 提取正文内容
5. extract_date(locator_type, locator_value) - 提取日期文本
6. save_locator(locator_type, locator_value, locator_desc) - 保存正文定位规则
7. save_date_locator(locator_type, locator_value, locator_desc) - 保存日期定位规则
8. give_up(reason) - 放弃处理当前新闻

【重要】遇到以下情况请立即调用 give_up 工具终止处理：
- 页面返回404错误
- 页面内容已被删除或失效
- 多次尝试都无法成功（不要浪费token继续尝试）

【高效工作流程（必须严格遵循）】：
1. get_page_dom(url) - 获取网页DOM结构，获取最终域名 final_domain
2. 如果页面返回404或无法访问，立即调用 give_up 放弃
3. get_existing_locator(final_domain) - 查询该域名是否已有定位规则
4. 如果已有 content 规则：
   - 直接 extract_content 提取正文
   - 成功后继续尝试提取日期
5. 如果已有 date 规则：
   - 直接 extract_date 提取日期
6. 如果没有规则：
   - 分析DOM，选择最可能的正文定位器，validate_locator 验证
   - 验证成功后 extract_content 提取正文
   - 分析DOM，选择最可能的日期定位器，validate_locator 验证
   - 验证成功后 extract_date 提取日期
7. 提取成功后分别调用 save_locator 和 save_date_locator 保存
8. 【关键】不要尝试多个定位器，找到一个能用的就停止！

【绝对关键提示】：
1. 提取正文和日期是独立任务，可以并行思考但需要分别保存
2. extract_content 成功后必须立即调用 save_locator
3. extract_date 成功后必须立即调用 save_date_locator
4. 不要继续尝试其他定位器，找到一个能用的就停止
5. 不要重复调用 get_page_dom，第一次获取后就不要再获取
6. 两个任务都完成后返回最终结果

请开始工作，严格按照上述流程调用工具。"""
        
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content="请开始处理这条新闻，找到正文并提取，同时找到并提取发布日期。记住：定位表达式必须是通用型，不能包含文章特定内容！")
        ]
        
        final_content = None
        final_date_text = None
        final_content_locator = None
        final_date_locator = None
        last_validated_content_locator = None
        last_validated_date_locator = None
        date_extraction_completed = False
        content_extraction_completed = False
        
        for step in range(MAX_AGENT_STEPS):
            log_print(f"Step {step + 1}/{MAX_AGENT_STEPS}", level="INFO")
            
            try:
                # API限流：在调用API前获取许可
                api_rate_limiter.acquire()
                
                response = self.llm_with_tools.invoke(messages)
                messages.append(response)
                
                if response.tool_calls:
                    for tool_call in response.tool_calls:
                        tool_name = tool_call["name"]
                        tool_args = tool_call["args"]
                        
                        log_print(f"→ {tool_name}({tool_args})", level="TOOL")
                        
                        tool_func = None
                        for t in self.tools:
                            if t.name == tool_name:
                                tool_func = t
                                break
                        
                        if tool_func:
                            if check_extraction_completed():
                                log_print(f"拦截: 提取已完成", level="INFO")
                                tool_result = json.dumps({"success": False, "error": "提取已完成，无需继续操作"}, ensure_ascii=False)
                            elif check_duplicate_tool_call(tool_name, tool_args):
                                tool_result = json.dumps({"success": False, "error": "重复调用，已跳过"}, ensure_ascii=False)
                            else:
                                record_tool_call(tool_name, tool_args)
                                try:
                                    tool_result = tool_func.invoke(tool_args)
                                    result_preview = tool_result[:100] if len(str(tool_result)) > 100 else tool_result
                                    log_print(f"← {result_preview}", level="INFO")
                                except Exception as e:
                                    tool_result = f"工具执行错误: {str(e)}"
                            
                            messages.append(ToolMessage(
                                content=str(tool_result),
                                tool_call_id=tool_call["id"]
                            ))
                            
                            if tool_name == "give_up":
                                try:
                                    result = json.loads(tool_result)
                                    log_print(f"放弃处理: {result.get('reason', '未知')}", level="ERROR")
                                    return {
                                        "success": False,
                                        "content": "",
                                        "locator": None,
                                        "error": result.get("reason", "Agent主动放弃"),
                                        "_give_up": True,
                                        "_give_up_reason": result.get("reason", "Agent主动放弃")
                                    }
                                except:
                                    return {
                                        "success": False,
                                        "content": "",
                                        "locator": None,
                                        "error": "Agent主动放弃",
                                        "_give_up": True,
                                        "_give_up_reason": "Agent主动放弃"
                                    }
                            
                            if tool_name == "validate_locator":
                                try:
                                    result = json.loads(tool_result)
                                    if result.get("success"):
                                        locator_type = tool_args.get("locator_type")
                                        locator_value = tool_args.get("locator_value")
                                        if content_extraction_completed:
                                            last_validated_date_locator = {
                                                "locator_type": locator_type,
                                                "locator_value": locator_value
                                            }
                                        else:
                                            last_validated_content_locator = {
                                                "locator_type": locator_type,
                                                "locator_value": locator_value
                                            }
                                except:
                                    pass
                            
                            if tool_name == "extract_content":
                                if not str(tool_result).startswith("错误"):
                                    final_content = tool_result
                                    content_extraction_completed = True
                                    log_print(f"正文提取成功 - 长度: {len(final_content)} 字符", level="SUCCESS")
                                    
                                    if last_validated_content_locator:
                                        log_print(f"检测到已验证的正文定位器", level="INFO")
                                        domain = _thread_local.current_news_info.get('final_domain', _thread_local.current_news_info.get('domain', ''))
                                        final_content_locator = {
                                            **last_validated_content_locator,
                                            "domain": domain,
                                            "locator_category": "content",
                                            "locator_desc": "自动保存",
                                            "create_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                            "is_valid": True,
                                            "usage_count": 0,
                                            "success_count": 0
                                        }
                            
                            if tool_name == "extract_date":
                                if not str(tool_result).startswith("错误") and tool_result.strip():
                                    final_date_text = tool_result
                                    date_extraction_completed = True
                                    print(f"\n[日期提取成功] 日期文本: {final_date_text}")
                                    
                                    if last_validated_date_locator:
                                        print(f"[检测到已验证的日期定位器]")
                                        domain = _thread_local.current_news_info.get('final_domain', _thread_local.current_news_info.get('domain', ''))
                                        final_date_locator = {
                                            **last_validated_date_locator,
                                            "domain": domain,
                                            "locator_category": "date",
                                            "locator_desc": "自动保存",
                                            "create_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                            "is_valid": True,
                                            "usage_count": 0,
                                            "success_count": 0
                                        }
                            
                            if tool_name == "save_locator":
                                try:
                                    result = json.loads(tool_result)
                                    if result.get("success"):
                                        locator = result.get("locator", {})
                                        if locator.get("locator_category") == "date":
                                            final_date_locator = locator
                                        else:
                                            final_content_locator = locator
                                except:
                                    pass
                            
                            if tool_name == "save_date_locator":
                                try:
                                    result = json.loads(tool_result)
                                    if result.get("success"):
                                        final_date_locator = result.get("locator")
                                        date_extraction_completed = True
                                except:
                                    pass
                            
                            if content_extraction_completed and date_extraction_completed:
                                print(f"\n[完成] 正文和日期都已提取完成，准备返回结果")
                                return {
                                    "success": True,
                                    "content": final_content or "",
                                    "date_text": final_date_text,
                                    "content_locator": final_content_locator,
                                    "date_locator": final_date_locator
                                }
                
                else:
                    content = response.content
                    
                    completion_keywords = [
                        "success", "完成", "成功", "已经完成", "提取成功",
                        "正文已提取", "日期已提取", "任务完成", "finished", "done"
                    ]
                    
                    has_completion = any(keyword in content.lower() or keyword in content for keyword in completion_keywords)
                    
                    if has_completion or (final_content and final_date_text):
                        if final_content and final_date_text:
                            print(f"\n[检测到完成信号] 直接返回结果")
                            return {
                                "success": True,
                                "content": final_content or "",
                                "date_text": final_date_text,
                                "content_locator": final_content_locator,
                                "date_locator": final_date_locator
                            }
                    
                    if step >= MAX_AGENT_STEPS - 1:
                        break
                    
                    messages.append(HumanMessage(content="请继续工作，使用工具完成任务。记住：定位表达式必须是通用型，找到一个能用的就停止！"))
            
            except Exception as e:
                print(f"  [Agent错误] {e}")
                if step >= MAX_AGENT_STEPS - 1:
                    break
                messages.append(HumanMessage(content=f"发生错误: {e}，请继续尝试。"))
        
        return {
            "success": False,
            "content": final_content or "",
            "date_text": final_date_text,
            "content_locator": final_content_locator,
            "date_locator": final_date_locator,
            "error": "达到最大步骤限制"
        }


def process_single_news(news_item, agent_config, memory_manager, error_manager):
    """处理单条新闻（线程任务）"""
    browser_manager = ScrapyManager()
    browser_manager.start()
    
    news_id = re.sub(r'[^\w\-]', '_', f"{news_item['author']}_{news_item['title'][:20]}")
    
    if not all(key in news_item for key in ['title', 'url', 'author']):
        print(f"字段缺失，跳过: {news_item.get('title', '未知标题')}")
        browser_manager.stop()
        return False, {**news_item, 'content': '', '_id': news_id}
    
    url = news_item['url']
    if url.startswith('//'):
        url = 'https:' + url
    elif not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    
    original_domain = extract_domain(url)
    
    print(f"\n{'='*60}")
    print(f"处理新闻: {news_item['title'][:50]}...")
    print(f"来源: {news_item['author']}")
    print(f"原始域名: {original_domain}")
    print(f"正在获取页面: {url}")
    
    html_content, final_url, status_code = browser_manager.extractor.fetch_page(url)
    final_domain = extract_domain(final_url) if final_url else original_domain
    
    if final_domain != original_domain:
        print(f"[重定向] {original_domain} -> {final_domain}")
    
    if error_manager.is_blacklisted(final_domain):
        print(f"[跳过] 域名 {final_domain} 在黑名单中")
        print(f"{'='*60}")
        browser_manager.stop()
        return False, {**news_item, 'content': '', '_id': news_id, 'status': 'blacklisted'}
    
    if status_code >= 400:
        error_manager.add_error(final_domain, f"HTTP {status_code}")
        print(f"[失败] 页面加载失败（HTTP {status_code}）")
        print(f"{'='*60}")
        browser_manager.stop()
        return False, {**news_item, 'content': '', '_id': news_id, 'status': 'http_error'}
    
    if not html_content:
        error_manager.add_error(final_domain, "页面内容为空")
        print(f"[失败] 页面内容为空")
        print(f"{'='*60}")
        browser_manager.stop()
        return False, {**news_item, 'content': '', '_id': news_id, 'status': 'empty_content'}
    
    locators = memory_manager.get_all_locators_by_domain(final_domain)
    if locators:
        print(f"[纯脚本模式] 域名 {final_domain} 找到 {len(locators)} 个定位器")
        print(f"{'='*60}")
        
        thread_news_info = {
            'title': news_item['title'],
            'url': url,
            'author': news_item['author'],
            'source': news_item.get('source', ''),
            'domain': final_domain,
            'html_content': html_content
        }
        _thread_local.init_context(browser_manager, thread_news_info)
        
        for idx, locator in enumerate(locators, 1):
            locator_type = locator.get('locator_type', '')
            locator_value = locator.get('locator_value', '')
            print(f"[纯脚本 {idx}/{len(locators)}] 使用 {locator_type}={locator_value} 提取正文")
            
            content = browser_manager.extractor.extract_text_by_selector(
                html_content, locator_type, locator_value
            ) if browser_manager.extractor else ""
            
            if content and len(content) >= 100:
                memory_manager.increment_locator_usage(final_domain, locator_value, success=True)
                print(f"\n[纯脚本成功] 提取正文长度: {len(content)} 字符")
                # browser_manager.stop()  # 暂时注释，避免多线程竞争
                return True, {**news_item, 'content': content, '_id': news_id, 'status': 'success', 'used_pure_script': True, 'used_agent': False}
            else:
                memory_manager.increment_locator_usage(final_domain, locator_value, success=False)
                print(f"[纯脚本 {idx}/{len(locators)}] 失败")
                sys.stdout.flush()
        
        print(f"[纯脚本全部失败] 回退到 Agent 处理")
        sys.stdout.flush()
    
    print(f"[Agent模式] 域名 {final_domain} 无有效定位器或纯脚本失败")
    print(f"{'='*60}")
    sys.stdout.flush()
    
    thread_news_info = {
        'title': news_item['title'],
        'url': url,
        'author': news_item['author'],
        'source': news_item.get('source', ''),
        'domain': final_domain,
        'html_content': html_content
    }
    _thread_local.init_context(browser_manager, thread_news_info)
    
    agent = DeepSeekAgentWithTools(agent_config)
    
    used_agent = True
    try:
        result = agent.process_news(news_item)
        
        if result.get("success"):
            content = result.get("content", "")
            locator = result.get("locator")
            
            if locator:
                loc_domain = locator.get('domain', '')
                existing = memory_manager.get_locator_by_domain(loc_domain)
                if existing:
                    old_locator_value = existing.get('locator_value', '')
                    new_locator_value = locator.get('locator_value', '')
                    
                    if old_locator_value != new_locator_value:
                        memory_manager.add_or_update_locator(
                            loc_domain,
                            locator.get('locator_type', 'xpath'),
                            new_locator_value,
                            locator.get('locator_desc', '')
                        )
                        print(f"\n[新增] 域名 {loc_domain} 添加新定位规则: {old_locator_value} -> {new_locator_value}")
                    else:
                        memory_manager.increment_locator_usage(loc_domain, new_locator_value, success=True)
                        print(f"\n[复用] 使用了缓存定位规则，域名: {loc_domain}")
                else:
                    memory_manager.add_or_update_locator(
                        loc_domain,
                        locator.get('locator_type', 'xpath'),
                        locator.get('locator_value', ''),
                        locator.get('locator_desc', '')
                    )
                    print(f"\n[新增] 域名 {loc_domain} 首次添加定位规则: {locator.get('locator_value', '')}")
            
            print(f"\n[成功] 提取正文长度: {len(content)} 字符")
            # browser_manager.stop()  # 暂时注释，避免多线程竞争
            return True, {**news_item, 'content': content, '_id': news_id, 'status': 'success', 'used_agent': used_agent}
        else:
            error_reason = result.get('error', '未知错误')
            error_manager.add_error(final_domain, error_reason)
            print(f"\n[失败] {error_reason}")
            # browser_manager.stop()  # 暂时注释，避免多线程竞争
            return False, {**news_item, 'content': '', '_id': news_id, 'status': 'failed', 'used_agent': used_agent}
            
    except Exception as e:
        error_manager.add_error(final_domain, f"处理异常: {str(e)}")
        print(f"\n[异常] 处理失败: {e}")
        # browser_manager.stop()  # 暂时注释，避免多线程竞争
        return False, {**news_item, 'content': '', '_id': news_id, 'status': 'error', 'used_agent': used_agent}


class NewsSpider(scrapy.Spider):
    """Scrapy爬虫 - 异步爬取新闻页面"""
    name = 'news_spider'
    
    custom_settings = {
        'CONCURRENT_REQUESTS': 5,
        'CONCURRENT_REQUESTS_PER_DOMAIN': 5,
        'DOWNLOAD_DELAY': 0.5,
        'RANDOMIZE_DOWNLOAD_DELAY': True,
        'USER_AGENT': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'ROBOTSTXT_OBEY': False,
        'RETRY_ENABLED': True,
        'RETRY_TIMES': 1,  # 减少重试次数，避免长时间等待
        'DOWNLOAD_TIMEOUT': 15,  # 降低超时时间，加快失败速度
    }
    
    def __init__(self, news_list=None, memory_manager=None, error_manager=None, *args, **kwargs):
        super(NewsSpider, self).__init__(*args, **kwargs)
        self.news_list = news_list or []
        self.memory_manager = memory_manager
        self.error_manager = error_manager
        self.results = []
        self.stats = {
            'processed_count': 0,
            'success_count': 0,
            'pure_script_count': 0,
            'blacklisted_count': 0,
            'agent_count': 0,
            'agent_success_count': 0,
        }
        
        # 设置全局变量
        global _spider_results, _spider_stats
        _spider_results = self.results
        _spider_stats = self.stats
    
    def start_requests(self):
        for news_item in self.news_list:
            url = news_item.get('url', '')
            if not url:
                continue
            
            if url.startswith('//'):
                url = 'https:' + url
            elif not url.startswith(('http://', 'https://')):
                url = 'https://' + url
            
            # 在发起请求前检测黑名单
            from urllib.parse import urlparse
            domain = urlparse(url).netloc
            
            if self.error_manager.is_blacklisted(domain):
                print(f"\n{'='*60}")
                print(f"[跳过] 域名 {domain} 在黑名单中")
                print(f"URL: {url}")
                sys.stdout.flush()
                result_item = {**news_item, 'content': '', 'status': 'blacklisted'}
                self.results.append(result_item)
                self.stats['processed_count'] += 1
                self.stats['blacklisted_count'] += 1
                continue  # 跳过，不发起请求
            
            yield scrapy.Request(
                url=url,
                callback=self.parse,
                meta={'news_item': news_item},
                errback=self.errback,
                dont_filter=True
            )
    
    def parse(self, response):
        news_item = response.meta['news_item']
        html_content = response.text
        final_url = response.url
        status_code = response.status
        final_domain = urlparse(final_url).netloc
        
        print(f"\n{'='*60}")
        print(f"处理新闻: {news_item.get('title', '未知标题')[:50]}...")
        print(f"来源: {news_item.get('author', '未知')}")
        print(f"域名: {final_domain}")
        print(f"URL: {final_url}")
        print(f"HTTP状态: {status_code}")
        sys.stdout.flush()  # 强制刷新输出
        
        # 注意：黑名单检测已在 start_requests() 中完成
        # 这里不再重复检测
        
        if status_code >= 400:
            self.error_manager.add_error(final_domain, f"HTTP {status_code}")
            print(f"[失败] 页面加载失败（HTTP {status_code}）")
            sys.stdout.flush()
            result_item = {**news_item, 'content': '', 'status': 'http_error'}
            self.results.append(result_item)
            self.stats['processed_count'] += 1
            return
        
        if not html_content:
            self.error_manager.add_error(final_domain, "页面内容为空")
            print(f"[失败] 页面内容为空")
            sys.stdout.flush()
            result_item = {**news_item, 'content': '', 'status': 'empty_content'}
            self.results.append(result_item)
            self.stats['processed_count'] += 1
            return
        
        print(f"[成功] 页面加载成功，HTML长度: {len(html_content)} 字符")
        sys.stdout.flush()
        
        # 使用 inlineCallbacks 处理异步操作
        from twisted.internet import defer as twisted_defer
        
        @twisted_defer.inlineCallbacks
        def process_async():
            try:
                result = yield deferToThread(
                    self._process_in_thread,
                    news_item,
                    html_content,
                    final_url,
                    final_domain
                )
                
                success, result_item = result
                self.results.append(result_item)
                self.stats['processed_count'] += 1
                
                if success:
                    self.stats['success_count'] += 1
                    if result_item.get('used_pure_script'):
                        self.stats['pure_script_count'] += 1
                    print(f"[完成] 处理成功，正文长度: {len(result_item.get('content', ''))} 字符")
                else:
                    print(f"[完成] 处理失败")
                sys.stdout.flush()
                
                if result_item.get('used_agent'):
                    self.stats['agent_count'] += 1
                    if success:
                        self.stats['agent_success_count'] += 1
            except Exception as e:
                print(f"[错误] 处理失败: {e}")
                sys.stdout.flush()
                news_id = re.sub(r'[^\w\-]', '_', f"{news_item.get('author', 'unknown')}_{news_item.get('title', 'unknown')[:20]}")
                result_item = news_item.copy()
                result_item['content'] = ''
                result_item['_id'] = news_id
                result_item['used_agent'] = False
                self.results.append(result_item)
                self.stats['processed_count'] += 1
            
            # 使用标准 return 语句，而不是 returnValue
            return []
        
        return process_async()
    
    def _process_in_thread(self, news_item, html_content, final_url, final_domain):
        """在线程池中处理（避免阻塞Scrapy）"""
        return process_single_news_with_html(
            news_item,
            html_content,
            final_url,
            final_domain,
            DEEPSEEK_CONFIG,
            self.memory_manager,
            self.error_manager
        )
    
    def errback(self, failure):
        news_item = failure.request.meta['news_item']
        url = failure.request.url
        
        # 提取域名
        from urllib.parse import urlparse
        domain = urlparse(url).netloc
        
        # 记录错误到黑名单
        error_reason = str(failure.value)
        if 'timeout' in error_reason.lower() or 'timed out' in error_reason.lower():
            error_reason = "请求超时"
        elif 'connection' in error_reason.lower():
            error_reason = "连接失败"
        else:
            error_reason = f"爬取失败: {error_reason[:50]}"
        
        self.error_manager.add_error(domain, error_reason)
        
        print(f"\n{'='*60}")
        print(f"[错误] 爬取失败: {url}")
        print(f"域名: {domain}")
        print(f"错误原因: {error_reason}")
        
        # 检查是否已加入黑名单
        if self.error_manager.is_blacklisted(domain):
            print(f"[黑名单] 域名 {domain} 已加入黑名单")
        
        sys.stdout.flush()
        
        news_id = re.sub(r'[^\w\-]', '_', f"{news_item.get('author', 'unknown')}_{news_item.get('title', 'unknown')[:20]}")
        result_item = news_item.copy()
        result_item['content'] = ''
        result_item['_id'] = news_id
        result_item['used_agent'] = False
        result_item['status'] = 'crawl_error'
        result_item['error_reason'] = error_reason
        self.results.append(result_item)
        self.stats['processed_count'] += 1


def process_single_news_with_html(news_item, html_content, final_url, final_domain, agent_config, memory_manager, error_manager):
    """处理单条新闻（已有HTML内容）"""
    browser_manager = ScrapyManager()
    browser_manager.start()
    
    # 确保 extractor 已初始化
    if not browser_manager.extractor:
        print("[错误] Scrapy 提取器初始化失败")
        news_id = re.sub(r'[^\w\-]', '_', f"{news_item.get('author', 'unknown')}_{news_item.get('title', 'unknown')[:20]}")
        return False, {**news_item, 'content': '', '_id': news_id, 'status': 'error', 'used_agent': False}
    
    news_id = re.sub(r'[^\w\-]', '_', f"{news_item['author']}_{news_item['title'][:20]}")
    
    if not all(key in news_item for key in ['title', 'url', 'author']):
        print(f"字段缺失，跳过: {news_item.get('title', '未知标题')}")
        browser_manager.stop()
        return False, {**news_item, 'content': '', '_id': news_id}
    
    url = news_item['url']
    if url.startswith('//'):
        url = 'https:' + url
    elif not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    
    print(f"\n{'='*60}")
    print(f"处理新闻: {news_item['title'][:50]}...")
    print(f"来源: {news_item['author']}")
    print(f"域名: {final_domain}")
    print(f"URL: {final_url}")
    
    locators = memory_manager.get_all_locators_by_domain(final_domain)
    if locators:
        print(f"[纯脚本模式] 域名 {final_domain} 找到 {len(locators)} 个定位器")
        print(f"{'='*60}")
        
        thread_news_info = {
            'title': news_item['title'],
            'url': url,
            'author': news_item['author'],
            'source': news_item.get('source', ''),
            'domain': final_domain,
            'html_content': html_content
        }
        _thread_local.init_context(browser_manager, thread_news_info)
        
        for idx, locator in enumerate(locators, 1):
            locator_type = locator.get('locator_type', '')
            locator_value = locator.get('locator_value', '')
            print(f"[纯脚本 {idx}/{len(locators)}] 使用 {locator_type}={locator_value} 提取正文")
            
            content = browser_manager.extractor.extract_text_by_selector(
                html_content, locator_type, locator_value
            )
            
            if content and len(content) >= 100:
                memory_manager.increment_locator_usage(final_domain, locator_value, success=True)
                print(f"\n[纯脚本成功] 提取正文长度: {len(content)} 字符")
                browser_manager.stop()
                return True, {**news_item, 'content': content, '_id': news_id, 'status': 'success', 'used_pure_script': True, 'used_agent': False}
            else:
                memory_manager.increment_locator_usage(final_domain, locator_value, success=False)
                print(f"[纯脚本 {idx}/{len(locators)}] 失败")
        
        print(f"[纯脚本全部失败] 回退到 Agent 处理")
    
    print(f"[Agent模式] 域名 {final_domain} 无有效定位器或纯脚本失败")
    print(f"{'='*60}")
    
    thread_news_info = {
        'title': news_item['title'],
        'url': url,
        'author': news_item['author'],
        'source': news_item.get('source', ''),
        'domain': final_domain,
        'html_content': html_content
    }
    _thread_local.init_context(browser_manager, thread_news_info)
    
    agent = DeepSeekAgentWithTools(agent_config)
    
    used_agent = True
    try:
        result = agent.process_news(news_item)
        
        if result.get("success"):
            content = result.get("content", "")
            locator = result.get("locator")
            
            if locator:
                loc_domain = locator.get('domain', '')
                existing = memory_manager.get_locator_by_domain(loc_domain)
                if existing:
                    old_locator_value = existing.get('locator_value', '')
                    new_locator_value = locator.get('locator_value', '')
                    
                    if old_locator_value != new_locator_value:
                        memory_manager.add_or_update_locator(
                            loc_domain,
                            locator.get('locator_type', 'xpath'),
                            new_locator_value,
                            locator.get('locator_desc', '')
                        )
                        print(f"\n[新增] 域名 {loc_domain} 添加新定位规则: {old_locator_value} -> {new_locator_value}")
                    else:
                        memory_manager.increment_locator_usage(loc_domain, new_locator_value, success=True)
                        print(f"\n[复用] 使用了缓存定位规则，域名: {loc_domain}")
                else:
                    memory_manager.add_or_update_locator(
                        loc_domain,
                        locator.get('locator_type', 'xpath'),
                        locator.get('locator_value', ''),
                        locator.get('locator_desc', '')
                    )
                    print(f"\n[新增] 域名 {loc_domain} 首次添加定位规则: {locator.get('locator_value', '')}")
            
            print(f"\n[成功] 提取正文长度: {len(content)} 字符")
            browser_manager.stop()
            return True, {**news_item, 'content': content, '_id': news_id, 'status': 'success', 'used_agent': used_agent}
        else:
            error_reason = result.get('error', '未知错误')
            error_manager.add_error(final_domain, error_reason)
            print(f"\n[失败] {error_reason}")
            browser_manager.stop()
            return False, {**news_item, 'content': '', '_id': news_id, 'status': 'failed', 'used_agent': used_agent}
            
    except Exception as e:
        error_manager.add_error(final_domain, f"处理异常: {str(e)}")
        print(f"\n[异常] 处理失败: {e}")
        browser_manager.stop()
        return False, {**news_item, 'content': '', '_id': news_id, 'status': 'error', 'used_agent': used_agent}


@defer.inlineCallbacks
def process_jsonl_file_scrapy(jsonl_file: str, concurrent_requests: int = 5):
    """使用Scrapy处理JSONL文件"""
    start_time = datetime.now()
    memory_manager = ThreadSafeMemoryManager(MEMORY_FILE)
    error_manager = ThreadSafeErrorManager(ERROR_FILE)
    
    total_items = 0
    news_items = []
    
    try:
        with open(jsonl_file, 'r', encoding='utf-8') as f:
            content = f.read().strip()
            if content.startswith('[') and content.endswith(']'):
                news_items = json.loads(content)
                total_items = len(news_items)
                print(f"检测文件包含 {total_items} 条新闻 (JSON数组格式)")
            else:
                lines = content.split('\n')
                for line in lines:
                    line = line.strip()
                    if line:
                        news_items.append(json.loads(line))
                total_items = len(news_items)
                print(f"检测文件包含 {total_items} 条新闻 (JSONL格式)")
    except Exception as e:
        print(f"读取和解析文件失败: {e}")
        return
    
    print(f"\n{'#'*60}")
    print(f"# Scrapy异步爬取模式")
    print(f"# 并发请求数: {concurrent_requests}")
    print(f"# 启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'#'*60}\n")
    
    configure_logging({'LOG_LEVEL': 'ERROR'})  # 只显示错误日志，其他用 print 输出
    
    # 直接创建 Settings 对象，而不是从项目加载
    from scrapy.settings import Settings
    settings = Settings()
    settings.set('CONCURRENT_REQUESTS', concurrent_requests)
    settings.set('CONCURRENT_REQUESTS_PER_DOMAIN', concurrent_requests)
    settings.set('USER_AGENT', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
    settings.set('ROBOTSTXT_OBEY', False)
    settings.set('RETRY_ENABLED', True)
    settings.set('RETRY_TIMES', 1)  # 减少重试次数
    settings.set('DOWNLOAD_TIMEOUT', 15)  # 降低超时时间
    settings.set('LOG_LEVEL', 'INFO')
    
    runner = CrawlerRunner(settings)
    
    # 传递 spider 类和参数，而不是 spider 对象
    yield runner.crawl(
        NewsSpider,
        news_list=news_items,
        memory_manager=memory_manager,
        error_manager=error_manager
    )
    
    end_time = datetime.now()
    elapsed_seconds = (end_time - start_time).total_seconds()
    
    print(f"\n\n{'='*60}")
    print(f"写入结果到 {OUTPUT_JSONL_FILE}...")
    
    # 使用全局变量获取 spider 的结果
    spider_results = _spider_results
    stats = _spider_stats
    
    with open(OUTPUT_JSONL_FILE, 'w', encoding='utf-8') as f:
        for item in spider_results:
            f.write(json.dumps(item, ensure_ascii=False) + '\n')
    
    print(f"\n处理完成!")
    print(f"  总条数: {total_items}")
    print(f"  处理条数: {stats['processed_count']}")
    print(f"  成功条数: {stats['success_count']}")
    print(f"  纯脚本提取: {stats['pure_script_count']}")
    print(f"  黑名单跳过: {stats['blacklisted_count']}")
    print(f"  正文成功率: {stats['success_count']/total_items*100:.1f}%" if total_items > 0 else "  正文成功率: 0%")
    print(f"  调用Agent次数: {stats['agent_count']}")
    print(f"  调用Agent率: {stats['agent_count']/total_items*100:.1f}%" if total_items > 0 else "  调用Agent率: 0%")
    print(f"  Agent调用成功次数: {stats['agent_success_count']}")
    print(f"  Agent调用成功率: {stats['agent_success_count']/stats['agent_count']*100:.1f}%" if stats['agent_count'] > 0 else "  Agent调用成功率: 0%")
    print(f"  完成所需时间: {elapsed_seconds:.1f}秒")
    
    # 输出API限流统计
    api_stats = api_rate_limiter.get_stats()
    print(f"\nAPI限流统计:")
    print(f"  总API调用次数: {api_stats['total_calls']}")
    print(f"  限流等待次数: {api_stats['total_waits']}")
    print(f"  QPS限制: {api_stats['qps_limit']}")
    print(f"  限流启用: {api_stats['enabled']}")
    
    print(f"{'='*60}")
    
    final_stats = {
        "start_time": start_time.strftime('%Y-%m-%d %H:%M:%S'),
        "end_time": end_time.strftime('%Y-%m-%d %H:%M:%S'),
        "elapsed_seconds": elapsed_seconds,
        "total_items": total_items,
        "processed_count": stats['processed_count'],
        "success_count": stats['success_count'],
        "pure_script_count": stats['pure_script_count'],
        "blacklisted_count": stats['blacklisted_count'],
        "agent_count": stats['agent_count'],
        "agent_success_count": stats['agent_success_count'],
        "agent_success_rate": stats['agent_success_count'] / stats['agent_count'] * 100 if stats['agent_count'] > 0 else 0,
        "success_rate": stats['success_count'] / total_items * 100 if total_items > 0 else 0,
        "agent_rate": stats['agent_count'] / total_items * 100 if total_items > 0 else 0,
        "api_rate_limit": api_rate_limiter.get_stats()
    }
    
    try:
        with open(STATS_FILE, 'w', encoding='utf-8') as f:
            json.dump(final_stats, f, ensure_ascii=False, indent=2)
        print(f"统计信息已保存到 {STATS_FILE}")
    except Exception as e:
        print(f"保存统计信息失败: {e}")


def process_jsonl_file_threaded(jsonl_file: str, max_workers: int = MAX_WORKERS):
    """兼容接口：使用Scrapy处理"""
    process_jsonl_file_scrapy(jsonl_file, concurrent_requests=max_workers)


if __name__ == "__main__":
    print(f"使用测试文件: {INPUT_JSONL_FILE}")
    print(f"并发请求数: {MAX_WORKERS}")
    print("开始Scrapy异步爬取处理...")
    
    from twisted.internet import reactor
    
    # 配置线程池大小
    reactor.suggestThreadPoolSize(MAX_WORKERS)
    print(f"线程池大小: {MAX_WORKERS}")
    
    def run_processing():
        """启动处理流程"""
        d = process_jsonl_file_scrapy(INPUT_JSONL_FILE, concurrent_requests=MAX_WORKERS)
        
        def stop_reactor(result):
            print("处理结束")
            reactor.stop()
            return result
        
        def handle_error(failure):
            print(f"处理出错: {failure}")
            reactor.stop()
            return failure
        
        d.addCallback(stop_reactor)
        d.addErrback(handle_error)
        
        return d
    
    # 延迟启动，确保 reactor 已经运行
    reactor.callWhenRunning(run_processing)
    
    # 启动 reactor（会阻塞直到处理完成）
    reactor.run()
    
    print("程序结束")
