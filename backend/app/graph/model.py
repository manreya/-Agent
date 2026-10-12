"""LangChain ChatModel 构建

复用 .env 的环境变量约定(LLM_API_KEY / LLM_BASE_URL / LLM_MODEL_ID),
兼容 OPENAI_* 回退。所有 OpenAI 兼容端点(DeepSeek/Qwen/Kimi/Zhipu...)通用。
"""

import os
import time
from typing import Any, Optional

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI
from pydantic import ValidationError

from .errors import (CallResult, ConfigError, ModelOutputError, TransientError,
                     call_external)

# ============ LLM 单次请求的上限(两道, 缺一不可) ============

# 单次请求超时(秒)。**必须显式设**, 否则吃编程接口默认的读超时 600 秒 ——
# 一个卡住的请求能把这一路拖住十分钟, 而四路汇合要等最慢的一路, 等于把整个
# 方案生成拖住。60 秒的取值依据: 项目自己给结构化输出的总预算是 45 秒
# (见 invoke_structured), 单次上限要高于它, 免得把"慢但成功"的正常调用误杀;
# 真实耗时请看日志里各路打印的"耗时 X.XXs", 都远在 30 秒内就可以往下调。
LLM_TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT_SECONDS") or 60.0)

# 编程接口自带的重试次数, **显式关掉**。项目自己有一整套重试纪律
# (退避 + 次数 + 总时长预算, 见 call_external); 让编程接口再偷偷重试两次
# 会变成"项目重试 × 接口重试": 既多花钱, 更要命的是**这两次重试在项目的
# 预算之外** —— 算出来的"45 秒总预算"当场失真, 日志里的请求数也对不上。
# 重试权只归一个地方管。
LLM_SDK_MAX_RETRIES = 0


def get_chat_model(temperature: float = 0.3, disable_thinking: bool = False,
                   model: str = None) -> ChatOpenAI:
    """构建 ChatOpenAI 实例

    disable_thinking: 对 DeepSeek 等思考模型关闭思考模式
    (思考模式不支持强制 tool_choice, 会导致结构化输出 400)
    model: 可选模型覆盖(如评测 judge 用便宜模型); None 时用环境变量默认模型

    超时与重试**在这里统一设定**(见上面两个常量的理由): 所有调用方都拿不到
    一个"没有上限、还会自己偷偷重试"的客户端。
    """
    api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL")
    model = model or os.getenv("LLM_MODEL_ID") or os.getenv("OPENAI_MODEL") or "gpt-4o-mini"

    if not api_key:
        # 走 ConfigError 而不是裸 ValueError: 这是**必需配置缺失**, 在项目的
        # 错误分类里是"不重试、不吞、必须冒出来"的那一类(PermanentError)。
        # 裸 ValueError 会被当成"认不出的程序缺陷", 日志里看不出是配置问题。
        raise ConfigError("LLM_API_KEY或OPENAI_API_KEY未配置,无法调用LLM")

    kwargs = {
        "model": model, "api_key": api_key, "temperature": temperature,
        "request_timeout": LLM_TIMEOUT_SECONDS,
        "max_retries": LLM_SDK_MAX_RETRIES,
    }
    if base_url:
        kwargs["base_url"] = base_url
    if disable_thinking:
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}

    return ChatOpenAI(**kwargs)


def invoke_llm(llm: Any, messages: list, budget: float) -> CallResult:
    """一次模型往返, 套项目的重试纪律(次数 + 退避 + 总时长预算)

    **这是"调用 LLM"的唯一入口**(结构化输出与餐饮那一路的工具循环都走这里)。
    存在的理由: 餐饮那一路自己有工具循环, 原来直接 `llm.invoke(...)` 裸调,
    于是**项目的重试/退避/预算整套纪律它一样都没享受到** —— 四路里轮次最多的
    那一路, 恰好是唯一没有纪律的那一路。

    单次请求自身的时长上限由客户端负责(见 LLM_TIMEOUT_SECONDS); 这里管的是
    "失败之后还要不要再来一次"。

    返回 CallResult 而不是抛异常: 调用方对"失败"的处置并不一样(结构化输出要
    记数并放弃, 餐饮那一路要转去程序化兜底), 由调用方决定更清楚。
    """
    return call_external(
        lambda: llm.invoke(list(messages)),
        kind="llm.invoke", retries=1, backoff=1.0,
        retry_budget=max(budget, 0.0),
    )


def get_structured_llm(schema, temperature: float = 0.3):
    """构建结构化输出模型(bind_tools + tool_choice='required', DeepSeek 已调通)

    项目唯一结构化输出封装。禁止改回 with_structured_output
    (DeepSeek 下会翻车); 思考模型必须 disable_thinking,
    否则 tool_choice 强制约束会 400。

    用法:
        llm = get_structured_llm(MySchema)
        response = llm.invoke(prompt)
        result = MySchema(**response.tool_calls[0]["args"])
    """
    return get_chat_model(temperature, disable_thinking=True).bind_tools(
        [schema], tool_choice="required"
    )


