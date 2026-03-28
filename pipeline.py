#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
流水线模式 - 整合爬取和处理
流程：
1. 读取输入记录
2. 对每条记录：
   a. 爬取新闻
   b. 立即处理这批新闻
"""

import json
import os
import asyncio
import sys
from datetime import datetime
import subprocess

# 配置
INPUT_JSONL_FILE = "news.jsonl"
NEWS_OUTPUT_FILE = "crawled_news.jsonl"
BATCH_SIZE = 10
NEWS_PER_QUERY = 5
USE_THREADED_PROCESSOR = False  # 是否使用多线程处理


def load_input_jsonl(file_path: str, from_end: bool = True, count: int = BATCH_SIZE):
    """加载输入JSONL文件"""
    items = []
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read().strip()
            if content.startswith('[') and content.endswith(']'):
                all_items = json.loads(content)
            else:
                all_items = []
                for line in content.split('\n'):
                    line = line.strip()
                    if line:
                        all_items.append(json.loads(line))
        
        if from_end:
            items = all_items[-count:] if count < len(all_items) else all_items
        else:
            items = all_items[:count] if count < len(all_items) else all_items
        
        print(f"加载了 {len(items)} 条记录（从{'末尾' if from_end else '开头'}开始）")
        return items
    except Exception as e:
        print(f"加载文件失败: {e}")
        return []


async def crawl_news_for_query(query: str, k: int = NEWS_PER_QUERY):
    """爬取新闻"""
    from brave_crawler import crawl_news as ths_crawl
    
    print(f"\n{'='*60}")
    print(f"爬取关键词: {query[:50]}...")
    print(f"{'='*60}")
    
    try:
        result = await ths_crawl(query, k)
        return result
    except Exception as e:
        print(f"爬取失败: {e}")
        return []


def process_news_with_threaded():
    """使用多线程处理器处理新闻"""
    try:
        result = subprocess.run(
            [sys.executable, "news_processor_threaded.py"],
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


def process_news_with_original():
    """使用原始串行处理器处理新闻"""
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


def load_processor_stats():
    """加载处理器统计信息"""
    try:
        stats_file = "processor_stats.json"
        if os.path.exists(stats_file):
            with open(stats_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        return None
    except Exception as e:
        print(f"读取统计信息失败: {e}")
        return None


async def main():
    """主函数 - 流水线模式"""
    overall_start_time = datetime.now()
    
    print(f"\n{'#'*60}")
    print(f"# 新闻爬取与处理（流水线模式）")
    print(f"# 启动时间: {overall_start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"# 处理模式: {'多线程处理' if USE_THREADED_PROCESSOR else '串行处理'}")
    print(f"{'#'*60}\n")
    
    print(f"配置:")
    print(f"  - 输入文件: {INPUT_JSONL_FILE}")
    print(f"  - 每次爬取新闻数: {NEWS_PER_QUERY}")
    print(f"  - 处理记录数: {BATCH_SIZE}")
    print(f"  - 模式: 爬一条→处理一条（流水线）")
    print()
    
    items = load_input_jsonl(INPUT_JSONL_FILE, from_end=True, count=BATCH_SIZE)
    if not items:
        print("没有找到待处理的记录")
        return
    
    total_items = len(items)
    total_crawled = 0
    total_processed = 0
    
    overall_stats = {
        "total_items": 0,
        "processed_count": 0,
        "success_count": 0,
        "pure_script_count": 0,
        "cached_count": 0,
        "blacklisted_count": 0,
        "agent_count": 0,
        "agent_success_count": 0
    }
    
    for idx, item in enumerate(items, 1):
        query = item.get('description', '')
        item_id = item.get('id', 'unknown')
        
        if not query or len(query.strip()) < 5:
            print(f"\n[{idx}/{total_items}] 跳过无效描述: id={item_id}")
            continue
        
        print(f"\n{'#'*60}")
        print(f"# [{idx}/{total_items}] 处理 id={item_id}")
        print(f"{'#'*60}")
        
        news_list = await crawl_news_for_query(query, NEWS_PER_QUERY)
        
        if not news_list:
            print(f"  未爬取到新闻，跳过")
            continue
        
        for news in news_list:
            news['source_id'] = item_id
            news['source_description'] = query
        
        total_crawled += len(news_list)
        print(f"  本次爬取: {len(news_list)} 条，累计: {total_crawled} 条")
        
        with open(NEWS_OUTPUT_FILE, 'w', encoding='utf-8') as f:
            json.dump(news_list, f, ensure_ascii=False, indent=2)
        print(f"  已保存到 {NEWS_OUTPUT_FILE}")
        
        print(f"\n  开始处理这批新闻...")
        if USE_THREADED_PROCESSOR:
            success = process_news_with_threaded()
        else:
            success = process_news_with_original()
        
        if success:
            total_processed += len(news_list)
            print(f"\n  处理完成!")
            print(f"  - 本次处理: {len(news_list)} 条")
            print(f"  - 累计处理: {total_processed} 条")
            
            batch_stats = load_processor_stats()
            if batch_stats:
                overall_stats["total_items"] += batch_stats.get("total_items", 0)
                overall_stats["processed_count"] += batch_stats.get("processed_count", 0)
                overall_stats["success_count"] += batch_stats.get("success_count", 0)
                overall_stats["pure_script_count"] += batch_stats.get("pure_script_count", 0)
                overall_stats["cached_count"] += batch_stats.get("cached_count", 0)
                overall_stats["blacklisted_count"] += batch_stats.get("blacklisted_count", 0)
                overall_stats["agent_count"] += batch_stats.get("agent_count", 0)
                overall_stats["agent_success_count"] += batch_stats.get("agent_success_count", 0)
        else:
            print(f"  处理失败，继续下一条")
    
    overall_end_time = datetime.now()
    overall_elapsed = (overall_end_time - overall_start_time).total_seconds()
    
    print(f"\n\n{'#'*60}")
    print(f"# 主程序完成")
    print(f"# 结束时间: {overall_end_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"# 总体耗时: {overall_elapsed:.1f}秒")
    print(f"{'#'*60}")
    
    print(f"\n{'='*60}")
    print(f"【总体统计信息】")
    print(f"{'='*60}")
    print(f"  总处理条数: {overall_stats['total_items']}")
    print(f"  成功条数: {overall_stats['success_count']}")
    print(f"  正文成功率: {overall_stats['success_count']/overall_stats['total_items']*100:.1f}%" if overall_stats['total_items'] > 0 else "  正文成功率: 0%")
    print(f"  纯脚本提取: {overall_stats['pure_script_count']}")
    print(f"  复用缓存: {overall_stats['cached_count']}")
    print(f"  调用Agent次数: {overall_stats['agent_count']}")
    print(f"  调用Agent率: {overall_stats['agent_count']/overall_stats['total_items']*100:.1f}%" if overall_stats['total_items'] > 0 else "  调用Agent率: 0%")
    print(f"  Agent调用成功次数: {overall_stats.get('agent_success_count', 0)}")
    if overall_stats['agent_count'] > 0:
        agent_sr = overall_stats.get('agent_success_count', 0) / overall_stats['agent_count'] * 100
        print(f"  Agent调用成功率: {agent_sr:.1f}%")
    else:
        print(f"  Agent调用成功率: 0%")
    print(f"  黑名单跳过: {overall_stats['blacklisted_count']}")
    print(f"{'='*60}")
    
    print(f"\n最终统计:")
    print(f"  - 处理搜索数: {total_items}")
    print(f"  - 爬取新闻数: {total_crawled}")
    print(f"  - 处理新闻数: {total_processed}")
    print()


if __name__ == "__main__":
    asyncio.run(main())
