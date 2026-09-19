"""自检套件。

运行方式：
    python main.py --selftest
    python -m tests.selftest

本包刻意保持 __init__ 为空壳：自检模块会被入口直接导入，
在这里做急切导入会导致 `python -m tests.selftest` 时模块被重复加载。
"""
