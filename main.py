import os
import sys
import json
import subprocess
import shutil
import threading
from fractions import Fraction

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


# ---------- 工具函数 ----------
def find_ffmpeg():
    """ 从系统 PATH 中查找 ffmpeg 和 ffprobe """
    ffmpeg = shutil.which('ffmpeg')
    ffprobe = shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:
        raise FileNotFoundError(
            '未找到 ffmpeg/ffprobe，请确保它们已添加到系统 PATH。\n'
            '可从 https://ffmpeg.org/download.html 下载并配置环境变量。'
        )
    return ffmpeg, ffprobe


def probe_video(ffprobe, input_path):
    """ 获取视频宽、高、帧率、总帧数 """
    cmd = [
        ffprobe, '-v', 'error',
        '-select_streams', 'v:0',
        '-show_entries', 'stream=width,height,avg_frame_rate,duration,nb_frames',
        '-of', 'json', input_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8')
    if result.returncode != 0:
        raise RuntimeError(f'ffprobe 读取失败：{result.stderr}')

    data = json.loads(result.stdout)
    stream = data['streams'][0]

    width = int(stream['width'])
    height = int(stream['height'])
    fps_str = stream.get('avg_frame_rate', '30/1')
    fps = float(Fraction(fps_str))

    # 总帧数优先使用 nb_frames，否则用 duration * fps 估算
    if 'nb_frames' in stream and stream['nb_frames'].isdigit():
        total_frames = int(stream['nb_frames'])
    else:
        duration = float(stream.get('duration', 0))
        total_frames = int(round(duration * fps))

    if total_frames < 2:
        raise ValueError('视频帧数不足 2 帧，无法处理')

    return width, height, fps, total_frames


def even_number(n):
    """ 取最近的偶数（向下） """
    n = int(n)
    if n % 2 == 1:
        n -= 1
    return max(2, n)


def calculate_output_size(orig_w, orig_h, target_size, mode):
    """ 根据目标长度和模式计算缩放后的尺寸 """
    joined_w = 2 * orig_w
    joined_h = orig_h

    if mode == 'long':
        long_side = max(joined_w, joined_h)
        scale = target_size / long_side
    else:  # 'short'
        short_side = min(joined_w, joined_h)
        scale = target_size / short_side

    out_w = even_number(joined_w * scale)
    out_h = even_number(joined_h * scale)
    return out_w, out_h


def parse_fps(fps_text):
    """ 解析帧率输入，返回浮点数或 None """
    fps_text = fps_text.strip()
    if not fps_text or fps_text == '0':
        return None
    try:
        fps = float(fps_text)
        if fps <= 0:
            raise ValueError
        return fps
    except ValueError:
        raise ValueError('帧率必须是正数或留空')


def process_video(input_path, output_path, target_size, mode, target_fps=None,
                  status_callback=None, progress_callback=None):
    """ 视频拼接处理函数 """
    ffmpeg, ffprobe = find_ffmpeg()

    if status_callback:
        status_callback('正在读取视频信息...')
    width, height, fps, total_frames = probe_video(ffprobe, input_path)

    left_frames = (total_frames + 1) // 2   # 余数帧归左边
    right_frames = total_frames - left_frames
    if status_callback:
        status_callback(f'总帧数：{total_frames}，左段：{left_frames} 帧，右段：{right_frames} 帧')

    out_w, out_h = calculate_output_size(width, height, target_size, mode)
    if status_callback:
        status_callback(f'输出尺寸：{out_w} x {out_h}')

    # 构建滤镜链
    filter_parts = [
        f"[0:v]trim=start_frame=0:end_frame={left_frames},setpts=PTS-STARTPTS[left]",
        f"[0:v]trim=start_frame={left_frames}:end_frame={total_frames},setpts=PTS-STARTPTS[right]",
        f"[left][right]hstack=inputs=2[stacked]",
        f"[stacked]scale={out_w}:{out_h}[scaled]"
    ]
    if target_fps is not None:
        filter_parts.append(f"[scaled]fps={target_fps}[outv]")
        map_label = '[outv]'
    else:
        map_label = '[scaled]'

    filter_complex = ";".join(filter_parts)

    cmd = [
        ffmpeg, '-y',
        '-i', input_path,
        '-filter_complex', filter_complex,
        '-map', map_label,
        '-c:v', 'prores_ks',
        '-profile:v', '1',      # 1 = ProRes 422 LT
        '-pix_fmt', 'yuv422p10le',
        '-an',                  # 丢弃音频
        output_path
    ]

    if status_callback:
        status_callback('正在处理视频...')
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding='utf-8', errors='replace')
    _, stderr = process.communicate()

    if process.returncode != 0:
        raise RuntimeError(f'ffmpeg 处理失败：\n{stderr[-1000:]}')

    if status_callback:
        status_callback('处理完成！')
    if progress_callback:
        progress_callback(100)


