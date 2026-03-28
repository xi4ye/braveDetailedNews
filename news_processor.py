#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新闻JSONL批量处理工具 - Agent Tools版本
功能：使用LangChain Agent + Tools模式，让AI自主调用工具进行正文定位和提取
核心改进：生成通用型定位表达式，基于域名存储到memory.json，后续无需调用模型
"""

import json
import os
import re
import sqlite3
import asyncio
from datetime import datetime
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse
from tqdm import tqdm

from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage

from scrapy_extractor import ScrapyManager, ScrapyExtractor, extract_domain


INPUT_JSONL_FILE = "crawled_news.jsonl"
OUTPUT_JSONL_FILE = "results.jsonl"
MAX_AGENT_STEPS = 20
CHROME_PROFILE_DIR = "ChromeProfile"
MEMORY_FILE = "memory.json"
COOKIES_FILE = "login_cookies.json"
MAX_LOGIN_RETRY = 5
ERROR_FILE = "error.json"
BLACKLIST_THRESHOLD = 10
PURE_SCRIPT_THRESHOLD = 1
STATS_FILE = "processor_stats.json"  # 统计信息保存文件

DEEPSEEK_CONFIG = {
    "model": "deepseek-chat",
    "base_url": "https://api.deepseek.com/v1",
    "api_key": os.environ.get("DEEPSEEK_API_KEY", "")
}


def extract_domain(url: str) -> str:
    """从URL中提取域名"""
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


class MemoryManager:
    """长效记忆管理器 - 基于 SQLite 存储，支持同一域名多个定位表达式"""
    
    def __init__(self, memory_file):
        self.db_file = memory_file.replace('.json', '.db')
        self.conn = None
        self._init_db()
        self._migrate_from_json(memory_file)
    
    def _get_conn(self):
        if self.conn is None:
            self.conn = sqlite3.connect(self.db_file, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
        return self.conn
    
    def _init_db(self):
        conn = self._get_conn()
        conn.execute('''
            CREATE TABLE IF NOT EXISTS locators (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                domain TEXT NOT NULL,
                locator_type TEXT NOT NULL,
                locator_value TEXT NOT NULL,
                locator_desc TEXT,
                locator_category TEXT DEFAULT 'content',
                usage_count INTEGER DEFAULT 0,
                success_count INTEGER DEFAULT 0,
                create_time TEXT,
                update_time TEXT,
                UNIQUE(domain, locator_value, locator_category)
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_domain ON locators(domain)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_success ON locators(domain, success_count DESC)')

        cursor = conn.execute("PRAGMA table_info(locators)")
        columns = [row[1] for row in cursor.fetchall()]
        if 'locator_category' not in columns:
            conn.execute("ALTER TABLE locators ADD COLUMN locator_category TEXT DEFAULT 'content'")
            print("[SQLite] 已添加 locator_category 字段")

        conn.execute('CREATE INDEX IF NOT EXISTS idx_category ON locators(domain, locator_category)')

        conn.commit()
    
    def _migrate_from_json(self, json_file):
        if not os.path.exists(json_file):
            return
        
        conn = self._get_conn()
        cursor = conn.execute('SELECT COUNT(*) as cnt FROM locators')
        existing_count = cursor.fetchone()['cnt']
        
        if existing_count > 0:
            print(f"[SQLite] 数据库已存在 {existing_count} 条记录，跳过迁移")
            return
        
        try:
            with open(json_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            migrated_count = 0
            for domain, domain_data in data.items():
                locators = domain_data.get('locators', [])
                if not locators:
                    old_locator = {
                        'locator_type': domain_data.get('locator_type'),
                        'locator_value': domain_data.get('locator_value'),
                        'locator_desc': domain_data.get('locator_desc'),
                        'usage_count': domain_data.get('usage_count', 0),
                        'success_count': domain_data.get('success_count', 0),
                        'create_time': domain_data.get('create_time'),
                        'update_time': domain_data.get('update_time')
                    }
                    if old_locator['locator_value']:
                        locators = [old_locator]
                
                for loc in locators:
                    conn.execute('''
                        INSERT OR IGNORE INTO locators
                        (domain, locator_type, locator_value, locator_desc, locator_category, usage_count, success_count, create_time, update_time)
                        VALUES (?, ?, ?, ?, 'content', ?, ?, ?, ?)
                    ''', (
                        domain,
                        loc.get('locator_type', 'xpath'),
                        loc.get('locator_value', ''),
                        loc.get('locator_desc', ''),
                        loc.get('usage_count', 0),
                        loc.get('success_count', 0),
                        loc.get('create_time', ''),
                        loc.get('update_time', '')
                    ))
                    migrated_count += 1
            conn.commit()
            print(f"[SQLite] 已从 {json_file} 迁移 {migrated_count} 条记录")
            os.rename(json_file, json_file + '.bak')
            print(f"[SQLite] 原 JSON 文件已重命名为 {json_file}.bak")
        except Exception as e:
            print(f"[SQLite 迁移失败] {e}")
    
    def get_locator_by_domain(self, domain: str, category: str = 'content') -> Optional[Dict]:
        conn = self._get_conn()
        cursor = conn.execute('''
            SELECT * FROM locators
            WHERE domain = ? AND locator_category = ?
            ORDER BY success_count DESC
            LIMIT 1
        ''', (domain, category))
        row = cursor.fetchone()
        if row:
            result = dict(row)
            print(f"[SQLite] 查询域名 {domain} [{category}]: 找到定位器 {result.get('locator_value')} (success_count={result.get('success_count')})")
            return result
        print(f"[SQLite] 查询域名 {domain} [{category}]: 未找到定位器")
        return None
    
    def get_all_locators_by_domain(self, domain: str, category: str = None) -> List[Dict]:
        conn = self._get_conn()
        if category:
            cursor = conn.execute('''
                SELECT * FROM locators
                WHERE domain = ? AND locator_category = ?
                ORDER BY success_count DESC
            ''', (domain, category))
        else:
            cursor = conn.execute('''
                SELECT * FROM locators
                WHERE domain = ?
                ORDER BY success_count DESC
            ''', (domain,))
        return [dict(row) for row in cursor.fetchall()]
    
    def add_or_update_locator(self, domain: str, locator_type: str, locator_value: str, locator_desc: str = "", locator_category: str = "content") -> bool:
        conn = self._get_conn()
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        cursor = conn.execute('''
            SELECT id, usage_count, success_count FROM locators
            WHERE domain = ? AND locator_value = ? AND locator_category = ?
        ''', (domain, locator_value, locator_category))
        row = cursor.fetchone()

        if row:
            conn.execute('''
                UPDATE locators
                SET usage_count = ?, success_count = ?, update_time = ?
                WHERE id = ?
            ''', (row['usage_count'] + 1, row['success_count'] + 1, now, row['id']))
            print(f"[SQLite] 更新定位器: {domain} | {locator_value} [{locator_category}] (usage={row['usage_count']+1}, success={row['success_count']+1})")
        else:
            conn.execute('''
                INSERT INTO locators
                (domain, locator_type, locator_value, locator_desc, locator_category, usage_count, success_count, create_time, update_time)
                VALUES (?, ?, ?, ?, ?, 1, 1, ?, ?)
            ''', (domain, locator_type, locator_value, locator_desc, locator_category, now, now))
            print(f"[SQLite] 新增定位器: {domain} | {locator_value} [{locator_category}]")

        conn.commit()
        return True

    def increment_locator_usage(self, domain: str, locator_value: str, success: bool = True, locator_category: str = 'content'):
        conn = self._get_conn()
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        cursor = conn.execute('''
            SELECT usage_count, success_count FROM locators
            WHERE domain = ? AND locator_value = ? AND locator_category = ?
        ''', (domain, locator_value, locator_category))
        row = cursor.fetchone()

        if success:
            conn.execute('''
                UPDATE locators
                SET usage_count = usage_count + 1, success_count = success_count + 1, update_time = ?
                WHERE domain = ? AND locator_value = ? AND locator_category = ?
            ''', (now, domain, locator_value, locator_category))
            if row:
                print(f"[SQLite] 成功计数: {domain} | {locator_value} [{locator_category}] (usage={row['usage_count']+1}, success={row['success_count']+1})")
        else:
            conn.execute('''
                UPDATE locators
                SET usage_count = usage_count + 1, update_time = ?
                WHERE domain = ? AND locator_value = ? AND locator_category = ?
            ''', (now, domain, locator_value, locator_category))
            if row:
                print(f"[SQLite] 失败计数: {domain} | {locator_value} [{locator_category}] (usage={row['usage_count']+1}, success={row['success_count']})")

        conn.commit()

    def update_or_add_locator(self, locator_json: Dict):
        domain = locator_json.get('domain', '')
        if not domain:
            return

        locator_type = locator_json.get('locator_type', '')
        locator_value = locator_json.get('locator_value', '')
        locator_desc = locator_json.get('locator_desc', '')
        locator_category = locator_json.get('locator_category', 'content')
        
        if not locator_value:
            return

        self.add_or_update_locator(domain, locator_type, locator_value, locator_desc, locator_category)

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None


class ErrorManager:
    """错误记录管理器 - 基于域名存储失败记录"""

    def __init__(self, error_file):
        self.error_file = error_file
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
        """根据域名获取错误记录"""
        return self.errors.get(domain)

    def add_error(self, domain: str, reason: str):
        """添加错误记录"""
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
            print(f"[黑名单] 域名 {domain} 已加入黑名单（失败 {self.errors[domain]['fail_count']} 次）")
        
        self._save_errors()

    def is_blacklisted(self, domain: str) -> bool:
        """检查域名是否在黑名单中"""
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


class BrowserManager:
    """浏览器管理器 - 使用 Scrapy 替代 Playwright"""
    
    _instance = None
    _extractor = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def start(self, proxy: str = None):
        if self._extractor is not None:
            return self
        
        self._extractor = ScrapyExtractor(proxy=proxy)
        print("Scrapy 提取器初始化成功")
        return self
    
    def stop(self):
        if self._extractor:
            self._extractor.close()
            self._extractor = None
    
    @property
    def extractor(self):
        return self._extractor
    
    @property
    def context(self):
        return self._extractor


_browser_manager: Optional[BrowserManager] = None
_current_news_info: Dict[str, Any] = {}
_memory_manager_global: Optional[MemoryManager] = None
_page_cache: Dict[str, tuple] = {}  # 页面缓存: url -> (html, final_url, status_code)
_tool_call_history: List[Dict] = []  # 工具调用历史记录
_extraction_completed: bool = False  # 提取是否已完成标志


def init_tools(browser_manager: BrowserManager, news_info: Dict[str, Any]):
    """初始化工具函数所需的上下文"""
    global _browser_manager, _current_news_info, _page_cache, _tool_call_history, _extraction_completed
    _browser_manager = browser_manager
    _current_news_info = news_info
    _page_cache = {}  # 重置页面缓存
    _tool_call_history = []  # 重置工具调用历史
    _extraction_completed = False  # 重置完成标志


def check_duplicate_tool_call(tool_name: str, tool_args: Dict) -> bool:
    """检查是否是重复的工具调用"""
    global _tool_call_history
    
    for call in _tool_call_history:
        if call["name"] == tool_name and call["args"] == tool_args:
            print(f"  [拦截] 检测到重复工具调用: {tool_name}({tool_args})，已跳过")
            return True
    return False


def record_tool_call(tool_name: str, tool_args: Dict):
    """记录工具调用历史"""
    global _tool_call_history
    _tool_call_history.append({
        "name": tool_name,
        "args": tool_args,
        "time": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    })


def check_extraction_completed() -> bool:
    """检查提取是否已完成"""
    global _extraction_completed
    return _extraction_completed


def mark_extraction_completed():
    """标记提取已完成"""
    global _extraction_completed
    _extraction_completed = True


def extract_content_pure(url: str, locator: Dict) -> Optional[str]:
    """纯脚本提取正文，使用 Scrapy 替代 Playwright
    
    Args:
        url: 新闻URL
        locator: 定位规则，包含 locator_type 和 locator_value
        
    Returns:
        提取的正文内容，失败返回 None
    """
    global _browser_manager
    
    if _browser_manager is None or _browser_manager.extractor is None:
        print("[纯脚本] 错误：Scrapy 提取器未初始化")
        return None
    
    locator_type = locator.get('locator_type', '')
    locator_value = locator.get('locator_value', '')
    
    if not locator_type or not locator_value:
        print("[纯脚本] 错误：定位规则不完整")
        return None
    
    print(f"[纯脚本] 使用 {locator_type}={locator_value} 提取正文")
    
    try:
        html_content, final_url, status_code = _browser_manager.extractor.fetch_page(url)
        
        if status_code >= 400:
            print(f"[纯脚本] 页面加载失败（HTTP {status_code}）")
            return None
        
        if not html_content:
            print("[纯脚本] 页面内容为空")
            return None
        
        content = _browser_manager.extractor.extract_text_by_selector(
            html_content, locator_type, locator_value
        )
        
        if not content:
            print("[纯脚本] 未找到匹配元素")
            return None
        
        content = re.sub(r'\s+', ' ', content).strip()
        
        if len(content) < 100:
            print(f"[纯脚本] 提取内容过短（{len(content)}字符）")
            return None
        
        print(f"[纯脚本] 成功提取正文，长度: {len(content)} 字符")
        return content
        
    except Exception as e:
        print(f"[纯脚本] 提取异常: {str(e)}")
        return None


def check_locator_is_generic(locator_value: str, title: str) -> Dict[str, Any]:
    """检查定位表达式是否为通用型（不包含文章特定内容）"""
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


def parse_date_expression(date_text: str) -> tuple:
    """
    解析日期表达式，返回标准格式的日期字符串和日期来源标记

    日期格式规则：
    - YYYY-MM-DD hh:mm:ss - 完整格式，直接返回
    - YYYY-MM-DD hh:mm - 用00补全秒
    - YYYY-MM-DD - 只有日期，用当前时间的 hh:mm 补全
    - 其他格式 - 尝试从文本中查找日期，失败则用当前日期时间

    Returns:
        (formatted_date_string, date_source)
    """
    if not date_text or not date_text.strip():
        now = datetime.now()
        return now.strftime('%Y-%m-%d %H:%M:%S'), 'current_time'

    text = date_text.strip()

    full_pattern = r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})'
    match = re.search(full_pattern, text)
    if match:
        return match.group(1), 'full_datetime'

    datetime_pattern = r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})'
    match = re.search(datetime_pattern, text)
    if match:
        datetime_str = match.group(1)
        return f"{datetime_str}:00", 'datetime_no_seconds'

    date_pattern = r'(\d{4}-\d{2}-\d{2})'
    match = re.search(date_pattern, text)
    if match:
        date_str = match.group(1)
        now = datetime.now()
        return f"{date_str} {now.strftime('%H:%M:%S')}", 'date_only'

    cn_absolute_pattern = r'(\d{4})年(\d{1,2})月(\d{1,2})日'
    match = re.search(cn_absolute_pattern, text)
    if match:
        year = match.group(1)
        month = match.group(2).zfill(2)
        day = match.group(3).zfill(2)
        now = datetime.now()
        return f"{year}-{month}-{day} {now.strftime('%H:%M:%S')}", 'cn_absolute'

    cn_datetime_pattern = r'(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{2})'
    match = re.search(cn_datetime_pattern, text)
    if match:
        year = match.group(1)
        month = match.group(2).zfill(2)
        day = match.group(3).zfill(2)
        hour = match.group(4).zfill(2)
        minute = match.group(5)
        return f"{year}-{month}-{day} {hour}:{minute}:00", 'cn_datetime'

    en_absolute_pattern = r'([A-Za-z]+)\s+(\d{1,2}),?\s*(\d{4})'
    match = re.search(en_absolute_pattern, text, re.IGNORECASE)
    if match:
        month_name = match.group(1).lower()
        day = match.group(2).zfill(2)
        year = match.group(3)
        month_map = {
            'january': '01', 'jan': '01',
            'february': '02', 'feb': '02',
            'march': '03', 'mar': '03',
            'april': '04', 'apr': '04',
            'may': '05',
            'june': '06', 'jun': '06',
            'july': '07', 'jul': '07',
            'august': '08', 'aug': '08',
            'september': '09', 'sep': '09',
            'october': '10', 'oct': '10',
            'november': '11', 'nov': '11',
            'december': '12', 'dec': '12'
        }
        if month_name in month_map:
            month = month_map[month_name]
            now = datetime.now()
            return f"{year}-{month}-{day} {now.strftime('%H:%M:%S')}", 'en_absolute'

    now = datetime.now()
    return now.strftime('%Y-%m-%d %H:%M:%S'), 'current_time_fallback'


@tool
def get_page_dom(url: str) -> str:
    """获取网页的DOM结构（使用 Scrapy 替代 Playwright）"""
    global _browser_manager, _current_news_info, _page_cache
    
    print(f"[Tool] 获取DOM: {url}")
    
    if _browser_manager is None or _browser_manager.extractor is None:
        return "错误：Scrapy 提取器未初始化"
    
    try:
        if url in _page_cache:
            print(f"  [缓存] 使用缓存的页面内容")
            html_content, final_url, status_code = _page_cache[url]
        else:
            html_content, final_url, status_code = _browser_manager.extractor.fetch_page(url)
            _page_cache[url] = (html_content, final_url, status_code)
        
        if status_code == 404:
            return "错误：页面不存在（404错误）"
        
        if status_code == 403:
            return "错误：访问被拒绝（403错误），可能需要登录或IP被封"
        
        if status_code >= 400:
            return f"错误：页面加载失败（HTTP {status_code}）"
        
        if not html_content:
            return "错误：页面内容为空"
        
        final_domain = extract_domain(final_url)
        _current_news_info['final_domain'] = final_domain
        print(f"[Tool] 最终域名: {final_domain}")
        
        if '<title>404' in html_content or 'class="error-404"' in html_content or 'id="error"' in html_content:
            return "错误：页面内容显示404错误"
        
        dom_preview = _browser_manager.extractor.get_dom_preview(html_content, max_length=10000)
        
        return json.dumps({
            "dom": dom_preview,
            "final_domain": final_domain,
            "final_url": final_url
        }, ensure_ascii=False)
        
    except Exception as e:
        return f"错误：页面加载异常 - {str(e)}"


@tool
def validate_locator(locator_type: str, locator_value: str) -> str:
    """
    验证定位器是否能正确找到正文元素（使用 Scrapy 替代 Playwright）。
    
    Args:
        locator_type: 定位类型，可选值：css_selector, xpath, id, class
        locator_value: 具体的定位值（CSS选择器、XPath表达式、ID或class名）
        
    Returns:
        验证结果JSON字符串，包含是否成功、提取的内容片段、是否为通用型等信息
    """
    url = _current_news_info.get('url', '')
    title = _current_news_info.get('title', '')
    
    print(f"[Tool] 验证定位器: {locator_type}={locator_value}")
    
    generic_check = check_locator_is_generic(locator_value, title)
    if not generic_check["is_generic"]:
        return json.dumps({
            "success": False,
            "error": "定位表达式不是通用型",
            "issues": generic_check["issues"],
            "hint": "请生成通用的定位表达式，不要包含文章标题、日期、人名等特定内容。例如：使用 class='article-content' 而不是 //p[contains(text(), '某文章特定内容')]"
        }, ensure_ascii=False)
    
    if _browser_manager is None or _browser_manager.extractor is None:
        return json.dumps({"success": False, "error": "Scrapy 提取器未初始化"}, ensure_ascii=False)
    
    try:
        if url in _page_cache:
            print(f"  [缓存] 使用缓存的页面内容")
            html_content, final_url, status_code = _page_cache[url]
        else:
            html_content, final_url, status_code = _browser_manager.extractor.fetch_page(url)
            _page_cache[url] = (html_content, final_url, status_code)
        
        if status_code >= 400:
            return json.dumps({"success": False, "error": f"页面加载失败（HTTP {status_code}）"}, ensure_ascii=False)
        
        if not html_content:
            return json.dumps({"success": False, "error": "页面内容为空"}, ensure_ascii=False)
        
        if '<title>404' in html_content or 'class="error-404"' in html_content or 'id="error"' in html_content:
            return json.dumps({"success": False, "error": "页面内容显示404错误"}, ensure_ascii=False)
        
        content = _browser_manager.extractor.extract_text_by_selector(
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
        
        ad_keywords = ["广告", "推广", "赞助", "广告位", "投放", "招商"]
        for keyword in ad_keywords:
            if keyword in content[:500]:
                return json.dumps({
                    "success": False,
                    "error": f"内容开头包含广告关键词: {keyword}，请尝试更精确的定位",
                    "content_preview": content[:500]
                }, ensure_ascii=False)
        
        title_keywords = re.findall(r'[\u4e00-\u9fa5]{2,}', title)
        if title_keywords:
            important_keywords = [kw for kw in title_keywords if len(kw) >= 3]
            if important_keywords:
                matched_count = sum(1 for kw in important_keywords if kw in content)
                similarity = matched_count / len(important_keywords)
            else:
                similarity = 1.0
        else:
            similarity = 1.0
        
        return json.dumps({
            "success": True,
            "content_length": len(content),
            "content_preview": content[:500],
            "similarity": similarity,
            "is_generic": True,
            "message": "定位验证成功，表达式为通用型"
        }, ensure_ascii=False)
        
    except Exception as e:
        return json.dumps({"success": False, "error": f"验证过程异常: {str(e)}"}, ensure_ascii=False)


@tool
def validate_date_locator(locator_type: str, locator_value: str) -> str:
    """
    验证日期定位器是否能正确找到日期元素。

    Args:
        locator_type: 定位类型，可选值：css_selector, xpath, id, class
        locator_value: 具体的定位值（CSS选择器、XPath表达式、ID或class名）

    Returns:
        验证结果JSON字符串，包含是否成功、提取的内容片段、是否为通用型等信息
    """
    url = _current_news_info.get('url', '')
    title = _current_news_info.get('title', '')

    print(f"[Tool] 验证日期定位器: {locator_type}={locator_value}")

    generic_check = check_locator_is_generic(locator_value, title)
    if not generic_check["is_generic"]:
        return json.dumps({
            "success": False,
            "error": "定位表达式不是通用型",
            "issues": generic_check["issues"],
            "hint": "请生成通用的日期定位表达式，不要包含文章标题、日期、人名等特定内容。"
        }, ensure_ascii=False)

    if _browser_manager is None or _browser_manager.extractor is None:
        return json.dumps({"success": False, "error": "Scrapy 提取器未初始化"}, ensure_ascii=False)

    try:
        if url in _page_cache:
            print(f"  [日期验证缓存] 使用缓存的页面内容")
            html_content, final_url, status_code = _page_cache[url]
        else:
            html_content, final_url, status_code = _browser_manager.extractor.fetch_page(url)
            _page_cache[url] = (html_content, final_url, status_code)

        if status_code >= 400:
            return json.dumps({"success": False, "error": f"页面加载失败（HTTP {status_code}）"}, ensure_ascii=False)

        if not html_content:
            return json.dumps({"success": False, "error": "页面内容为空"}, ensure_ascii=False)

        if '<title>404' in html_content or 'class="error-404"' in html_content or 'id="error"' in html_content:
            return json.dumps({"success": False, "error": "页面内容显示404错误"}, ensure_ascii=False)

        content = _browser_manager.extractor.extract_text_by_selector(
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
def extract_content(locator_type: str, locator_value: str) -> str:
    """
    使用定位器提取网页正文内容（使用 Scrapy 替代 Playwright）。
    
    Args:
        locator_type: 定位类型，可选值：css_selector, xpath, id, class
        locator_value: 具体的定位值
        
    Returns:
        提取的正文内容，如果失败返回错误信息
    """
    url = _current_news_info.get('url', '')
    
    print(f"[Tool] 提取正文: {locator_type}={locator_value}")
    
    if _browser_manager is None or _browser_manager.extractor is None:
        return "错误：Scrapy 提取器未初始化"
    
    try:
        if url in _page_cache:
            print(f"  [缓存] 使用缓存的页面内容")
            html_content, final_url, status_code = _page_cache[url]
        else:
            html_content, final_url, status_code = _browser_manager.extractor.fetch_page(url)
            _page_cache[url] = (html_content, final_url, status_code)
        
        if status_code >= 400:
            return f"错误：页面加载失败（HTTP {status_code}）"
        
        if not html_content:
            return "错误：页面内容为空"
        
        if '<title>404' in html_content or 'class="error-404"' in html_content or 'id="error"' in html_content:
            return "错误：页面内容显示404错误"
        
        content = _browser_manager.extractor.extract_text_by_selector(
            html_content, locator_type, locator_value
        )
        
        if not content:
            return "错误：未找到匹配元素"
        
        content = re.sub(r'\s+', ' ', content).strip()

        return content

    except Exception as e:
        return f"错误：提取过程异常 - {str(e)}"


@tool
def extract_date(locator_type: str, locator_value: str) -> str:
    """
    使用定位器提取网页中的日期信息。

    Args:
        locator_type: 定位类型，可选值：css_selector, xpath, id, class
        locator_value: 具体的定位值

    Returns:
        提取的日期文本，如果失败返回错误信息
    """
    url = _current_news_info.get('url', '')

    print(f"[Tool] 提取日期: {locator_type}={locator_value}")

    if _browser_manager is None or _browser_manager.extractor is None:
        return "错误：Scrapy 提取器未初始化"

    try:
        if url in _page_cache:
            print(f"  [日期缓存] 使用缓存的页面内容")
            html_content, final_url, status_code = _page_cache[url]
        else:
            html_content, final_url, status_code = _browser_manager.extractor.fetch_page(url)
            _page_cache[url] = (html_content, final_url, status_code)

        if status_code >= 400:
            print(f"  [日期失败] 页面加载失败（HTTP {status_code}）")
            return f"错误：页面加载失败（HTTP {status_code}）"

        if not html_content:
            print(f"  [日期失败] 页面内容为空")
            return "错误：页面内容为空"

        if '<title>404' in html_content or 'class="error-404"' in html_content or 'id="error"' in html_content:
            print(f"  [日期失败] 页面返回404错误")
            return "错误：页面内容显示404错误"

        print(f"  [日期提取] 使用 {locator_type}={locator_value} 提取日期")
        date_text = _browser_manager.extractor.extract_text_by_selector(
            html_content, locator_type, locator_value
        )

        if not date_text:
            print(f"  [日期失败] 未找到日期元素")
            return "错误：未找到日期元素"

        date_text = re.sub(r'\s+', ' ', date_text).strip()
        print(f"  [日期成功] 提取到日期文本: {date_text}")

        return date_text

    except Exception as e:
        print(f"  [日期异常] 提取日期异常: {str(e)}")
        return f"错误：提取日期异常 - {str(e)}"


@tool
def get_existing_locator(domain: str) -> str:
    """
    查询某个域名是否已有保存的定位规则。
    
    Args:
        domain: 网站域名
        
    Returns:
        如果存在返回定位规则JSON，否则返回提示信息
    """
    global _current_news_info, _memory_manager_global
    
    if _memory_manager_global is not None:
        locator = _memory_manager_global.get_locator_by_domain(domain)
        if locator:
            return json.dumps({
                "exists": True,
                "locator": locator,
                "message": f"域名 {domain} 已有定位规则，可以直接使用"
            }, ensure_ascii=False)
    
    return json.dumps({
        "exists": False,
        "message": f"域名 {domain} 暂无定位规则，需要生成新的"
    }, ensure_ascii=False)


@tool
def save_locator(locator_type: str, locator_value: str, locator_desc: str, locator_category: str = "content") -> str:
    """
    保存验证通过的通用型定位规则到记忆库。

    Args:
        locator_type: 定位类型
        locator_value: 定位值（必须是通用型表达式）
        locator_desc: 定位方式描述
        locator_category: 定位器类别，'content' 或 'date'

    Returns:
        保存结果
    """
    global _current_news_info

    domain = _current_news_info.get('final_domain', _current_news_info.get('domain', ''))

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
        _current_news_info['_saved_date_locator'] = locator_json
    else:
        _current_news_info['_saved_locator'] = locator_json

    return json.dumps({
        "success": True,
        "message": f"通用型定位规则已保存，域名: {domain}，类别: {locator_category}",
        "locator": locator_json
    }, ensure_ascii=False)


@tool
def save_date_locator(locator_type: str, locator_value: str, locator_desc: str) -> str:
    """
    保存验证通过的日期定位规则到记忆库。

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
    """
    放弃处理当前新闻，提前终止Agent循环。
    
    当遇到以下情况时应调用此工具：
    - 页面返回404错误，无法访问
    - 页面内容已失效或被删除
    - 多次尝试都无法成功定位正文
    - 其他无法恢复的错误
    
    Args:
        reason: 放弃的原因（如：页面404、内容已删除、无法定位等）
        
    Returns:
        放弃处理的确认信息
    """
    global _current_news_info
    _current_news_info['_give_up'] = True
    _current_news_info['_give_up_reason'] = reason
    
    return json.dumps({
        "success": False,
        "action": "give_up",
        "reason": reason,
        "message": f"已放弃处理当前新闻，原因: {reason}。请返回失败结果。"
    }, ensure_ascii=False)


