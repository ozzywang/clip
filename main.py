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
        raise ValueError('视频帧数不足 2 帧，无法分割')

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


def process_video(input_path, output_path, target_size, mode,
                  status_callback=None, progress_callback=None):
    """ 主处理函数 """
    ffmpeg, ffprobe = find_ffmpeg()

    status_callback('正在读取视频信息...')
    width, height, fps, total_frames = probe_video(ffprobe, input_path)

    left_frames = (total_frames + 1) // 2   # 余数帧归左边
    right_frames = total_frames - left_frames
    status_callback(f'总帧数：{total_frames}，左段：{left_frames} 帧，右段：{right_frames} 帧')

    out_w, out_h = calculate_output_size(width, height, target_size, mode)
    status_callback(f'输出尺寸：{out_w} x {out_h}')

    filter_complex = (
        f"[0:v]trim=start_frame=0:end_frame={left_frames},"
        f"setpts=PTS-STARTPTS[left];"
        f"[0:v]trim=start_frame={left_frames}:end_frame={total_frames},"
        f"setpts=PTS-STARTPTS[right];"
        f"[left][right]hstack=inputs=2[stacked];"
        f"[stacked]scale={out_w}:{out_h}[out]"
    )

    cmd = [
        ffmpeg, '-y',
        '-i', input_path,
        '-filter_complex', filter_complex,
        '-map', '[out]',
        '-c:v', 'prores_ks',
        '-profile:v', '1',      # 1 = ProRes 422 LT
        '-pix_fmt', 'yuv422p10le',
        '-an',                  # 丢弃音频
        output_path
    ]

    status_callback('正在处理视频...')
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding='utf-8', errors='replace')
    _, stderr = process.communicate()

    if process.returncode != 0:
        raise RuntimeError(f'ffmpeg 处理失败：\n{stderr[-1000:]}')

    status_callback('处理完成！')
    progress_callback(100)


# ---------- GUI ----------
class App:
    def __init__(self, root):
        self.root = root
        root.title('视频左右拼接工具 - ProRes 422LT')
        root.geometry('600x400')
        root.resizable(False, False)

        # 输入文件
        tk.Label(root, text='输入视频文件：').grid(row=0, column=0, sticky='w', padx=10, pady=10)
        self.input_var = tk.StringVar()
        tk.Entry(root, textvariable=self.input_var, width=45).grid(row=0, column=1, padx=5)
        tk.Button(root, text='浏览...', command=self.browse_input).grid(row=0, column=2, padx=5)

        # 输出文件
        tk.Label(root, text='输出文件：').grid(row=1, column=0, sticky='w', padx=10, pady=10)
        self.output_var = tk.StringVar()
        tk.Entry(root, textvariable=self.output_var, width=45).grid(row=1, column=1, padx=5)
        tk.Button(root, text='浏览...', command=self.browse_output).grid(row=1, column=2, padx=5)

        # 目标长度
        tk.Label(root, text='目标长度（像素）：').grid(row=2, column=0, sticky='w', padx=10, pady=10)
        self.target_var = tk.StringVar(value='1800')
        tk.Entry(root, textvariable=self.target_var, width=15).grid(row=2, column=1, sticky='w', padx=5)

        # 模式选择
        tk.Label(root, text='设置方式：').grid(row=3, column=0, sticky='w', padx=10, pady=10)
        self.mode_var = tk.StringVar(value='long')
        tk.Radiobutton(root, text='长边', variable=self.mode_var, value='long').grid(row=3, column=1, sticky='w')
        tk.Radiobutton(root, text='短边', variable=self.mode_var, value='short').grid(row=3, column=1, sticky='e', padx=80)

        # 状态显示
        self.status_var = tk.StringVar(value='就绪')
        tk.Label(root, textvariable=self.status_var, fg='blue').grid(row=4, column=0, columnspan=3, pady=10)

        # 进度条
        self.progress = ttk.Progressbar(root, length=500, mode='determinate')
        self.progress.grid(row=5, column=0, columnspan=3, pady=10)

        # 开始按钮
        self.start_btn = tk.Button(root, text='开始处理', command=self.start_process,
                                   bg='#4CAF50', fg='white', height=2)
        self.start_btn.grid(row=6, column=0, columnspan=3, pady=20)

    def browse_input(self):
        path = filedialog.askopenfilename(
            title='选择视频文件',
            filetypes=[('视频文件', '*.mp4 *.mov *.avi *.mkv *.m4v *.webm'), ('所有文件', '*.*')]
        )
        if path:
            self.input_var.set(path)
            base, ext = os.path.splitext(path)
            self.output_var.set(f'{base}_joined.mov')

    def browse_output(self):
        path = filedialog.asksaveasfilename(
            title='保存输出文件',
            defaultextension='.mov',
            filetypes=[('QuickTime 视频', '*.mov'), ('所有文件', '*.*')]
        )
        if path:
            self.output_var.set(path)

    def start_process(self):
        input_path = self.input_var.get().strip()
        output_path = self.output_var.get().strip()
        target_text = self.target_var.get().strip()
        mode = self.mode_var.get()

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

        self.start_btn.config(state='disabled')
        self.progress['value'] = 0
        self.status_var.set('开始处理...')

        def worker():
            try:
                process_video(
                    input_path, output_path, target_size, mode,
                    status_callback=lambda msg: self.status_var.set(msg),
                    progress_callback=lambda v: self.progress.config(value=v)
                )
                messagebox.showinfo('完成', '视频处理完成！')
            except Exception as e:
                messagebox.showerror('错误', str(e))
            finally:
                self.start_btn.config(state='normal')

        threading.Thread(target=worker, daemon=True).start()


if __name__ == '__main__':
    root = tk.Tk()
    app = App(root)
    root.mainloop()