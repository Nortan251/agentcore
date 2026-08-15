"""Model layer: vendor-neutral LLM access.

Satisfies the existing ``agentcore.core.interfaces.ModelProvider`` Protocol.
Core components depend on that Protocol and on the neutral message types here,
never on a concrete vendor.
"""

from agentcore.models.base import BaseModelProvider
from agentcore.models.errors import (
    ModelAuthError,
    ModelConfigError,
    ModelConnectionError,
    ModelError,
    ModelProviderError,
    ModelRateLimitError,
    ModelResponseError,
    ModelTimeoutError,
)
from agentcore.models.factory import (
    available_providers,
    create_provider,
    default_base_url,
    provider_from_config,
    register_provider,
)
from agentcore.models.messages import (
    FinishReason,
    Message,
    ModelRequest,
    ModelResponse,
    Role,
    ToolCall,
    Usage,
)
from agentcore.models.providers.echo import EchoProvider
from agentcore.models.providers.openai_compatible import OpenAICompatibleProvider
from agentcore.models.transport import (
    HttpClient,
    HttpResponse,
    RetryPolicy,
    Transport,
    UrllibTransport,
)

__all__ = [
    # contract
    "BaseModelProvider",
    # messages
    "Message",
    "Role",
    "ModelRequest",
    "ModelResponse",
    "FinishReason",
    "Usage",
    "ToolCall",
    # providers
    "EchoProvider",
    "OpenAICompatibleProvider",
    "create_provider",
    "provider_from_config",
    "register_provider",
    "available_providers",
    "default_base_url",
    # transport
    "Transport",
    "UrllibTransport",
    "HttpClient",
    "HttpResponse",
    "RetryPolicy",
    # errors
    "ModelError",
    "ModelConfigError",
    "ModelAuthError",
    "ModelRateLimitError",
    "ModelTimeoutError",
    "ModelConnectionError",
    "ModelResponseError",
    "ModelProviderError",
]
