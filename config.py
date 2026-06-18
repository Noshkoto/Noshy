"""
Noshy configuration — loads from ~/.noshy/config.toml with env var overrides.
Env vars take precedence over config file values (standard precedence).
"""
import os
import logging
from pathlib import Path
from typing import Optional, Dict, Any

log = logging.getLogger("noshy.config")

_defaults = {
    "db_path": "~/.noshy/memories.db",
    "embed_provider": "auto",
    "embed_model": "",
    "embed_api_base": "",
    "embed_api_key": "",
    "api_base": "http://127.0.0.1:8642/v1",
    "api_key": "",
    "model": "hermes-agent",
    "http_host": "127.0.0.1",
    "http_port": 8720,
    "http_token": "",
}

_config: Optional[Dict[str, Any]] = None


def load_config() -> Dict[str, Any]:
    """Load configuration from file and environment variables."""
    global _config
    if _config is not None:
        return _config

    cfg = dict(_defaults)

    # Try loading config file
    config_path = Path(os.environ.get("NOSHY_CONFIG", "~/.noshy/config.toml")).expanduser()
    if config_path.exists():
        try:
            import tomllib
            with open(config_path, "rb") as f:
                file_cfg = tomllib.load(f)
            # Flatten nested [noshy] section if present
            section = file_cfg.get("noshy", file_cfg)
            for key, val in section.items():
                cfg_key = key.replace("-", "_")
                cfg[cfg_key] = val
            log.info(f"Loaded config from {config_path}")
        except ImportError:
            log.debug("tomllib not available (Python < 3.11) — env vars only")
        except Exception as e:
            log.warning(f"Failed to load config from {config_path}: {e}")

    # Env var overrides (highest precedence)
    env_map = {
        "NOSHY_DB": "db_path",
        "NOSHY_EMBED_PROVIDER": "embed_provider",
        "NOSHY_EMBED_MODEL": "embed_model",
        "NOSHY_EMBED_API_BASE": "embed_api_base",
        "NOSHY_EMBED_API_KEY": "embed_api_key",
        "NOSHY_API_BASE": "api_base",
        "NOSHY_API_KEY": "api_key",
        "NOSHY_MODEL": "model",
        "NOSHY_HTTP_HOST": "http_host",
        "NOSHY_HTTP_PORT": "http_port",
        "NOSHY_HTTP_TOKEN": "http_token",
    }
    for env_key, cfg_key in env_map.items():
        val = os.environ.get(env_key)
        if val is not None:
            # Cast numeric values
            if cfg_key in ("http_port",):
                try:
                    val = int(val)
                except ValueError:
                    pass
            cfg[cfg_key] = val

    # Also check common env vars that aren't NOSHY-prefixed
    if not cfg.get("embed_api_key") and os.environ.get("OPENAI_API_KEY"):
        cfg["embed_api_key"] = os.environ["OPENAI_API_KEY"]
    if not cfg.get("api_key") and os.environ.get("API_SERVER_KEY"):
        cfg["api_key"] = os.environ["API_SERVER_KEY"]

    _config = cfg
    return cfg


def get(key: str, default=None):
    """Get a single config value."""
    return load_config().get(key, default)


def clear_cache():
    """Drop the cached config so the next call re-reads file + env. Tests use this."""
    global _config
    _config = None


def create_default_config():
    """Write a default config file to ~/.noshy/config.toml."""
    config_path = Path("~/.noshy/config.toml").expanduser()
    config_path.parent.mkdir(parents=True, exist_ok=True)

    template = """# Noshy configuration
# Env vars override these values. See docs for full reference.

[noshy]
db_path = "~/.noshy/memories.db"
embed_provider = "auto"        # auto, openai, fastembed, hermes, none
embed_model = ""               # leave empty for provider default
embed_api_base = ""            # leave empty for provider default
embed_api_key = ""             # leave empty to use OPENAI_API_KEY

# LLM extraction settings
api_base = "http://127.0.0.1:8642/v1"
api_key = ""                   # leave empty to use API_SERVER_KEY
model = "hermes-agent"

# HTTP server
http_host = "127.0.0.1"        # use 0.0.0.0 to allow remote access
http_port = 8720
http_token = ""                # set to require Bearer auth on all endpoints
"""
    config_path.write_text(template)
    log.info(f"Created default config at {config_path}")
    return str(config_path)
