#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新闻JSONL批量处理工具 - 多线程版本
功能：使用多线程并发处理多条新闻
"""

import json
import os
import re
import sqlite3
import threading
from datetime import datetime
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed

from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage

from scrapy_extractor import ScrapyManager, ScrapyExtractor, extract_domain


INPUT_JSONL_FILE = "crawled_news.jsonl"
OUTPUT_JSONL_FILE = "results_threaded.jsonl"
MAX_AGENT_STEPS = 20
MEMORY_FILE = "memory.json"
ERROR_FILE = "error_threaded.json"
BLACKLIST_THRESHOLD = 10
PURE_SCRIPT_THRESHOLD = 1
MAX_WORKERS = 5  # 线程数，可以根据实际情况调整
STATS_FILE = "processor_stats.json"  # 统计信息保存文件


DEEPSEEK_CONFIG = {
    "model": "deepseek-chat",
    "base_url": "https://api.deepseek.com/v1",
    "api_key": os.environ.get("DEEPSEEK_API_KEY", "")
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


_thread_local = ThreadLocalContext()


class ThreadSafeMemoryManager:
    """线程安全的 MemoryManager - 使用 SQLite 的 WAL 模式减少锁争用"""
    
    def __init__(self, memory_file):
        self.db_file = memory_file.replace('.json', '.db')
        self._init_db()
    
    def _get_conn(self):
        conn = sqlite3.connect(self.db_file, check_same_thread=False, timeout=30)
        conn.execute('PRAGMA journal_mode=WAL')
        return conn
    
    def _init_db(self):
        conn = self._get_conn()
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
        conn.close()
    
    def get_locator_by_domain(self, domain: str) -> Optional[Dict]:
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
        finally:
            conn.close()
    
    def increment_locator_usage(self, domain: str, locator_value: str, success: bool = True):
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
    
    print(f"[Tool] 获取DOM: {url}")
    
    if check_extraction_completed():
        return json.dumps({"success": False, "error": "提取已完成，无需继续操作"}, ensure_ascii=False)
    
    if check_duplicate_tool_call("get_page_dom", {"url": url}):
        return json.dumps({"success": False, "error": "重复调用，已从缓存返回"}, ensure_ascii=False)
    
    if browser_manager is None or browser_manager.extractor is None:
        return "错误：Scrapy 提取器未初始化"
    
    try:
        if url in page_cache:
            print(f"  [缓存] 使用缓存的页面内容")
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
        print(f"[Tool] 最终域名: {final_domain}")
        
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
    
    print(f"[Tool] 验证定位器: {locator_type}={locator_value}")
    
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
            print(f"  [缓存] 使用缓存的页面内容")
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
    
    print(f"[Tool] 提取正文: {locator_type}={locator_value}")
    
    if browser_manager is None or browser_manager.extractor is None:
        return "错误：Scrapy 提取器未初始化"
    
    try:
        if url in page_cache:
            print(f"  [缓存] 使用缓存的页面内容")
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
def get_existing_locator(domain: str) -> str:
    """查询已有定位规则"""
    return json.dumps({
        "exists": False,
        "message": f"域名 {domain} 暂无定位规则，需要生成新的"
    }, ensure_ascii=False)


@tool
def save_locator(locator_type: str, locator_value: str, locator_desc: str) -> str:
    """保存定位规则"""
    return json.dumps({
        "success": True, 
        "message": "定位规则已保存",
        "locator": {
            "locator_type": locator_type,
            "locator_value": locator_value,
            "locator_desc": locator_desc
        }
    }, ensure_ascii=False)


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
        self.tools = [get_page_dom, validate_locator, extract_content, get_existing_locator, save_locator, give_up]
        self.llm_with_tools = self.llm.bind_tools(self.tools)
    
    def process_news(self, news_item: Dict[str, Any]) -> Dict[str, Any]:
        """处理单条新闻"""
        url = news_item['url']
        if url.startswith('//'):
            url = 'https:' + url
        elif not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        
        system_prompt = f"""你是一个专业的网页正文定位专家。你的任务是找到网页中新闻正文的位置并提取内容。

【核心要求】生成的定位表达式必须是【通用型】：
- 同一域名下的所有文章都能使用该表达式
- 禁止包含文章标题、日期、人名、公司名等特定内容
- 禁止使用 contains(text(), '某具体内容') 这种特定内容的定位方式
- 应该使用页面结构特征：如 class名、id名、标签层级关系等

【正确示例】：
- css_selector: "div.article-content", "#js_content", ".rich_media_content", "div.content article"
- xpath: "//article//div[@class='content']", "//div[contains(@class, 'article-body')]"
- id: "artibody", "js_content", "article-content"

当前新闻信息：
- 标题: {news_item['title']}
- 来源: {news_item['author']}
- URL: {news_item['url']}

【绝对关键提示】：
1. extract_content 成功后必须立即调用 save_locator，然后立即返回最终结果
2. 不要继续尝试其他定位器，哪怕有更多可能的选项
3. 不要重复调用 get_page_dom，第一次获取后就不要再获取
4. 一旦有一个定位器验证成功且提取成功，立即结束整个流程！