def schema_fix_instruction(err: Exception) -> str:
    """把 pydantic 校验失败翻译成"哪个字段、收到什么、错在哪"的修正指令

    只说"参数不合法"等于没说 —— 模型不知道改哪里, 下一轮多半再错一次。

    **公开**(不再是 `_` 私有): 餐饮那一路自己有一个带工具调用的多轮循环, 它同样
    会遇到"模型填的参数过不了校验", 需要拼同一份修正指令。这份翻译逻辑只该有一份
    —— 否则两处措辞会各自演化, 模型在其中一条路径上得到的信息越来越少。
    """
    details = []
    errors = getattr(err, "errors", None)
    if callable(errors):
        try:
            for d in err.errors()[:6]:
                loc = ".".join(str(x) for x in d.get("loc", ())) or "(整体)"
                got = repr(d.get("input"))[:80]
                details.append(f"  - 字段 {loc}: {d.get('msg')}；收到的是 {got}")
        except Exception:  # noqa: BLE001 - 取错误明细失败不该掩盖原始校验错误
            details = []
    if not details:
        details = [f"  - {str(err)[:200]}"]
    return ("上一次工具调用的参数没有通过校验:\n" + "\n".join(details)
            + "\n请针对上述字段给出可用的取值后重新调用工具, 其余字段保持不变。")


def invoke_structured(schema, prompt: str, max_retries: int = 2, temperature: float = 0.3,
                      total_budget: float = 45.0):
    """结构化输出统一调用入口: 两类失败分开处置, 失败抛出最后一次异常

    旧实现把**两类完全不同的失败**丢进同一个计数器裸重试, 重试时原样重发 prompt:

      - 网络/限流抖动      -> 退避重试、原样重发是对的
      - 模型给的参数不合 schema -> 原样重发**基本没用**(模型多半再错一遍),
        正确做法是把"哪个字段、错在哪"告诉它再让它填一次

    两类混在一起计数, 日志里也看不出到底是"服务在抖"还是"模型不会填"。

    现在:
      - 网络这一层交给 `invoke_llm`(内部是 `call_external`): 次数上限 + 指数退避
        + 时长预算 —— 与餐饮那一路共用同一套纪律, 只有一份定义;
      - schema 这一层每轮把**字段级修正指令**追加进对话重填, 并单独计数/记日志;
      - 两层共享同一个 `total_budget`, 超了就停 —— 否则一轮轮重试叠加起来
        可能让一次请求等上好几分钟。

    单次请求**自身**的时长上限(60s, 见 LLM_TIMEOUT_SECONDS)不在这里 —— 那是
    客户端的事。这里管的是"失败之后还要不要再来一次"。

    用法(推荐, 替代手动 invoke+解析):
        result = invoke_structured(MySchema, prompt)
    """
    llm = get_structured_llm(schema, temperature)
    messages: list = [HumanMessage(content=prompt)]   # 累加式对话: 修正指令追加在后面
    started = time.monotonic()
    net_calls = 0          # 网络层实际发出去的请求数(含重试)
    schema_rounds = 0      # schema 不合格的轮数
    last_err: Optional[Exception] = None

    for attempt in range(1, max_retries + 2):
        remaining = total_budget - (time.monotonic() - started)
        if attempt > 1 and remaining <= 0:
            print(f"[llm] 总预算({total_budget:.0f}s)已用完, 不再重填 "
                  f"(已发出 {net_calls} 次请求, schema 不合格 {schema_rounds} 轮)")
            break

        # ---- 网络层: 退避 + 次数 + 预算, 全部由 invoke_llm 统一负责 ----
        call = invoke_llm(llm, messages, remaining)
        net_calls += call.attempts
        if not call.usable:
            # 兜底也要是**可分类**的异常: 裸 RuntimeError 会被上层当成"认不出的
            # 程序缺陷", 于是一个纯粹的"上游挂了"看起来像代码 bug, 排查方向全错。
            last_err = call.error or TransientError(
                "LLM 调用失败(未分类)", code="llm_error")
            print(f"[llm] 网络层已放弃(共 {net_calls} 次请求): {str(last_err)[:120]}")
            break

        response = call.value
        calls = response.tool_calls or []

        # ---- schema 层: 缺 tool_calls 或参数过不了校验 -> 带修正指令回填 ----
        if not calls:
            schema_rounds += 1
            last_err = ModelOutputError(f"LLM 未返回 tool_calls(第{attempt}次)",
                                        code="llm_no_tool_calls")
            print(f"[llm.schema] 第 {attempt}/{max_retries + 1} 轮: 未返回 tool_calls")
            messages.append(HumanMessage(
                content="上一次回复没有调用工具。请直接调用工具并填写全部必填参数。"))
            continue

        try:
            return schema(**calls[0]["args"])
        except ValidationError as e:
            schema_rounds += 1
            last_err = ModelOutputError(f"LLM 返回参数不合 schema: {str(e)[:160]}",
                                        code="llm_schema_invalid")
            print(f"[llm.schema] 第 {attempt}/{max_retries + 1} 轮: 参数校验失败, "
                  f"已回填修正指令 —— {str(e)[:160]}")
            messages.append(AIMessage(content=response.content, tool_calls=calls))
            # ToolMessage 承载修正指令: AIMessage 带 tool_calls 之后按协议必须紧跟
            # 对应的 tool 结果, 否则部分 OpenAI 兼容端点会直接 400
            messages.append(ToolMessage(content=schema_fix_instruction(e),
                                        tool_call_id=calls[0].get("id")))

    print(f"[llm] 结构化输出失败: 共 {net_calls} 次网络请求, "
          f"schema 不合格 {schema_rounds} 轮")
    raise last_err if last_err is not None else ModelOutputError("结构化输出失败")
