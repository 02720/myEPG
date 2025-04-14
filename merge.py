#!/usr/bin/env python3
"""
EPG合并工具 - 独立版
从多个EPG源抓取并合并为一个XML文件
支持 .xml 和 .gz 格式的输入
"""

import os
import re
import gzip
import shutil
import xml.etree.ElementTree as ET
from datetime import datetime
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from xml.dom import minidom
from time import time

try:
    import requests
    from tqdm import tqdm
except ImportError:
    print("请先安装依赖: pip install requests tqdm")
    exit(1)

# 配置
CONFIG_FILE = "config.txt"
OUTPUT_XML = "output/epg.xml"
OUTPUT_GZ = "output/epg.gz"
MAX_WORKERS = 10
REQUEST_TIMEOUT = 60


def read_config():
    """从配置文件读取EPG链接"""
    urls = []
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    urls.append(line)
    return urls


def fetch_epg(url):
    """抓取EPG数据，支持.gz和.xml格式"""
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        content = response.content

        # 自动检测是否为gzip格式
        if url.endswith('.gz') or content[:2] == b'\x1f\x8b':
            try:
                content = gzip.decompress(content)
            except Exception as e:
                print(f"  解压gzip失败 {url}: {e}")
                return None

        return content.decode('utf-8', errors='ignore')
    except Exception as e:
        print(f"  抓取失败 {url}: {e}")
        return None


def parse_epg(epg_content):
    """解析EPG XML内容"""
    try:
        parser = ET.XMLParser(encoding='UTF-8')
        root = ET.fromstring(epg_content, parser=parser)
    except ET.ParseError as e:
        print(f"  XML解析错误: {e}")
        return {}, defaultdict(list)

    channels = {}
    programmes = defaultdict(list)

    # 提取频道信息
    for channel in root.findall('channel'):
        channel_id = channel.get('id')
        display_name_elem = channel.find('display-name')
        if display_name_elem is not None and display_name_elem.text:
            display_name = display_name_elem.text.strip()
            channels[channel_id] = display_name

    # 提取节目信息
    for programme in root.findall('programme'):
        channel_id = programme.get('channel')
        if not channel_id:
            continue

        # 复制节目元素
        prog_copy = ET.Element('programme')
        for key, value in programme.attrib.items():
            prog_copy.set(key, value)

        # 复制子元素
        for child in programme:
            child_copy = ET.SubElement(prog_copy, child.tag, child.attrib)
            if child.text:
                child_copy.text = child.text

        programmes[channel_id].append(prog_copy)

    return channels, programmes


def merge_epgs(epg_results):
    """合并多个EPG结果"""
    merged_programmes = defaultdict(list)
    seen_channels = set()

    for channels, programmes in epg_results:
        if not channels:
            continue

        for channel_id, display_name in channels.items():
            # 使用频道ID或显示名称作为唯一标识
            key = channel_id if not channel_id.isdigit() else display_name

            if key not in seen_channels:
                seen_channels.add(key)
                merged_programmes[display_name] = programmes[channel_id]

    return merged_programmes


def write_to_xml(programmes, path):
    """写入XML文件"""
    root = ET.Element('tv', attrib={
        'date': datetime.now().strftime("%Y%m%d%H%M%S +0800"),
        'generator-info-name': 'EPG Merger'
    })

    for channel_id, data in programmes.items():
        # 添加频道定义
        channel_elem = ET.SubElement(root, 'channel', attrib={"id": channel_id})
        display_name_elem = ET.SubElement(channel_elem, 'display-name', attrib={"lang": "zh"})
        display_name_elem.text = channel_id

        # 添加节目
        for prog in data:
            prog.set('channel', channel_id)
            root.append(prog)

    # 确保输出目录存在
    target_dir = os.path.dirname(path)
    os.makedirs(target_dir, exist_ok=True)

    # 生成格式化的XML
    xml_str = ET.tostring(root, 'utf-8')
    pretty_xml = minidom.parseString(xml_str).toprettyxml(indent='  ', newl='\n')

    # 删除空行
    pretty_xml = '\n'.join([line for line in pretty_xml.split('\n') if line.strip()])

    with open(path, 'w', encoding='utf-8') as f:
        f.write(pretty_xml)

    print(f"✓ XML文件已保存: {path}")


def compress_to_gz(input_path, output_path):
    """压缩为.gz文件"""
    with open(input_path, 'rb') as f_in:
        with gzip.open(output_path, 'wb') as f_out:
            shutil.copyfileobj(f_in, f_out)
    print(f"✓ GZ文件已保存: {output_path}")


def main():
    print("=" * 60)
    print("EPG合并工具")
    print("=" * 60)

    # 读取配置
    urls = read_config()
    if not urls:
        print(f"错误: 未在 {CONFIG_FILE} 中找到EPG链接")
        print("请创建配置文件并添加EPG源链接，每行一个")
        return

    print(f"\n找到 {len(urls)} 个EPG源:")
    for i, url in enumerate(urls, 1):
        print(f"  {i}. {url}")

    # 抓取所有EPG
    print(f"\n开始抓取EPG数据...")
    epg_results = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_url = {executor.submit(fetch_epg, url): url for url in urls}

        for future in tqdm(future_to_url, desc="抓取进度", unit="源"):
            url = future_to_url[future]
            try:
                content = future.result()
                if content:
                    channels, programmes = parse_epg(content)
                    if channels:
                        epg_results.append((channels, programmes))
                        print(f"  ✓ {url}: {len(channels)} 个频道")
                    else:
                        print(f"  ✗ {url}: 无有效数据")
            except Exception as e:
                print(f"  ✗ {url}: {e}")

    if not epg_results:
        print("\n错误: 没有成功抓取到任何EPG数据")
        return

    print(f"\n成功抓取 {len(epg_results)} 个源")

    # 合并EPG
    print("\n合并EPG数据...")
    merged = merge_epgs(epg_results)
    print(f"合并完成: {len(merged)} 个唯一频道")

    # 统计节目数量
    total_programmes = sum(len(progs) for progs in merged.values())
    print(f"节目总数: {total_programmes}")

    # 输出文件
    print("\n生成输出文件...")
    write_to_xml(merged, OUTPUT_XML)
    compress_to_gz(OUTPUT_XML, OUTPUT_GZ)

    # 输出统计
    xml_size = os.path.getsize(OUTPUT_XML) / 1024 / 1024
    gz_size = os.path.getsize(OUTPUT_GZ) / 1024 / 1024

    print("\n" + "=" * 60)
    print("完成!")
    print(f"  - XML: {OUTPUT_XML} ({xml_size:.2f} MB)")
    print(f"  - GZ:  {OUTPUT_GZ} ({gz_size:.2f} MB)")
    print("=" * 60)


if __name__ == "__main__":
    main()
