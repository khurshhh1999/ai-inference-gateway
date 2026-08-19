from typing import Any, Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator

Role = Literal["system", "user", "assistant", "tool"]


class FunctionDef(BaseModel):
    name: str = Field(min_length=1)
    description: str | None = None
    parameters: dict[str, Any] | None = None


class ToolSpec(BaseModel):
    type: Literal["function"] = "function"
    function: FunctionDef


class FunctionNameOnly(BaseModel):
    name: str = Field(min_length=1)


class ToolChoiceNamed(BaseModel):
    type: Literal["function"] = "function"
    function: FunctionNameOnly


class FunctionCallBody(BaseModel):
    name: str
    arguments: str = "{}"


class ToolCall(BaseModel):
    id: str
    type: Literal["function"] = "function"
    function: FunctionCallBody


class ChatMessage(BaseModel):
    role: Role
    content: str | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] | None = None

    @model_validator(mode="after")
    def _role_payload(self) -> Self:
        if self.role == "tool":
            if not self.tool_call_id:
                raise ValueError("tool messages require tool_call_id")
            if self.content is None:
                raise ValueError("tool messages require content")
            return self
        if self.role in {"system", "user"}:
            if self.content is None:
                raise ValueError(f"{self.role} message requires content")
            return self
        if not self.tool_calls and self.content is None:
            raise ValueError("assistant message requires content or tool_calls")
        return self

    def text(self) -> str:
        """Plain text used for token estimates / cache embeddings."""
        parts: list[str] = []
        if self.content:
            parts.append(self.content)
        if self.tool_calls:
            for call in self.tool_calls:
                parts.append(call.function.name)
                parts.append(call.function.arguments)
        if self.role == "tool" and self.tool_call_id:
            parts.append(self.tool_call_id)
        return " ".join(parts)


class ChatCompletionRequest(BaseModel):
    model: str = Field(min_length=1)
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    max_tokens: int | None = Field(default=None, ge=1, le=8192)
    temperature: float | None = Field(default=None, ge=0, le=2)
    tools: list[ToolSpec] | None = None
    tool_choice: Literal["none", "auto", "required"] | ToolChoiceNamed | None = None

    def prompt_token_estimate(self) -> int:
        total = sum(len(message.text().split()) for message in self.messages)
        return max(1, total)

    def is_tool_request(self) -> bool:
        if self.tools:
            return True
        return any(m.role == "tool" or m.tool_calls for m in self.messages)


class ChatChoiceMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: str | None = None
    tool_calls: list[ToolCall] | None = None


class ChatChoice(BaseModel):
    index: int = 0
    message: ChatChoiceMessage
    finish_reason: str = "stop"


class Usage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[ChatChoice]
    usage: Usage
    provider: str
    cached: bool = False
    route_reason: str | None = None


class EmbeddingRequest(BaseModel):
    model: str = Field(min_length=1)
    input: str | list[str]
    encoding_format: Literal["float"] = "float"
    user: str | None = None
    dimensions: int | None = Field(default=None, ge=32, le=4096)

    @field_validator("input")
    @classmethod
    def _input_non_empty(cls, value: str | list[str]) -> str | list[str]:
        if isinstance(value, str):
            return value
        if not value:
            raise ValueError("input must be a non-empty string or list of strings")
        if any(not isinstance(item, str) for item in value):
            raise ValueError("input list items must be strings")
        return value


class EmbeddingData(BaseModel):
    object: Literal["embedding"] = "embedding"
    embedding: list[float]
    index: int


class EmbeddingUsage(BaseModel):
    prompt_tokens: int
    total_tokens: int


class EmbeddingResponse(BaseModel):
    object: Literal["list"] = "list"
    data: list[EmbeddingData]
    model: str
    usage: EmbeddingUsage
    embedding_provider: str
    dim: int


class ModelCard(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int
    owned_by: str
    purpose: Literal["chat", "embeddings"]


class ModelListResponse(BaseModel):
    object: Literal["list"] = "list"
    data: list[ModelCard]
