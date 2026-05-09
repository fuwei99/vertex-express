from pydantic import BaseModel, ConfigDict # Field removed
from typing import List, Dict, Any, Optional, Union, Literal

# Define data models
class ImageUrl(BaseModel):
    url: str

class ContentPartImage(BaseModel):
    type: Literal["image_url"]
    image_url: ImageUrl

class ContentPartText(BaseModel):
    type: Literal["text"]
    text: str

class OpenAIMessage(BaseModel):
    role: str
    content: Union[str, List[Union[ContentPartText, ContentPartImage, Dict[str, Any]]], None] = None # Allow content to be None for tool calls
    name: Optional[str] = None  # For tool role, the name of the tool
    tool_calls: Optional[List[Dict[str, Any]]] = None  # For assistant messages requesting tool calls
    tool_call_id: Optional[str] = None  # For tool role, the ID of the tool call

class OpenAIRequest(BaseModel):
    model: str
    messages: List[OpenAIMessage]
    temperature: Optional[float] = 1.0
    max_tokens: Optional[int] = None
    top_p: Optional[float] = 1.0
    top_k: Optional[int] = None
    stream: Optional[bool] = False
    stop: Optional[List[str]] = None
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None
    seed: Optional[int] = None
    logprobs: Optional[int] = None
    response_logprobs: Optional[bool] = None
    n: Optional[int] = None  # Maps to candidate_count in Vertex AI
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Union[str, Dict[str, Any]]] = None

    # Allow extra fields to pass through without causing validation errors
    model_config = ConfigDict(extra='allow')