import asyncio
import json
import re
import traceback
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

# Google specific imports
from google.genai import types

# Local module imports
from models import OpenAIRequest
from auth import get_api_key
from message_processing import (
    create_gemini_prompt,
    create_encrypted_gemini_prompt,
    create_encrypted_full_gemini_prompt,
    ENCRYPTION_INSTRUCTIONS,
)
from api_helpers import (
    create_generation_config, # Corrected import name
    create_openai_error_response,
    execute_gemini_call,
)
from openai_handler import OpenAIDirectHandler
from project_id_discovery import discover_project_id
from credentials_manager import _refresh_auth
from gemini_rest_client import GeminiRestClientContext
from gemini_rest_client import generate_content_raw, predict_raw, stream_generate_content_raw
from gemini_native_models import apply_native_model_config, get_gemini_native_model

router = APIRouter()

EXPRESS_PREFIX = "[EXPRESS] "
PAY_PREFIX = "[PAY]"
MARKDOWN_DATA_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(data:(image/[^;)\s]+);base64,([^)]+)\)")


def _split_markdown_images_to_gemini_parts(text: str) -> list[dict]:
    parts = []
    cursor = 0
    for match in MARKDOWN_DATA_IMAGE_RE.finditer(text):
        before = text[cursor:match.start()]
        if before:
            parts.append({"text": before})

        mime_type, data = match.groups()
        parts.append({"inlineData": {"mimeType": mime_type, "data": data}})
        cursor = match.end()

    after = text[cursor:]
    if after:
        parts.append({"text": after})

    return parts


def _convert_markdown_images_in_content(content: dict) -> dict:
    updated_content = dict(content)
    converted_parts = []
    changed = False

    for part in updated_content.get("parts") or []:
        if isinstance(part, dict) and isinstance(part.get("text"), str) and MARKDOWN_DATA_IMAGE_RE.search(part["text"]):
            converted_parts.extend(_split_markdown_images_to_gemini_parts(part["text"]))
            changed = True
        else:
            converted_parts.append(part)

    if changed:
        updated_content["parts"] = converted_parts
    return updated_content


def _convert_request_markdown_images_to_inline_data(payload: dict) -> dict:
    updated_payload = dict(payload)

    if isinstance(updated_payload.get("contents"), list):
        updated_payload["contents"] = [
            _convert_markdown_images_in_content(content) if isinstance(content, dict) else content
            for content in updated_payload["contents"]
        ]

    if isinstance(updated_payload.get("systemInstruction"), dict):
        updated_payload["systemInstruction"] = _convert_markdown_images_in_content(updated_payload["systemInstruction"])

    return updated_payload


def _merge_generation_config(payload: dict, updates: dict) -> dict:
    updated_payload = dict(payload)
    generation_config = dict(updated_payload.get("generationConfig") or {})
    generation_config.update(updates)
    updated_payload["generationConfig"] = generation_config
    return updated_payload


def _extract_prompt_from_gemini_payload(payload: dict) -> str:
    prompt_parts = []
    for content in payload.get("contents") or []:
        if not isinstance(content, dict):
            continue
        for part in content.get("parts") or []:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                prompt_parts.append(part["text"])
    return "\n".join(part.strip() for part in prompt_parts if part.strip()).strip()


def _gemini_payload_to_imagen_predict_payload(payload: dict) -> dict:
    if isinstance(payload.get("instances"), list):
        return payload

    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        prompt = _extract_prompt_from_gemini_payload(payload)

    parameters = dict(payload.get("parameters") or {})
    generation_config = dict(payload.get("generationConfig") or {})
    image_config = dict(generation_config.get("imageConfig") or {})

    candidate_count = generation_config.get("candidateCount")
    if candidate_count is not None and "sampleCount" not in parameters:
        parameters["sampleCount"] = candidate_count

    image_size = image_config.get("imageSize")
    if image_size is not None and "sampleImageSize" not in parameters:
        parameters["sampleImageSize"] = image_size

    aspect_ratio = image_config.get("aspectRatio")
    if aspect_ratio is not None and "aspectRatio" not in parameters:
        parameters["aspectRatio"] = aspect_ratio

    output_options = generation_config.get("outputOptions")
    if isinstance(output_options, dict) and "outputOptions" not in parameters:
        parameters["outputOptions"] = output_options

    return {
        "instances": [{"prompt": prompt}],
        "parameters": parameters,
    }


