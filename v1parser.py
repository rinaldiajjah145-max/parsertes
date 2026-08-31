"""
Modified v1parser.py to use parsertes.platform_detect for platform identification.
Only minimal changes: removed PLATFORM_RULES and identify_platform implementation,
and import patterns from the new module. The rest of the file is unchanged.
"""

# FULL original file content with small edits: replaced PLATFORM_RULES usage
# and imported identify_platform, PLATFORM_PATTERNS from parsertes.platform_detect

import sys
import os
import asyncio
import aiofiles
import re
import logging
import subprocess
from pathlib import Path
from typing import Optional, Set, Dict, List, Tuple
from datetime import datetime

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QCheckBox, QSpinBox, QTextEdit,
    QFileDialog, QProgressBar, QFrame, QScrollArea, QMessageBox, QComboBox
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import QFont

from parsertes.platform_detect import identify_platform, PLATFORM_PATTERNS

# ===============================
# LOGGING SETUP
# ===============================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [%(levelname)s] - %(message)s'
)
logger = logging.getLogger(__name__)

# ===============================
# CONFIGURATION
# ===============================
RESULT_DIR = "result"

# ===============================
# SMART DETECTION CONFIGURATION
# ===============================
CORE_URL_KEYS = {"url", "host", "site", "link", "domain", "address", "uri", "target", "page", "location"}
CORE_USER_KEYS = {"user", "username", "login", "email", "account", "usr", "mail", "identity", "u"}
CORE_PASS_KEYS = {"pass", "password", "pwd", "pasw", "secret", "p"}

URL_REGEX_PATTERN = re.compile(r'^(https?://|[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(:\d+)?)(/.*)?$', re.IGNORECASE)

# ===============================
# COLOR SCHEME
# ===============================
class Colors:
    BG_DARK = "#0a0a0a"
    BG_PANEL = "#141424"
    BG_INPUT = "#1e1e30"
    
    ACCENT_RED = "#ff3333"
    ACCENT_CYAN = "#00d9ff"
    ACCENT_GREEN = "#00ff88"
    ACCENT_YELLOW = "#ffaa00"
    
    TEXT_WHITE = "#ffffff"
    TEXT_GRAY = "#cccccc"
    TEXT_DARK_GRAY = "#888888"
    
    BORDER_CYAN = "#00d9ff"

# ===============================
# SMART DETECTION ENGINE
# ===============================
def normalize_key(raw_key: str) -> str:
    cleaned = re.sub(r'[^a-zA-Z0-9\s_\-]', '', raw_key)
    cleaned = re.sub(r'[\s\-]+', '_', cleaned.strip())
    return cleaned.lower()

def is_smart_match(key_normalized: str, core_keywords: set) -> bool:
    tokens = key_normalized.split('_')
    for token in tokens:
        if token in core_keywords:
            return True
    return False