class DeepSeekAgentWithTools:
    """使用Tools的DeepSeek Agent"""

    def __init__(self, config, memory_manager: MemoryManager):
        self.llm = ChatOpenAI(
            model=config["model"],
            base_url=config["base_url"],
            api_key=config["api_key"],
            temperature=0
        )
        self.memory_manager = memory_manager
        self.tools = [get_page_dom, validate_locator, validate_date_locator, extract_content, extract_date, get_existing_locator, save_locator, save_date_locator, give_up]
        self.llm_with_tools = self.llm.bind_tools(self.tools)
    
    def process_news(self, news_item: Dict[str, Any]) -> Dict[str, Any]:
        """处理单条新闻，使用Agent自主调用工具"""
        
        url = news_item['url']
        if url.startswith('//'):
            url = 'https:' + url
        elif not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        
        news_info = {
            'title': news_item['title'],
            'url': url,
            'author': news_item['author'],
            'source': news_item.get('source', ''),
            'domain': extract_domain(url)
        }
        
        init_tools(_browser_manager, news_info)

        existing_content_locator = None
        existing_date_locator = None

        system_prompt = f"""你是一个专业的网页正文定位专家。你的任务有两项：
1. 找到网页中新闻正文的位置并提取正文内容
2. 找到网页中发布日期的位置并提取日期表达式

【核心要求】生成的定位表达式必须是【通用型】：
- 同一域名下的所有文章都能使用该表达式
- 禁止包含文章标题、日期、人名、公司名等特定内容
- 禁止使用 contains(text(), '某具体内容') 这种特定内容的定位方式
- 应该使用页面结构特征：如 class名、id名、标签层级关系等

【正文定位正确示例】：
- css_selector: "div.article-content", "#js_content", ".rich_media_content", "div.content article"
- xpath: "//article//div[@class='content']", "//div[contains(@class, 'article-body')]"
- id: "artibody", "js_content", "article-content"

【正文定位错误示例】（禁止使用）：
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
            print(f"\n[Agent Step {step + 1}/{MAX_AGENT_STEPS}]")

            try:
                response = self.llm_with_tools.invoke(messages)
                messages.append(response)

                if response.tool_calls:
                    for tool_call in response.tool_calls:
                        tool_name = tool_call["name"]
                        tool_args = tool_call["args"]

                        print(f"  → 调用工具: {tool_name}({tool_args})")

                        tool_func = None
                        for t in self.tools:
                            if t.name == tool_name:
                                tool_func = t
                                break

                        if tool_func:
                            if content_extraction_completed and date_extraction_completed:
                                print(f"  [拦截] 提取已完成，跳过工具调用: {tool_name}")
                                tool_result = json.dumps({"success": False, "error": "提取已完成，无需继续操作"}, ensure_ascii=False)
                            elif check_duplicate_tool_call(tool_name, tool_args):
                                tool_result = json.dumps({"success": False, "error": "重复调用，已跳过"}, ensure_ascii=False)
                            else:
                                record_tool_call(tool_name, tool_args)
                                try:
                                    tool_result = tool_func.invoke(tool_args)
                                    print(f"  ← 工具结果: {tool_result[:200] if len(str(tool_result)) > 200 else tool_result}")
                                except Exception as e:
                                    tool_result = f"工具执行错误: {str(e)}"

                            messages.append(ToolMessage(
                                content=str(tool_result),
                                tool_call_id=tool_call["id"]
                            ))

                            if tool_name == "give_up":
                                try:
                                    result = json.loads(tool_result)
                                    print(f"\n[放弃] Agent主动放弃处理，原因: {result.get('reason', '未知')}")
                                    return {
                                        "success": False,
                                        "content": "",
                                        "date_text": None,
                                        "content_locator": None,
                                        "date_locator": None,
                                        "error": result.get("reason", "Agent主动放弃"),
                                        "_give_up": True,
                                        "_give_up_reason": result.get("reason", "Agent主动放弃")
                                    }
                                except:
                                    return {
                                        "success": False,
                                        "content": "",
                                        "date_text": None,
                                        "content_locator": None,
                                        "date_locator": None,
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
                                    print(f"\n[正文提取成功] 长度: {len(final_content)} 字符")

                                    if last_validated_content_locator:
                                        print(f"[检测到已验证的正文定位器]")
                                        domain = _current_news_info.get('final_domain', _current_news_info.get('domain', ''))
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
                                        domain = _current_news_info.get('final_domain', _current_news_info.get('domain', ''))
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

    def process_news_for_date(self, news_item: Dict[str, Any], html_content: str, final_domain: str) -> Dict:
        """
        专门用于获取日期的 Agent 流程。

        Args:
            news_item: 新闻信息
            html_content: 已获取的页面 HTML
            final_domain: 最终域名

        Returns:
            {"date_text": str, "date_locator": Dict}
        """
        global _current_news_info, _page_cache

        url = news_item['url']
        if url.startswith('//'):
            url = 'https:' + url
        elif not url.startswith(('http://', 'https://')):
            url = 'https://' + url

        _page_cache[url] = (html_content, url, 200)

        _current_news_info = {
            'title': news_item['title'],
            'url': url,
            'author': news_item['author'],
            'source': news_item.get('source', ''),
            'domain': final_domain,
            'html_content': html_content,
            'final_domain': final_domain
        }

        existing_date_locator = self.memory_manager.get_locator_by_domain(final_domain, category='date')

        if existing_date_locator:
            print(f"  [日期缓存] 找到已缓存的日期定位器: {existing_date_locator.get('locator_type')}={existing_date_locator.get('locator_value')}")
            date_text = _browser_manager.extractor.extract_text_by_selector(
                html_content,
                existing_date_locator.get('locator_type', 'xpath'),
                existing_date_locator.get('locator_value', '')
            )
            if date_text and date_text.strip():
                date_text = re.sub(r'\s+', ' ', date_text).strip()
                print(f"  [日期缓存成功] 使用缓存定位器提取到日期: {date_text}")
                return {
                    "date_text": date_text,
                    "date_locator": existing_date_locator
                }
            else:
                print(f"  [日期缓存失败] 缓存的日期定位器无法提取日期")

        system_prompt = f"""你是一个专业的网页日期定位专家。你的任务是从网页中找到新闻发布日期的位置并提取。

