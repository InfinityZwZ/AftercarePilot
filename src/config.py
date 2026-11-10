"""
E-Commerce Support Agent - Configuration
"""
from pydantic_settings import BaseSettings
from pydantic import Field, SecretStr
from typing import Literal


class Settings(BaseSettings):
    # OpenAI
    openai_api_key: str = ""
    openai_model: str = "qwen3.8-max"
    openai_base_url: str = "https://llm-z79r25z37dkxb5zu.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    temperature: float = 0.0
    evaluation_output_tokens: int = Field(default=4096, ge=1200, le=8192)
    openai_enable_thinking: bool = False

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_base_url: str = ""

    # PostgreSQL URLs are never included in logs or repr output.
    database_url: SecretStr = SecretStr("")
    readonly_database_url: SecretStr = SecretStr("")
    default_policy_id: str = "demo-return"
    read_transport: Literal["direct", "mcp"] = "mcp"
    app_timezone: str = "local"
    operator_id: str = Field(default="demo_operator", min_length=1, max_length=64)
    operator_token: SecretStr = SecretStr("")
    plan_ttl_hours: int = Field(default=24, ge=1, le=72)
    max_tool_rounds: int = Field(default=3, ge=1, le=3)
    max_tool_calls: int = Field(default=6, ge=1, le=6)
    max_llm_calls: int = Field(default=7, ge=1, le=7)
    llm_timeout_seconds: float = Field(default=30, gt=0, le=30)
    read_timeout_seconds: float = Field(default=10, gt=0, le=10)
    mcp_read_timeout_seconds: float = Field(default=10, gt=0, le=10)
    analysis_timeout_seconds: float = Field(default=120, gt=0, le=120)

    # Langfuse Observability (optional — set to enable tracing)
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    model_config = {
        "env_file": (".local/database.env", ".local/operator.env", ".env"),
        "env_file_encoding": "utf-8",
    }


settings = Settings()
