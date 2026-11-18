"""Optional local OCR; original images always remain evidence."""

import json
import shutil
import subprocess
from pathlib import Path

from .discovery import image_clues
from .models import redact


def inspect_image(path, output_dir, executable='tesseract', languages='eng', observation_command=None):
    path, output_dir = Path(path), Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    result = {'text': '', 'observations': '', 'clues': {'urls': [], 'files': []},
              'gaps': [], 'engine': executable}
    if not shutil.which(executable):
        result['gaps'].append('OCR 工具不可用，原图已保留；安装 Tesseract 后可重试图片识别')
    else:
        try:
            proc = subprocess.run([executable, str(path), 'stdout', '-l', languages],
                                  capture_output=True, text=True, timeout=60)
            if proc.returncode:
                result['gaps'].append('OCR 识别失败，请检查图片格式及语言包')
            else:
                result['text'] = proc.stdout[:200000]
                result['clues'] = image_clues(result['text'])
        except (OSError, subprocess.TimeoutExpired):
            result['gaps'].append('OCR 工具调用失败或超时')
    if observation_command:
        try:
            proc = subprocess.run([*observation_command, str(path)], capture_output=True, text=True, timeout=90)
            if proc.returncode:
                result['gaps'].append('图像观察命令执行失败')
            else:
                result['observations'] = proc.stdout[:200000]
        except (OSError, subprocess.TimeoutExpired):
            result['gaps'].append('图像观察命令不可用或超时')
    else:
        result['gaps'].append('未配置图像内容理解，后续 Agent 可直接查看原图')
    (output_dir / 'recognition.json').write_text(json.dumps(redact(result), ensure_ascii=False, indent=2), encoding='utf-8')
    return result
