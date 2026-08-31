"""
FULLFILTER_GUI - Realtime Progress & 12 Target Platforms
(v2.5 - Refactored: platform detection, I/O and parsing engines)

This file now acts as the GUI/entrypoint only. Parsing, I/O and
platform detection were moved into the parsertes package to make
things testable and maintainable.
"""

import sys
import os
import asyncio
import logging
import subprocess
from pathlib import Path
from typing import List, Set, Dict, Optional
from datetime import datetime

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QCheckBox, QSpinBox, QTextEdit,
    QFileDialog, QProgressBar, QFrame, QScrollArea, QMessageBox, QComboBox
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import QFont

# Import the refactored modules
from parsertes.platform_detect import identify_platform, PLATFORM_PATTERNS
from parsertes.io_utils import get_txt_files, count_txt_files_in_folder, FileWriter, writer_worker
from parsertes.parse_engine import process_file_line_mode, process_file_block_mode

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
# SMART DETECTION CONFIGURATION (kept for parse_block_smart heuristics)
# ===============================
CORE_URL_KEYS = {"url", "host", "site", "link", "domain", "address", "uri", "target", "page", "location"}
CORE_USER_KEYS = {"user", "username", "login", "email", "account", "usr", "mail", "identity", "u"}
CORE_PASS_KEYS = {"pass", "password", "pwd", "pasw", "secret", "p"}

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
# INITIALIZATION HELPERS
# ===============================
def initialize_result_directory() -> None:
    os.makedirs(RESULT_DIR, exist_ok=True)
    # create files for each known platform pattern
    for filename in PLATFORM_PATTERNS.keys():
        filepath = os.path.join(RESULT_DIR, filename)
        if not os.path.exists(filepath):
            Path(filepath).touch()
    # file for unclassified combos to help tuning
    unclassified_path = os.path.join(RESULT_DIR, "unclassified.txt")
    if not os.path.exists(unclassified_path):
        Path(unclassified_path).touch()

# ===============================
# THREAD WORKER (UI -> asyncio bridge)
# ===============================
class ProcessingThread(QThread):
    progress_update = pyqtSignal(dict)
    log_update = pyqtSignal(str)
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, file_paths: List[str], parse_mode: str = "line", deduplicate: bool = True, max_concurrency: int = 150):
        super().__init__()
        self.file_paths = file_paths
        self.parse_mode = parse_mode
        self.deduplicate = deduplicate
        self.max_concurrency = max_concurrency
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._running_tasks: List[asyncio.Task] = []
        self._stop_requested = False

    def run(self):
        try:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self._run_process())
        except Exception as e:
            self.error.emit(str(e))
        finally:
            if self.loop and not self.loop.is_closed():
                try:
                    self.loop.close()
                except Exception:
                    pass

    def request_stop(self):
        """Request a graceful stop from the outside (UI thread).
        This will cancel the running asyncio tasks and let the writer flush.
        """
        self._stop_requested = True
        if self.loop and self._running_tasks:
            for t in list(self._running_tasks):
                try:
                    self.loop.call_soon_threadsafe(t.cancel)
                except Exception:
                    pass

    async def _run_process(self):
        try:
            files, file_stats = get_txt_files(self.file_paths)
            if not files:
                self.error.emit("❌ Tidak ditemukan file .txt sama sekali!")
                return

            self.log_update.emit(
                f"[★] Memulai pemrosesan {len(files)} file .txt\n"
                f"    → Target Platforms: {len(PLATFORM_PATTERNS)} Custom Platforms\n"
                f"    → Mode Parser: {self.parse_mode.upper()} (Multi-Chunk Active)\n"
                f"    → Max Concurrency: {self.max_concurrency}"
            )

            initialize_result_directory()

            stats = {
                "files": 0, "blocks": 0, "lines": 0,
                "matches": 0, "unique": 0, "total_files": len(files)
            }

            global_set: Set[str] = set()
            sem = asyncio.Semaphore(self.max_concurrency)
            write_queue: asyncio.Queue = asyncio.Queue()
            file_writer = FileWriter(RESULT_DIR)

            async def log_wrapper(msg_type, msg):
                if msg_type == "log":
                    self.log_update.emit(msg)

            writer_task = asyncio.create_task(writer_worker(write_queue, file_writer))

            # choose processor
            processor = process_file_block_mode if self.parse_mode == "block" else process_file_line_mode

            # create processing tasks and keep references so we can cancel them
            tasks = []
            for path in files:
                t = asyncio.create_task(
                    processor(
                        path, global_set, stats, sem,
                        write_queue, log_wrapper, self.deduplicate
                    )
                )
                tasks.append(t)

            self._running_tasks = tasks

            # start a periodic stats updater
            stop_updater = False

            async def stats_updater():
                while not stop_updater:
                    await asyncio.sleep(0.1)
                    self.progress_update.emit(stats.copy())

            updater_task = asyncio.create_task(stats_updater())

            # wait for processors to finish (or be cancelled)
            try:
                await asyncio.gather(*tasks)
            except asyncio.CancelledError:
                # cancellation requested
                self.log_update.emit("[!] Pemrosesan dibatalkan oleh user...")
                for t in tasks:
                    if not t.done():
                        try:
                            t.cancel()
                        except Exception:
                            pass
            finally:
                # ensure writer receives termination sentinel so files flush & close
                try:
                    await write_queue.put(None)
                except Exception:
                    pass

                # wait for writer task to finish
                try:
                    await writer_task
                except Exception as e:
                    logger.warning(f"Writer task issue: {e}")

                stop_updater = True
                try:
                    await updater_task
                except Exception:
                    pass

                # final stats emit and finish
                self.log_update.emit("[✓] Pemrosesan 12 Platform Selesai!")
                self.progress_update.emit(stats)
                self.finished.emit(stats)

        except Exception as e:
            self.error.emit(f"Process error: {str(e)}")

