import os
from typing import Dict, List, Optional, Tuple


ModelOption = Tuple[str, str]


PROVIDER_LABELS: Dict[str, str] = {
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "google": "Google",
    "deepseek": "DeepSeek",
    "openrouter": "Openrouter",
    "ollama": "Ollama",
}

PROVIDER_BASE_URLS: Dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/",
    "google": "https://generativelanguage.googleapis.com/v1",
    "deepseek": "https://api.deepseek.com",
    "openrouter": "https://openrouter.ai/api/v1",
    "ollama": "http://localhost:11434/v1",
}

MODEL_OPTIONS: Dict[str, Dict[str, List[ModelOption]]] = {
    "openai": {
        "shallow": [
            ("GPT-4o-mini - Fast and efficient for quick tasks", "gpt-4o-mini"),
            ("GPT-4.1-nano - Ultra-lightweight model for basic operations", "gpt-4.1-nano"),
            ("GPT-4.1-mini - Compact model with good performance", "gpt-4.1-mini"),
            ("GPT-4o - Standard model with solid capabilities", "gpt-4o"),
        ],
        "deep": [
            ("GPT-4.1-nano - Ultra-lightweight model for basic operations", "gpt-4.1-nano"),
            ("GPT-4.1-mini - Compact model with good performance", "gpt-4.1-mini"),
            ("GPT-4o - Standard model with solid capabilities", "gpt-4o"),
            ("o4-mini - Specialized reasoning model (compact)", "o4-mini"),
            ("o3-mini - Advanced reasoning model (lightweight)", "o3-mini"),
            ("o3 - Full advanced reasoning model", "o3"),
            ("o1 - Premier reasoning and problem-solving model", "o1"),
        ],
    },
    "anthropic": {
        "shallow": [
            ("Claude Haiku 3.5 - Fast inference and standard capabilities", "claude-3-5-haiku-latest"),
            ("Claude Sonnet 3.5 - Highly capable standard model", "claude-3-5-sonnet-latest"),
            ("Claude Sonnet 3.7 - Exceptional hybrid reasoning and agentic capabilities", "claude-3-7-sonnet-latest"),
            ("Claude Sonnet 4 - High performance and excellent reasoning", "claude-sonnet-4-0"),
        ],
        "deep": [
            ("Claude Haiku 3.5 - Fast inference and standard capabilities", "claude-3-5-haiku-latest"),
            ("Claude Sonnet 3.5 - Highly capable standard model", "claude-3-5-sonnet-latest"),
            ("Claude Sonnet 3.7 - Exceptional hybrid reasoning and agentic capabilities", "claude-3-7-sonnet-latest"),
            ("Claude Sonnet 4 - High performance and excellent reasoning", "claude-sonnet-4-0"),
            ("Claude Opus 4 - Most powerful Anthropic model", "claude-opus-4-0"),
        ],
    },
    "google": {
        "shallow": [
            ("Gemini 2.0 Flash-Lite - Cost efficiency and low latency", "gemini-2.0-flash-lite"),
            ("Gemini 2.0 Flash - Next generation features, speed, and thinking", "gemini-2.0-flash"),
            ("Gemini 2.5 Flash - Adaptive thinking, cost efficiency", "gemini-2.5-flash-preview-05-20"),
        ],
        "deep": [
            ("Gemini 2.0 Flash-Lite - Cost efficiency and low latency", "gemini-2.0-flash-lite"),
            ("Gemini 2.0 Flash - Next generation features, speed, and thinking", "gemini-2.0-flash"),
            ("Gemini 2.5 Flash - Adaptive thinking, cost efficiency", "gemini-2.5-flash-preview-05-20"),
            ("Gemini 2.5 Pro", "gemini-2.5-pro-preview-06-05"),
        ],
    },
    "deepseek": {
        "shallow": [
            ("DeepSeek V4 Flash - Fast model for quick tasks", "deepseek-v4-flash"),
            ("DeepSeek V4 Pro - DeepSeek flagship model", "deepseek-v4-pro"),
        ],
        "deep": [
            ("DeepSeek V4 Flash - Fast model for quick tasks", "deepseek-v4-flash"),
            ("DeepSeek V4 Pro - DeepSeek flagship model", "deepseek-v4-pro"),
        ],
    },
    "openrouter": {
        "shallow": [
            ("Meta: Llama 4 Scout", "meta-llama/llama-4-scout:free"),
            ("Meta: Llama 3.3 8B Instruct - A lightweight and ultra-fast variant of Llama 3.3 70B", "meta-llama/llama-3.3-8b-instruct:free"),
            ("google/gemini-2.0-flash-exp:free - Gemini Flash 2.0 offers a significantly faster time to first token", "google/gemini-2.0-flash-exp:free"),
        ],
        "deep": [
            ("DeepSeek V3 - a 685B-parameter, mixture-of-experts model", "deepseek/deepseek-chat-v3-0324:free"),
            ("Deepseek - latest iteration of the flagship chat model family from the DeepSeek team.", "deepseek/deepseek-chat-v3-0324:free"),
        ],
    },
    "ollama": {
        "shallow": [
            ("llama3.1 local", "llama3.1"),
            ("llama3.2 local", "llama3.2"),
        ],
        "deep": [
            ("llama3.1 local", "llama3.1"),
            ("qwen3", "qwen3"),
        ],
    },
}

OPENAI_COMPATIBLE_PROVIDERS = {"openai", "deepseek", "openrouter", "ollama"}
MEMORY_EMBEDDING_PROVIDERS = {"openai", "ollama"}

API_KEY_ENV_VARS: Dict[str, Optional[str]] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "ollama": None,
}


def normalize_provider(provider: str) -> str:
    return (provider or "").strip().lower()


def is_openai_compatible_provider(provider: str) -> bool:
    return normalize_provider(provider) in OPENAI_COMPATIBLE_PROVIDERS


def supports_memory_embeddings(provider: str) -> bool:
    return normalize_provider(provider) in MEMORY_EMBEDDING_PROVIDERS


def get_provider_api_key(provider: str) -> str:
    env_var = API_KEY_ENV_VARS.get(normalize_provider(provider))
    return os.getenv(env_var, "") if env_var else ""


def build_graph_config(default_config: Dict, request_config: Dict, session_id: Optional[str] = None) -> Dict:
    provider = normalize_provider(request_config.get("llm_provider", default_config.get("llm_provider", "")))
    graph_config = default_config.copy()

    graph_config.update(
        {
            "llm_provider": provider,
            "backend_url": request_config.get("backend_url") or PROVIDER_BASE_URLS.get(provider, ""),
            "api_key": request_config.get("api_key") or get_provider_api_key(provider),
            "quick_think_llm": request_config.get("shallow_thinker")
            or request_config.get("quick_think_llm")
            or graph_config.get("quick_think_llm"),
            "deep_think_llm": request_config.get("deep_thinker")
            or request_config.get("deep_think_llm")
            or graph_config.get("deep_think_llm"),
        }
    )

    if "research_depth" in request_config:
        research_depth = int(request_config["research_depth"])
        graph_config["max_debate_rounds"] = research_depth
        graph_config["max_risk_discuss_rounds"] = research_depth

    if session_id is not None:
        graph_config["session_id"] = session_id

    return graph_config