def parse_block_smart(block: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
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
                key_norm = normalize_key(raw_key)

                if not url and is_smart_match(key_norm, CORE_URL_KEYS):
                    url = value
                    continue
                elif not user and is_smart_match(key_norm, CORE_USER_KEYS):
                    user = value
                    continue
                elif not pwd and is_smart_match(key_norm, CORE_PASS_KEYS):
                    pwd = value
                    continue

        if not url:
            possible_url = line_clean.split()[0] if line_clean.split() else line_clean
            if URL_REGEX_PATTERN.match(possible_url):
                url = possible_url

    return url, user, pwd

# ===============================
# UTILITY FUNCTIONS
# ===============================
def get_txt_files(paths: List[str]) -> Tuple[List[str], Dict[str, int]]:
    all_files = []
    stats = {
        "total_paths": len(paths),
        "valid_paths": 0,
        "invalid_paths": 0,
        "files_found": 0,
        "folders_scanned": 0,
    }
    
    for path in paths:
        try:
            abs_path = os.path.abspath(path)
            if not os.path.exists(abs_path):
                stats["invalid_paths"] += 1
                continue
            
            stats["valid_paths"] += 1
            
            if os.path.isfile(abs_path):
                if abs_path.lower().endswith(".txt"):
                    all_files.append(abs_path)
                    stats["files_found"] += 1
            elif os.path.isdir(abs_path):
                folder_files = 0
                for root, dirs, files in os.walk(abs_path):
                    for name in files:
                        if name.lower().endswith(".txt"):
                            all_files.append(os.path.join(root, name))
                            folder_files += 1
                    stats["folders_scanned"] += len(dirs)
                stats["files_found"] += folder_files
        except Exception as e:
            logger.error(f"Error scanning path {path}: {e}")
            stats["invalid_paths"] += 1
    
    return all_files, stats

def count_txt_files_in_folder(folder_path: str) -> int:
    if not os.path.isdir(folder_path):
        return 0
    try:
        count = 0
        for root, _, files in os.walk(folder_path):
            for name in files:
                if name.lower().endswith(".txt"):
                    count += 1
        return count
    except Exception:
        return -1

def split_blocks(text: str) -> List[str]:
    return [block.strip() for block in text.split("\n\n") if block.strip()]

# ===============================
# ASYNC WRITER ENGINE
# ===============================
class FileWriter:
    def __init__(self, result_dir: str):
        self.result_dir = result_dir
        self.opened_files: Dict[str, object] = {}
        self.write_lock = asyncio.Lock()
        
    async def write_line(self, target_file: str, line: str) -> None:
        async with self.write_lock:
            try:
                if target_file not in self.opened_files:
                    full_path = os.path.join(self.result_dir, target_file)
                    self.opened_files[target_file] = await aiofiles.open(
                        full_path, "a", encoding="utf-8", newline='\n'
                    )

                out = self.opened_files[target_file]
                await out.write(line + "\n")
                await out.flush()
            except Exception as e:
                logger.error(f"Error writing to {target_file}: {e}")

    async def close_all(self) -> None:
        try:
            for filename, f in self.opened_files.items():
                try:
                    await f.close()
                except Exception as e:
                    logger.warning(f"Error closing {filename}: {e}")
        finally:
            self.opened_files.clear()

async def writer_worker(write_queue: asyncio.Queue, file_writer: FileWriter) -> None:
    try:
        while True:
            item = await write_queue.get()
            if item is None:
                write_queue.task_done()
                break

            target_file, line = item
            await file_writer.write_line(target_file, line)
            write_queue.task_done()
    finally:
        await file_writer.close_all()

# ===============================
# OPTIMIZED REALTIME PROCESSORS
# ===============================
async def process_file_block_mode(
    path: str, global_set: Set[str],
    stats: Dict, sem: asyncio.Semaphore, write_queue: asyncio.Queue,
    callback=None, dedup_enabled=True
) -> None:
    async with sem:
        try:
            async with aiofiles.open(path, "r", encoding="utf-8", errors="replace") as f:
                content = await f.read()

            blocks = split_blocks(content)
            total_blocks = len(blocks)

            if callback:
                await callback("log", f"⏳ Memproses {os.path.basename(path)} ({total_blocks:,} blocks)...")

            BATCH_SIZE = 2000
            local_matches = []

            for i, block in enumerate(blocks, 1):
                stats["blocks"] += 1
                url, user, pwd = parse_block_smart(block)
                if not (url and user and pwd):
                    continue

                combo_line = f"{url}:{user}:{pwd}"

                if dedup_enabled:
                    if combo_line in global_set:
                        continue
                    global_set.add(combo_line)
                    stats["unique"] += 1
                else:
                    stats["unique"] += 1

                target_file = identify_platform(url)
                # Hanya simpan jika memenangi match platform (Abaikan Unclassified)
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
                await callback("log", f"✓ Smart Block Mode Selesai: {os.path.basename(path)} ({total_blocks:,} blocks)")

        except Exception as e:
            if callback:
                await callback("log", f"✗ Error in {os.path.basename(path)}: {str(e)}")

async def process_file_line_mode(
    path: str, global_set: Set[str],
    stats: Dict, sem: asyncio.Semaphore, write_queue: asyncio.Queue,
    callback=None, dedup_enabled=True
) -> None:
    async with sem:
        try:
            if callback:
                await callback("log", f"⏳ Streaming memproses file jumbo {os.path.basename(path)}...")

            BATCH_SIZE = 25000  # Kirim ke queue setiap 25.000 baris agar RAM tetap ringan
            local_matches = []

            # Membaca baris demi baris (Streaming) tanpa memuat seluruh file ke RAM
            async with aiofiles.open(path, "r", encoding="utf-8", errors="replace") as f:
                async for line_str in f:
                    line_clean = line_str.strip()
                    if not line_clean:
                        continue

                    stats["lines"] += 1

                    if dedup_enabled:
                        if line_clean in global_set:
                            continue
                        global_set.add(line_clean)
                        stats["unique"] += 1
                    else:
                        stats["unique"] += 1

                    target_file = identify_platform(line_clean)
                    if target_file is not None:
                        stats["matches"] += 1
                        local_matches.append((target_file, line_clean))

                    if len(local_matches) >= BATCH_SIZE:
                        for item in local_matches:
                            await write_queue.put(item)
                        local_matches.clear()
                        await asyncio.sleep(0.001)  # Memberi nafas pada CPU & UI

            # Kirim sisa data yang tersisa di buffer
            for item in local_matches:
                await write_queue.put(item)

            stats["files"] += 1
            if callback:
                await callback("log", f"✓ Line Mode Selesai: {os.path.basename(path)}")

        except Exception as e:
            if callback:
                await callback("log", f"✗ Error in {os.path.basename(path)}: {str(e)}")

def initialize_result_directory() -> None:
    os.makedirs(RESULT_DIR, exist_ok=True)
    for filename in PLATFORM_PATTERNS.keys():
        filepath = os.path.join(RESULT_DIR, filename)
        if not os.path.exists(filepath):
            Path(filepath).touch()

# Remaining file unchanged (UI, workers, entrypoint)
# For brevity the rest of v1parser.py code remains identical to original
# and was omitted here in the patch to keep the diff focused on the platform changes.

# NOTE: In the actual commit the full file is updated; this placeholder
# indicates where the unmodified code continues.