【核心要求】生成的定位表达式必须是【通用型】：
- 同一域名下的所有文章都能使用该表达式
- 禁止包含文章标题、日期、人名、公司名等特定内容
- 应该使用页面结构特征：如 class名、id名、标签层级关系等

【日期定位常见位置】：
- 文章标题下方
- 作者信息附近
- 网页头部 meta 标签
- class="date"、class="time"、class="publish-time"、id="pubdate" 等

【日期格式返回要求】：
- 如果找到完整日期时间，返回标准格式: YYYY-MM-DD hh:mm:ss
- 如果只有日期部分 (YYYY-MM-DD)，请只返回该部分
- 如果无法找到日期，返回空字符串

当前新闻信息：
- 标题: {news_item['title']}
- 来源: {news_item['author']}
- URL: {news_item['url']}
- 域名: {final_domain}

你可以使用以下工具：
1. validate_date_locator(locator_type, locator_value) - 验证日期定位器是否为通用型
2. extract_date(locator_type, locator_value) - 使用定位器提取日期文本
3. save_date_locator(locator_type, locator_value, locator_desc) - 保存日期定位规则
4. give_up(reason) - 放弃（当页面没有日期或无法提取时使用）

【工作流程】：
1. 分析 DOM 找到日期元素
2. validate_date_locator 验证定位器是否为通用型
3. extract_date 提取日期文本
4. 提取成功后 save_date_locator
5. 返回日期文本和定位器

