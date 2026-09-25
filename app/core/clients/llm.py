import warnings
from functools import lru_cache

from langchain_openai import AzureChatOpenAI

from app.config import get_settings

# LangChain's structured-output path returns a response whose internal `parsed`
# field is typed Optional; when it carries our QueryAnalysis / StructuredQuery
# model, pydantic emits a benign "Pydantic serializer warnings" UserWarning on
# serialization. It's harmless noise, so silence just that warning.
warnings.filterwarnings(
    "ignore",
    message="Pydantic serializer warnings",
    category=UserWarning,
)


@lru_cache
def get_llm(temperature: float | None = None, streaming: bool = False) -> AzureChatOpenAI:
    """The chat client for one (temperature, streaming) pair.

    `temperature` is what the caller wants; it reaches the deployment only when
    `llm_temperature_supported` says the deployment takes one. Every call in the
    app comes through here, so this is the one place a model that rejects the
    parameter has to be accommodated.
    """
    settings = get_settings()
    return _build_llm(
        endpoint=settings.azure_openai_endpoint,
        api_key=settings.azure_openai_api_key,
        api_version=settings.azure_openai_api_version,
        deployment=settings.azure_openai_model,
        temperature=temperature if settings.llm_temperature_supported else None,
        streaming=streaming,
    )


def get_structured_llm(streaming: bool = False) -> AzureChatOpenAI:
    return get_llm(
        temperature=get_settings().llm_structured_temperature, streaming=streaming
    )


def _build_llm(
    *,
    endpoint: str,
    api_key: str,
    api_version: str,
    deployment: str,
    temperature: float | None,
    streaming: bool,
) -> AzureChatOpenAI:
    kwargs = {
        "azure_endpoint": endpoint,
        "api_key": api_key,
        "api_version": api_version,
        "azure_deployment": deployment,
        "streaming": streaming,
        # gpt-5.x deployments are only served over Azure's newer Responses API,
        # not the legacy chat-completions path AzureChatOpenAI defaults to.
        "use_responses_api": True,
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    return AzureChatOpenAI(**kwargs)