def restore_video(input_path, output_path, target_fps=None,
                  status_callback=None, progress_callback=None):
    """ 视频还原处理函数：将左右并排的视频拆分为两半并顺序拼接 """
    ffmpeg, ffprobe = find_ffmpeg()

    if status_callback:
        status_callback('正在读取视频信息...')
    width, height, fps, total_frames = probe_video(ffprobe, input_path)

    if width % 2 != 0:
        raise ValueError('输入视频宽度必须为偶数才能分割还原')
    out_w = width // 2
    out_h = height

    if status_callback:
        status_callback(f'输入尺寸：{width}x{height}，输出尺寸：{out_w}x{out_h}')

    # 构建滤镜链：裁剪左半和右半，然后时间轴拼接
    filter_parts = [
        f"[0:v]crop={out_w}:{out_h}:0:0,setpts=PTS-STARTPTS[left]",
        f"[0:v]crop={out_w}:{out_h}:{out_w}:0,setpts=PTS-STARTPTS[right]",
        f"[left][right]concat=n=2:v=1:a=0[concatenated]"
    ]
    if target_fps is not None:
        filter_parts.append(f"[concatenated]fps={target_fps}[outv]")
        map_label = '[outv]'
    else:
        map_label = '[concatenated]'

    filter_complex = ";".join(filter_parts)

    cmd = [
        ffmpeg, '-y',
        '-i', input_path,
        '-filter_complex', filter_complex,
        '-map', map_label,
        '-c:v', 'prores_ks',
        '-profile:v', '1',
        '-pix_fmt', 'yuv422p10le',
        '-an',
        output_path
    ]

    if status_callback:
        status_callback('正在还原视频...')
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding='utf-8', errors='replace')
    _, stderr = process.communicate()

    if process.returncode != 0:
        raise RuntimeError(f'ffmpeg 处理失败：\n{stderr[-1000:]}')

    if status_callback:
        status_callback('还原完成！')
    if progress_callback:
        progress_callback(100)