def _imagen_response_to_generate_content_response(response: dict) -> dict:
    parts = []
    for prediction in response.get("predictions") or []:
        if not isinstance(prediction, dict):
            continue
        markdown = prediction.get("markdown")
        if isinstance(markdown, str) and markdown:
            parts.append({"text": markdown})
        elif isinstance(prediction.get("raiFilteredReason"), str):
            parts.append({"text": f"[Image filtered: {prediction['raiFilteredReason']}]"})

    return {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": parts,
                },
                "finishReason": "STOP",
            }
        ]
    }


async def _single_event_stream(payload: dict):
    yield f"data: {json.dumps(payload)}\n\n"


def _apply_gemini_native_model_options(model: str, payload: dict) -> tuple[str, dict]:
    configured_model = get_gemini_native_model(model)
    if configured_model:
        return apply_native_model_config(model, payload)

    model_to_call = model
    payload_to_call = dict(payload)

    is_grounded_search = model_to_call.endswith("-search")
    is_nothinking_model = model_to_call.endswith("-nothinking")
    is_max_thinking_model = model_to_call.endswith("-max")
    is_2k_image_model = model_to_call.lower().endswith("-2k")
    is_4k_image_model = model_to_call.lower().endswith("-4k")

    if is_grounded_search:
        model_to_call = model_to_call[:-len("-search")]
    elif is_nothinking_model:
        model_to_call = model_to_call[:-len("-nothinking")]
    elif is_max_thinking_model:
        model_to_call = model_to_call[:-len("-max")]
    elif is_2k_image_model:
        model_to_call = model_to_call[:-len("-2K")]
    elif is_4k_image_model:
        model_to_call = model_to_call[:-len("-4K")]

    if is_grounded_search:
        tools = list(payload_to_call.get("tools") or [])
        tools.append({"googleSearch": {}})
        payload_to_call["tools"] = tools

    if is_2k_image_model or is_4k_image_model:
        payload_to_call = _merge_generation_config(
            payload_to_call,
            {
                "responseModalities": ["TEXT", "IMAGE"],
                "imageConfig": {"imageSize": "2K" if is_2k_image_model else "4K"},
            },
        )

    if is_nothinking_model or is_max_thinking_model:
        if is_nothinking_model:
            budget = 128 if ("gemini-2.5-pro" in model_to_call or "gemini-3-pro" in model_to_call) else 0
        else:
            budget = 32768 if ("gemini-2.5-pro" in model_to_call or "gemini-3-pro" in model_to_call) else 24576

        generation_config = dict(payload_to_call.get("generationConfig") or {})
        thinking_config = dict(generation_config.get("thinkingConfig") or {})
        thinking_config["thinkingBudget"] = budget
        thinking_config["includeThoughts"] = budget != 0
        generation_config["thinkingConfig"] = thinking_config
        payload_to_call["generationConfig"] = generation_config

    return model_to_call, payload_to_call