请开始工作，严格按照上述流程调用工具。成功提取正文后，返回最终结果。"""
        
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content="请开始处理这条新闻，找到正文并提取。记住：定位表达式必须是通用型，找到一个能用的就停止！")
        ]
        
        final_content = None
        final_locator = None
        last_validated_locator = None
        
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
                            if check_extraction_completed():
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
                                        last_validated_locator = {
                                            "locator_type": tool_args.get("locator_type"),
                                            "locator_value": tool_args.get("locator_value")
                                        }
                                except:
                                    pass
                            
                            if tool_name == "extract_content":
                                if not str(tool_result).startswith("错误"):
                                    final_content = tool_result
                                    mark_extraction_completed()
                                    
                                    if last_validated_locator:
                                        print(f"\n[优化] 检测到已有验证通过的定位器，直接保存并结束")
                                        domain = _thread_local.current_news_info.get('final_domain', _thread_local.current_news_info.get('domain', ''))
                                        final_locator = {
                                            **last_validated_locator,
                                            "domain": domain,
                                            "locator_desc": "自动保存",
                                            "create_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                            "is_valid": True,
                                            "usage_count": 0,
                                            "success_count": 0
                                        }
                                        return {
                                            "success": True,
                                            "content": final_content,
                                            "locator": final_locator
                                        }
                
                else:
                    content = response.content
                    
                    completion_keywords = [
                        "success", "完成", "成功", "已经完成", "提取成功",
                        "正文已提取", "任务完成", "finished", "done"
                    ]
                    
                    has_completion = any(keyword in content.lower() or keyword in content for keyword in completion_keywords)
                    
                    if has_completion or final_content:
                        if final_content:
                            print(f"\n[检测到完成信号] 直接返回结果")
                            return {
                                "success": True,
                                "content": final_content,
                                "locator": final_locator
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
            "locator": final_locator,
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


def process_jsonl_file_threaded(jsonl_file: str, max_workers: int = MAX_WORKERS):
    """多线程处理JSONL文件"""
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
    print(f"# 多线程处理模式")
    print(f"# 线程数: {max_workers}")
    print(f"# 启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'#'*60}\n")
    
    processed_count = 0
    success_count = 0
    pure_script_count = 0
    blacklisted_count = 0
    agent_count = 0
    agent_success_count = 0
    news_results = []
    
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_news = {
                executor.submit(
                    process_single_news, 
                    item, 
                    DEEPSEEK_CONFIG, 
                    memory_manager, 
                    error_manager
                ): item for item in news_items
            }
            
            for future in tqdm(as_completed(future_to_news), total=total_items, desc="处理进度"):
                news_item = future_to_news[future]
                try:
                    success, result_item = future.result()
                    news_results.append(result_item)
                    processed_count += 1
                    
                    if success:
                        success_count += 1
                        if result_item.get('used_pure_script'):
                            pure_script_count += 1
                    elif result_item.get('status') == 'blacklisted':
                        blacklisted_count += 1
                    
                    if result_item.get('used_agent'):
                        agent_count += 1
                        if success:
                            agent_success_count += 1
                        
                except Exception as e:
                    print(f"处理新闻失败: {e}")
                    news_id = re.sub(r'[^\w\-]', '_', f"{news_item.get('author', 'unknown')}_{news_item.get('title', 'unknown')[:20]}")
                    result_item = news_item.copy()
                    result_item['content'] = ''
                    result_item['_id'] = news_id
                    result_item['used_agent'] = False
                    news_results.append(result_item)
                    processed_count += 1
    
    finally:
        pass
    
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
    print(f"  黑名单跳过: {blacklisted_count}")
    print(f"  正文成功率: {success_count/total_items*100:.1f}%" if total_items > 0 else "  正文成功率: 0%")
    print(f"  调用Agent次数: {agent_count}")
    print(f"  调用Agent率: {agent_count/total_items*100:.1f}%" if total_items > 0 else "  调用Agent率: 0%")
    print(f"  Agent调用成功次数: {agent_success_count}")
    print(f"  Agent调用成功率: {agent_success_count/agent_count*100:.1f}%" if agent_count > 0 else "  Agent调用成功率: 0%")
    print(f"  完成所需时间: {elapsed_seconds:.1f}秒")
    print(f"{'='*60}")
    
    stats = {
        "start_time": start_time.strftime('%Y-%m-%d %H:%M:%S'),
        "end_time": end_time.strftime('%Y-%m-%d %H:%M:%S'),
        "elapsed_seconds": elapsed_seconds,
        "total_items": total_items,
        "processed_count": processed_count,
        "success_count": success_count,
        "pure_script_count": pure_script_count,
        "blacklisted_count": blacklisted_count,
        "agent_count": agent_count,
        "agent_success_count": agent_success_count,
        "agent_success_rate": agent_success_count / agent_count * 100 if agent_count > 0 else 0,
        "success_rate": success_count / total_items * 100 if total_items > 0 else 0,
        "agent_rate": agent_count / total_items * 100 if total_items > 0 else 0
    }
    
    try:
        with open(STATS_FILE, 'w', encoding='utf-8') as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
        print(f"统计信息已保存到 {STATS_FILE}")
    except Exception as e:
        print(f"保存统计信息失败: {e}")


if __name__ == "__main__":
    print(f"使用测试文件: {INPUT_JSONL_FILE}")
    print(f"线程数: {MAX_WORKERS}")
    print("开始多线程处理...")
    process_jsonl_file_threaded(INPUT_JSONL_FILE)
    print("处理结束")
