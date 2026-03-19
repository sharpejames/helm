import yaml
import secrets
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config.yaml"

_config: dict = None

def load_config() -> dict:
    global _config
    with open(CONFIG_PATH) as f:
        _config = yaml.safe_load(f)
    if not _config['server'].get('secret_key'):
        _config['server']['secret_key'] = secrets.token_hex(32)
    return _config

def get_config() -> dict:
    if _config is None:
        return load_config()
    return _config
