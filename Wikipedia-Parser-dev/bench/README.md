#!/usr/bin/env python3
"""
并行处理工具：用于测试和分析并行性能。

read_parse.py  - 基准测试：仅读取 vs 完整流程（空写入器）吞吐量，定位瓶颈
write_latency.py - 基准测试：模拟数据库写入延迟，观察何时写入成为瓶颈
"""

# read_parse.py
# 用途：测试读取和解析性能，不涉及数据库写入
# 用法：python bench/read_parse.py [dump.xml.bz2] [N]
#       N = 最大处理页面数（默认 6000）

# write_latency.py  
# 用途：测试不同写入延迟下的性能影响
# 用法：python bench/write_latency.py [dump.xml.bz2] [N] [workers]
#       N = 最大处理页面数
#       workers = 工作进程数（默认 4）
# 原理：每个批次写入前睡眠指定毫秒数（模拟数据库往返延迟）
#       两个写入器（raw + processed）并行运行，瓶颈取决于较慢的一方
