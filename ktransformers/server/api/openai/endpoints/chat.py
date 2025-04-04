import json
from time import time
from uuid import uuid4
from typing import Dict, List, Optional, Any, Literal, Union
from pydantic import BaseModel, Field
import re
from fastapi import APIRouter
from fastapi.requests import Request
from ktransformers.server.utils.create_interface import get_interface
from ktransformers.server.schemas.assistants.streaming import chat_stream_response
from ktransformers.server.schemas.endpoints.chat import ChatCompletionCreate
from ktransformers.server.schemas.endpoints.chat import RawUsage, Role
from ktransformers.server.backend.base import BackendInterfaceBase
from ktransformers.server.config.config import Config
from ktransformers.server.config.log import logger

from ktransformers.server.schemas.endpoints.chat import ChatCompletionChunk

# 定义我们自己的数据结构替代 OpenAI 的导入
class CompletionUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    prompt_tokens_details: Optional[Dict[str, Any]] = None
    completion_tokens_details: Optional[Dict[str, Any]] = None

class Choice(BaseModel):
    index: int
    message: Optional[Dict[str, Any]] = None
    finish_reason: Optional[str] = None
    logprobs: Optional[Any] = None
    delta: Optional[Dict[str, Any]] = None
    content_filter_results: Optional[Dict[str, Any]] = None

