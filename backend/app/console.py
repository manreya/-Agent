"""控制台输出加固: 让一行日志永远不可能弄崩一次请求

【为什么需要】
代码里到处是带 emoji 的进度日志(🚀 ✅ ⚠️ 🧹 …)。在 UTF-8 终端上它们很好看;
但 Windows 下把输出重定向到文件/管道时, Python 用**本地编码**(GBK)写 stdout,
而多数 emoji 不在 GBK 里 —— 一句 print 会抛 UnicodeEncodeError。

这不是"日志样式"问题: print 抛出的异常会沿着调用栈往上冒, 把一个本来已经
成功的业务动作变成失败。本项目里实测到过一次: 低估一条诊断日志的破坏力 ——
重新评估风险时打印"丢弃了一条覆盖不到标记的备选", 在 GBK 管道下直接抛错,
把整条重评路径带崩, 而它想报告的那件事(丢了一条备选)本身是无害的。

【怎么修】
在进程启动时把 stdout/stderr 的编码错误策略改成 backslashreplace: 能编码的照常
输出, 编不了的转义成 \\uXXXX 而不是抛异常。**一处修, 全场生效** —— 比逐个删
emoji 稳, 也管得住以后新加的日志。不改编码(仍是终端本地编码), 免得破坏
终端里正常的中文显示。

不抛异常是这里的硬要求: 它由启动路径调用, 自己绝不能再成为新的失败点。
"""

import sys
from typing import Any


def harden_stream(stream: Any) -> bool:
    """把单个输出流的编码错误策略改成 backslashreplace; 改不了就安静放弃

    返回是否真的改到了 —— 不返回也不是错误, 只是"没加固成"(如被替换成
    不支持 reconfigure 的流, 或被 StringIO 之类占用)。
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return False
    try:
        reconfigure(errors="backslashreplace")
        return True
    except Exception:
        # 流已关闭/被别的库接管/不支持该参数: 加固失败不该影响业务
        return False


def harden_stdout() -> None:
    """加固 stdout/stderr: 幂等, 可在任意入口重复调用"""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is not None:
            harden_stream(stream)