# ===============================
# MAIN GUI WINDOW
# ===============================
class FilterULPApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("FullFilterGUI v2.5 - Clean & High Performance Edition")
        self.resize(950, 680)
        self.setMinimumSize(800, 580)
        
        self.selected_paths: List[str] = []
        self.processing_thread: Optional[ProcessingThread] = None
        
        self.init_ui()
        
    def init_ui(self):
        main_scroll = QScrollArea()
        main_scroll.setWidgetResizable(True)
        main_scroll.setStyleSheet(f"background-color: {Colors.BG_DARK}; border: none;")
        self.setCentralWidget(main_scroll)
        
        container = QWidget()
        main_layout = QVBoxLayout(container)
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.setSpacing(8)
        main_scroll.setWidget(container)
        
        # 1. HEADER
        header_layout = QHBoxLayout()
        title_label = QLabel("FULLFILTER_GUI")
        title_label.setFont(QFont("Consolas", 18, QFont.Bold))
        title_label.setStyleSheet(f"color: {Colors.ACCENT_RED};")
        header_layout.addWidget(title_label)
        
        version_label = QLabel("v2.5 [FAST & CLEAN]")
        version_label.setFont(QFont("Consolas", 10))
        version_label.setStyleSheet(f"color: {Colors.ACCENT_YELLOW};")
        header_layout.addWidget(version_label)
        
        self.status_indicator = QLabel("●")
        self.status_indicator.setStyleSheet(f"color: {Colors.ACCENT_GREEN}; font-size: 14pt;")
        header_layout.addWidget(self.status_indicator)
        
        self.status_label = QLabel("Ready")
        self.status_label.setStyleSheet(f"color: {Colors.TEXT_GRAY}; font-weight: bold;")
        header_layout.addWidget(self.status_label)
        header_layout.addStretch()
        main_layout.addLayout(header_layout)
        
        # 2. INPUT TARGET SECTION
        input_frame = QFrame()
        input_frame.setStyleSheet(f"border: 1px solid {Colors.BORDER_CYAN}; border-radius: 6px; background-color: {Colors.BG_PANEL}; padding: 6px;")
        input_layout = QVBoxLayout(input_frame)
        
        input_label = QLabel("📥 Target Input (File Combo / Folder Stealer Logs)")
        input_label.setStyleSheet(f"color: {Colors.ACCENT_RED}; font-weight: bold; border: none;")
        input_layout.addWidget(input_label)
        
        path_layout = QHBoxLayout()
        self.path_input = QLineEdit()
        self.path_input.setPlaceholderText("Pilih File .txt atau Folder Target...")
        self.path_input.setReadOnly(True)
        self.path_input.setStyleSheet(f"background-color: {Colors.BG_INPUT}; color: {Colors.TEXT_WHITE}; border: 1px solid #333; padding: 5px; border-radius: 4px;")
        path_layout.addWidget(self.path_input)
        
        browse_file_btn = QPushButton("📁 Browse File(s)")
        browse_file_btn.setStyleSheet(f"background-color: {Colors.ACCENT_RED}; color: white; font-weight: bold; padding: 6px 12px; border-radius: 4px;")
        browse_file_btn.clicked.connect(self.browse_files)
        path_layout.addWidget(browse_file_btn)
        
        browse_folder_btn = QPushButton("📂 Browse Folder")
        browse_folder_btn.setStyleSheet(f"background-color: {Colors.ACCENT_CYAN}; color: black; font-weight: bold; padding: 6px 12px; border-radius: 4px;")
        browse_folder_btn.clicked.connect(self.browse_folder)
        path_layout.addWidget(browse_folder_btn)
        
        input_layout.addLayout(path_layout)
        
        self.info_label = QLabel("")
        self.info_label.setStyleSheet(f"color: {Colors.TEXT_DARK_GRAY}; font-size: 9pt; border: none;")
        input_layout.addWidget(self.info_label)
        
        main_layout.addWidget(input_frame)
        
        # 3. CONFIGURATION SECTION
        config_frame = QFrame()
        config_frame.setStyleSheet(f"border: 1px solid {Colors.BORDER_CYAN}; border-radius: 6px; background-color: {Colors.BG_PANEL}; padding: 6px;")
        config_layout = QHBoxLayout(config_frame)
        
        mode_label = QLabel("Parse Mode:")
        mode_label.setStyleSheet(f"color: {Colors.TEXT_GRAY}; border: none;")
        config_layout.addWidget(mode_label)
        
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Line-by-Line Mode (Single-line)", "line")
        self.mode_combo.addItem("Smart Block-Based Mode (Stealer Logs)", "block")
        self.mode_combo.setStyleSheet(f"background-color: {Colors.BG_INPUT}; color: {Colors.TEXT_WHITE}; border: 1px solid #444; padding: 3px;")
        config_layout.addWidget(self.mode_combo)
        
        config_layout.addSpacing(15)
        
        self.dedup_checkbox = QCheckBox("Enable Deduplication")
        self.dedup_checkbox.setChecked(True)
        self.dedup_checkbox.setStyleSheet(f"color: {Colors.TEXT_WHITE}; border: none;")
        config_layout.addWidget(self.dedup_checkbox)
        
        config_layout.addStretch()
        
        threads_label = QLabel("Concurrency:")
        threads_label.setStyleSheet(f"color: {Colors.TEXT_GRAY}; border: none;")
        config_layout.addWidget(threads_label)
        
        self.threads_spin = QSpinBox()
        self.threads_spin.setRange(10, 500)
        self.threads_spin.setValue(150)
        self.threads_spin.setStyleSheet(f"background-color: {Colors.BG_INPUT}; color: {Colors.TEXT_WHITE}; border: 1px solid #444; padding: 3px;")
        config_layout.addWidget(self.threads_spin)
        
        main_layout.addWidget(config_frame)
        
        # 4. STATISTICS GRID (Cleaned)
        stats_frame = QFrame()
        stats_frame.setStyleSheet(f"border: 1px solid {Colors.BORDER_CYAN}; border-radius: 6px; background-color: {Colors.BG_PANEL}; padding: 6px;")
        stats_layout = QHBoxLayout(stats_frame)
        
        self.stats_widgets = {}
        stats_items = [
            ("Files Processed", "files"),
            ("Lines / Blocks", "count"),
            ("Unique Combos", "unique"),
            ("Matches (12 Target)", "matches"),
        ]
        
        for label_text, key in stats_items:
            box = QVBoxLayout()
            lbl = QLabel(label_text)
            lbl.setStyleSheet(f"color: {Colors.TEXT_DARK_GRAY}; font-size: 8pt; border: none;")
            val = QLabel("0")
            val.setStyleSheet(f"color: {Colors.ACCENT_CYAN}; font-size: 11pt; font-weight: bold; border: none;")
            box.addWidget(lbl)
            box.addWidget(val)
            self.stats_widgets[key] = val
            stats_layout.addLayout(box)
            
        main_layout.addWidget(stats_frame)
        
        # 5. PROGRESS BAR
        self.progress_bar = QProgressBar()
        self.progress_bar.setStyleSheet(f"""
            QProgressBar {{ border: 1px solid #333; border-radius: 4px; text-align: center; color: white; background-color: {Colors.BG_INPUT}; height: 18px; }}
            QProgressBar::chunk {{ background-color: {Colors.ACCENT_CYAN}; }}
        """)
        self.progress_bar.setValue(0)
        main_layout.addWidget(self.progress_bar)
        
        # 6. CONSOLE LOG
        console_layout = QVBoxLayout()
        console_header = QHBoxLayout()
        c_lbl = QLabel("🖥️  Console Output")
        c_lbl.setStyleSheet(f"color: {Colors.ACCENT_RED}; font-weight: bold;")
        console_header.addWidget(c_lbl)
        console_header.addStretch()
        
        clear_btn = QPushButton("Clear")
        clear_btn.setStyleSheet("background-color: #333; color: white; padding: 2px 8px; border-radius: 3px;")
        clear_btn.clicked.connect(lambda: self.console_output.clear())
        console_header.addWidget(clear_btn)
        console_layout.addLayout(console_header)
        
        self.console_output = QTextEdit()
        self.console_output.setReadOnly(True)
        self.console_output.setFont(QFont("Consolas", 9))
        self.console_output.setStyleSheet(f"background-color: {Colors.BG_INPUT}; color: {Colors.ACCENT_GREEN}; border: 1px solid #222;")
        self.console_output.setFixedHeight(180)
        console_layout.addWidget(self.console_output)
        
        main_layout.addLayout(console_layout)
        
        # 7. ACTION BUTTONS
        btn_layout = QHBoxLayout()
        self.start_btn = QPushButton("🚀 START PROCESSING")
        self.start_btn.setStyleSheet(f"background-color: {Colors.ACCENT_RED}; color: white; font-weight: bold; padding: 10px; font-size: 11pt; border-radius: 4px;")
        self.start_btn.clicked.connect(self.start_processing)
        btn_layout.addWidget(self.start_btn)
        
        self.stop_btn = QPushButton("🛑 STOP")
        self.stop_btn.setEnabled(False)
        self.stop_btn.setStyleSheet("background-color: #444; color: white; font-weight: bold; padding: 10px; border-radius: 4px;")
        self.stop_btn.clicked.connect(self.stop_processing)
        btn_layout.addWidget(self.stop_btn)
        
        open_btn = QPushButton("📂 OPEN RESULT")
        open_btn.setStyleSheet(f"background-color: {Colors.BG_INPUT}; color: {Colors.ACCENT_CYAN}; font-weight: bold; padding: 10px; border-radius: 4px; border: 1px solid {Colors.ACCENT_CYAN};")
        open_btn.clicked.connect(self.open_result_folder)
        btn_layout.addWidget(open_btn)
        
        main_layout.addLayout(btn_layout)
        
        self.log_console("✓ FullFilterGUI v2.5 Ready")

    def log_console(self, message: str):
        self.console_output.append(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

    def browse_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Pilih File Target (.txt)", "", "Text Files (*.txt);;All Files (*)"
        )
        if files:
            valid_files = [f for f in files if os.path.isfile(f)]
            if not valid_files:
                return
            
            self.selected_paths = valid_files
            display_names = [os.path.basename(p) for p in valid_files]
            self.path_input.setText(f"[Files ({len(valid_files)})]: {', '.join(display_names[:2])}" + 
                                   (f"... +{len(display_names)-2} more" if len(display_names) > 2 else ""))
            self.info_label.setText(f"✓ {len(valid_files)} file .txt dipilih")
            
            idx = self.mode_combo.findData("line")
            if idx != -1: self.mode_combo.setCurrentIndex(idx)
            self.log_console(f"[✓] Terpilih {len(valid_files)} file -> Mode diset: Line-by-Line")

    def browse_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Pilih Folder Target (akan scan recursive)", "", QFileDialog.ShowDirsOnly)
        if folder:
            txt_count = count_txt_files_in_folder(folder)
            if txt_count <= 0:
                QMessageBox.warning(self, "Warning", f"Tidak ditemukan file .txt di:\n{folder}")
                return

            self.selected_paths = [folder]
            self.path_input.setText(f"[Folder]: {folder}")
            self.info_label.setText(f"✓ Folder terpilih ({txt_count} file .txt terdeteksi)")
            
            idx = self.mode_combo.findData("block")
            if idx != -1: self.mode_combo.setCurrentIndex(idx)
            self.log_console(f"[✓] Folder diset: {folder} ({txt_count} file) -> Mode diset: Smart Block-Based Mode")

    def start_processing(self):
        if not self.selected_paths:
            QMessageBox.warning(self, "Warning", "Silakan pilih file atau folder target terlebih dahulu!")
            return

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status_indicator.setStyleSheet(f"color: {Colors.ACCENT_YELLOW}; font-size: 14pt;")
        self.status_label.setText("Processing...")
        
        self.progress_bar.setRange(0, 0)

        self.processing_thread = ProcessingThread(
            file_paths=self.selected_paths,
            parse_mode=self.mode_combo.currentData(),
            deduplicate=self.dedup_checkbox.isChecked(),
            max_concurrency=self.threads_spin.value()
        )
        
        self.processing_thread.progress_update.connect(self.update_progress)
        self.processing_thread.log_update.connect(self.log_console)
        self.processing_thread.finished.connect(self.on_finished)
        self.processing_thread.error.connect(self.on_error)
        
        self.processing_thread.start()

    def stop_processing(self):
        if self.processing_thread and self.processing_thread.isRunning():
            # request graceful stop instead of force terminate
            try:
                self.processing_thread.request_stop()
                self.log_console("[🛑] Permintaan penghentian (graceful) dikirim.")
            except Exception as e:
                self.log_console(f"[✗] Gagal meminta penghentian: {e}")
            self.reset_ui_state()

    def update_progress(self, stats: dict):
        count_val = stats.get("lines", 0) if stats.get("lines", 0) > 0 else stats.get("blocks", 0)
        
        self.stats_widgets["files"].setText(f"{stats.get('files', 0):,}")
        self.stats_widgets["count"].setText(f"{count_val:,}")
        self.stats_widgets["matches"].setText(f"{stats.get('matches', 0):,}")
        self.stats_widgets["unique"].setText(f"{stats.get('unique', 0):,}")

    def on_finished(self, stats: dict):
        self.reset_ui_state()
        QMessageBox.information(self, "Success", "Pemrosesan 12 platform selesai!")

    def on_error(self, err_msg: str):
        self.log_console(f"❌ [ERROR] {err_msg}")
        self.reset_ui_state()
        QMessageBox.critical(self, "Error", f"Terjadi kesalahan:\n{err_msg}")

    def reset_ui_state(self):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status_indicator.setStyleSheet(f"color: {Colors.ACCENT_GREEN}; font-size: 14pt;")
        self.status_label.setText("Ready")
        
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)

    def open_result_folder(self):
        abs_result_path = os.path.abspath(RESULT_DIR)
        os.makedirs(abs_result_path, exist_ok=True)
        try:
            if sys.platform == "win32":
                os.startfile(abs_result_path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", abs_result_path])
            else:
                subprocess.Popen(["xdg-open", abs_result_path])
        except Exception as e:
            self.log_console(f"[✗] Gagal membuka folder result: {e}")

# ===============================
# ENTRY POINT
# ===============================
if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setFont(QFont("Segoe UI", 9))
    window = FilterULPApp()
    window.show()
    sys.exit(app.exec_())
