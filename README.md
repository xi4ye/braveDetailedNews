# Brave Detailed News - 新闻爬取与正文提取系统

一个强大的新闻爬虫与正文提取系统，支持多个搜索引擎和智能正文定位。

## 功能特性

- 🔍 **多搜索引擎支持** - 支持 Brave、Bing国内版、Bing国际版
- 🤖 **智能正文定位** - 使用Agent自动学习网站定位器
- 📅 **自动日期解析** - 支持多种日期格式
- 💾 **SQLite记忆库** - 自动保存定位器，下次直接使用
- 📊 **详细统计信息** - 完整的处理统计和性能指标

## 项目结构

```
braveDetailedNews/
├── brave_crawler.py           # Brave搜索引擎爬虫
├── bing_crawler.py            # Bing国内版爬虫
├── bing_crawler_en.py         # Bing国际版爬虫
├── news_processor.py          # 新闻正文处理核心
├── pipeline.py                # 批量处理流水线
├── main.py                    # 单条新闻处理入口
├── scrapy_extractor.py        # 正文提取工具
├── memory.db                  # SQLite定位器库
├── crawled_news.jsonl         # 爬取的新闻
└── results.jsonl              # 处理后的结果
```

## 快速开始

### 安装依赖

#### 方式1：使用虚拟环境（推荐）✅

```bash
# 1. 创建并激活虚拟环境
python3 -m venv venv
source venv/bin/activate

# 2. 安装所有依赖（使用清华镜像）
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
```

#### 方式2：系统环境安装

```bash
pip install pydoll-python scrapy twisted parsel langchain-openai langchain-core
```

**注意**：如果你的系统使用 PEP 668（Ubuntu 23.04+、Debian 12+），必须使用虚拟环境。详见 [VENV_GUIDE.md](VENV_GUIDE.md)

### 使用方法

#### 1. 单条新闻处理

```bash
python main.py "新闻描述" 爬取数量 [搜索引擎]
```

**搜索引擎选项**:
- `brave` - Brave搜索引擎
- `bing` - Bing国内版
- `bing_en` - Bing国际版（默认）

**示例**:

```bash
# 使用默认（Bing国际版）
python main.py "人工智能最新进展" 5

# 使用Bing国内版
python main.py "特朗普最新政策" 10 bing

# 使用Brave搜索
python main.py "科技新闻" 3 brave
```

#### 2. 批量处理

```bash
python pipeline.py
```

会读取 `news.jsonl` 文件，批量处理。

## 详细功能

### 搜索引擎选择

在启动时，程序会提供交互式选择：

```
请选择搜索引擎来源:
  1. Brave搜索引擎
  2. Bing国内版
  3. Bing国际版 (默认)

请输入选择 (1/2/3, 默认 3):
```

### 定位器记忆系统

系统会自动学习并保存每个网站的正文和日期定位器：

- 首次处理新网站时，Agent会自动探索并找到最佳定位方式
- 下次处理相同网站时，直接使用已保存的定位器（纯脚本模式）
- 定位器保存在 `memory.db` SQLite数据库中

### 日期统计

详细的日期处理统计信息：

```
日期统计:
  日期来源统计:
    - 正文: xxx
    - Brave: xxx
    - 当前时间: xxx
  日期Agent统计:
    - 调用次数: xxx
    - 提取成功: xxx
    - 解析成功: xxx
    - 解析失败: xxx
    - 无文本: xxx
  纯脚本日期统计:
    - 提取成功: xxx
    - 解析失败: xxx
    - 无文本: xxx
  回退统计:
    - 回退Brave: xxx
    - 回退当前时间: xxx
```

## 更新日志

### 2026-04-25 - 验证码求解器优化

#### 1. 验证码缺口检测算法改进
- ✅ 动态调整模糊核大小，基于滑块半径自动缩放
- ✅ 动态调整搜索区域高度，从固定60px改为滑块半径
- ✅ 搜索起点从固定30%改为滑块右边缘+10px
- ✅ 新增对比度评分机制（中轴40% + 半径30% + 对比度30%）
- ✅ 支持不同尺寸的验证码图片（生产环境310x153）