class ChatCompletion(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[Choice]
    usage: Optional[CompletionUsage] = None
    system_fingerprint: Optional[str] = None
    prompt_filter_results: Optional[List[Dict[str, Any]]] = None

# 仅用于非流式响应构建
class ChatCompletionMessageToolCallFunction(BaseModel):
    name: str
    arguments: str

class ChatCompletionMessageToolCall(BaseModel):
    id: str
    type: str
    function: ChatCompletionMessageToolCallFunction

class ChatCompletionMessage(BaseModel):
    role: str
    content: Optional[str] = None
    tool_calls: Optional[List[ChatCompletionMessageToolCall]] = None

router = APIRouter()

@router.get('/models', tags=['openai'])
async def list_models():
    return {"data": [{"id": Config().model_name, "name": Config().model_name}], "object": "list"}


@router.post('/chat/completions', tags=['openai'])
async def chat_completion(request: Request, create: ChatCompletionCreate):
    id = str(uuid4().hex)
    
    # 1. 使用system提示让模型了解如何使用工具
    enhanced_messages = list(create.messages)
    
    # 如果有工具，且第一条消息是system，在system提示中添加工具使用指导
    if create.tools and len(create.tools) > 0 and enhanced_messages[0].role == Role.system:
        tool_instructions = "你可以使用以下工具：\n\n"
        for tool in create.tools:
            tool_instructions += f"name - {tool.function.name}: {tool.function.description} parameters: {tool.function.parameters}\n"
        
        # 修改工具使用指南，鼓励JSON格式输出
        tool_instructions += "工具仅在用户明确提出，或者你认为需要调用工具的时候调用。当确实调用工具的关键信息时，你可以先向用户索取关键信息再调用工具\n"
        tool_instructions += "\n当你需要使用工具时，请以下列格式输出，格式为：\n"
        tool_instructions += '<｜tool▁calls▁begin｜><｜tool▁call▁begin｜>function<｜tool▁sep｜>name\n```json {"参数名": "参数值","参数名2": "参数值2"...}\n```<｜tool▁call▁end｜><｜tool▁calls▁end｜>\n'
        tool_instructions += "不要尝试解释你在做什么，直接输出工具调用即可。"
        
        enhanced_messages[0].content = enhanced_messages[0].content + "\n\n" + tool_instructions
    
    # 处理请求
    interface: BackendInterfaceBase = get_interface()
    input_message = [json.loads(m.model_dump_json()) for m in enhanced_messages]
    
    if Config().api_key != '':
        assert request.headers.get('Authorization', '').split()[-1] == Config().api_key
    
    if create.stream:
        async def inner():
            chunk = ChatCompletionChunk(
                id=id,
                choices=[],
                object='chat.completion.chunk',
                created=int(time()),
                model=Config().model_name,
                system_fingerprint=f"fp_{uuid4().hex[:12]}",
            )
            
            # 收集模型完整输出，但专门处理工具调用
            full_content = ""
            buffer = ""  # 用于临时存储当前文本块
            tool_call_mode = False  # 标记是否正在处理工具调用
            tool_calls = []  # 存储所有检测到的工具调用
            
            # 自定义模型特殊标记
            tool_calls_begin_marker = "<｜tool▁calls▁begin｜>"
            tool_call_begin_marker = "<｜tool▁call▁begin｜>"
            tool_sep_marker = "<｜tool▁sep｜>"
            tool_call_end_marker = "<｜tool▁call▁end｜>"
            tool_calls_end_marker = "<｜tool▁calls▁end｜>"
            
            async for res in interface.inference(input_message, id, create.temperature, create.top_p):
                if isinstance(res, RawUsage):
                    # 最后返回使用情况
                    raw_usage = res
                    chunk.choices = []
                    chunk.usage = CompletionUsage(
                        prompt_tokens=raw_usage.prefill_count,
                        completion_tokens=raw_usage.decode_count,
                        total_tokens=raw_usage.prefill_count + raw_usage.decode_count
                    )
                    yield chunk
                elif isinstance(res, tuple) and len(res) == 2:
                    token, finish_reason = res
                    
                    # 检测模型特定格式的工具调用开始
                    if not tool_call_mode and tool_calls_begin_marker in buffer + token:
                        tool_call_mode = True
                        
                        # 调整full_content，删除工具调用部分
                        if buffer.endswith(tool_calls_begin_marker):
                            full_content = full_content[:-len(tool_calls_begin_marker)]
                        elif tool_calls_begin_marker in (buffer + token):
                            idx = (buffer + token).find(tool_calls_begin_marker)
                            full_content = full_content[:-(len(buffer) - idx)]
                        buffer = ""
                        
                        # 发送当前累积的文本内容（如果有的话）
                        if full_content:
                            chunk.choices = [{
                                "index": 0,
                                "delta": {"content": full_content},
                                "finish_reason": None
                            }]
                            yield chunk
                            full_content = ""
                    
                    # 在非工具调用模式下累积内容
                    if not tool_call_mode:
                        full_content += token
                        buffer += token
                        # 保持缓冲区在合理大小
                        if len(buffer) > 200:
                            buffer = buffer[-200:]
                    else:
                        # 在工具调用模式下，继续收集工具调用相关文本
                        buffer += token
                        
                        # 如果找到工具调用结束标记
                        if tool_calls_end_marker in buffer:
                            try:
                                # 解析调用文本提取工具调用信息
                                full_tool_call = buffer
                                
                                # 提取函数名称
                                function_name_start = full_tool_call.find(tool_sep_marker) + len(tool_sep_marker)
                                function_name_end = full_tool_call.find("\n", function_name_start)
                                function_name = full_tool_call[function_name_start:function_name_end].strip()
                                
                                # 提取JSON参数 - 提取```json和```之间的内容
                                json_pattern = r'```json\s*(.*?)\s*```'
                                json_match = re.search(json_pattern, full_tool_call, re.DOTALL)
                                
                                if json_match:
                                    arguments_str = json_match.group(1).strip()
                                    # 生成工具调用ID
                                    tool_call_id = f"call_{uuid4().hex[:24]}"
                                    
                                    # 添加到工具调用列表
                                    tool_calls.append({
                                        "id": tool_call_id,
                                        "type": "function",
                                        "function": {
                                            "name": function_name,
                                            "arguments": arguments_str
                                        }
                                    })
                                    
                                    # 重置状态
                                    tool_call_mode = False
                                    buffer = ""
                                    
                                    # 发送工具调用事件
                                    for idx, tool_call in enumerate(tool_calls):
                                        # 首条工具调用消息
                                        chunk.choices = [{
                                            "index": 0,
                                            "delta": {
                                                "role": "assistant",
                                                "content": None,
                                                "tool_calls": [{
                                                    "index": idx,
                                                    "id": tool_call["id"],
                                                    "type": "function",
                                                    "function": {
                                                        "name": tool_call["function"]["name"],
                                                        "arguments": ""
                                                    }
                                                }]
                                            },
                                            "finish_reason": None
                                        }]
                                        yield chunk
                                        
                                        # 发送参数
                                        chunk.choices = [{
                                            "index": 0,
                                            "delta": {
                                                "tool_calls": [{
                                                    "index": idx,
                                                    "function": {"arguments": tool_call["function"]["arguments"]}
                                                }]
                                            },
                                            "finish_reason": None
                                        }]
                                        yield chunk
                                    
                                    # 发送完成消息
                                    chunk.choices = [{
                                        "index": 0,
                                        "delta": {},
                                        "finish_reason": "tool_calls"
                                    }]
                                    yield chunk
                                    
                                    # 返回后，不继续处理
                                    return
                                else:
                                    # JSON提取失败，可能是格式不完整
                                    logger.warning("Failed to extract JSON from tool call")
                                    tool_call_mode = False
                                    buffer = ""
                            except Exception as e:
                                logger.error(f"Error processing tool call: {e}")
                                tool_call_mode = False
                                buffer = ""
                    
                    # 正常文本输出 (仅在非工具调用模式下)
                    if not tool_call_mode and token:
                        if finish_reason is not None:
                            # 最终消息
                            chunk.choices = [{
                                "index": 0,
                                "delta": {},
                                "finish_reason": finish_reason
                            }]
                            yield chunk
                        else:
                            # 正常文本块
                            # 检查token中是否包含任何工具调用开始标记的部分
                            if any(marker in token for marker in [tool_calls_begin_marker, tool_call_begin_marker]):
                                # 跳过，因为这将在下一个迭代中处理
                                pass
                            else:
                                chunk.choices = [{
                                    "index": 0,
                                    "delta": {"content": token},
                                    "finish_reason": None
                                }]
                                yield chunk
            
            # 如果我们已经到了这里而没有返回，说明没有检测到完整的工具调用
            # 发送常规完成消息
            if not tool_call_mode:
                chunk.choices = [{
                    "index": 0, 
                    "delta": {}, 
                    "finish_reason": "stop"
                }]
                yield chunk
        
        return chat_stream_response(request, inner())
    else:
        # 非流式响应处理
        full_content = ""
        finish_reason = None
        tool_calls = []
        buffer = ""
        tool_call_mode = False
        
        # 自定义模型特殊标记
        tool_calls_begin_marker = "<｜tool▁calls▁begin｜>"
        tool_call_begin_marker = "<｜tool▁call▁begin｜>"
        tool_sep_marker = "<｜tool▁sep｜>"
        tool_call_end_marker = "<｜tool▁call▁end｜>"
        tool_calls_end_marker = "<｜tool▁calls▁end｜>"
        
        async for res in interface.inference(input_message, id, create.temperature, create.top_p):
            if isinstance(res, RawUsage):
                raw_usage = res
                usage = CompletionUsage(
                    prompt_tokens=raw_usage.prefill_count,
                    completion_tokens=raw_usage.decode_count,
                    total_tokens=raw_usage.prefill_count + raw_usage.decode_count
                )
            elif isinstance(res, tuple) and len(res) == 2:
                token, finish_reason = res
                
                # 检测模型特定格式的工具调用开始
                if not tool_call_mode and tool_calls_begin_marker in buffer + token:
                    tool_call_mode = True
                    
                    # 调整full_content，删除工具调用部分
                    if buffer.endswith(tool_calls_begin_marker):
                        full_content = full_content[:-len(tool_calls_begin_marker)]
                    elif tool_calls_begin_marker in (buffer + token):
                        idx = (buffer + token).find(tool_calls_begin_marker)
                        full_content = full_content[:-(len(buffer) - idx)]
                    buffer = ""
                
                # 在非工具调用模式下累积内容
                if not tool_call_mode:
                    full_content += token
                    buffer += token
                    # 保持缓冲区在合理大小
                    if len(buffer) > 200:
                        buffer = buffer[-200:]
                else:
                    # 在工具调用模式下，继续收集工具调用相关文本
                    buffer += token
                    
                    # 如果找到工具调用结束标记
                    if tool_calls_end_marker in buffer:
                        try:
                            # 解析调用文本提取工具调用信息
                            full_tool_call = buffer
                            
                            # 提取函数名称
                            function_name_start = full_tool_call.find(tool_sep_marker) + len(tool_sep_marker)
                            function_name_end = full_tool_call.find("\n", function_name_start)
                            function_name = full_tool_call[function_name_start:function_name_end].strip()
                            
                            # 提取JSON参数 - 提取```json和```之间的内容
                            json_pattern = r'```json\s*(.*?)\s*```'
                            json_match = re.search(json_pattern, full_tool_call, re.DOTALL)
                            
                            if json_match:
                                arguments_str = json_match.group(1).strip()
                                # 生成工具调用ID
                                tool_call_id = f"call_{uuid4().hex[:24]}"
                                
                                # 添加到工具调用列表
                                tool_calls.append({
                                    "id": tool_call_id,
                                    "index": 0,
                                    "type": "function",
                                    "function": {
                                        "name": function_name,
                                        "arguments": arguments_str
                                    }
                                })
                                
                                # 如果成功解析了工具调用，设置完成原因
                                finish_reason = "tool_calls"
                                
                                # 重置状态
                                tool_call_mode = False
                                buffer = ""
                            else:
                                # JSON提取失败，可能是格式不完整
                                logger.warning("Failed to extract JSON from tool call")
                                tool_call_mode = False
                                buffer = ""
                        except Exception as e:
                            logger.error(f"Error processing tool call: {e}")
                            tool_call_mode = False
                            buffer = ""
                            
        # 构建响应
        response = {
            "id": id,
            "object": "chat.completion",
            "created": int(time()),
            "model": Config().model_name,
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None if tool_calls else full_content,
                    "tool_calls": tool_calls if tool_calls else None
                },
                "finish_reason": finish_reason or "stop"
            }],
            "usage": usage.__dict__,
            "system_fingerprint": f"fp_{uuid4().hex[:12]}"
        }
        
        return response