from typing import List, Optional, Union, Dict, Any
from typing_extensions import Literal
from enum import Enum

from pydantic import BaseModel, Field

from ktransformers.server.schemas.base import Object

from openai.types.completion_usage import CompletionUsage
from openai.types.chat.chat_completion_chunk import Choice

class Role(Enum):
    system = 'system'
    user = 'user'
    assistant = 'assistant'
    tool = 'tool'
    function = 'function'

class Message(BaseModel):
    content: Optional[str] = None
    role: Role
    name: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    
    def to_tokenizer_message(self):
        message = {'role': self.role.value}
        if self.content is not None:
            message['content'] = self.content
        if self.name is not None:
            message['name'] = self.name
        if self.tool_calls is not None:
            message['tool_calls'] = self.tool_calls
        if self.tool_call_id is not None:
            message['tool_call_id'] = self.tool_call_id
        return message

class FunctionParameters(BaseModel):
    type: str = "object"
    properties: Dict[str, Any] = {}
    required: Optional[List[str]] = None

class FunctionDefinition(BaseModel):
    name: str
    description: Optional[str] = None
    parameters: FunctionParameters = Field(default_factory=FunctionParameters)

class ToolFunction(BaseModel):
    function: FunctionDefinition
    
class Tool(BaseModel):
    type: Literal["function"]
    function: FunctionDefinition

class ChatCompletionCreate(BaseModel):
    messages: List[Message]
    model: str
    stream: bool = False
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    tools: Optional[List[Tool]] = None
    tool_choice: Optional[Union[str, Dict[str, Any]]] = None
    stream_options: Optional[Dict[str, Any]] = None
    frequency_penalty: float = 0
    presence_penalty: float = 0
    
    def get_tokenizer_messages(self):
        return [m.to_tokenizer_message() for m in self.messages]

class ChatCompletionChunk(BaseModel):
    id: str
    choices: List[Choice]
    created: int
    model: str
    object: Literal["chat.completion.chunk"]
    service_tier: Optional[Literal["scale", "default"]] = None
    system_fingerprint: Optional[str] = None
    usage: Optional[CompletionUsage] = None

    def to_stream_reply(self):
        return f"data: {self.model_dump_json()}\n\n"

class RawUsage(BaseModel):
    tokenize_time: float
    prefill_time: float
    decode_time: float
    prefill_count: int
    decode_count: int