# ---------- GUI ----------
class App:
    def __init__(self, root):
        self.root = root
        root.title('视频拼接/还原工具 - ProRes 422LT')
        root.geometry('650x500')
        root.resizable(False, False)

        # 创建 Notebook（标签页）
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill='both', expand=True, padx=10, pady=10)

        # 标签页1：视频拼接
        self.tab_join = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_join, text='视频拼接')

        # 标签页2：视频还原
        self.tab_restore = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_restore, text='视频还原')

        # 初始化两个标签页的控件
        self._build_join_tab()
        self._build_restore_tab()

    def _build_join_tab(self):
        """ 构建拼接标签页 """
        tab = self.tab_join
        pad_x = 10
        pad_y = 10

        # 输入文件
        tk.Label(tab, text='输入视频文件：').grid(row=0, column=0, sticky='w', padx=pad_x, pady=pad_y)
        self.join_input_var = tk.StringVar()
        tk.Entry(tab, textvariable=self.join_input_var, width=45).grid(row=0, column=1, padx=5)
        tk.Button(tab, text='浏览...', command=self.browse_join_input).grid(row=0, column=2, padx=5)

        # 输出文件
        tk.Label(tab, text='输出文件：').grid(row=1, column=0, sticky='w', padx=pad_x, pady=pad_y)
        self.join_output_var = tk.StringVar()
        tk.Entry(tab, textvariable=self.join_output_var, width=45).grid(row=1, column=1, padx=5)
        tk.Button(tab, text='浏览...', command=self.browse_join_output).grid(row=1, column=2, padx=5)

        # 目标长度
        tk.Label(tab, text='目标长度（像素）：').grid(row=2, column=0, sticky='w', padx=pad_x, pady=pad_y)
        self.join_target_var = tk.StringVar(value='1800')
        tk.Entry(tab, textvariable=self.join_target_var, width=15).grid(row=2, column=1, sticky='w', padx=5)

        # 模式选择
        tk.Label(tab, text='设置方式：').grid(row=3, column=0, sticky='w', padx=pad_x, pady=pad_y)
        self.join_mode_var = tk.StringVar(value='long')
        tk.Radiobutton(tab, text='长边', variable=self.join_mode_var, value='long').grid(row=3, column=1, sticky='w')
        tk.Radiobutton(tab, text='短边', variable=self.join_mode_var, value='short').grid(row=3, column=1, sticky='e', padx=80)

        # 输出帧率
        tk.Label(tab, text='输出帧率（留空为原帧率）：').grid(row=4, column=0, sticky='w', padx=pad_x, pady=pad_y)
        self.join_fps_var = tk.StringVar(value='')
        tk.Entry(tab, textvariable=self.join_fps_var, width=15).grid(row=4, column=1, sticky='w', padx=5)

        # 状态显示
        self.join_status_var = tk.StringVar(value='就绪')
        tk.Label(tab, textvariable=self.join_status_var, fg='blue').grid(row=5, column=0, columnspan=3, pady=10)

        # 进度条
        self.join_progress = ttk.Progressbar(tab, length=500, mode='determinate')
        self.join_progress.grid(row=6, column=0, columnspan=3, pady=10)

        # 开始按钮
        self.join_start_btn = tk.Button(tab, text='开始拼接', command=self.start_join,
                                        bg='#4CAF50', fg='white', height=2)
        self.join_start_btn.grid(row=7, column=0, columnspan=3, pady=20)

    def _build_restore_tab(self):
        """ 构建还原标签页 """
        tab = self.tab_restore
        pad_x = 10
        pad_y = 10

        # 输入文件
        tk.Label(tab, text='输入视频文件（左右并排）：').grid(row=0, column=0, sticky='w', padx=pad_x, pady=pad_y)
        self.restore_input_var = tk.StringVar()
        tk.Entry(tab, textvariable=self.restore_input_var, width=45).grid(row=0, column=1, padx=5)
        tk.Button(tab, text='浏览...', command=self.browse_restore_input).grid(row=0, column=2, padx=5)

        # 输出文件
        tk.Label(tab, text='输出文件：').grid(row=1, column=0, sticky='w', padx=pad_x, pady=pad_y)
        self.restore_output_var = tk.StringVar()
        tk.Entry(tab, textvariable=self.restore_output_var, width=45).grid(row=1, column=1, padx=5)
        tk.Button(tab, text='浏览...', command=self.browse_restore_output).grid(row=1, column=2, padx=5)

        # 输出帧率
        tk.Label(tab, text='输出帧率（留空为原帧率）：').grid(row=2, column=0, sticky='w', padx=pad_x, pady=pad_y)
        self.restore_fps_var = tk.StringVar(value='')
        tk.Entry(tab, textvariable=self.restore_fps_var, width=15).grid(row=2, column=1, sticky='w', padx=5)

        # 状态显示
        self.restore_status_var = tk.StringVar(value='就绪')
        tk.Label(tab, textvariable=self.restore_status_var, fg='blue').grid(row=3, column=0, columnspan=3, pady=10)

        # 进度条
        self.restore_progress = ttk.Progressbar(tab, length=500, mode='determinate')
        self.restore_progress.grid(row=4, column=0, columnspan=3, pady=10)

        # 开始按钮
        self.restore_start_btn = tk.Button(tab, text='开始还原', command=self.start_restore,
                                           bg='#2196F3', fg='white', height=2)
        self.restore_start_btn.grid(row=5, column=0, columnspan=3, pady=20)

    # ---------- 浏览文件 ----------
    def browse_join_input(self):
        path = filedialog.askopenfilename(
            title='选择视频文件',
            filetypes=[('视频文件', '*.mp4 *.mov *.avi *.mkv *.m4v *.webm'), ('所有文件', '*.*')]
        )
        if path:
            self.join_input_var.set(path)
            base, ext = os.path.splitext(path)
            self.join_output_var.set(f'{base}_joined.mov')

    def browse_join_output(self):
        path = filedialog.asksaveasfilename(
            title='保存输出文件',
            defaultextension='.mov',
            filetypes=[('QuickTime 视频', '*.mov'), ('所有文件', '*.*')]
        )
        if path:
            self.join_output_var.set(path)

    def browse_restore_input(self):
        path = filedialog.askopenfilename(
            title='选择视频文件',
            filetypes=[('视频文件', '*.mp4 *.mov *.avi *.mkv *.m4v *.webm'), ('所有文件', '*.*')]
        )
        if path:
            self.restore_input_var.set(path)
            base, ext = os.path.splitext(path)
            self.restore_output_var.set(f'{base}_restored.mov')

    def browse_restore_output(self):
        path = filedialog.asksaveasfilename(
            title='保存输出文件',
            defaultextension='.mov',
            filetypes=[('QuickTime 视频', '*.mov'), ('所有文件', '*.*')]
        )
        if path:
            self.restore_output_var.set(path)

    # ---------- 开始处理 ----------
    def start_join(self):
        input_path = self.join_input_var.get().strip()
        output_path = self.join_output_var.get().strip()
        target_text = self.join_target_var.get().strip()
        mode = self.join_mode_var.get()
        fps_text = self.join_fps_var.get().strip()

        if not input_path:
            messagebox.showerror('错误', '请选择输入视频')
            return
        if not output_path:
            messagebox.showerror('错误', '请选择输出路径')
            return
        try:
            target_size = int(target_text)
            if target_size <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror('错误', '目标长度必须是正整数')
            return
        try:
            target_fps = parse_fps(fps_text)
        except ValueError as e:
            messagebox.showerror('错误', str(e))
            return

        self.join_start_btn.config(state='disabled')
        self.join_progress['value'] = 0
        self.join_status_var.set('开始处理...')

        def worker():
            try:
                process_video(
                    input_path, output_path, target_size, mode, target_fps,
                    status_callback=lambda msg: self.join_status_var.set(msg),
                    progress_callback=lambda v: self.join_progress.config(value=v)
                )
                messagebox.showinfo('完成', '视频拼接完成！')
            except Exception as e:
                messagebox.showerror('错误', str(e))
            finally:
                self.join_start_btn.config(state='normal')

        threading.Thread(target=worker, daemon=True).start()

    def start_restore(self):
        input_path = self.restore_input_var.get().strip()
        output_path = self.restore_output_var.get().strip()
        fps_text = self.restore_fps_var.get().strip()

        if not input_path:
            messagebox.showerror('错误', '请选择输入视频')
            return
        if not output_path:
            messagebox.showerror('错误', '请选择输出路径')
            return
        try:
            target_fps = parse_fps(fps_text)
        except ValueError as e:
            messagebox.showerror('错误', str(e))
            return

        self.restore_start_btn.config(state='disabled')
        self.restore_progress['value'] = 0
        self.restore_status_var.set('开始处理...')

        def worker():
            try:
                restore_video(
                    input_path, output_path, target_fps,
                    status_callback=lambda msg: self.restore_status_var.set(msg),
                    progress_callback=lambda v: self.restore_progress.config(value=v)
                )
                messagebox.showinfo('完成', '视频还原完成！')
            except Exception as e:
                messagebox.showerror('错误', str(e))
            finally:
                self.restore_start_btn.config(state='normal')

        threading.Thread(target=worker, daemon=True).start()


if __name__ == '__main__':
    root = tk.Tk()
    app = App(root)
    root.mainloop()