async def _create_gemini_rest_context(fastapi_request: Request, model: str) -> tuple[GeminiRestClientContext, str]:
    credential_manager_instance = fastapi_request.app.state.credential_manager
    express_key_manager_instance = fastapi_request.app.state.express_key_manager
    model_to_call = model

    force_express = False
    if model_to_call.startswith(PAY_PREFIX):
        model_to_call = model_to_call[len(PAY_PREFIX):]

    if model_to_call.startswith(EXPRESS_PREFIX):
        force_express = True
        model_to_call = model_to_call[len(EXPRESS_PREFIX):]

    has_sa_creds = credential_manager_instance.get_total_credentials() > 0
    has_express_key = express_key_manager_instance.get_total_keys() > 0
    use_express = force_express or (has_express_key and not has_sa_creds)

    if use_express:
        if not has_express_key:
            raise RuntimeError(f"Model '{model}' requires an Express API key, but none are configured.")
        total_keys = express_key_manager_instance.get_total_keys()
        last_error = None
        for attempt in range(total_keys):
            key_tuple = express_key_manager_instance.get_express_api_key()
            if not key_tuple:
                continue
            original_idx, key_val = key_tuple
            try:
                project_id = await discover_project_id(key_val)
                print(f"INFO: Native Gemini request using Express key index {original_idx} for model '{model_to_call}'")
                return (
                    GeminiRestClientContext(
                        project_id=project_id,
                        api_key=key_val,
                        location="global",
                    ),
                    model_to_call,
                )
            except Exception as exc:
                last_error = exc
                print(f"WARNING: Native Gemini Express init failed on attempt {attempt + 1}/{total_keys}: {exc}")

        raise RuntimeError(f"All configured Express API keys failed for model '{model}'. Last error: {last_error}")

    if has_express_key and has_sa_creds:
        print(f"INFO: Native Gemini request has both SA and Express auth; using SA for unprefixed model '{model_to_call}'")

    rotated_credentials, rotated_project_id = credential_manager_instance.get_credentials()
    if not rotated_credentials or not rotated_project_id:
        raise RuntimeError(f"Model '{model}' requires SA credentials for Gemini, but none are available or loaded.")

    gcp_token = _refresh_auth(rotated_credentials)
    if not gcp_token:
        raise RuntimeError(f"Failed to obtain valid GCP token for project {rotated_project_id}")

    print(f"INFO: Native Gemini request using SA credential for model '{model_to_call}' (project: {rotated_project_id})")
    return (
        GeminiRestClientContext(
            project_id=rotated_project_id,
            bearer_token=gcp_token,
            location="global",
        ),
        model_to_call,
    )


@router.post("/v1beta/models/{model}:generateContent")
@router.post("/v1/models/{model}:generateContent")
async def gemini_generate_content(
    fastapi_request: Request,
    model: str,
    api_key: str = Depends(get_api_key),
):
    try:
        payload = await fastapi_request.json()
        if not isinstance(payload, dict):
            return JSONResponse(status_code=400, content={"error": {"message": "Gemini request body must be a JSON object."}})
        payload = _convert_request_markdown_images_to_inline_data(payload)
        model_config = get_gemini_native_model(model)
        model, payload = _apply_gemini_native_model_options(model, payload)
        client_to_use, model_to_call = await _create_gemini_rest_context(fastapi_request, model)
        if model_config and model_config.get("api") == "predict":
            response = await predict_raw(client_to_use, model_to_call, _gemini_payload_to_imagen_predict_payload(payload))
            return JSONResponse(content=_imagen_response_to_generate_content_response(response))
        response = await generate_content_raw(client_to_use, model_to_call, payload)
        return JSONResponse(content=response)
    except Exception as e:
        error_msg = f"Unexpected error in gemini_generate_content endpoint: {str(e)}"
        print(error_msg)
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"error": {"message": error_msg}})


@router.post("/v1beta/models/{model}:streamGenerateContent")
@router.post("/v1/models/{model}:streamGenerateContent")
async def gemini_stream_generate_content(
    fastapi_request: Request,
    model: str,
    api_key: str = Depends(get_api_key),
):
    try:
        payload = await fastapi_request.json()
        if not isinstance(payload, dict):
            return JSONResponse(status_code=400, content={"error": {"message": "Gemini request body must be a JSON object."}})
        payload = _convert_request_markdown_images_to_inline_data(payload)
        model_config = get_gemini_native_model(model)
        model, payload = _apply_gemini_native_model_options(model, payload)
        client_to_use, model_to_call = await _create_gemini_rest_context(fastapi_request, model)
        if model_config and model_config.get("api") == "predict":
            response = await predict_raw(client_to_use, model_to_call, _gemini_payload_to_imagen_predict_payload(payload))
            return StreamingResponse(
                _single_event_stream(_imagen_response_to_generate_content_response(response)),
                media_type="text/event-stream",
            )
        return StreamingResponse(
            stream_generate_content_raw(client_to_use, model_to_call, payload),
            media_type="text/event-stream",
        )
    except Exception as e:
        error_msg = f"Unexpected error in gemini_stream_generate_content endpoint: {str(e)}"
        print(error_msg)
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"error": {"message": error_msg}})


