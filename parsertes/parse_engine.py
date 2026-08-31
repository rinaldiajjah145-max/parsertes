"""
Parsing engine for parsertes package.
Contains line- and block-based processors intended to be run within
an asyncio event loop. Uses parsertes.platform_detect.identify_platform
for platform classification and expects a write_queue + FileWriter to
output results.
"""

import re
import asyncio
from typing import Optional, Set, Dict, List
from urllib.parse import urlparse

from parsertes.platform_detect import identify_platform


URL_REGEX_PATTERN = re.compile(r'^(https?://|[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(:\d+)?)(/.*)?$', re.IGNORECASE)


def split_blocks(text: str) -> List[str]:
    return [block.strip() for block in text.split("\n\n") if block.strip()]


def normalize_combo_url(url: str) -> str:
    """Normalize URL for dedup purposes: remove scheme, trailing slash, lowercase."""
    if not url:
        return url
    s = url.strip()
    parse_target = s if re.match(r'^[a-zA-Z][a-zA-Z0-9+\-.]*://', s) else ("http://" + s)
    try:
        p = urlparse(parse_target)
        host = (p.netloc or "").lower()
        path = (p.path or "").rstrip('/').lower()
        if path:
            return f"{host}{path}"
        return host
    except Exception:
        return s.lower().rstrip('/')


def parse_block_smart(block: str) -> (Optional[str], Optional[str], Optional[str]):
    url = user = pwd = None
    lines = block.splitlines()

    for line in lines:
        line_clean = line.strip()
        if not line_clean:
            continue

        if ":" in line_clean:
            key_part, value_part = line_clean.split(":", 1)
            raw_key = key_part.strip()
            value = value_part.strip()

            if value:
                key_norm = re.sub(r'[^a-zA-Z0-9\s_\-]', '', raw_key)
                key_norm = re.sub(r'[\s\-]+', '_', key_norm.strip()).lower()

                if not url and any(t in key_norm for t in ("url","host","site","link","domain","address","uri","page","location")):
                    url = value
                    continue
                elif not user and any(t in key_norm for t in ("user","username","login","email","account","usr","mail","identity","u")):
                    user = value
                    continue
                elif not pwd and any(t in key_norm for t in ("pass","password","pwd","pasw","secret","p")):
                    pwd = value
                    continue

        if not url:
            possible_url = line_clean.split()[0] if line_clean.split() else line_clean
            if URL_REGEX_PATTERN.match(possible_url):
                url = possible_url

    return url, user, pwd


async def process_file_block_mode(
    path: str, global_set: Set[str],
    stats: Dict, sem: asyncio.Semaphore, write_queue: asyncio.Queue,
    callback=None, dedup_enabled=True
) -> None:
    async with sem:
        try:
            # Read whole file into memory (block mode)
            async with asyncio.open_file as afile:
                pass
        except Exception:
            # Fallback to aiofiles if asyncio.open_file not available
            import aiofiles
            async with aiofiles.open(path, "r", encoding="utf-8", errors="replace") as f:
                content = await f.read()

            blocks = split_blocks(content)
            total_blocks = len(blocks)

            if callback:
                await callback("log", f"⏳ Memproses {path} ({total_blocks:,} blocks)...")

            BATCH_SIZE = 2000
            local_matches = []

            for i, block in enumerate(blocks, 1):
                stats["blocks"] += 1
                url, user, pwd = parse_block_smart(block)
                if not (url and user and pwd):
                    continue

                # Normalize URL for dedup
                norm_url = normalize_combo_url(url)
                combo_line = f"{norm_url}:{user}:{pwd}"

                if dedup_enabled:
                    if combo_line in global_set:
                        continue
                    global_set.add(combo_line)
                    stats["unique"] += 1
                else:
                    stats["unique"] += 1

                target_file = identify_platform(url)
                if target_file is not None:
                    stats["matches"] += 1
                    local_matches.append((target_file, combo_line))

                if i % BATCH_SIZE == 0:
                    for item in local_matches:
                        await write_queue.put(item)
                    local_matches.clear()
                    await asyncio.sleep(0.001)

            for item in local_matches:
                await write_queue.put(item)

            stats["files"] += 1
            if callback:
                await callback("log", f"✓ Smart Block Mode Selesai: {path} ({total_blocks:,} blocks)")


async def process_file_line_mode(
    path: str, global_set: Set[str],
    stats: Dict, sem: asyncio.Semaphore, write_queue: asyncio.Queue,
    callback=None, dedup_enabled=True
) -> None:
    async with sem:
        try:
            import aiofiles
            if callback:
                await callback("log", f"⏳ Streaming memproses file jumbo {path}...")

            BATCH_SIZE = 25000
            local_matches = []

            async with aiofiles.open(path, "r", encoding="utf-8", errors="replace") as f:
                async for line_str in f:
                    line_clean = line_str.strip()
                    if not line_clean:
                        continue

                    stats["lines"] += 1

                    # Normalize URL for dedup when possible
                    parts = line_clean.split(":")
                    if len(parts) >= 3:
                        raw_url = parts[0]
                        norm_url = normalize_combo_url(raw_url)
                        combo_key = f"{norm_url}:{parts[1]}:{parts[2]}"
                    else:
                        combo_key = line_clean.lower()

                    if dedup_enabled:
                        if combo_key in global_set:
                            continue
                        global_set.add(combo_key)
                        stats["unique"] += 1
                    else:
                        stats["unique"] += 1

                    target_file = identify_platform(line_clean)
                    if target_file is not None:
                        stats["matches"] += 1
                        local_matches.append((target_file, combo_key))

                    if len(local_matches) >= BATCH_SIZE:
                        for item in local_matches:
                            await write_queue.put(item)
                        local_matches.clear()
                        await asyncio.sleep(0.001)

            for item in local_matches:
                await write_queue.put(item)

            stats["files"] += 1
            if callback:
                await callback("log", f"✓ Line Mode Selesai: {path}")

        except Exception as e:
            if callback:
                await callback("log", f"✗ Error in {path}: {str(e)}")
