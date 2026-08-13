from typing import Literal

from pydantic import BaseModel, Field, field_validator

Role = Literal["system", "user", "assistant"]


class ChatMessage(BaseModel):
    role: Role
    content: str


class ChatCompletionRequest(BaseModel):
    model: str = Field(min_length=1)
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    max_tokens: int | None = Field(default=None, ge=1, le=8192)
    temperature: float | None = Field(default=None, ge=0, le=2)


class ChatChoiceMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: str


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
