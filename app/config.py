import os
from config_store import apply_config_json_to_env

apply_config_json_to_env()

# Default password if not set in environment
DEFAULT_PASSWORD = "123456"

# Get password from environment variable or use default
API_KEY = os.environ.get("API_KEY", DEFAULT_PASSWORD)

# HuggingFace Authentication Settings
HUGGINGFACE = os.environ.get("HUGGINGFACE", "false").lower() == "true"
HUGGINGFACE_API_KEY = os.environ.get("HUGGINGFACE_API_KEY", "") # Default to empty string, auth logic will verify if HF_MODE is true and this key is needed

# Directory for service account credential files
CREDENTIALS_DIR = os.environ.get("CREDENTIALS_DIR", "/app/credentials")

# JSON string for service account credentials (can be one or multiple comma-separated)
GOOGLE_CREDENTIALS_JSON_STR = os.environ.get("GOOGLE_CREDENTIALS_JSON")

# API Key for Vertex Express Mode
raw_vertex_keys = os.environ.get("VERTEX_EXPRESS_API_KEY")
if raw_vertex_keys:
    VERTEX_EXPRESS_API_KEY_VAL = [key.strip() for key in raw_vertex_keys.split(',') if key.strip()]
else:
    VERTEX_EXPRESS_API_KEY_VAL = []

# Fake streaming settings for debugging/testing
FAKE_STREAMING_ENABLED = os.environ.get("FAKE_STREAMING", "false").lower() == "true"
FAKE_STREAMING_INTERVAL_SECONDS = float(os.environ.get("FAKE_STREAMING_INTERVAL", "1.0"))

# URL for the remote JSON file containing model lists
MODELS_CONFIG_URL = os.environ.get("MODELS_CONFIG_URL", "https://raw.githubusercontent.com/gzzhongqi/vertex2openai/refs/heads/main/vertexModels.json")

# Constant for the Vertex reasoning tag
VERTEX_REASONING_TAG = "vertex_think_tag"

# Round-robin credential selection strategy
ROUNDROBIN = os.environ.get("ROUNDROBIN", "false").lower() == "true"

# Safety score display setting
SAFETY_SCORE = os.environ.get("SAFETY_SCORE", "false").lower() == "true"
# Validation logic moved to app/auth.py

# Proxy settings
PROXY_URL = os.environ.get("PROXY_URL")
SSL_CERT_FILE = os.environ.get("SSL_CERT_FILE")

# Retry/node switching settings
MAX_RETRIES_429 = int(os.environ.get("MAX_RETRIES_429", "6"))
RETRIES_BEFORE_SWITCH = int(os.environ.get("RETRIES_BEFORE_SWITCH", "1"))
ANTI429_ASSIST = os.environ.get("ANTI429_ASSIST", "false").lower() == "true"
PROXY_ROUTE_ENABLED = os.environ.get("PROXY_ROUTE_ENABLED", "true").lower() == "true"
DROP_MAX_TOKENS = os.environ.get("DROP_MAX_TOKENS", "false").lower() == "true"
VERTEX_LOCATION = os.environ.get("VERTEX_LOCATION", "global")
