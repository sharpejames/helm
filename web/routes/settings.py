import logging
import yaml
from fastapi import APIRouter, Request
from pathlib import Path
from config import CONFIG_PATH, get_config

logger = logging.getLogger(__name__)
router = APIRouter()

def redact_keys(config: dict) -> dict:
    """Return config with API keys redacted."""
    safe = config.copy()
    if 'llm' in safe and 'api_key' in safe['llm']:
        key = safe['llm']['api_key']
        safe['llm']['api_key'] = key[:8] + '...' if len(key) > 8 else '***'
    if 'vision' in safe and 'api_key' in safe['vision']:
        key = safe['vision']['api_key']
        safe['vision']['api_key'] = key[:8] + '...' if len(key) > 8 else '***'
    if 'server' in safe and 'secret_key' in safe['server']:
        safe['server']['secret_key'] = '***'
    return safe

@router.get("/settings")
async def get_settings(request: Request):
    config = get_config()
    return redact_keys(config)

@router.post("/settings")
async def update_settings(request: Request):
    body = await request.json()
    
    # Load current config
    with open(CONFIG_PATH) as f:
        config = yaml.safe_load(f)
    
    # Update fields (only allow certain fields to be updated)
    allowed = ['llm', 'vision', 'pipeline', 'scheduler', 'executor']
    for key in allowed:
        if key in body:
            if key not in config:
                config[key] = {}
            config[key].update(body[key])
    
    # Save back to file
    with open(CONFIG_PATH, 'w') as f:
        yaml.dump(config, f, default_flow_style=False)
    
    logger.info("Settings updated")
    return {"updated": True, "message": "Settings saved. Restart Helm to apply changes."}

@router.get("/settings/test")
async def test_settings(request: Request):
    """Test model connections."""
    results = {}
    
    # Test LLM
    try:
        llm = request.app.state.llm
        response = llm.complete("You are a test.", [{"role": "user", "content": "Reply with OK"}])
        results['llm'] = {"status": "ok", "response": response[:50]}
    except Exception as e:
        results['llm'] = {"status": "error", "error": str(e)}
    
    # Test Vision
    try:
        vision = request.app.state.vision
        if vision and vision._configured:
            results['vision'] = {"status": "ok", "model": vision.model_name}
        else:
            results['vision'] = {"status": "not_configured"}
    except Exception as e:
        results['vision'] = {"status": "error", "error": str(e)}
    
    return results
