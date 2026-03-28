# Brave Detailed News - 新闻爬取与正文提取系统

## 项目简介

本项目是一个自动化的新闻爬取和正文提取系统，能够从 Brave Search 搜索新闻，并智能提取新闻正文内容。系统采用流水线模式，支持批量处理和定位规则复用。

---

## 核心功能

- **新闻爬取**: 使用 Brave Search 搜索并爬取新闻
- **智能正文提取**: 支持纯脚本提取和 AI Agent 两种模式
- **智能日期提取**: 支持纯脚本提取和 AI Agent 两种模式，日期优先级：正文日期 > Brave 日期 > 当前时间
- **定位规则复用**: SQLite 数据库存储定位规则（分 content/date 两类），同域名文章可直接复用
- **反反爬机制**: 内置 Brave Search 验证码自动检测与处理
- **黑名单机制**: 失败 10 次自动加入黑名单，跳过后续处理

---

## 项目结构

```
braveDetailedNews/
├── main.py                     # 命令行快速版本（输入：描述 + 数量）
├── pipeline.py                  # 批量流水线版本（输入：JSONL 文件）
├── brave_crawler.py            # Brave Search 新闻爬虫 (pydoll + Edge)
├── brave_captcha_solver.py      # Brave Search 验证码解决器
├── news_processor.py           # 新闻正文提取处理器（串行）
├── news_processor_threaded.py   # 新闻正文提取处理器（多线程）
├── scrapy_extractor.py         # Scrapy 风格的 HTTP 提取器
├── memory.db                   # SQLite 定位规则数据库
├── error.json                  # 错误记录与黑名单
├── processor_stats.json         # 处理统计信息
├── news.jsonl                  # 输入文件（搜索关键词）
├── crawled_news.jsonl          # 爬取的新闻数据（中间文件）
├── results.jsonl               # 最终处理结果
├── .env.example                # 环境变量示例
└── .gitignore                  # Git 忽略配置
```

---

## 核心技术亮点

### 1. SQLite 存储定位规则

使用 SQLite 替代 JSON 存储：
- 单条记录更新更快（O(log n) vs 重写整个文件）
- 支持索引查询
- 支持同一域名多个定位器

**数据库结构**:
```sql
CREATE TABLE locators (
    id INTEGER PRIMARY KEY,
    domain TEXT NOT NULL,
    locator_type TEXT NOT NULL,
    locator_value TEXT NOT NULL,
    locator_desc TEXT,
    locator_category TEXT DEFAULT 'content',  -- 'content' 或 'date'
    usage_count INTEGER DEFAULT 0,
    success_count INTEGER DEFAULT 0,
    create_time TEXT,
    update_time TEXT,
    UNIQUE(domain, locator_value, locator_category)
)
```

### 2. 真实域名查询

先获取最终域名，再查询数据库：
```
solar.ofweek.com → [重定向] → solar.m.ofweek.com
                                      │
                                      ▼
                              用 solar.m.ofweek.com 查询数据库
```

### 3. 智能正文定位

使用 LangChain Agent + Tools 模式，让 AI 自主调用工具：
- `get_page_dom` - 获取网页 DOM
- `get_existing_locator` - 查询已有定位器
- `validate_locator` - 验证正文定位器
- `extract_content` - 提取正文
- `save_locator` - 保存正文定位规则
- `give_up` - 放弃处理

### 3.1 智能日期定位

使用独立的日期 Agent 流程：
- `validate_date_locator` - 验证日期定位器
- `extract_date` - 提取日期文本
- `save_date_locator` - 保存日期定位规则
- 日期优先级：正文日期 > Brave 日期 > 当前时间
- 支持多种日期格式自动解析

### 4. 通用型定位表达式

系统强制生成通用型定位规则：
- 不包含文章特定内容
- 基于页面结构特征
- 同域名文章可复用

### 5. 多层防护机制

#### 第 1 层：提示词优化（软约束）
- 在 System Prompt 中明确要求 Agent 不要重复调用
- 强调"找到一个能用的就停止"

#### 第 2 层：代码层面拦截（硬约束，最重要）
- 全局状态变量追踪工具调用历史
- 工具调用前检查是否重复
- 页面内容缓存避免重复请求
- 提取完成后立即标记锁死

```python
# 工具调用前拦截
if check_extraction_completed():
    print(f"  [拦截] 提取已完成，跳过工具调用: {tool_name}")
    return "错误：提取已完成，无需继续操作"

if check_duplicate_tool_call(tool_name, tool_args):
    return "错误：重复调用，已跳过"
```

### 6. 纯脚本提取优化

当 `success_count >= 1` 时，直接使用脚本提取：
- 不调用 AI Agent
- 节省 API 调用成本
- 提升处理速度

### 7. 黑名单机制

当同一域名失败次数 >= 10 次时：
- 自动加入黑名单
- 后续遇到该域名直接跳过
- 记录失败原因到 error.json

### 8. 反反爬机制

Brave Search 验证码自动处理：
- 等待超 15 秒自动触发反反爬
- 方法1：直接查找验证码按钮（支持多种选择器）
- 方法2：遍历 Shadow DOM 查找验证码
- 支持多层 Shadow Root 嵌套

