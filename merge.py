import xml.etree.ElementTree as ET
from collections import defaultdict
import aiohttp
import asyncio
from tqdm.asyncio import tqdm_asyncio  # 引入 tqdm 的异步支持
from datetime import datetime, timezone, timedelta
import gzip
import shutil
from xml.dom import minidom
import re
from opencc import OpenCC
import os
from tqdm import tqdm  # 引入 tqdm 的同步支持

TZ_UTC_PLUS_8 = timezone(timedelta(hours=8))
CC_T2S = OpenCC("t2s")
TIME_FORMAT = "%Y%m%d%H%M%S%z"
TIME_FORMAT_NO_TZ = "%Y%m%d%H%M%S"
OUTPUT_TIME_FORMAT = "%Y%m%d%H%M%S %z"


def transform2_zh_hans(string):
    if string is None:
        return ""
    return CC_T2S.convert(string)


async def fetch_epg(url):
    connector = aiohttp.TCPConnector(limit=16, ssl=False)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/113.0.0.0 Safari/537.36"
    }
    try:
        async with aiohttp.ClientSession(connector=connector, trust_env=True, headers=headers) as session:
            async with session.get(url) as response:
                if url.endswith('.gz'):
                    compressed_data = await response.read()
                    return gzip.decompress(compressed_data).decode('utf-8', errors='ignore')
                else:
                    return await response.text(encoding='utf-8')
    except aiohttp.ClientError as e:
        print(f"{url}HTTP请求错误: {e}")
    except asyncio.TimeoutError:
        print("{url}请求超时")
    except Exception as e:
        print(f"{url}其他错误: {e}")
    return None




def parse_xmltv_datetime(raw_time):
    cleaned_time = re.sub(r'\s+', '', raw_time or '')
    if not cleaned_time:
        raise ValueError('empty time string')

    normalized_time = cleaned_time
    if normalized_time.endswith('Z'):
        normalized_time = normalized_time[:-1] + '+0000'

    try:
        dt = datetime.strptime(normalized_time, TIME_FORMAT)
    except ValueError:
        dt = datetime.strptime(normalized_time, TIME_FORMAT_NO_TZ)
        dt = dt.replace(tzinfo=TZ_UTC_PLUS_8)

    return dt.astimezone(TZ_UTC_PLUS_8)

def parse_epg(epg_content):
    try:
        parser = ET.XMLParser(encoding='UTF-8')
        root = ET.fromstring(epg_content, parser=parser)
    except ET.ParseError as e:
        print(f"Error parsing XML: {e}")
        print(f"Problematic content: {epg_content[:500]}")
        return {}, defaultdict(list)

    channels = {}
    programmes = defaultdict(list)

    for channel in root.findall('channel'):
        channel_id = transform2_zh_hans(channel.get('id'))
        if not channel_id:
            continue
        channel_display_names = []
        for name in channel.findall('display-name'):
            display_name = transform2_zh_hans(name.text)
            if display_name:
                channel_display_names.append([display_name, name.get('lang', 'zh')])
        existing_display_names = {name[0] for name in channel_display_names}
        if not channel_id.isdigit() and channel_id not in existing_display_names:
            channel_display_names.append([channel_id, 'zh'])
        channels[channel_id] = channel_display_names

    today = datetime.now(TZ_UTC_PLUS_8).date()
    valid_channels = set()

    for programme in root.findall('programme'):
        channel_id = transform2_zh_hans(programme.get('channel'))
        start_raw = programme.get('start')
        stop_raw = programme.get('stop')
        if not start_raw or not stop_raw:
            continue

        try:
            channel_start = parse_xmltv_datetime(start_raw)
            channel_stop = parse_xmltv_datetime(stop_raw)
        except ValueError as exc:
            print(f"Skip invalid programme time: start={start_raw}, stop={stop_raw}, error={exc}")
            continue

        if channel_stop.date() == today:
            valid_channels.add(channel_id)

        channel_elem = ET.Element(
            'programme', attrib={
                "channel": channel_id,
                "start": channel_start.strftime(OUTPUT_TIME_FORMAT),
                "stop": channel_stop.strftime(OUTPUT_TIME_FORMAT),
            }
        )
        for title in programme.findall('title'):
            if title.text is None:
                channel_title = "精彩节目"
            else:
                channel_title = title.text.strip()
            langattr = title.get('lang')
            if langattr == 'zh' or langattr is None:
                channel_title = transform2_zh_hans(channel_title)
            channel_elem_t = ET.SubElement(channel_elem, 'title')
            channel_elem_t.text = channel_title
            if langattr is not None:
                channel_elem_t.set('lang', langattr)
        for desc in programme.findall('desc'):
            if desc.text is None:
                continue
            langattr = desc.get('lang')
            channel_desc = desc.text.strip()
            if langattr == 'zh' or langattr is None:
                channel_desc = transform2_zh_hans(channel_desc)
            channel_elem_d = ET.SubElement(channel_elem, 'desc')
            channel_elem_d.text = channel_desc.strip()
            if langattr is not None:
                channel_elem_d.set('lang', langattr)
        programmes[channel_id].append(channel_elem)
        
    # Filter channels that don't have any program ending today
    channels = {k: v for k, v in channels.items() if k in valid_channels}
    # Optional: Filter programmes as well to keep data consistent, 
    # though only valid channels are returned so main loop might be fine.
    # But filtering programmes dict saves memory and ensures correctness if main iterates programmes keys logic changes.
    programmes = {k: v for k, v in programmes.items() if k in valid_channels}

    return channels, programmes


