"""
I/O utilities for parsertes package.
Contains file discovery helpers and an async FileWriter + writer_worker
so the main GUI/entrypoint can import and remain thin.
"""

import os
import asyncio
import aiofiles
from pathlib import Path
from typing import List, Dict
import logging

logger = logging.getLogger(__name__)


def get_txt_files(paths: List[str]) -> (List[str], Dict[str, int]):
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


class FileWriter:
    def __init__(self, result_dir: str):
        self.result_dir = result_dir
        self.opened_files = {}
        self.write_lock = asyncio.Lock()

    async def write_line(self, target_file: str, line: str) -> None:
        async with self.write_lock:
            try:
                if target_file not in self.opened_files:
                    full_path = os.path.join(self.result_dir, target_file)
                    # ensure directory exists
                    os.makedirs(os.path.dirname(full_path) or self.result_dir, exist_ok=True)
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
            for filename, f in list(self.opened_files.items()):
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
