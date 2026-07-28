from abc import ABC, abstractmethod

from app.models import ChatCompletionRequest, ChatCompletionResponse


class Provider(ABC):
    name: str

    @abstractmethod
    async def complete(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        raise NotImplementedError

    async def health(self) -> bool:
        return True