---

## 工作流程

```
┌─────────────────────────────────────────────────────────────────┐
│                      process_news_item()                        │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  1. Scrapy 获取页面                                              │
│     - 发送 HTTP 请求                                             │
│     - 获取最终域名 (处理重定向)                                   │
│     - 返回 html_content, final_url, status_code                 │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  2. 检查黑名单 (error.json)                                      │
│     - 如果 final_domain 在黑名单中 → 跳过                        │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  3. 查询 SQLite 数据库                                           │
│     - 用 final_domain 查询定位器                                 │
│     - 按 success_count 降序排列                                 │
└─────────────────────────────────────────────────────────────────┘
                              │
              ┌───────────────┴───────────────┐
              │                               │
              ▼                               ▼
┌─────────────────────────┐     ┌─────────────────────────┐
│  有定位器且             │     │  无定位器或             │
│  success_count >= 1    │     │  纯脚本失败             │
└───────────┬─────────────┘     └───────────┬─────────────┘
            │                               │
            ▼                               ▼
┌─────────────────────────┐     ┌─────────────────────────┐
│  纯脚本模式             │     │  Agent 模式             │
│  - 直接用定位器提取     │     │  - AI 分析页面结构      │
│  - 不调用 AI           │     │  - 生成通用定位器       │
│  - 速度快，成本低       │     │  - 保存到 SQLite        │
└───────────┬─────────────┘     └───────────┬─────────────┘
            │                               │
            └───────────────┬───────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│  4. 更新计数                                                     │
│     - 成功: usage_count++, success_count++                      │
│     - 失败: usage_count++, 记录到 error.json                    │
│     - 失败 10 次: 加入黑名单                                    │
└─────────────────────────────────────────────────────────────────┘
```

---

## 完整数据流向

```
┌─────────────────┐
│   news.jsonl    │  (输入：搜索关键词)
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ brave_crawler   │  (pydoll + Edge 爬取 Brave Search)
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│crawled_news.jsonl│  (中间：新闻列表)
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ scrapy_extractor│  (requests + parsel 获取页面)
│  - 获取真实域名  │
│  - 处理重定向    │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  memory.db      │  (SQLite 查询定位器)
│  - 按 success   │
│    排序         │
└────────┬────────┘
         │
    ┌────┴────┐
    │         │
    ▼         ▼
┌───────┐ ┌───────┐
│纯脚本 │ │ Agent │
│模式   │ │ 模式  │
└───┬───┘ └───┬───┘
    │         │
    └────┬────┘
         │
         ▼
┌─────────────────┐
│ results.jsonl   │  (输出：完整新闻)
└─────────────────┘
```

---

## 前置要求

1. **开启代理** (必须)
   ```bash
   source /etc/profile.d/clash.sh
   proxy_on
   ```

2. **安装依赖**
   ```bash
   pip install pydoll-python langchain-openai requests parsel
   ```

3. **配置 API Key**
   - 从 `.env.example` 复制创建 `.env` 文件
   - 或设置环境变量：`export DEEPSEEK_API_KEY=your_api_key`

4. **安装 Microsoft Edge 浏览器** (Linux)
   ```bash
   wget https://packages.microsoft.com/repos/edge/pool/main/m/microsoft-edge-dev/microsoft-edge-dev_123.0.2400.1_amd64.deb
   sudo dpkg -i microsoft-edge-dev_123.0.2400.1_amd64.deb
   ```

---

## 运行方式

### 方式一：命令行快速模式（推荐用于少量新闻）

```bash
# 1. 开启代理
source /etc/profile.d/clash.sh
proxy_on

# 2. 设置 API Key
export DEEPSEEK_API_KEY=your_api_key_here

# 3. 运行命令行版本
python main.py "新闻描述" K

# 示例：爬取 10 条关于特朗普关税的新闻
python main.py "特朗普关税提高到80%" 10
```

**main.py 与 pipeline.py 的区别：**

| 特性 | main.py | pipeline.py |
|------|---------|-------------|
| 输入方式 | 命令行参数 | JSONL 文件 |
| 适用场景 | 少量新闻快速处理 | 批量处理大量新闻 |
| 用法 | `python main.py "描述" K` | 修改配置后 `python pipeline.py` |

### 方式二：完整流水线（批量处理）

```bash
# 1. 开启代理
source /etc/profile.d/clash.sh
proxy_on

# 2. 设置 API Key
export DEEPSEEK_API_KEY=your_api_key_here

# 3. 运行流水线
python pipeline.py
```

主程序会自动完成：
- 读取输入文件
- 爬取新闻
- 提取正文
- 保存结果

### 方式三：分步运行

```bash
# 1. 开启代理
source /etc/profile.d/clash.sh
proxy_on

# 2. 单独运行爬虫
python brave_crawler.py

# 3. 单独运行处理器（串行）
python news_processor.py

# 或运行多线程版本
python news_processor_threaded.py
```

---

## 配置说明

### main.py 配置
直接在命令行参数中指定：
```bash
python main.py "新闻描述" K
# 例如：
python main.py "特朗普关税" 10
```

