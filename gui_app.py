import tkinter as tk
from tkinter import filedialog, ttk, messagebox
import os
import glob
import concurrent.futures
import threading
import queue
import time
from collections import defaultdict
import multiprocessing
import sys

APP_VERSION = "1.0.3"

# Import logic from the existing script
# We assume render_orthophotos.py is in the same directory
try:
    # Set PROJ_LIB for bundled environment
    if getattr(sys, 'frozen', False):
        base_path = sys._MEIPASS

        try:
            candidates = [
                os.path.join(base_path, 'rasterio', 'proj_data'),
                os.path.join(base_path, 'Library', 'share', 'proj'),
                os.path.join(base_path, 'proj_data_share'),
                os.path.join(base_path, 'osgeo', 'data', 'proj'),
                os.path.join(base_path, 'pyproj', 'proj_dir', 'share', 'proj'),
            ]

            import sqlite3

            def _proj_db_minor(d):
                try:
                    db = os.path.join(d, 'proj.db')
                    if not os.path.exists(db):
                        return -1
                    con = sqlite3.connect(db)
                    cur = con.cursor()
                    rows = dict(cur.execute('select key,value from metadata').fetchall())
                    con.close()
                    return int(rows.get('DATABASE.LAYOUT.VERSION.MINOR', '-1'))
                except Exception:
                    return -1

            best_dir = None
            best_minor = -1
            for d in candidates:
                m = _proj_db_minor(d)
                if m > best_minor:
                    best_minor = m
                    best_dir = d
        except Exception:
            best_dir = None
        
        # Priority 1: Force overwrite path (Library/share/proj)
        # This matches the path where PyInstaller/PROJ seems to be looking automatically
        p1 = os.path.join(base_path, 'Library', 'share', 'proj')
        
        # Priority 2: Custom backup path
        p2 = os.path.join(base_path, 'proj_data_share')
        
        proj_lib_path = None

        if best_dir and os.path.exists(os.path.join(best_dir, 'proj.db')):
            proj_lib_path = best_dir
        elif os.path.exists(os.path.join(p1, 'proj.db')):
            proj_lib_path = p1
            print(f"DEBUG: Found PROJ data at Priority 1: {proj_lib_path}")
        elif os.path.exists(os.path.join(p2, 'proj.db')):
            proj_lib_path = p2
            print(f"DEBUG: Found PROJ data at Priority 2: {proj_lib_path}")
        else:
            # Fallback to search if manual bundle fails
            print("DEBUG: Manually bundled PROJ_DATA not found, searching...")
            candidates = []
            for root, dirs, files in os.walk(sys._MEIPASS):
                if 'proj.db' in files:
                    candidates.append(root)
            
            # Filter: prefer path containing 'pyproj' or 'Library'
            for p in candidates:
                if 'Library' in p or 'pyproj' in p:
                    proj_lib_path = p
                    break
            
            if not proj_lib_path and candidates:
                proj_lib_path = candidates[0]
            
        if proj_lib_path:
            # Force set both variables
            os.environ['PROJ_LIB'] = proj_lib_path
            os.environ['PROJ_DATA'] = proj_lib_path
            # Also set GDAL_DATA if it exists nearby
            # gdal_data = os.path.join(os.path.dirname(proj_lib_path), 'gdal')
            # if os.path.exists(gdal_data): os.environ['GDAL_DATA'] = gdal_data
            
            print(f"DEBUG: Setting PROJ_LIB={proj_lib_path}")
        else:
            print("WARNING: Could not find proj.db in bundled environment.")

        try:
            dll_dirs = [
                base_path,
                os.path.join(base_path, 'Library', 'bin'),
                os.path.join(base_path, 'lib'),
            ]
            existing = os.environ.get('PATH', '')
            prepend = [d for d in dll_dirs if os.path.isdir(d)]
            os.environ['PATH'] = os.pathsep.join(prepend + [existing])
            for d in prepend:
                try:
                    os.add_dll_directory(d)
                except Exception:
                    pass
        except Exception:
            pass
            
    import render_orthophotos as core
except ImportError:
    # Handle case where it might be packaged or running standalone differently
    # For now, assume it's there.
    pass

class OrthoRendererApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"ObjOrthoRender {APP_VERSION}")
        self.root.geometry("600x610")
        
        # Variables
        self.input_dir = tk.StringVar(value="")
        self.metadata_path = tk.StringVar(value="")
        self.output_dir = tk.StringVar(value="")
        self.gsd = tk.DoubleVar(value=0.05)
        # Merge options: 0=None, 1=Subfolder, 2=All
        self.merge_mode = tk.IntVar(value=2)
        self.workers = tk.IntVar(value=min(4, os.cpu_count() or 4))
        
        self.is_running = False
        self.log_queue = queue.Queue()
        
        self._setup_ui()
        self._start_log_poller()
        
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    def on_closing(self):
        if self.is_running:
            if messagebox.askyesno("确认退出", "任务正在进行中，确定要退出吗？\n(这将强制终止所有进程)"):
                self.root.destroy()
                import os
                os._exit(0)
        else:
            self.root.destroy()
            import os
            os._exit(0)

    def _setup_ui(self):
        # Input Directory
        frame_input = ttk.LabelFrame(self.root, text="输入文件夹 (包含 OBJ 模型)")
        frame_input.pack(fill="x", padx=10, pady=5)
        
        entry_input = ttk.Entry(frame_input, textvariable=self.input_dir)
        entry_input.pack(side="left", fill="x", expand=True, padx=5, pady=5)
        btn_input = ttk.Button(frame_input, text="浏览...", command=self._browse_input)
        btn_input.pack(side="right", padx=5, pady=5)

        frame_metadata = ttk.LabelFrame(self.root, text="metadata.xml (可选，不填则使用输入文件夹下的 metadata.xml)")
        frame_metadata.pack(fill="x", padx=10, pady=5)
        
        entry_metadata = ttk.Entry(frame_metadata, textvariable=self.metadata_path)
        entry_metadata.pack(side="left", fill="x", expand=True, padx=5, pady=5)
        btn_metadata = ttk.Button(frame_metadata, text="浏览...", command=self._browse_metadata)
        btn_metadata.pack(side="right", padx=5, pady=5)
        
        # Output Directory
        frame_output = ttk.LabelFrame(self.root, text="输出文件夹")
        frame_output.pack(fill="x", padx=10, pady=5)
        
        entry_output = ttk.Entry(frame_output, textvariable=self.output_dir)
        entry_output.pack(side="left", fill="x", expand=True, padx=5, pady=5)
        btn_output = ttk.Button(frame_output, text="浏览...", command=self._browse_output)
        btn_output.pack(side="right", padx=5, pady=5)
        
        # Settings
        frame_settings = ttk.LabelFrame(self.root, text="渲染设置")
        frame_settings.pack(fill="x", padx=10, pady=5)
        
        # GSD
        lbl_gsd = ttk.Label(frame_settings, text="分辨率 (米/像素):")
        lbl_gsd.grid(row=0, column=0, padx=5, pady=5, sticky="e")
        entry_gsd = ttk.Entry(frame_settings, textvariable=self.gsd, width=10)
        entry_gsd.grid(row=0, column=1, padx=5, pady=5, sticky="w")
        
        # Workers
        lbl_workers = ttk.Label(frame_settings, text="CPU 线程数:")
        lbl_workers.grid(row=0, column=2, padx=5, pady=5, sticky="e")
        spin_workers = ttk.Spinbox(frame_settings, from_=1, to=64, textvariable=self.workers, width=5)
        spin_workers.grid(row=0, column=3, padx=5, pady=5, sticky="w")
        
        # Merge Options
        frame_merge = ttk.LabelFrame(self.root, text="合并选项")
        frame_merge.pack(fill="x", padx=10, pady=5)
        
        rb_no_merge = ttk.Radiobutton(frame_merge, text="不合并", variable=self.merge_mode, value=0)
        rb_no_merge.pack(side="left", padx=10, pady=5)
        
        rb_subfolder = ttk.Radiobutton(frame_merge, text="按子文件夹合并", variable=self.merge_mode, value=1)
        rb_subfolder.pack(side="left", padx=10, pady=5)
        
        rb_all = ttk.Radiobutton(frame_merge, text="合并所有", variable=self.merge_mode, value=2)
        rb_all.pack(side="left", padx=10, pady=5)
        
        # Progress
        frame_progress = ttk.Frame(self.root)
        frame_progress.pack(fill="x", padx=10, pady=10)
        
        self.lbl_status = ttk.Label(frame_progress, text="就绪")
        self.lbl_status.pack(anchor="w")
        
        self.progress_bar = ttk.Progressbar(frame_progress, orient="horizontal", mode="determinate")
        self.progress_bar.pack(fill="x", pady=5)
        
        # Start Button
        self.btn_start = ttk.Button(self.root, text="开始处理", command=self._start_processing)
        self.btn_start.pack(pady=5)
        
        # Log Area
        frame_log = ttk.LabelFrame(self.root, text="日志 (Log)")
        frame_log.pack(fill="both", expand=True, padx=10, pady=5)
        
        self.txt_log = tk.Text(frame_log, height=10, state="disabled")
        scroll_log = ttk.Scrollbar(frame_log, orient="vertical", command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=scroll_log.set)
        
        self.txt_log.pack(side="left", fill="both", expand=True)
        scroll_log.pack(side="right", fill="y")

    def _safe_unlink(self, path):
        try:
            os.remove(path)
            return True
        except Exception:
            return False

    def _prune_empty_dirs(self, root_dir):
        try:
            if not root_dir or not os.path.isdir(root_dir):
                return
            for current_root, dirnames, filenames in os.walk(root_dir, topdown=False):
                if current_root == root_dir:
                    continue
                if filenames:
                    continue
                if dirnames:
                    continue
                try:
                    os.rmdir(current_root)
                except Exception:
                    pass
        except Exception:
            return

    def _browse_input(self):
        d = filedialog.askdirectory()
        if d:
            self.input_dir.set(d)

    def _browse_metadata(self):
        p = filedialog.askopenfilename(filetypes=[("XML files", "*.xml"), ("All files", "*.*")])
        if p:
            self.metadata_path.set(p)

    def _browse_output(self):
        d = filedialog.askdirectory()
        if d:
            self.output_dir.set(d)
            
    def _log(self, message):
        self.log_queue.put(message)
        
    def _start_log_poller(self):
        while not self.log_queue.empty():
            msg = self.log_queue.get_nowait()
            self.txt_log.configure(state="normal")
            self.txt_log.insert("end", msg + "\n")
            self.txt_log.see("end")
            self.txt_log.configure(state="disabled")
        self.root.after(100, self._start_log_poller)
        
    def _start_processing(self):
        if self.is_running:
            return
            
        input_dir = self.input_dir.get()
        output_dir = self.output_dir.get()
        
        if not os.path.exists(input_dir):
            messagebox.showerror("Error", "Input directory does not exist.")
            return
            
        metadata_path = (self.metadata_path.get() or "").strip()
        if not metadata_path:
            metadata_path = os.path.join(input_dir, "metadata.xml")
        if not os.path.exists(metadata_path):
            messagebox.showerror("Error", f"metadata.xml not found: {metadata_path}")
            return
            
        self.is_running = True
        self.btn_start.configure(state="disabled")
        self.progress_bar['value'] = 0
        self.lbl_status.configure(text="Initializing...")
        
        # Start thread
        t = threading.Thread(target=self._process_thread, args=(input_dir, output_dir, metadata_path))
        t.start()
        
    def _process_thread(self, input_dir, output_dir, metadata_path):
        try:
            self._log("Loading metadata...")
            try:
                self._log(f"Metadata path: {metadata_path}")
                metadata = core.load_metadata(metadata_path)
                self._log(f"Metadata loaded: {metadata['srs']}")
            except Exception as e:
                self._log(f"Error loading metadata: {e}")
                return

            global_origin = metadata['origin']
            srs_code = metadata['srs']
            gsd = self.gsd.get()
            workers = self.workers.get()
            
            self._log("Searching for OBJ files...")
            obj_files = glob.glob(os.path.join(input_dir, '**', '*.obj'), recursive=True)
            total_files = len(obj_files)
            self._log(f"Found {total_files} OBJ files.")
            
            if total_files == 0:
                self._log("No files to process.")
                return

            self.root.after(0, lambda: self.progress_bar.configure(maximum=total_files))
            
            generated_files_by_dir = defaultdict(list)
            
            # Processing
            self._log(f"Starting processing with {workers} workers...")
            
            with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as executor:
                futures = {}
                for obj_path in obj_files:
                    f = executor.submit(
                        core.process_single_file,
                        obj_path,
                        input_dir,
                        output_dir,
                        gsd,
                        srs_code,
                        global_origin
                    )
                    futures[f] = obj_path
                    
                completed_count = 0
                for future in concurrent.futures.as_completed(futures):
                    completed_count += 1
                    obj_path = futures[future]
                    try:
                        target_dir, output_path, error = future.result()
                        if target_dir and output_path:
                            generated_files_by_dir[target_dir].append(output_path)
                            self._log(f"[{completed_count}/{total_files}] Done: {os.path.basename(obj_path)}")
                        else:
                            self._log(f"[{completed_count}/{total_files}] Failed: {os.path.basename(obj_path)} - {error}")
                    except Exception as e:
                        self._log(f"[{completed_count}/{total_files}] Error {os.path.basename(obj_path)}: {e}")
                    
                    # Update Progress
                    self.root.after(0, lambda v=completed_count: self.progress_bar.configure(value=v))
                    self.root.after(0, lambda v=completed_count: self.lbl_status.configure(text=f"Processing {v}/{total_files}"))

            # Merging
            merge_mode = self.merge_mode.get()
            
            if merge_mode > 0:
                self._log("Starting Merge...")
                self.root.after(0, lambda: self.lbl_status.configure(text="Merging TIFFs..."))
                
                merge_tasks = []
                
                # Option 1: Merge by Subfolder
                if merge_mode == 1:
                    for dir_path, tiff_files in generated_files_by_dir.items():
                        if len(tiff_files) > 0:
                            dir_name = os.path.basename(dir_path)
                            if not dir_name: dir_name = "merged"
                            parent_dir = os.path.dirname(dir_path)
                            merged_output_path = os.path.join(parent_dir, f"{dir_name}.tif")
                            merge_tasks.append((tiff_files, merged_output_path, list(tiff_files)))
                            
                # Option 2: Merge All
                elif merge_mode == 2:
                    all_tiffs = []
                    for tiffs in generated_files_by_dir.values():
                        all_tiffs.extend(tiffs)
                    
                    if len(all_tiffs) > 0:
                        merged_output_path = os.path.join(output_dir, "merged_all.tif")
                        merge_tasks.append((all_tiffs, merged_output_path, list(all_tiffs)))
                
                total_merges = len(merge_tasks)
                # We can reuse the progress bar for merges or just log it
                self.root.after(0, lambda: self.progress_bar.configure(value=0, maximum=total_merges))
                
                for i, (tiff_files, out_path, cleanup_files) in enumerate(merge_tasks):
                    try:
                        core.merge_tiffs(tiff_files, out_path)
                        self._log(f"Merged: {os.path.basename(out_path)}")
                        removed = 0
                        out_norm = os.path.normpath(out_path)
                        for fp in cleanup_files:
                            if os.path.normpath(fp) == out_norm:
                                continue
                            if self._safe_unlink(fp):
                                removed += 1
                        self._log(f"Cleanup: removed {removed} tiles")
                    except Exception as e:
                        self._log(f"Merge Failed {out_path}: {e}")
                    
                    self.root.after(0, lambda v=i+1: self.progress_bar.configure(value=v))
                self._prune_empty_dirs(output_dir)
            
            self._log("All tasks completed!")
            messagebox.showinfo("Success", "Processing Completed!")
            
        except Exception as e:
            self._log(f"Critical Error: {e}")
            import traceback
            traceback.print_exc()
            messagebox.showerror("Error", str(e))
        finally:
            self.is_running = False
            self.root.after(0, lambda: self.btn_start.configure(state="normal"))
            self.root.after(0, lambda: self.lbl_status.configure(text="Ready"))

def main():
    """Launch the GUI. Exposed as the objorthorender-gui console script."""
    multiprocessing.freeze_support()

    root = tk.Tk()
    OrthoRendererApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
