from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # API
    app_name: str = "Terraform RAG Backend"
    debug: bool = False
    mcp_seed_api_key: str = ""  # pre-generated trag_* key seeded on startup

    # PostgreSQL
    postgres_user: str = "terraform_rag"
    postgres_password: str = "changeme"
    postgres_db: str = "terraform_rag"
    postgres_host: str = "postgres"
    postgres_port: int = 5432

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    # Redis / Celery
    redis_url: str = "redis://redis:6379/0"

    # Embeddings
    # provider: "local" = sentence-transformers (CPU), "bedrock" = Amazon Bedrock API
    embedding_provider: str = "local"
    embedding_model: str = "Snowflake/snowflake-arctic-embed-m-v2.0"
    embedding_dim: int = 768
    # Bedrock embeddings (used when embedding_provider=bedrock)
    embedding_bedrock_model_id: str = "amazon.titan-embed-text-v2:0"
    embedding_bedrock_region: str = ""  # empty = use aws_bedrock_region

    # LLM (query answering)
    anthropic_api_key: str = ""
    llm_model: str = "claude-sonnet-4-6"
    # Optional proxy / OpenRouter: https://openrouter.ai/api/v1
    # When set, client switches to OpenAI-compatible format
    llm_base_url: str = ""
    llm_concurrent_prompts: int = 1  # >1 enables parallel LLM calls (e.g. 4 for LMStudio)
    llm_thinking_budget: int = 8192  # budget_tokens for Anthropic extended thinking (0 = disabled)
    llm_max_retries: int = 3         # max retries on transient API errors (429, 500, 529)

    # AWS Bedrock — when set, uses AnthropicBedrock instead of direct Anthropic API.
    # Model IDs follow Bedrock format, e.g. "us.anthropic.claude-sonnet-4-6-20250514-v1:0"
    aws_bedrock_region: str = ""          # e.g. "us-east-1" — empty = Bedrock disabled
    aws_bedrock_profile: str = ""         # AWS profile name (optional, for local dev)
    aws_bedrock_role_arn: str = ""        # role to assume (optional, for cross-account)

    # LLM for module descriptions (indexing) — defaults to main LLM if empty
    description_llm_model: str = ""
    description_llm_base_url: str = ""
    description_llm_api_key: str = ""
    description_llm_temperature: float = 0.2

    # Git / Repo cache
    repo_cache_dir: str = "/tmp/repo_cache"

    # Versioning — git tag discovery
    tag_pattern: str = r"^.+$"                      # all non-empty tags
    max_tags_to_index: int = 10000                   # safety cap

    # Retriever — reference code injection
    retriever_fetch_reference_code: bool = True   # fetch raw HCL for best-matching usage snippets
    retriever_max_reference_snippets: int = 2     # max code fragments to inject into LLM context
    retriever_max_reference_lines: int = 150      # truncate fragments longer than this

    # Auth
    auth_mode: str = "disabled"           # "sso" | "local" | "disabled"
    frontend_url: str = "http://localhost:3000"
    jwt_secret: str = "change-me"         # for local mode JWT signing
    jwt_access_ttl_minutes: int = 15
    jwt_refresh_ttl_days: int = 7
    # SSO mode (ALB OIDC)
    sso_admin_groups: str = "terraform-rag-admins"   # comma-separated
    sso_user_groups: str = "terraform-rag-users"
    sso_default_role: str = "user"                   # role when no groups match ("user", "readonly", "admin")
    sso_region: str = "eu-west-1"
    # Local mode - seed admin on first startup
    admin_initial_email: str = ""
    admin_initial_password: str = ""
    # MCP OAuth (Cognito-backed, used when auth_mode=sso)
    cognito_user_pool_id: str = ""          # e.g. "eu-west-1_AbCdEfGhI"
    cognito_domain: str = ""                # e.g. "terraform-rag-prod.auth.eu-west-1.amazoncognito.com"
    cognito_mcp_client_id: str = ""         # Cognito app client ID for MCP OAuth proxy
    cognito_mcp_client_secret: str = ""     # Cognito app client secret (server-side code exchange)
    mcp_oauth_issuer_url: str = ""          # e.g. "https://terraform-rag-prod-int.domain.com"

    # Demo mode - disables expensive LLM tools (query_modules, pick_modules in MCP)
    demo_mode: bool = False

    # Agent-based compose (replaces shopping-list pipeline with Claude tool-use loop)
    agent_compose_enabled: bool = False   # feature toggle
    agent_max_turns: int = 15             # max agent iterations before forced stop
    agent_model: str = ""                 # model for agent loop (empty = use llm_model)
    agent_thinking_budget: int = 4096     # extended thinking budget for agent turns (0 = disabled)

    # Audit logging
    audit_log_enabled: bool = True
    audit_log_llm_prompts: bool = True   # False → redacts prompt/response text (keeps lengths)

    # Webhooks (HMAC secret per provider)
    github_webhook_secret: str = ""
    gitlab_webhook_token: str = ""

    class Config:
        env_file = ".env"
        extra = "ignore"   # ignore unknown variables from .env instead of raising error


@lru_cache
def get_settings() -> Settings:
    return Settings()