### pipeline.py 配置
在 `pipeline.py` 中修改配置：

```python
INPUT_JSONL_FILE = "news.jsonl"        # 输入文件
NEWS_OUTPUT_FILE = "crawled_news.jsonl" # 中间文件
BATCH_SIZE = 10                        # 处理记录数
NEWS_PER_QUERY = 5                     # 每个搜索获取新闻数
USE_THREADED_PROCESSOR = False        # False=串行, True=多线程
```

### API Key 配置

**方式一：环境变量**
```bash
export DEEPSEEK_API_KEY=your_api_key_here
```

**方式二：.env 文件**
```bash
cp .env.example .env
# 然后编辑 .env 文件，填入你的 API Key
```

---

## 输出文件说明

| 文件 | 说明 |
|------|------|
| `news.jsonl` | 输入文件（搜索关键词） |
| `crawled_news.jsonl` | 爬取的原始新闻数据 |
| `results.jsonl` | 包含正文的最终结果 |
| `memory.db` | SQLite 定位规则库（复用提高效率） |
| `error.json` | 错误记录与黑名单 |
| `processor_stats.json` | 处理统计信息 |

---

## 统计信息

运行完成后会显示详细统计：

```
============================================================
处理完成!
  总条数: 50
  处理条数: 50
  成功条数: 33
  纯脚本提取: 25
  复用缓存: 0
  黑名单跳过: 15
  正文成功率: 66.0%
  调用Agent次数: 9
  调用Agent率: 18.0%
  Agent调用成功次数: 7
  Agent调用成功率: 77.8%
  完成所需时间: 150.5秒
============================================================
  日期统计:
    日期Agent调用次数: 5
    日期Agent成功次数: 3
    日期Agent成功率: 60.0%
    日期来源-正文: 20
    日期来源-Brave: 10
    日期来源-当前时间: 3
============================================================

最终统计:
  - 处理搜索数: 10
  - 爬取新闻数: 50
  - 处理新闻数: 50
```

---

## 常见问题

### Q: 为什么要开启代理？

A: Brave Search 在国内需要代理才能访问。代理配置在 `brave_crawler.py` 中：
```python
options.add_argument('--proxy-server=127.0.0.1:7890')
```

### Q: 为什么使用 SQLite 而不是 JSON？

A: SQLite 在写入性能上更优：
- JSON 每次写入需要重写整个文件
- SQLite 只更新单条记录
- 支持索引查询，查询速度更快

### Q: 如何查看处理进度？

A: 程序会实时输出：
- 当前处理条数
- 爬取新闻数
- 成功/失败统计
- Agent 调用日志

### Q: 如何修改爬取数量？

A: 使用 main.py 时直接在命令行指定：
```bash
python main.py "描述" 100
```

使用 pipeline.py 时在配置中修改：
```python
BATCH_SIZE = 100        # 处理记录数
NEWS_PER_QUERY = 5      # 每个搜索获取新闻数
```

---

## JavaScript 渲染网站处理

当前使用 `scrapy_extractor.py`（requests + parsel）**无法执行 JavaScript**，对于以下网站会提取失败：

### 受影响的网站类型

1. **SPA（单页应用）**
   - React / Vue / Angular 构建的网站
   - 初始 HTML 只有容器，正文由 JS 渲染

2. **动态加载内容**
   - 正文通过 XHR/Fetch 异步获取
   - 滚动加载、点击加载等

3. **反爬虫机制**
   - 需要 JS 执行才能生成有效请求
   - 签名、指纹等 JS 计算

### 解决方案

| 方案 | 优点 | 缺点 | 推荐度 |
|------|------|------|--------|
| 保持现状 | 速度快、成本低 | 无法处理 JS 网站 | ⭐⭐ |
| 混合策略 | 兼顾速度和兼容性 | 需要维护两套代码 | ⭐⭐⭐⭐ |
| 全量 Playwright | 处理所有网站 | 速度慢、成本高 | ⭐⭐⭐ |

---

## 注意事项

1. **代理必须开启** - 否则无法访问 Brave Search
2. **API Key 配置** - 需要有效的 DeepSeek API Key（从环境变量或 .env 文件读取）
3. **浏览器依赖** - Linux 需要安装 Microsoft Edge
4. **黑名单阈值** - 失败 10 次自动加入黑名单
5. **隐私保护** - API Key 等敏感信息不会提交到 Git

---

## 性能优化效果

| 指标 | 优化前 | 优化后 |
|------|--------|--------|
| 平均工具调用步数 | ~15-20 步 | ~5-10 步 |
| API 调用成本 | 高 | 降低 50%+ |
| 处理速度 | 较慢 | 提升 2-3 倍 |
| 不必要的工具调用 | 多 | 大幅减少 |

---

## 快速开始

### 单条新闻快速处理
```bash
source /etc/profile.d/clash.sh && proxy_on && export DEEPSEEK_API_KEY=your_key && python main.py "特朗普关税" 5
```

### 批量处理
```bash
source /etc/profile.d/clash.sh && proxy_on && export DEEPSEEK_API_KEY=your_key && python pipeline.py
```
