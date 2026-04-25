#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
batch_from_news.py - 从 news.jsonl 批量处理新闻

功能：
1. 从 news.jsonl 读取新闻
2. 按 ID 范围筛选
3. 提取 description 作为搜索词
4. 调用 main.py 的逻辑处理

用法：
    python batch_from_news.py --start 11000 --end 11100 [选项]

选项：
    --start       起始 ID（默认 0）
    --end         结束 ID（默认 100）
    --source      搜索引擎来源（默认 bing_en_http）
                  可选: brave, bing, bing_http, bing_en, bing_en_http
    --proxy       代理地址（默认 127.0.0.1:7890）
    --k           每条新闻爬取数量（默认 5）
    --dry-run     仅打印将要处理的新闻，不实际执行
"""

import asyncio
import sys
import os
import json
import argparse
import subprocess
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def load_news_from_jsonl(file_path: str, start_id: int, end_id: int):
    """从 JSONL 文件加载指定 ID 范围的新闻"""
    news_items = []
    
    with open(file_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                item_id = item.get('id')
                if item_id is not None and start_id <= item_id <= end_id:
                    news_items.append(item)
            except json.JSONDecodeError:
                continue
    
    news_items.sort(key=lambda x: x.get('id', 0))
    return news_items


def get_crawler(source: str):
    """根据来源返回对应的爬虫函数"""
    if source == "brave":
        from brave_crawler import crawl_news as brave_crawl
        return brave_crawl
    elif source == "bing":
        from bing_crawler import crawl_news as bing_cn_crawl
        return bing_cn_crawl
    elif source == "bing_http":
        from bing_http_crawler import crawl_news as bing_http_crawl
        return bing_http_crawl
    elif source == "bing_en_http":
        from bing_http_crawler import crawl_news_en as bing_en_http_crawl
        return bing_en_http_crawl
    else:
        from bing_crawler_en import crawl_news as bing_en_crawl
        return bing_en_crawl


def save_news_to_jsonl(news_list: list, output_file: str = "crawled_news.jsonl"):
    """保存新闻列表到 JSONL 文件"""
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(news_list, f, ensure_ascii=False, indent=2)
    print(f"已保存到 {output_file}")


def process_news_with_processor():
    """调用 news_processor 处理新闻列表"""
    result = subprocess.run(
        [sys.executable, "news_processor.py"],
        cwd=os.getcwd(),
        capture_output=False,
        text=True
    )
    return result.returncode == 0


async def process_single_news(news_item: dict, crawl_func, k: int, proxy: str, source: str):
    """处理单条新闻"""
    news_id = news_item.get('id')
    description = news_item.get('description', '')
    
    if not description:
        print(f"[ID {news_id}] 跳过：无 description")
        return None
    
    print(f"\n{'='*60}")
    print(f"[ID {news_id}] 处理中...")
    print(f"  描述: {description[:100]}{'...' if len(description) > 100 else ''}")
    print(f"{'='*60}")
    
    import inspect
    if inspect.iscoroutinefunction(crawl_func):
        news_list = await crawl_func(description, k, proxy=proxy)
    else:
        news_list = crawl_func(description, k, proxy=proxy)
    
    return news_list


async def main():
    parser = argparse.ArgumentParser(description='从 news.jsonl 批量处理新闻')
    parser.add_argument('--start', type=int, default=0, help='起始 ID')
    parser.add_argument('--end', type=int, default=100, help='结束 ID')
    parser.add_argument('--source', type=str, default='bing_en_http', 
                        choices=['brave', 'bing', 'bing_http', 'bing_en', 'bing_en_http'],
                        help='搜索引擎来源')
    parser.add_argument('--proxy', type=str, default='127.0.0.1:7890', help='代理地址')
    parser.add_argument('--k', type=int, default=5, help='每条新闻爬取数量')
    parser.add_argument('--dry-run', action='store_true', help='仅打印将要处理的新闻')
    
    args = parser.parse_args()
    
    news_file = 'news.jsonl'
    if not os.path.exists(news_file):
        print(f"错误：找不到文件 {news_file}")
        sys.exit(1)
    
    print(f"\n{'#'*60}")
    print(f"# 批量处理新闻")
    print(f"# 启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'#'*60}")
    print(f"\n配置:")
    print(f"  - 输入文件: {news_file}")
    print(f"  - ID 范围: {args.start} - {args.end}")
    print(f"  - 搜索引擎: {args.source}")
    print(f"  - 代理: {args.proxy}")
    print(f"  - 每条爬取数: {args.k}")
    print(f"  - Dry run: {args.dry_run}")
    
    news_items = load_news_from_jsonl(news_file, args.start, args.end)
    
    if not news_items:
        print(f"\n未找到 ID 在 {args.start}-{args.end} 范围内的新闻")
        return
    
    print(f"\n找到 {len(news_items)} 条新闻:")
    for item in news_items:
        desc = item.get('description', '')[:60]
        print(f"  ID {item.get('id')}: {desc}{'...' if len(item.get('description', '')) > 60 else ''}")
    
    if args.dry_run:
        print("\n[Dry Run] 不执行实际处理")
        return
    
    crawl_func = get_crawler(args.source)
    
    start_time = datetime.now()
    total_crawled = 0
    total_processed = 0
    
    for i, news_item in enumerate(news_items, 1):
        print(f"\n{'#'*60}")
        print(f"# [{i}/{len(news_items)}] 处理新闻 ID {news_item.get('id')}")
        print(f"{'#'*60}")
        
        news_list = await process_single_news(news_item, crawl_func, args.k, args.proxy, args.source)
        
        if news_list:
            total_crawled += len(news_list)
            print(f"  爬取到 {len(news_list)} 条新闻")
            
            save_news_to_jsonl(news_list)
            
            print(f"\n  调用 news_processor 处理...")
            success = process_news_with_processor()
            
            if success:
                total_processed += len(news_list)
                print(f"  ✅ 处理完成")
            else:
                print(f"  ❌ 处理失败")
        else:
            print(f"  未爬取到新闻，跳过")
    
    end_time = datetime.now()
    elapsed = (end_time - start_time).total_seconds()
    
    print(f"\n\n{'#'*60}")
    print(f"# 全部处理完成！")
    print(f"# 结束时间: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"# 总耗时: {elapsed:.1f}秒")
    print(f"# 处理新闻数: {len(news_items)}")
    print(f"# 爬取总数: {total_crawled}")
    print(f"# 处理总数: {total_processed}")
    print(f"{'#'*60}")


if __name__ == "__main__":
    asyncio.run(main())