#### 2. 正文提取优化
- ✅ 修复 xpath 提取逻辑，避免重复追加 `//text()`
- ✅ 更新 Agent 提示词，明确排除 script/style/noscript 标签
- ✅ 添加错误示例：`//*[text()]` 会匹配 JS 代码

#### 3. 验证码检测逻辑优化
- ✅ 先检查新闻元素是否存在，再决定是否处理验证码
- ✅ 避免不必要的验证码处理

#### 4. 代理支持
- ✅ `news_processor.py` 添加命令行代理参数 `--proxy`
- ✅ 示例：`python news_processor.py --proxy 127.0.0.1:7890`

#### 5. 批量处理脚本
- ✅ 新增 `batch_from_news.py`，支持从 `news.jsonl` 按 ID 范围批量处理
- ✅ 每条 description 爬取后立即调用 news_processor 处理
- ✅ 示例：`python batch_from_news.py --start 11000 --end 11100`

#### 6. 统计修复
- ✅ 修复日期统计字段未传递到 result_item 的问题

### 2026-04-21 - 重大更新

#### 1. 多搜索引擎支持
- ✅ 新增 Bing国内版爬虫 (`bing_crawler.py`)
- ✅ 新增 Bing国际版爬虫 (`bing_crawler_en.py`)
- ✅ 在 `main.py` 和 `pipeline.py` 中增加搜索引擎选择功能
- ✅ 支持命令行参数指定，或交互式选择

#### 2. news.qq.com定位器修复
- ✅ 删除了错误的 `//*[text()]`定位器
- ✅ 添加了正确的定位器：
  - `css_selector: .content-article` - 完整内容
  - `css_selector: #article-content` - 正文部分
- ✅ 现在可以正确提取QQ新闻的正文内容

#### 3. 日期Agent错误修复
- ✅ 修复了日期Agent提取成功后继续调用的问题
- ✅ 确保每个工具调用都有对应的ToolMessage，避免API错误
- ✅ 在提取成功后直接返回，不再继续处理

#### 4. 日期统计系统全面升级
- ✅ 修复了 `parse_date_expression`函数，解析失败时正确返回 `(None, None)`
- ✅ 增加了详细的日期处理标记（`_date_agent_*`, `_date_fallback_*`等）
- ✅ 新增了8个统计变量，提供更细粒度的统计
- ✅ 改进了统计输出格式，更清晰易读
- ✅ 在 `stats.json` 中保存详细的统计数据

## 配置说明

### 代理配置

在 `bing_crawler.py`、`bing_crawler_en.py` 或 `brave_crawler.py` 中配置代理：

```python
# options.add_argument('--proxy-server=your-proxy:port')
```

### 无头模式

```python
options.headless = True  # 不显示浏览器窗口
```

## 统计文件

处理完成后会生成以下文件：

- `results.jsonl` - 处理后的新闻结果
- `processor_stats.json` - 详细的处理统计数据
- `screenshots/` - 爬虫截图文件夹（如果启用）

## 常见问题

### Q: 如何清除已保存的定位器？

A: 删除或重命名 `memory.db` 文件即可。

### Q: 为什么有些新闻正文提取失败？

A: 可能是该网站使用了特殊的动态加载，或者页面结构太复杂。可以查看 `results.jsonl` 中的具体条目。

### Q: 如何查看定位器的详细信息？

A: 使用SQLite工具打开 `memory.db`，查看 `locators` 表。

## 技术栈

- Python 3.7+
- pydoll (浏览器自动化)
- Scrapy (页面解析)
- SQLite (定位器存储)
- LangChain (Agent框架)
- OpenAI API (可选，用于DeepSeek等)

## 许可证

本项目仅供学习和研究使用。

---

**项目地址**: [GitHub]
**最后更新**: 2026-04-21
