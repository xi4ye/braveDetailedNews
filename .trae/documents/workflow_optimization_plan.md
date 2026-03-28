# 工作流优化计划：纯脚本提取与黑名单机制

## 需求概述

1. **纯脚本提取优化**：当域名在 memory.json 中且成功验证次数 ≥ 3 次时，直接使用纯脚本提取正文，不调用 AI Agent
2. **黑名单机制**：当同一域名失败次数 ≥ 3 次时，直接跳过，不再尝试

## 实现步骤

### 步骤 1：添加错误记录管理器 (ErrorManager)

在 `news_processor.py` 中添加新的 `ErrorManager` 类：

```python
class ErrorManager:
    """错误记录管理器 - 基于域名存储失败记录"""
    
    def __init__(self, error_file):
        self.error_file = error_file
        self.errors = self._load_errors()
    
    def _load_errors(self):
        # 加载 error.json，格式: {domain: {fail_count, reasons: []}}
    
    def get_error_by_domain(self, domain: str) -> Optional[Dict]:
        # 获取域名的错误记录
    
    def add_error(self, domain: str, reason: str):
        # 添加错误记录，失败次数 +1
    
    def is_blacklisted(self, domain: str) -> bool:
        # 检查域名是否在黑名单中（失败次数 >= 3）
    
    def _save_errors(self):
        # 保存到 error.json
```

### 步骤 2：添加纯脚本正文提取函数

在 `news_processor.py` 中添加 `extract_content_pure` 函数：

```python
def extract_content_pure(url: str, locator: Dict) -> str:
    """纯脚本提取正文，不使用 Agent"""
    # 使用 Playwright 直接访问页面
    # 根据 locator 中的 locator_type 和 locator_value 提取正文
    # 返回正文内容
```

### 步骤 3：修改 process_news_item 函数

修改主处理函数，添加判断逻辑：

```python
def process_news_item(news_item, agent, memory_manager, error_manager):
    domain = extract_domain(url)
    
    # 1. 检查黑名单
    if error_manager.is_blacklisted(domain):
        print(f"[跳过] 域名 {domain} 在黑名单中")
        return False, {...}
    
    # 2. 检查是否有缓存规则且成功次数 >= 3
    locator = memory_manager.get_locator_by_domain(domain)
    if locator and locator.get('success_count', 0) >= 3:
        print(f"[纯脚本] 域名 {domain} 成功次数 >= 3，使用纯脚本提取")
        content = extract_content_pure(url, locator)
        if content:
            return True, {...}
        # 如果纯脚本失败，回退到 Agent
    
    # 3. 使用 Agent 处理
    result = agent.process_news(news_item)
    
    # 4. 处理失败情况，记录到 error_manager
    if result.get('_give_up'):
        error_manager.add_error(domain, result.get('_give_up_reason'))
```

### 步骤 4：修改 give_up 工具

在 `give_up` 工具中记录失败原因：

```python
@tool
def give_up(reason: str) -> str:
    # 设置 _give_up 和 _give_up_reason 标志
    # 返回失败结果
```

### 步骤 5：修改 DeepSeekAgentWithTools 类

在 Agent 处理中返回 give_up 信息：

```python
def process_news(self, news_item):
    # ... 现有逻辑 ...
    
    # 当检测到 give_up 时，返回额外信息
    if tool_name == "give_up":
        return {
            "success": False,
            "content": "",
            "locator": None,
            "error": result.get("reason"),
            "_give_up": True,
            "_give_up_reason": result.get("reason")
        }
```

### 步骤 6：修改 process_jsonl_file 函数

添加 ErrorManager 初始化：

```python
def process_jsonl_file(jsonl_file: str):
    memory_manager = MemoryManager(MEMORY_FILE)
    error_manager = ErrorManager(ERROR_FILE)  # 新增
    
    # ... 传递 error_manager 给 process_news_item ...
```

### 步骤 7：添加配置常量

在文件顶部添加：

```python
ERROR_FILE = "error.json"
BLACKLIST_THRESHOLD = 3  # 黑名单阈值
PURE_SCRIPT_THRESHOLD = 3  # 纯脚本阈值
```

## 文件变更清单

| 文件 | 变更内容 |
|------|----------|
| `news_processor.py` | 添加 ErrorManager 类、extract_content_pure 函数、修改 process_news_item 逻辑 |

## 数据结构

### error.json 格式

```json
{
  "www.example.com": {
    "fail_count": 3,
    "reasons": [
      {"time": "2026-03-11 19:30:00", "reason": "页面404"},
      {"time": "2026-03-11 19:35:00", "reason": "无法定位正文"},
      {"time": "2026-03-11 19:40:00", "reason": "页面已删除"}
    ],
    "blacklisted": true
  }
}
```

## 流程图

```
                    ┌─────────────────┐
                    │   获取域名       │
                    └────────┬────────┘
                             │
                             ▼
                    ┌─────────────────┐
                    │ 检查黑名单？     │
                    └────────┬────────┘
                             │
              ┌──────────────┴──────────────┐
              │ 是                          │ 否
              ▼                             ▼
        ┌───────────┐              ┌─────────────────┐
        │ 跳过处理   │              │ 检查 memory.json │
        └───────────┘              └────────┬────────┘
                                            │
                              ┌─────────────┴─────────────┐
                              │ 成功次数 >= 3？            │
                              └─────────────┬─────────────┘
                                            │
                         ┌──────────────────┴──────────────────┐
                         │ 是                                  │ 否
                         ▼                                     ▼
                 ┌───────────────┐                    ┌───────────────┐
                 │ 纯脚本提取     │                    │ Agent 处理    │
                 └───────┬───────┘                    └───────┬───────┘
                         │                                    │
                         ▼                                    ▼
                 ┌───────────────┐                    ┌───────────────┐
                 │ 成功？        │                    │ 成功？        │
                 └───────┬───────┘                    └───────┬───────┘
                         │                                    │
              ┌──────────┴──────────┐              ┌──────────┴──────────┐
              │ 是                  │ 否           │ 是                  │ 否
              ▼                     ▼              ▼                     ▼
        ┌──────────┐         ┌──────────┐    ┌──────────┐         ┌──────────┐
        │ 返回结果  │         │Agent处理 │    │ 更新memory│         │记录error │
        └──────────┘         └──────────┘    └──────────┘         └──────────┘
```
