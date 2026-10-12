"""控制台输出加固测试: 一行日志永远不该弄崩一次请求

背景(实测踩到过): Windows 下把输出重定向到文件/管道时, Python 用本地编码(GBK)
写 stdout, 而项目里到处是 emoji 进度日志。一句 print 会抛 UnicodeEncodeError,
并且会沿调用栈往上冒 —— 一次本来成功的业务动作就这样变成了失败(风险重评路径
上的一条"丢弃备选"日志正是如此)。

修法见 app/console.py: 启动时把编码错误策略改成 backslashreplace, 编不了的
字符转义输出, 不抛异常。这里钉住三件事: 加固前确实会抛、加固后不抛、加固本身
遇到不支持的流也不抛。

运行:
    cd backend && py -3.13 -m unittest discover -s tests -v
"""

import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

from app.console import harden_stream, harden_stdout


def _gbk_stream() -> io.TextIOWrapper:
    """一条 GBK 编码、遇到编不了的字符就抛的输出流(等价于 Windows 重定向)"""
    return io.TextIOWrapper(io.BytesIO(), encoding="gbk", errors="strict")


class TestHardenStream(unittest.TestCase):
    def test_emoji_print_raises_before_hardening(self):
        """先证明问题真实存在, 否则"修好了"没有意义"""
        stream = _gbk_stream()
        with self.assertRaises(UnicodeEncodeError):
            stream.write("⚠️ 危险")

    def test_emoji_print_survives_after_hardening(self):
        stream = _gbk_stream()
        self.assertTrue(harden_stream(stream))
        stream.write("⚠️ 危险")      # 不许抛
        stream.flush()
        written = stream.buffer.getvalue().decode("gbk")
        self.assertIn("危险", written, "能编码的部分必须照常输出")
        self.assertIn("u26a0", written, "编不了的部分转义输出, 而不是丢或者抛")

    def test_hardening_is_idempotent(self):
        stream = _gbk_stream()
        self.assertTrue(harden_stream(stream))
        self.assertTrue(harden_stream(stream))
        stream.write("✅")           # 仍然是"不抛"

    def test_stream_without_reconfigure_is_left_alone(self):
        """被替换成不支持 reconfigure 的流(CI/测试捕获器)时安静放弃, 不抛"""
        self.assertFalse(harden_stream(io.StringIO()))
        self.assertFalse(harden_stream(object()))


class TestStartupHooks(unittest.TestCase):
    def test_harden_stdout_never_raises(self):
        harden_stdout()     # 幂等, 任意入口可重复调用

    def test_importing_app_hardens_stdout(self):
        """包的导入即加固: 覆盖服务/评测/单测所有入口(而不是逐个删 emoji)"""
        import app     # noqa: F401  (导入本身就是被测行为)

        self.assertTrue(app.__version__)
        if hasattr(sys.stdout, "reconfigure"):
            self.assertEqual(sys.stdout.errors, "backslashreplace")


if __name__ == "__main__":
    unittest.main()