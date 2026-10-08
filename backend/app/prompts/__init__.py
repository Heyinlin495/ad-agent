"""提示词模板加载（独立 .txt 文件，便于调优）。"""
from __future__ import annotations

import string
from pathlib import Path

PROMPT_DIR = Path(__file__).parent


def load_prompt(name: str) -> str:
    """读取 prompts/<name>.txt 原始内容。"""
    return (PROMPT_DIR / f"{name}.txt").read_text(encoding="utf-8")


def render_prompt(name: str, **kwargs: object) -> str:
    """读取并用 $variable 占位符渲染模板（safe_substitute 忽略缺失变量）。"""
    template = string.Template(load_prompt(name))
    return template.safe_substitute(**kwargs)
