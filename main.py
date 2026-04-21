#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py - 命令行版本的新闻爬取与处理程序

与 pipeline.py 的区别：
- pipeline.py: 从文件读取搜索关键词列表
- main.py: 直接接受命令行参数（新闻描述 + 爬取数量）

用法：
    python main.py "新闻描述" K [来源]

来源可选：
    - brave  (Brave 搜索引擎)
    - bing   (Bing 国内版)
    - bing_en (Bing 国际版，默认)

示例：
    python main.py "特朗普关税提高到80%" 10
    python main.py "特朗普关税提高到80%" 10 bing
    python main.py "polar ice core" 5 bing_en
"""

import asyncio
import sys
import os
import subprocess
from datetime import datetime
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def select_search_source():
    """交互式选择搜索引擎来源"""
    print("\n请选择搜索引擎来源:")
    print("  1. Brave 搜索引擎")
    print("  2. Bing 国内版")
    print("  3. Bing 国际版 (默认)")
    
    choice = input("\n请输入选择 (1/2/3，默认 3): ").strip()
    
    if choice == "1":
        return "brave"
    elif choice == "2":
        return "bing"
    else:
        return "bing_en"


def get_crawler(source: str):
    """根据来源返回对应的爬虫函数"""
    if source == "brave":
        try:
            from brave_crawler import crawl_news as brave_crawl
            print(f"✅ 使用 Brave 搜索引擎")
            return brave_crawl
        except ImportError:
            print("⚠️  找不到 brave_crawler.py，使用 Bing 国际版")
            from bing_crawler_en import crawl_news as bing_crawl
            return bing_crawl
    elif source == "bing":
        try:
            from bing_crawler import crawl_news as bing_cn_crawl
            print(f"✅ 使用 Bing 国内版搜索引擎")
            return bing_cn_crawl
        except ImportError:
            print("⚠️  找不到 bing_crawler.py，使用 Bing 国际版")
            from bing_crawler_en import crawl_news as bing_crawl
            return bing_crawl
    else:
        from bing_crawler_en import crawl_news as bing_en_crawl
        print(f"✅ 使用 Bing 国际版搜索引擎")
        return bing_en_crawl


def process_news_with_processor():
    """调用 news_processor 处理新闻列表"""
    try:
        result = subprocess.run(
            [sys.executable, "news_processor.py"],
            cwd=os.getcwd(),
            capture_output=False,
            text=True
        )
        if result.returncode != 0:
            print(f"处理失败，返回码: {result.returncode}")
            return False
        return True
    except Exception as e:
        print(f"处理失败: {e}")
        return False


def save_news_to_jsonl(news_list: list, output_file: str = "crawled_news.jsonl"):
    """保存新闻列表到 JSONL 文件"""
    try:
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(news_list, f, ensure_ascii=False, indent=2)
        print(f"已保存到 {output_file}")
        return True
    except Exception as e:
        print(f"保存失败: {e}")
        return False


async def main():
    """主函数"""
    if len(sys.argv) < 3:
        print("用法: python main.py \"新闻描述\" K [来源]")
        print()
        print("来源可选:")
        print("  - brave  (Brave 搜索引擎)")
        print("  - bing   (Bing 国内版)")
        print("  - bing_en (Bing 国际版，默认)")
        print()
        print("示例:")
        print('  python main.py "特朗普关税提高到80%" 10')
        print('  python main.py "特朗普关税提高到80%" 10 bing')
        print('  python main.py "polar ice core" 5 bing_en')
        sys.exit(1)

    news_description = sys.argv[1]
    K = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    
    # 从命令行参数获取来源，如果没有则交互式选择
    if len(sys.argv) >= 4:
        source = sys.argv[3].lower()
        if source not in ["brave", "bing", "bing_en"]:
            print(f"⚠️  无效的来源 '{source}'，使用交互式选择")
            source = select_search_source()
    else:
        source = select_search_source()

    # 获取对应的爬虫函数
    crawl_func = get_crawler(source)

    print(f"\n{'#'*60}")
    print(f"# 新闻爬取与处理（命令行模式）")
    print(f"# 启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"# 搜索引擎: {source}")
    print(f"# 处理模式: 串行处理")
    print(f"{'#'*60}\n")

    print(f"配置:")
    print(f"  - 搜索描述: {news_description}")
    print(f"  - 爬取数量: {K}")
    print(f"  - 搜索引擎: {source}")
    print()

    start_time = datetime.now()

    print(f"{'='*60}")
    print(f"# 阶段 1：爬取新闻")
    print(f"{'='*60}\n")

    news_list = await crawl_func(news_description, K)

    if not news_list:
        print("没有爬取到任何新闻，结束")
        return

    print(f"\n阶段 1 完成！共爬取 {len(news_list)} 条新闻")

    save_news_to_jsonl(news_list)

    print(f"\n{'='*60}")
    print(f"# 阶段 2：处理新闻（提取正文）")
    print(f"{'='*60}\n")

    success = process_news_with_processor()

    end_time = datetime.now()
    elapsed = (end_time - start_time).total_seconds()

    if success:
        print(f"\n\n{'#'*60}")
        print(f"# 处理完成！")
        print(f"# 结束时间: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"# 总耗时: {elapsed:.1f}秒")
        print(f"{'#'*60}")
    else:
        print(f"\n处理失败，请检查日志")


if __name__ == "__main__":
    asyncio.run(main())