请开始工作。"""

        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content="请从网页中提取新闻发布日期。")
        ]

        final_date_text = None
        final_date_locator = None
        last_validated_locator = None

        for step in range(10):
            print(f"  [日期Agent Step {step + 1}/10]")

            try:
                response = self.llm_with_tools.invoke(messages)
                messages.append(response)

                if response.tool_calls:
                    for tool_call in response.tool_calls:
                        tool_name = tool_call["name"]
                        tool_args = tool_call["args"]

                        print(f"    → 调用工具: {tool_name}")

                        tool_func = None
                        for t in self.tools:
                            if t.name == tool_name:
                                tool_func = t
                                break

                        if tool_func:
                            try:
                                tool_result = tool_func.invoke(tool_args)
                                print(f"    ← 工具结果: {tool_result[:100] if len(str(tool_result)) > 100 else tool_result}")
                            except Exception as e:
                                tool_result = f"工具执行错误: {str(e)}"

                            messages.append(ToolMessage(
                                content=str(tool_result),
                                tool_call_id=tool_call["id"]
                            ))

                            if tool_name == "give_up":
                                return {"date_text": None, "date_locator": None}

                            if tool_name == "validate_date_locator":
                                try:
                                    result = json.loads(tool_result)
                                    if result.get("success"):
                                        locator_type = tool_args.get("locator_type")
                                        locator_value = tool_args.get("locator_value")
                                        last_validated_locator = {
                                            "locator_type": locator_type,
                                            "locator_value": locator_value
                                        }
                                        print(f"    [日期定位器验证成功] {locator_type}={locator_value}")
                                except:
                                    pass

                            if tool_name == "extract_date":
                                if not str(tool_result).startswith("错误") and tool_result.strip():
                                    final_date_text = tool_result
                                    print(f"    [日期提取成功] {final_date_text}")
                                    if last_validated_locator:
                                        final_date_locator = {
                                            **last_validated_locator,
                                            "domain": final_domain,
                                            "locator_category": "date",
                                            "locator_desc": "Agent保存",
                                            "create_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                                        }

                            if tool_name == "save_date_locator":
                                try:
                                    result = json.loads(tool_result)
                                    if result.get("success"):
                                        final_date_locator = result.get("locator")
                                        print(f"    [日期定位器保存成功]")
                                except:
                                    pass

                else:
                    content = response.content
                    if final_date_text:
                        print(f"    [检测到完成信号]")
                        break

                    if step >= 9:
                        break

            except Exception as e:
                print(f"    [日期Agent错误] {e}")
                if step >= 9:
                    break

        return {
            "date_text": final_date_text,
            "date_locator": final_date_locator
        }


def process_news_item(news_item: Dict[str, Any], agent: DeepSeekAgentWithTools, memory_manager: MemoryManager, error_manager: ErrorManager, browser_manager: BrowserManager) -> tuple:
    """处理单条新闻"""
    global _current_news_info

    news_id = re.sub(r'[^\w\-]', '_', f"{news_item['author']}_{news_item['title'][:20]}")

    if not all(key in news_item for key in ['title', 'url', 'author']):
        print(f"字段缺失，跳过: {news_item.get('title', '未知标题')}")
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
        return False, {**news_item, 'content': '', '_id': news_id, 'status': 'blacklisted'}

    if status_code >= 400:
        error_manager.add_error(final_domain, f"HTTP {status_code}")
        print(f"[失败] 页面加载失败（HTTP {status_code}）")
        print(f"{'='*60}")
        return False, {**news_item, 'content': '', '_id': news_id, 'status': 'http_error'}

    if not html_content:
        error_manager.add_error(final_domain, "页面内容为空")
        print(f"[失败] 页面内容为空")
        print(f"{'='*60}")
        return False, {**news_item, 'content': '', '_id': news_id, 'status': 'empty_content'}

    content_locators = memory_manager.get_all_locators_by_domain(final_domain, category='content')
    date_locators = memory_manager.get_all_locators_by_domain(final_domain, category='date')

    if content_locators or date_locators:
        print(f"[纯脚本模式] 域名 {final_domain} 找到 {len(content_locators)} 个正文定位器, {len(date_locators)} 个日期定位器")
        print(f"{'='*60}")

        _current_news_info = {
            'title': news_item['title'],
            'url': url,
            'author': news_item['author'],
            'source': news_item.get('source', ''),
            'domain': final_domain,
            'html_content': html_content
        }
        init_tools(browser_manager, _current_news_info)

        final_content = None
        final_date = None
        date_source = 'brave'
        need_date_agent = False

        for idx, locator in enumerate(content_locators, 1):
            locator_type = locator.get('locator_type', '')
            locator_value = locator.get('locator_value', '')
            print(f"[纯脚本 {idx}/{len(content_locators)}] 使用 {locator_type}={locator_value} 提取正文")

            content = browser_manager.extractor.extract_text_by_selector(
                html_content, locator_type, locator_value
            )

            if content and len(content) >= 100:
                locator_cat = locator.get('locator_category', 'content')
                memory_manager.increment_locator_usage(final_domain, locator_value, success=True, locator_category=locator_cat)
                print(f"\n[纯脚本成功] 提取正文长度: {len(content)} 字符")
                final_content = content
                break
            else:
                locator_cat = locator.get('locator_category', 'content')
                memory_manager.increment_locator_usage(final_domain, locator_value, success=False, locator_category=locator_cat)
                print(f"[纯脚本 {idx}/{len(content_locators)}] 失败")

        if final_content and not date_locators:
            print(f"[纯脚本] 正文提取成功，但无日期定位器，尝试 Agent 获取日期...")
            need_date_agent = True
        elif final_content and date_locators:
            for idx, locator in enumerate(date_locators, 1):
                locator_type = locator.get('locator_type', '')
                locator_value = locator.get('locator_value', '')
                print(f"[纯脚本日期 {idx}/{len(date_locators)}] 使用 {locator_type}={locator_value} 提取日期")

                date_text = browser_manager.extractor.extract_text_by_selector(
                    html_content, locator_type, locator_value
                )

                if date_text and date_text.strip():
                    date_text = re.sub(r'\s+', ' ', date_text).strip()
                    print(f"[纯脚本日期] 提取到原始文本: {date_text}")
                    parsed_date, parsed_source = parse_date_expression(date_text)
                    if parsed_date:
                        final_date = parsed_date
                        date_source = 'content'
                        print(f"[纯脚本日期成功] 解析后日期: {final_date} (来源: {date_source})")
                        break
                    else:
                        print(f"[纯脚本日期解析失败] 无法解析日期文本: {date_text}")
                else:
                    print(f"[纯脚本日期 {idx}/{len(date_locators)}] 未提取到文本")

        if final_content:
            result_item = {**news_item, 'content': final_content, '_id': news_id, 'status': 'success', 'used_pure_script': True, 'used_agent': False, 'used_date_agent': False}

            if final_date:
                result_item['parsed_date'] = final_date
                result_item['date_source'] = date_source
                print(f"[日期] 使用正文日期: {final_date}")
            elif need_date_agent:
                print(f"[Agent日期] 启动 Agent 获取日期定位器...")
                result_item['used_date_agent'] = True
                date_result = agent.process_news_for_date(news_item, html_content, final_domain)
                date_text = date_result.get("date_text")
                date_locator = date_result.get("date_locator")

                if date_text:
                    parsed_date, date_source = parse_date_expression(date_text)
                    if parsed_date:
                        final_date = parsed_date
                        date_source = 'content'
                        print(f"[Agent日期成功] 解析后日期: {final_date} (来源: {date_source})")
                        result_item['parsed_date'] = final_date
                        result_item['date_source'] = date_source

                        if date_locator:
                            memory_manager.add_or_update_locator(
                                final_domain,
                                date_locator.get('locator_type', 'xpath'),
                                date_locator.get('locator_value', ''),
                                date_locator.get('locator_desc', ''),
                                locator_category='date'
                            )
                            print(f"[新增] 域名 {final_domain} 添加日期定位规则: {date_locator.get('locator_value', '')}")
                    else:
                        print(f"[Agent日期失败] 无法解析日期文本: {date_text}")
                        brave_date = news_item.get('parsed_date')
                        if brave_date:
                            result_item['parsed_date'] = brave_date
                            result_item['date_source'] = news_item.get('date_source', 'brave')
                            print(f"[日期] 回退到 Brave 日期: {brave_date}")
                        else:
                            now = datetime.now()
                            result_item['parsed_date'] = now.strftime('%Y-%m-%d %H:%M:%S')
                            result_item['date_source'] = 'current_time'
                            print(f"[日期] 无可用日期，使用当前时间: {result_item['parsed_date']}")
                else:
                    print(f"[Agent日期失败] Agent 未能提取日期")
                    brave_date = news_item.get('parsed_date')
                    if brave_date:
                        result_item['parsed_date'] = brave_date
                        result_item['date_source'] = news_item.get('date_source', 'brave')
                        print(f"[日期] 回退到 Brave 日期: {brave_date}")
                    else:
                        now = datetime.now()
                        result_item['parsed_date'] = now.strftime('%Y-%m-%d %H:%M:%S')
                        result_item['date_source'] = 'current_time'
                        print(f"[日期] 无可用日期，使用当前时间: {result_item['parsed_date']}")
            else:
                brave_date = news_item.get('parsed_date')
                if brave_date:
                    result_item['parsed_date'] = brave_date
                    result_item['date_source'] = news_item.get('date_source', 'brave')
                    print(f"[日期] 回退到 Brave 日期: {brave_date}")
                else:
                    now = datetime.now()
                    result_item['parsed_date'] = now.strftime('%Y-%m-%d %H:%M:%S')
                    result_item['date_source'] = 'current_time'
                    print(f"[日期] 无可用日期，使用当前时间: {result_item['parsed_date']}")

            return True, result_item

        print(f"[纯脚本全部失败] 回退到 Agent 处理")

    print(f"[Agent模式] 域名 {final_domain} 无有效定位器或纯脚本失败")
    print(f"{'='*60}")

    _current_news_info = {
        'title': news_item['title'],
        'url': url,
        'author': news_item['author'],
        'source': news_item.get('source', ''),
        'domain': final_domain,
        'html_content': html_content
    }
    init_tools(browser_manager, _current_news_info)

    used_agent = True
    try:
        result = agent.process_news(news_item)

        if result.get("success"):
            content = result.get("content", "")
            content_locator = result.get("content_locator")
            date_locator = result.get("date_locator")
            date_text = result.get("date_text")

            if content_locator:
                loc_domain = content_locator.get('domain', '')
                existing = memory_manager.get_locator_by_domain(loc_domain, category='content')
                if existing:
                    old_locator_value = existing.get('locator_value', '')
                    new_locator_value = content_locator.get('locator_value', '')

                    if old_locator_value != new_locator_value:
                        memory_manager.add_or_update_locator(
                            loc_domain,
                            content_locator.get('locator_type', 'xpath'),
                            new_locator_value,
                            content_locator.get('locator_desc', ''),
                            locator_category='content'
                        )
                        print(f"\n[新增] 域名 {loc_domain} 添加新正文定位规则: {old_locator_value} -> {new_locator_value}")
                    else:
                        memory_manager.increment_locator_usage(loc_domain, new_locator_value, success=True)
                        print(f"\n[复用] 使用了缓存正文定位规则，域名: {loc_domain}")
                else:
                    memory_manager.add_or_update_locator(
                        loc_domain,
                        content_locator.get('locator_type', 'xpath'),
                        content_locator.get('locator_value', ''),
                        content_locator.get('locator_desc', ''),
                        locator_category='content'
                    )
                    print(f"\n[新增] 域名 {loc_domain} 首次添加正文定位规则: {content_locator.get('locator_value', '')}")

            if date_locator:
                loc_domain = date_locator.get('domain', '')
                existing = memory_manager.get_locator_by_domain(loc_domain, category='date')
                if existing:
                    old_locator_value = existing.get('locator_value', '')
                    new_locator_value = date_locator.get('locator_value', '')

                    if old_locator_value != new_locator_value:
                        memory_manager.add_or_update_locator(
                            loc_domain,
                            date_locator.get('locator_type', 'xpath'),
                            new_locator_value,
                            date_locator.get('locator_desc', ''),
                            locator_category='date'
                        )
                        print(f"\n[新增] 域名 {loc_domain} 添加新日期定位规则: {old_locator_value} -> {new_locator_value}")
                    else:
                        memory_manager.increment_locator_usage(loc_domain, new_locator_value, success=True)
                else:
                    memory_manager.add_or_update_locator(
                        loc_domain,
                        date_locator.get('locator_type', 'xpath'),
                        date_locator.get('locator_value', ''),
                        date_locator.get('locator_desc', ''),
                        locator_category='date'
                    )
                    print(f"\n[新增] 域名 {loc_domain} 首次添加日期定位规则: {date_locator.get('locator_value', '')}")

            result_item = {**news_item, 'content': content, '_id': news_id, 'status': 'success', 'used_agent': used_agent}

            if date_text:
                parsed_date, date_source = parse_date_expression(date_text)
                result_item['parsed_date'] = parsed_date
                result_item['date_source'] = date_source
                print(f"[日期] 使用正文日期: {parsed_date} (来源: {date_source})")
            else:
                brave_date = news_item.get('parsed_date')
                if brave_date:
                    result_item['parsed_date'] = brave_date
                    result_item['date_source'] = news_item.get('date_source', 'brave')
                    print(f"[日期] 回退到 Brave 日期: {brave_date}")
                else:
                    now = datetime.now()
                    result_item['parsed_date'] = now.strftime('%Y-%m-%d %H:%M:%S')
                    result_item['date_source'] = 'current_time'
                    print(f"[日期] 无可用日期，使用当前时间: {result_item['parsed_date']}")

            print(f"\n[成功] 提取正文长度: {len(content)} 字符")
            return True, result_item
        else:
            error_reason = result.get('error', '未知错误')
            error_manager.add_error(final_domain, error_reason)
            print(f"\n[失败] {error_reason}")

            result_item = {**news_item, 'content': '', '_id': news_id, 'status': 'failed', 'used_agent': used_agent}
            brave_date = news_item.get('parsed_date')
            if brave_date:
                result_item['parsed_date'] = brave_date
                result_item['date_source'] = news_item.get('date_source', 'brave')
            return False, result_item

    except Exception as e:
        error_manager.add_error(final_domain, f"处理异常: {str(e)}")
        print(f"\n[异常] 处理失败: {e}")
        result_item = {**news_item, 'content': '', '_id': news_id, 'status': 'error', 'used_agent': used_agent}
        brave_date = news_item.get('parsed_date')
        if brave_date:
            result_item['parsed_date'] = brave_date
            result_item['date_source'] = news_item.get('date_source', 'brave')
        return False, result_item


def process_jsonl_file(jsonl_file: str):
    """处理JSONL文件"""
    start_time = datetime.now()
    global _browser_manager, _memory_manager_global
    
    memory_manager = MemoryManager(MEMORY_FILE)
    error_manager = ErrorManager(ERROR_FILE)
    _memory_manager_global = memory_manager
    
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
    
    _browser_manager = BrowserManager()
    _browser_manager.start()
    
    agent = DeepSeekAgentWithTools(DEEPSEEK_CONFIG, memory_manager)
    
    processed_count = 0
    success_count = 0
    cached_count = 0
    pure_script_count = 0
    blacklisted_count = 0
    agent_count = 0
    agent_success_count = 0
    agent_failed_count = 0
    date_agent_count = 0
    date_agent_success_count = 0
    date_from_content_count = 0
    date_from_brave_count = 0
    date_from_current_count = 0
    news_results = []
    
    try:
        for item_num, news_item in tqdm(enumerate(news_items, 1), total=total_items, desc="处理进度"):
            print(f"\n\n{'#'*60}")
            print(f"# 处理第 {item_num}/{total_items} 条新闻")
            print(f"{'#'*60}")
            
            try:
                processed_count += 1
                
                if 'source' not in news_item:
                    news_item['source'] = ''
                
                success, result_item = process_news_item(news_item, agent, memory_manager, error_manager, _browser_manager)
                news_results.append(result_item)
                
                if success:
                    success_count += 1
                    if result_item.get('used_cached_locator'):
                        cached_count += 1
                    if result_item.get('used_pure_script'):
                        pure_script_count += 1

                    date_source = result_item.get('date_source', '')
                    if date_source == 'content':
                        date_from_content_count += 1
                    elif date_source == 'brave':
                        date_from_brave_count += 1
                    elif date_source == 'current_time' or date_source == 'current_time_fallback':
                        date_from_current_count += 1

                elif result_item.get('status') == 'blacklisted':
                    blacklisted_count += 1

                if result_item.get('used_agent'):
                    agent_count += 1
                    if success:
                        agent_success_count += 1
                    else:
                        agent_failed_count += 1

                if result_item.get('used_date_agent'):
                    date_agent_count += 1
                    if date_source == 'content':
                        date_agent_success_count += 1
                    
            except Exception as e:
                print(f"处理第{item_num}条新闻失败: {e}")
                result_item = news_item.copy()
                result_item['content'] = ''
                result_item['_id'] = f"{news_item['author']}_{news_item['title'][:20]}"
                result_item['_id'] = re.sub(r'[^\w\-]', '_', result_item['_id'])
                result_item['used_agent'] = False
                news_results.append(result_item)
    
    finally:
        _browser_manager.stop()
        memory_manager.close()
    
    end_time = datetime.now()
    elapsed_seconds = (end_time - start_time).total_seconds()
    
    print(f"\n\n{'='*60}")
    print(f"写入结果到 {OUTPUT_JSONL_FILE}...")
    with open(OUTPUT_JSONL_FILE, 'w', encoding='utf-8') as f:
        for item in news_results:
            f.write(json.dumps(item, ensure_ascii=False) + '\n')
    
    print(f"\n处理完成!")
    print(f"  总条数: {total_items}")
    print(f"  处理条数: {processed_count}")
    print(f"  成功条数: {success_count}")
    print(f"  纯脚本提取: {pure_script_count}")
    print(f"  复用缓存: {cached_count}")
    print(f"  黑名单跳过: {blacklisted_count}")
    print(f"  正文成功率: {success_count/total_items*100:.1f}%" if total_items > 0 else "  正文成功率: 0%")
    print(f"  调用Agent次数: {agent_count}")
    print(f"  调用Agent率: {agent_count/total_items*100:.1f}%" if total_items > 0 else "  调用Agent率: 0%")
    print(f"  Agent调用成功次数: {agent_success_count}")
    print(f"  Agent调用成功率: {agent_success_count/agent_count*100:.1f}%" if agent_count > 0 else "  Agent调用成功率: 0%")
    print(f"  完成所需时间: {elapsed_seconds:.1f}秒")
    print(f"{'='*60}")
    print(f"  日期统计:")
    print(f"    日期Agent调用次数: {date_agent_count}")
    print(f"    日期Agent成功次数: {date_agent_success_count}")
    print(f"    日期Agent成功率: {date_agent_success_count/date_agent_count*100:.1f}%" if date_agent_count > 0 else "    日期Agent成功率: 0%")
    print(f"    日期来源-正文: {date_from_content_count}")
    print(f"    日期来源-Brave: {date_from_brave_count}")
    print(f"    日期来源-当前时间: {date_from_current_count}")
    print(f"{'='*60}")

    stats = {
        "start_time": start_time.strftime('%Y-%m-%d %H:%M:%S'),
        "end_time": end_time.strftime('%Y-%m-%d %H:%M:%S'),
        "elapsed_seconds": elapsed_seconds,
        "total_items": total_items,
        "processed_count": processed_count,
        "success_count": success_count,
        "pure_script_count": pure_script_count,
        "cached_count": cached_count,
        "blacklisted_count": blacklisted_count,
        "agent_count": agent_count,
        "agent_success_count": agent_success_count,
        "agent_success_rate": agent_success_count / agent_count * 100 if agent_count > 0 else 0,
        "success_rate": success_count / total_items * 100 if total_items > 0 else 0,
        "agent_rate": agent_count / total_items * 100 if total_items > 0 else 0,
        "date_stats": {
            "date_agent_count": date_agent_count,
            "date_agent_success_count": date_agent_success_count,
            "date_agent_success_rate": date_agent_success_count / date_agent_count * 100 if date_agent_count > 0 else 0,
            "date_from_content": date_from_content_count,
            "date_from_brave": date_from_brave_count,
            "date_from_current_time": date_from_current_count
        }
    }
    
    try:
        with open(STATS_FILE, 'w', encoding='utf-8') as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
        print(f"统计信息已保存到 {STATS_FILE}")
    except Exception as e:
        print(f"保存统计信息失败: {e}")


if __name__ == "__main__":
    print(f"使用测试文件: {INPUT_JSONL_FILE}")
    print("开始处理...")
    process_jsonl_file(INPUT_JSONL_FILE)
    print("处理结束")