@router.post("/v1beta/models/{model}:predict")
@router.post("/v1/models/{model}:predict")
async def gemini_predict(
    fastapi_request: Request,
    model: str,
    api_key: str = Depends(get_api_key),
):
    try:
        payload = await fastapi_request.json()
        if not isinstance(payload, dict):
            return JSONResponse(status_code=400, content={"error": {"message": "Predict request body must be a JSON object."}})
        model, payload = _apply_gemini_native_model_options(model, payload)
        client_to_use, model_to_call = await _create_gemini_rest_context(fastapi_request, model)
        response = await predict_raw(client_to_use, model_to_call, _gemini_payload_to_imagen_predict_payload(payload))
        return JSONResponse(content=response)
    except Exception as e:
        error_msg = f"Unexpected error in gemini_predict endpoint: {str(e)}"
        print(error_msg)
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"error": {"message": error_msg}})

@router.post("/v1/chat/completions")
async def chat_completions(fastapi_request: Request, request: OpenAIRequest, api_key: str = Depends(get_api_key)):
    try:
        credential_manager_instance = fastapi_request.app.state.credential_manager
        OPENAI_DIRECT_SUFFIX = "-openai"
        OPENAI_SEARCH_SUFFIX = "-openaisearch"
        EXPERIMENTAL_MARKER = "-exp-"
        # Note the space in EXPRESS_PREFIX for easier stripping.
        
        # Model validation based on a predefined list has been removed as per user request.
        # The application will now attempt to use any provided model string.
        # We still need to fetch vertex_express_model_ids for the Express Mode logic.
        # vertex_express_model_ids = await get_vertex_express_models() # We'll use the prefix now

        # Updated logic for is_openai_direct_model
        is_openai_direct_model = False
        is_openai_search_model = False
        if request.model.endswith(OPENAI_DIRECT_SUFFIX) or request.model.endswith(OPENAI_SEARCH_SUFFIX):
            is_openai_search_model = request.model.endswith(OPENAI_SEARCH_SUFFIX)
            suffix_to_remove = OPENAI_SEARCH_SUFFIX if is_openai_search_model else OPENAI_DIRECT_SUFFIX
            temp_name_for_marker_check = request.model[:-len(suffix_to_remove)]
            # An OpenAI model can be prefixed with PAY, EXPRESS, or contain EXP
            if temp_name_for_marker_check.startswith(PAY_PREFIX) or \
               temp_name_for_marker_check.startswith(EXPRESS_PREFIX) or \
               EXPERIMENTAL_MARKER in temp_name_for_marker_check:
                is_openai_direct_model = True
        is_auto_model = request.model.endswith("-auto")
        is_grounded_search = request.model.endswith("-search")
        is_encrypted_model = request.model.endswith("-encrypt")
        is_encrypted_full_model = request.model.endswith("-encrypt-full")
        is_nothinking_model = request.model.endswith("-nothinking")
        is_max_thinking_model = request.model.endswith("-max")
        is_2k_image_model = request.model.endswith("-2k")
        is_4k_image_model = request.model.endswith("-4k")
        base_model_name = request.model # Start with the full model name

        # Determine base_model_name by stripping known prefixes and suffixes
        # Order of stripping: Prefixes first, then suffixes.
        
        is_express_model_request = False
        if base_model_name.startswith(EXPRESS_PREFIX):
            is_express_model_request = True
            base_model_name = base_model_name[len(EXPRESS_PREFIX):]

        if base_model_name.startswith(PAY_PREFIX):
            base_model_name = base_model_name[len(PAY_PREFIX):]

        # Suffix stripping (applied to the name after prefix removal)
        # This order matters if a model could have multiple (e.g. -encrypt-auto, though not currently a pattern)
        if is_openai_direct_model: # This check is based on request.model, so it's fine here
            # If it was an OpenAI direct model, its base name is request.model minus suffix.
            # We need to ensure PAY_PREFIX or EXPRESS_PREFIX are also stripped if they were part of the original.
            suffix_to_remove = OPENAI_SEARCH_SUFFIX if is_openai_search_model else OPENAI_DIRECT_SUFFIX
            temp_base_for_openai = request.model[:-len(suffix_to_remove)]
            if temp_base_for_openai.startswith(EXPRESS_PREFIX):
                temp_base_for_openai = temp_base_for_openai[len(EXPRESS_PREFIX):]
            if temp_base_for_openai.startswith(PAY_PREFIX):
                temp_base_for_openai = temp_base_for_openai[len(PAY_PREFIX):]
            base_model_name = temp_base_for_openai # Assign the fully stripped name
        elif is_auto_model: base_model_name = base_model_name[:-len("-auto")]
        elif is_grounded_search: base_model_name = base_model_name[:-len("-search")]
        elif is_encrypted_full_model: base_model_name = base_model_name[:-len("-encrypt-full")] # Must be before -encrypt
        elif is_encrypted_model: base_model_name = base_model_name[:-len("-encrypt")]
        elif is_nothinking_model: base_model_name = base_model_name[:-len("-nothinking")]
        elif is_max_thinking_model: base_model_name = base_model_name[:-len("-max")]
        elif is_2k_image_model: base_model_name = base_model_name[:-len("-2k")]
        elif is_4k_image_model: base_model_name = base_model_name[:-len("-4k")]
        
        # # Specific model variant checks (if any remain exclusive and not covered dynamically)
        # if is_nothinking_model and not (base_model_name.startswith("gemini-2.5-flash") or base_model_name == "gemini-2.5-pro-preview-06-05"):
        #     return JSONResponse(status_code=400, content=create_openai_error_response(400, f"Model '{request.model}' (-nothinking) is only supported for models starting with 'gemini-2.5-flash' or 'gemini-2.5-pro-preview-06-05'.", "invalid_request_error"))
        # if is_max_thinking_model and not (base_model_name.startswith("gemini-2.5-flash") or base_model_name == "gemini-2.5-pro-preview-06-05"):
        #     return JSONResponse(status_code=400, content=create_openai_error_response(400, f"Model '{request.model}' (-max) is only supported for models starting with 'gemini-2.5-flash' or 'gemini-2.5-pro-preview-06-05'.", "invalid_request_error"))

        # This will now be a dictionary
        gen_config_dict = create_generation_config(request)

        if "gemini-2.5-flash" in base_model_name or "gemini-2.5-pro" in base_model_name:
            if "thinking_config" not in gen_config_dict:
                gen_config_dict["thinking_config"] = {}
            gen_config_dict["thinking_config"]["include_thoughts"] = True

        if "gemini-2.5-flash-lite" in base_model_name:
            gen_config_dict["thinking_config"]["include_thoughts"] = False

        client_to_use = None
        express_key_manager_instance = fastapi_request.app.state.express_key_manager

        # This client initialization logic is for Gemini models (i.e., non-OpenAI Direct models).
        # If 'is_openai_direct_model' is true, this section will be skipped, and the
        # dedicated 'if is_openai_direct_model:' block later will handle it.
        if is_express_model_request: # Changed from elif to if
            if express_key_manager_instance.get_total_keys() == 0:
                error_msg = f"Model '{request.model}' is an Express model and requires an Express API key, but none are configured."
                print(f"ERROR: {error_msg}")
                return JSONResponse(status_code=401, content=create_openai_error_response(401, error_msg, "authentication_error"))

            print(f"INFO: Attempting Vertex Express Mode for model request: {request.model} (base: {base_model_name})")
            
            # Use the ExpressKeyManager to get keys and handle retries
            total_keys = express_key_manager_instance.get_total_keys()
            for attempt in range(total_keys):
                key_tuple = express_key_manager_instance.get_express_api_key()
                if key_tuple:
                    original_idx, key_val = key_tuple
                    try:
                        # Check if model contains "gemini-2.5-pro" or "gemini-2.5-flash" for direct URL approach
                        if "gemini-2.5-pro" in base_model_name or "gemini-2.5-flash" in base_model_name:
                            project_id = await discover_project_id(key_val)
                            client_to_use = GeminiRestClientContext(
                                project_id=project_id,
                                api_key=key_val,
                                location="global",
                            )
                            print(f"INFO: Attempt {attempt+1}/{total_keys} - Using Vertex Express Mode with custom base URL for model {request.model} (base: {base_model_name}) with API key (original index: {original_idx}).")
                        else:
                            project_id = await discover_project_id(key_val)
                            client_to_use = GeminiRestClientContext(
                                project_id=project_id,
                                api_key=key_val,
                                location="global",
                            )
                            print(f"INFO: Attempt {attempt+1}/{total_keys} - Using Vertex Express Mode REST for model {request.model} (base: {base_model_name}) with API key (original index: {original_idx}).")
                        break # Successfully initialized client
                    except Exception as e:
                        print(f"WARNING: Attempt {attempt+1}/{total_keys} - Vertex Express Mode client init failed for API key (original index: {original_idx}) for model {request.model}: {e}. Trying next key.")
                        client_to_use = None # Ensure client_to_use is None for this attempt
                else:
                    # Should not happen if total_keys > 0, but adding a safeguard
                    print(f"WARNING: Attempt {attempt+1}/{total_keys} - get_express_api_key() returned None unexpectedly.")
                    client_to_use = None
                    # Optional: break here if None indicates no more keys are expected

            if client_to_use is None: # All configured Express keys failed or none were returned
                error_msg = f"All {total_keys} configured Express API keys failed to initialize or were unavailable for model '{request.model}'."
                print(f"ERROR: {error_msg}")
                return JSONResponse(status_code=500, content=create_openai_error_response(500, error_msg, "server_error"))
        
        else: # Not an Express model request, therefore an SA credential model request for Gemini
            print(f"INFO: Model '{request.model}' is an SA credential request for Gemini. Attempting SA credentials.")
            rotated_credentials, rotated_project_id = credential_manager_instance.get_credentials()
            
            if rotated_credentials and rotated_project_id:
                try:
                    gcp_token = _refresh_auth(rotated_credentials)
                    if not gcp_token:
                        raise Exception(f"Failed to obtain valid GCP token for project {rotated_project_id}")
                    client_to_use = GeminiRestClientContext(
                        project_id=rotated_project_id,
                        bearer_token=gcp_token,
                        location="global",
                    )
                    print(f"INFO: Using SA credential for Gemini model {request.model} (project: {rotated_project_id})")
                except Exception as e:
                    client_to_use = None # Ensure it's None on failure
                    error_msg = f"SA credential client initialization failed for Gemini model '{request.model}': {e}."
                    print(f"ERROR: {error_msg}")
                    return JSONResponse(status_code=500, content=create_openai_error_response(500, error_msg, "server_error"))
            else: # No SA credentials available for an SA model request
                error_msg = f"Model '{request.model}' requires SA credentials for Gemini, but none are available or loaded."
                print(f"ERROR: {error_msg}")
                return JSONResponse(status_code=401, content=create_openai_error_response(401, error_msg, "authentication_error"))

        # If we reach here and client_to_use is still None, it means it's an OpenAI Direct Model,
        # which handles its own client and responses.
        # For Gemini models (Express or SA), client_to_use must be set, or an error returned above.
        if not is_openai_direct_model and client_to_use is None:
             # This case should ideally not be reached if the logic above is correct,
             # as each path (Express/SA for Gemini) should either set client_to_use or return an error.
             # This is a safeguard.
            print(f"CRITICAL ERROR: Client for Gemini model '{request.model}' was not initialized, and no specific error was returned. This indicates a logic flaw.")
            return JSONResponse(status_code=500, content=create_openai_error_response(500, "Critical internal server error: Gemini client not initialized.", "server_error"))

        if is_openai_direct_model:
            # Use the new OpenAI handler
            if is_express_model_request:
                openai_handler = OpenAIDirectHandler(express_key_manager=express_key_manager_instance)
                return await openai_handler.process_request(request, base_model_name, is_express=True, is_openai_search=is_openai_search_model)
            else:
                openai_handler = OpenAIDirectHandler(credential_manager=credential_manager_instance)
                return await openai_handler.process_request(request, base_model_name, is_openai_search=is_openai_search_model)
        elif is_auto_model:
            print(f"Processing auto model: {request.model}")
            attempts = [
                {"name": "base", "model": base_model_name, "prompt_func": create_gemini_prompt, "config_modifier": lambda c: c},
                {"name": "encrypt", "model": base_model_name, "prompt_func": create_encrypted_gemini_prompt, "config_modifier": lambda c: {**c, "system_instruction": ENCRYPTION_INSTRUCTIONS}},
                {"name": "old_format", "model": base_model_name, "prompt_func": create_encrypted_full_gemini_prompt, "config_modifier": lambda c: c}
            ]
            last_err = None
            for attempt in attempts:
                print(f"Auto-mode attempting: '{attempt['name']}' for model {attempt['model']}")
                # Apply modifier to the dictionary. Ensure modifier returns a dict.
                current_gen_config_dict = attempt["config_modifier"](gen_config_dict.copy())
                try:
                    # Pass is_auto_attempt=True for auto-mode calls
                    result = await execute_gemini_call(client_to_use, attempt["model"], attempt["prompt_func"], current_gen_config_dict, request, is_auto_attempt=True)
                    return result
                except Exception as e_auto:
                    last_err = e_auto
                    print(f"Auto-attempt '{attempt['name']}' for model {attempt['model']} failed: {e_auto}")
                    await asyncio.sleep(1)
            
            print(f"All auto attempts failed. Last error: {last_err}")
            err_msg = f"All auto-mode attempts failed for model {request.model}. Last error: {str(last_err)}"
            if not request.stream and last_err:
                 return JSONResponse(status_code=500, content=create_openai_error_response(500, err_msg, "server_error"))
            elif request.stream:
                # This is the final error handling for auto-mode if all attempts fail AND it was a streaming request
                async def final_auto_error_stream():
                    err_content = create_openai_error_response(500, err_msg, "server_error")
                    json_payload_final_auto_error = json.dumps(err_content)
                    # Log the final error being sent to client after all auto-retries failed
                    print(f"DEBUG: Auto-mode all attempts failed. Yielding final error JSON: {json_payload_final_auto_error}")
                    yield f"data: {json_payload_final_auto_error}\n\n"
                    yield "data: [DONE]\n\n"
                return StreamingResponse(final_auto_error_stream(), media_type="text/event-stream")
            return JSONResponse(status_code=500, content=create_openai_error_response(500, "All auto-mode attempts failed without specific error.", "server_error"))

        else: # Not an auto model
            current_prompt_func = create_gemini_prompt
            # Determine the actual model string to call the API with (e.g., "gemini-1.5-pro-search")

            if is_grounded_search:
                search_tool = types.Tool(google_search=types.GoogleSearch())
                # Add or update the 'tools' key in the gen_config_dict
                if "tools" in gen_config_dict and isinstance(gen_config_dict["tools"], list):
                    gen_config_dict["tools"].append(search_tool)
                else:
                    gen_config_dict["tools"] = [search_tool]
            
            # For encrypted models, system instructions are handled by the prompt_func
            elif is_encrypted_model:
                current_prompt_func = create_encrypted_gemini_prompt
            elif is_encrypted_full_model:
                current_prompt_func = create_encrypted_full_gemini_prompt
            
            # Ensure thinking_config is a dictionary before updating
            if not isinstance(gen_config_dict.get("thinking_config"), dict):
                gen_config_dict["thinking_config"] = {}

            gen_config_dict["thinking_config"]["include_thoughts"] = True
            
            if "gemini-2.5-flash-lite" in base_model_name and is_max_thinking_model:
                gen_config_dict["thinking_config"]["include_thoughts"] = True
            elif "gemini-2.5-flash-lite" in base_model_name or "image" in base_model_name:
                gen_config_dict["thinking_config"]["include_thoughts"] = False
            else:
                gen_config_dict["thinking_config"]["include_thoughts"] = True
            # For -nothinking or -max, the thinking_config is already set in create_generation_config
            # or can be adjusted here if needed, but it's part of the dictionary.
            # Example: if is_nothinking_model: gen_config_dict["thinking_config"] = {"thinking_budget": 0}
            # This is already handled by create_generation_config based on current logic.
            # If specific overrides are needed here, they would modify gen_config_dict.
            if is_nothinking_model or is_max_thinking_model:
                if is_nothinking_model:
                    budget = 128 if ("gemini-2.5-pro" in base_model_name or "gemini-3-pro" in base_model_name) else 0
                else:  # is_max_thinking_model
                    budget = 32768 if ("gemini-2.5-pro" in base_model_name or "gemini-3-pro" in base_model_name) else 24576
                gen_config_dict["thinking_config"]["thinking_budget"] = budget
                if budget == 0:
                    gen_config_dict["thinking_config"]["include_thoughts"] = False

            return await execute_gemini_call(client_to_use, base_model_name, current_prompt_func, gen_config_dict, request)

    except Exception as e:
        error_msg = f"Unexpected error in chat_completions endpoint: {str(e)}"
        print(error_msg)
        return JSONResponse(status_code=500, content=create_openai_error_response(500, error_msg, "server_error"))
