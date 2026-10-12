"""智能旅行规划助手 - 后端应用"""

# 导入即加固控制台输出(带 emoji 的日志在 GBK 管道下会抛 UnicodeEncodeError,
# 把一次本来成功的请求变成失败)。放在包入口是为了覆盖所有入口: FastAPI 服务、
# 评测脚本、单测 —— 它们都 import 这个包。
from .console import harden_stdout as _harden_stdout

_harden_stdout()

__version__ = "1.0.0"

