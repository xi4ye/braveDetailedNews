# 用 Scrapy 替代 Playwright 计划

## 需求概述

将 `news_processor.py` 中的 Playwright 浏览器自动化替换为 Scrapy 框架，并设置 UA 和反反爬机制。

## 当前 Playwright 使用情况

| 功能 | 位置 | 说明 |
|------|------|------|
| BrowserManager 类 | 第 256-305 行 | 浏览器管理器 |
| extract_content_pure 函数 | 第 320-391 行 | 纯脚本提取正文 |
| get_page_dom 工具 | 第 432-503 行 | 获取网页 DOM |
| validate_locator 工具 | 第 506-577 行 | 验证定位器 |
| extract_content 工具 | 第 580-610 行 | 提取正文 |

## 实现步骤

### 步骤 1：创建 Scrapy 提取器模块

创建 `scrapy_extractor.py` 文件，包含：

- `ScrapyExtractor` 类：封装 Scrapy 的页面获取和内容提取
- 随机 User-Agent 支持
- 反反爬配置
- 同步包装器（因为 Scrapy 是异步框架）

### 步骤 2：实现反反爬机制

```python
ANTI_ANTI_CRAWL_SETTINGS = {
    # 随机 User-Agent
    'USER_AGENT': UserAgent().random,
    
    # 随机请求间隔
    'DOWNLOAD_DELAY': random.uniform(0.5, 2),
    'RANDOMIZE_DOWNLOAD_DELAY': True,
    
    # 禁用 Robots.txt
    'ROBOTSTXT_OBEY': False,
    
    # 请求头伪装
    'DEFAULT_REQUEST_HEADERS': {
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
    },
    
    # 重试设置
    'RETRY_TIMES': 3,
    'RETRY_HTTP_CODES': [500, 502, 503, 504, 408, 429],
}
```

### 步骤 3：修改 BrowserManager 类

将 `BrowserManager` 替换为 `ScrapyManager`：

- 移除 Playwright 相关代码
- 使用 `ScrapyExtractor` 实例
- 保持相同的接口（start/stop/context）

### 步骤 4：修改工具函数

#### 4.1 修改 get_page_dom 工具
- 使用 `ScrapyExtractor.fetch_page()` 获取页面
- 返回格式保持不变

#### 4.2 修改 validate_locator 工具
- 使用 `parsel.Selector` 进行元素选择
- 支持相同的定位类型（css_selector, xpath, id, class）

#### 4.3 修改 extract_content_pure 函数
- 使用 Scrapy 获取页面
- 使用 parsel 提取内容

### 步骤 5：修改 process_jsonl_file 函数

- 初始化 `ScrapyManager` 替代 `BrowserManager`
- 清理时调用 `ScrapyManager.stop()`

### 步骤 6：更新依赖

需要安装的新依赖：
```
pip install scrapy parsel fake-useragent
```

## 文件变更清单

| 文件 | 变更内容 |
|------|----------|
| `scrapy_extractor.py` | 新建，Scrapy 提取器模块 |
| `news_processor.py` | 替换 Playwright 为 Scrapy |

## 反反爬机制总结

| 机制 | 说明 |
|------|------|
| 随机 User-Agent | 使用 fake_useragent 库随机生成 |
| 随机延迟 | DOWNLOAD_DELAY + RANDOMIZE_DOWNLOAD_DELAY |
| 请求头伪装 | 模拟正常浏览器请求头 |
| 禁用 Robots.txt | ROBOTSTXT_OBEY = False |
| 重试机制 | RETRY_TIMES = 3 |
| Cookie 支持 | COOKIES_ENABLED = True |

## 注意事项

1. **同步/异步问题**：Scrapy 是异步框架，需要使用同步包装器
2. **JavaScript 渲染**：Scrapy 不支持 JS 渲染，如果目标网站需要 JS，可能需要配合 Splash
3. **代理设置**：可以额外配置代理中间件