def write_to_xml(channels_id, channels_names, programmes, filename):
    # 目录不存在
    if not os.path.exists('output'):
        os.makedirs('output')
    current_time = datetime.now(TZ_UTC_PLUS_8).strftime(OUTPUT_TIME_FORMAT)
    root = ET.Element('tv', attrib={'date': current_time})
    for channel_id in channels_id:
        channel_elem = ET.SubElement(
            root, 'channel', attrib={"id": channel_id})
        for display_name_node in channels_names[channel_id]:
            display_name = display_name_node[0]
            langattr = display_name_node[1]
            display_name_elem = ET.SubElement(
                channel_elem, 'display-name', attrib={"lang": langattr})
            display_name_elem.text = display_name
        for prog in programmes[channel_id]:
            prog.set('channel', channel_id)  # 设置 programme 的 channel 属性
            root.append(prog)

    # Beautify the XML output
    rough_string = ET.tostring(root, 'utf-8')
    reparsed = minidom.parseString(rough_string)
    with open(filename, 'w', encoding='utf-8') as f:
        f.write(reparsed.toprettyxml(indent='\t', newl='\n'))


def compress_to_gz(input_filename, output_filename):
    with open(input_filename, 'rb') as f_in:
        with gzip.open(output_filename, 'wb') as f_out:
            shutil.copyfileobj(f_in, f_out)


def get_urls():
    urls = []
    with open('config.txt', 'r', encoding='utf-8') as file:
        for line in file:
            line = line.strip()
            if line and not line.startswith('#'):
                urls.append(line)
    return urls


async def main():
    urls = get_urls()
    tasks = [fetch_epg(url) for url in urls]
    print("Fetching EPG data...")
    epg_contents = await tqdm_asyncio.gather(*tasks, desc="Fetching URLs")
    all_channels_map = {}
    all_channel_id = set()
    all_channel_names = defaultdict(list)
    all_programmes = defaultdict(list)
    print("Finished.")
    i = 0
    for epg_content in epg_contents:
        i += 1
        print(f"Processing EPG source...{i}/{len(epg_contents)}")
        if epg_content is None:
            continue
        print("Parsing EPG data...")
        channels, programmes = parse_epg(epg_content)
        print("Finished.")
        with tqdm(total=len(channels), desc="Merging EPG", unit="file") as pbar:
            for channel_id, display_names in channels.items():
                if len(programmes[channel_id]) == 0:
                    continue
                map_id = all_channels_map.get(channel_id, channel_id)
                is_in_map = map_id != channel_id
                for display_name_node in display_names:
                    display_name = display_name_node[0]
                    if is_in_map:
                        break
                    existing_map_id = all_channels_map.get(display_name)
                    if existing_map_id:
                        map_id = existing_map_id
                        is_in_map = True
                if not is_in_map:
                    all_channel_id.add(channel_id)
                    all_channel_names[channel_id] = display_names
                    all_programmes[channel_id] = programmes[channel_id]
                    all_channels_map[channel_id] = channel_id
                    for display_name_node in display_names:
                        display_name = display_name_node[0]
                        all_channels_map[display_name] = channel_id
                elif len(all_programmes[map_id]) < len(programmes[channel_id]):
                    all_programmes[map_id] = programmes[channel_id]
                    for display_name_node in display_names:
                        display_name = display_name_node[0]
                        if display_name not in all_channels_map:
                            all_channel_names[map_id].append(display_name_node)
                            all_channels_map[display_name] = map_id
                pbar.update(1)  # 更新进度条
    print("Writing to XML...")
    write_to_xml(all_channel_id, all_channel_names,
                all_programmes, 'output/epg.xml')
    compress_to_gz('output/epg.xml', 'output/epg.gz')

if __name__ == '__main__':
    asyncio.run(main())
