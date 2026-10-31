from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from aiohttp import web

from src.client_detect import detect_client
from src.config import AppConfig
from src.db import (
    check_rate_limit,
    get_site_models,
    is_site_model_pattern,
    record_client_token_usage,
    record_client_usage,
    record_success,
    record_token_usage,
    record_usage,
    resolve_model_ex,
    resolve_site_model_exact,
    resolve_site_model_pattern,
)
from src.models import Provider, TokenEntry
from src.pool import resolve_token
from src.throughput import make_tracker

_V1_PRIVACY_SKIP = frozenset({"user", "metadata", "extra_headers"})

logger = logging.getLogger(__name__)

PROVIDER_TO_LITELLM_PREFIX: dict[str, str] = {}


def _litellm_usage_to_tuple(usage) -> tuple[int, int, int, int]:
    if usage is None:
        return 0, 0, 0, 0
    input_tokens = getattr(usage, "prompt_tokens", 0) or 0
    output_tokens = getattr(usage, "completion_tokens", 0) or 0
    cache_read = getattr(usage, "_cache_read_input_tokens", 0) or 0
    cache_write = getattr(usage, "_cache_creation_input_tokens", 0) or 0
    ptd = getattr(usage, "prompt_tokens_details", None)
    if ptd is not None:
        cached = getattr(ptd, "cached_tokens", None)
        if isinstance(cached, int):
            cache_read = cache_read or cached
        created = getattr(ptd, "cache_creation_tokens", None)
        if isinstance(created, int):
            cache_write = cache_write or created
    if cache_read > 0:
        input_tokens = max(0, input_tokens - cache_read)
    return int(input_tokens), int(output_tokens), int(cache_read), int(cache_write)


def _get_litellm_model(provider: Provider, model: str) -> str:
    prefix = PROVIDER_TO_LITELLM_PREFIX.get(provider.name, "openai")
    return f"{prefix}/{model}"


def _get_litellm_base_url(provider: Provider) -> str:
    if provider.v1_base:
        return provider.v1_base
    upstream = provider.upstream
    if upstream.endswith("/v1"):
        return upstream
    return upstream.rstrip("/") + "/v1"


def _with_identity_encoding(headers: dict | None) -> dict:
    merged = {
        key: value
        for key, value in (headers or {}).items()
        if key.lower() != "accept-encoding"
    }
    merged["Accept-Encoding"] = "identity"
    return merged


def _parse_request_model(body: bytes | None) -> str:
    if not body:
        return ""
    try:
        obj = json.loads(body)
        return obj.get("model", "")
    except Exception:
        return ""


def _parse_request_stream(body: bytes | None) -> bool:
    if not body:
        return False
    try:
        obj = json.loads(body)
        return obj.get("stream", False)
    except Exception:
        return False


def _get_provider_by_name(cfg: AppConfig, name: str) -> Provider | None:
    for p in cfg.providers:
        if p.name == name:
            return p
    return None


def _resolve_v1_mapping(
    db_conn,
    cfg: AppConfig,
    proxy_token: str,
    model: str,
) -> tuple[str | None, Provider | None, str, bool]:
    mapped_model, mapped_provider_name, matched = resolve_model_ex(
        db_conn, proxy_token, model
    )
    if matched:
        provider = _get_provider_by_name(cfg, mapped_provider_name)
        return mapped_model, provider, model, True
    row = resolve_site_model_pattern(db_conn, model)
    if row:
        provider = _get_provider_by_name(cfg, row["provider"])
        return row["target"] or model, provider, model, False
    return model, None, model, False


def create_v1_handlers(
    cfg: AppConfig,
    token_pool: dict[str, TokenEntry],
    db_conn,
    monitor=None,
    usage_monitor=None,
):
    def _authenticate(
        request: web.Request,
    ) -> tuple[str | None, web.StreamResponse | None]:
        auth_header = request.headers.get("Authorization", "")
        proxy_token, _ = resolve_token(
            auth_header, cfg.providers[0], token_pool, cfg.real_tokens
        )
        if not proxy_token:
            logger.warning(
                "V1 auth failed: %s", auth_header[:40] if auth_header else "(none)"
            )
            return None, web.json_response({"error": "unauthorized"}, status=401)
        return proxy_token, None

    def _record_v1_usage(
        proxy_token: str | None,
        provider_name: str,
        response,
        is_stream: bool,
        client: str = "unknown",
        model: str = "",
    ) -> None:
        if not proxy_token:
            return
        record_usage(db_conn, proxy_token, provider_name)
        record_success(db_conn, proxy_token, provider_name)
        record_client_usage(db_conn, proxy_token, provider_name, client)
        usage = None
        try:
            if is_stream:
                import litellm

                chunks = getattr(response, "chunks", None)
                if chunks:
                    aggregated = litellm.stream_chunk_builder(chunks)
                    usage = getattr(aggregated, "usage", None)
            else:
                usage = getattr(response, "usage", None)
        except Exception as e:
            logger.warning("V1 usage extraction failed: %s", e)
        tin, tout, tcr, tcw = _litellm_usage_to_tuple(usage)
        entry = token_pool.get(proxy_token)
        if entry is not None:
            cr_w = (
                entry.cache_read_weight
                if entry.cache_read_weight >= 0
                else cfg.cache_read_weight
            )
            cw_w = (
                entry.cache_write_weight
                if entry.cache_write_weight >= 0
                else cfg.cache_write_weight
            )
        else:
            cr_w, cw_w = cfg.cache_read_weight, cfg.cache_write_weight
        record_token_usage(
            db_conn,
            proxy_token,
            provider_name,
            tin,
            tout,
            tcr,
            tcw,
            cr_w,
            cw_w,
            model=model,
        )
        record_client_token_usage(
            db_conn,
            proxy_token,
            provider_name,
            client,
            tin,
            tout,
            tcr,
            tcw,
            model=model,
        )
        if usage_monitor is not None and usage_monitor.is_active(proxy_token):
            now_ts = int(time.time())
            hour_ts = now_ts - (now_ts % 3600)
            hour_key = datetime.fromtimestamp(hour_ts, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            usage_monitor.notify_usage(
                proxy_token,
                {
                    "hour": hour_key,
                    "provider": provider_name,
                    "client": client,
                    "model": model,
                    "input": tin,
                    "output": tout,
                    "cache_read": tcr,
                    "cache_write": tcw,
                    "requests": 1,
                    "ts": now_ts,
                },
            )

    async def v1_chat_completions(request: web.Request) -> web.StreamResponse:
        import litellm

        body = await request.read() if request.body_exists else None
        if not body:
            return web.json_response({"error": "empty request body"}, status=400)

        model = _parse_request_model(body)
        if not model:
            return web.json_response(
                {"error": "model not found in request body"}, status=400
            )

        proxy_token, auth_err = _authenticate(request)
        if auth_err:
            return auth_err

        mapped_model, target_provider, original_model, token_matched = (
            _resolve_v1_mapping(db_conn, cfg, proxy_token, model)
        )
        if mapped_model is None:
            return web.json_response(
                {
                    "error": f"model '{original_model}' not found in mapping rules",
                    "model": original_model,
                },
                status=404,
            )

        if not target_provider:
            target_provider = cfg.providers[0]

        entry = token_pool.get(proxy_token)
        if not token_matched and entry is not None and entry.models != ["*"]:
            if not entry.has_model_permission(original_model):
                return web.json_response(
                    {"error": f"token not authorized for model '{original_model}'"},
                    status=403,
                )
        if entry is None or not entry.has_permission(target_provider):
            return web.json_response(
                {
                    "error": f"token not authorized for provider '{target_provider.name}'"
                },
                status=403,
            )

        if entry.has_limits:
            allowed, triggered, quota_info = check_rate_limit(db_conn, entry)
            if not allowed:
                return web.json_response(
                    {
                        "error": "rate limit exceeded",
                        "triggered": triggered,
                        "quota": quota_info,
                    },
                    status=429,
                )

        real_token = cfg.real_tokens.get(target_provider.name, "")
        litellm_model = _get_litellm_model(target_provider, mapped_model)
        base_url = _get_litellm_base_url(target_provider)
        is_stream = _parse_request_stream(body)
        client = detect_client(request.headers)

        try:
            req_obj = json.loads(body)
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        messages = req_obj.get("messages", [])
        kwargs: dict = {}
        for key in (
            "temperature",
            "top_p",
            "max_tokens",
            "max_completion_tokens",
            "stop",
            "stream",
            "stream_options",
            "n",
            "presence_penalty",
            "frequency_penalty",
            "logit_bias",
            "user",
            "response_format",
            "seed",
            "tools",
            "tool_choice",
            "parallel_tool_calls",
            "logprobs",
            "top_logprobs",
            "modalities",
            "audio",
            "prediction",
            "reasoning_effort",
            "extra_headers",
            "thinking",
        ):
            if cfg.privacy_strip_tracking and key in _V1_PRIVACY_SKIP:
                continue
            if key in req_obj:
                kwargs[key] = req_obj[key]

        kwargs["stream"] = is_stream

        tracker = make_tracker(
            monitor,
            proxy_token,
            model=original_model,
            provider_name=target_provider.name,
            request_url=request.path,
            target_url=base_url,
            request_headers=dict(request.headers),
            request_body=body,
        )
        if tracker is not None:
            tracker.emit_start()

        start_time = time.monotonic()
        try:
            if is_stream:
                kwargs["extra_headers"] = _with_identity_encoding(
                    kwargs.get("extra_headers")
                )
                response = await litellm.acompletion(
                    model=litellm_model,
                    messages=messages,
                    api_key=real_token,
                    api_base=base_url,
                    **kwargs,
                )
                resp = web.StreamResponse(
                    status=200,
                    headers={
                        "Content-Type": "text/event-stream",
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "X-Accel-Buffering": "no",
                    },
                )
                await resp.prepare(request)
                async for chunk in response:
                    line = chunk.model_dump_json(exclude_none=True)
                    data = f"data: {line}\n\n".encode("utf-8")
                    if tracker is not None:
                        tracker.feed_chunk(data, "text/event-stream")
                    await resp.write(data)
                await resp.write(b"data: [DONE]\n\n")
                await resp.write_eof()
                if tracker is not None:
                    tracker.emit_end_stream(200, dict(resp.headers))
                _record_v1_usage(
                    proxy_token,
                    target_provider.name,
                    response,
                    is_stream=True,
                    client=client,
                    model=mapped_model,
                )
                return resp
            else:
                response = await litellm.acompletion(
                    model=litellm_model,
                    messages=messages,
                    api_key=real_token,
                    api_base=base_url,
                    **kwargs,
                )
                resp_body = response.model_dump_json(exclude_none=True)
                elapsed = time.monotonic() - start_time
                logger.debug(
                    "V1 %s -> [%s] %s %.2fs",
                    original_model,
                    target_provider.name,
                    200,
                    elapsed,
                )
                if tracker is not None:
                    tracker.feed_non_stream_response(resp_body.encode("utf-8"), 200, {})
                _record_v1_usage(
                    proxy_token,
                    target_provider.name,
                    response,
                    is_stream=False,
                    client=client,
                    model=mapped_model,
                )
                return web.Response(
                    text=resp_body,
                    content_type="application/json",
                )
        except litellm.exceptions.AuthenticationError:
            return web.json_response(
                {"error": "upstream authentication failed"}, status=401
            )
        except litellm.exceptions.NotFoundError as e:
            return web.json_response({"error": str(e)}, status=404)
        except litellm.exceptions.RateLimitError:
            return web.json_response(
                {"error": "upstream rate limit exceeded"}, status=429
            )
        except litellm.exceptions.Timeout:
            return web.json_response(
                {"error": "upstream request timed out"}, status=504
            )
        except Exception as e:
            logger.error(
                "V1 error: %s %s -> %s", original_model, target_provider.name, e
            )
            return web.json_response({"error": f"upstream error: {e}"}, status=502)

    async def v1_responses(request: web.Request) -> web.StreamResponse:
        import litellm
        from litellm.responses.litellm_completion_transformation.streaming_iterator import (
            LiteLLMCompletionStreamingIterator,
        )
        from litellm.responses.litellm_completion_transformation.transformation import (
            LiteLLMCompletionResponsesConfig,
        )

        body = await request.read() if request.body_exists else None
        if not body:
            return web.json_response({"error": "empty request body"}, status=400)

        try:
            req_obj = json.loads(body)
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        model = req_obj.get("model", "")
        if not model:
            return web.json_response(
                {"error": "model not found in request body"}, status=400
            )

        proxy_token, auth_err = _authenticate(request)
        if auth_err:
            return auth_err

        mapped_model, target_provider, original_model, token_matched = (
            _resolve_v1_mapping(db_conn, cfg, proxy_token, model)
        )
        if mapped_model is None:
            return web.json_response(
                {
                    "error": f"model '{original_model}' not found in mapping rules",
                    "model": original_model,
                },
                status=404,
            )

        if not target_provider:
            target_provider = cfg.providers[0]

        entry = token_pool.get(proxy_token)
        if not token_matched and entry is not None and entry.models != ["*"]:
            if not entry.has_model_permission(original_model):
                return web.json_response(
                    {"error": f"token not authorized for model '{original_model}'"},
                    status=403,
                )
        if entry is None or not entry.has_permission(target_provider):
            return web.json_response(
                {
                    "error": f"token not authorized for provider '{target_provider.name}'"
                },
                status=403,
            )

        if entry.has_limits:
            allowed, triggered, quota_info = check_rate_limit(db_conn, entry)
            if not allowed:
                return web.json_response(
                    {
                        "error": "rate limit exceeded",
                        "triggered": triggered,
                        "quota": quota_info,
                    },
                    status=429,
                )

        real_token = cfg.real_tokens.get(target_provider.name, "")
        litellm_model = _get_litellm_model(target_provider, mapped_model)
        base_url = _get_litellm_base_url(target_provider)
        is_stream = req_obj.get("stream", False)
        input_data = req_obj.get("input", "")
        client = detect_client(request.headers)

        resp_kwargs: dict = {}
        for key in (
            "include",
            "instructions",
            "max_output_tokens",
            "temperature",
            "top_p",
            "tools",
            "tool_choice",
            "parallel_tool_calls",
            "truncation",
            "user",
            "reasoning",
            "store",
            "text",
            "extra_headers",
            "extra_body",
            "previous_response_id",
            "metadata",
        ):
            if cfg.privacy_strip_tracking and key in _V1_PRIVACY_SKIP:
                continue
            if key in req_obj:
                resp_kwargs[key] = req_obj[key]

        tracker = make_tracker(
            monitor,
            proxy_token,
            model=original_model,
            provider_name=target_provider.name,
            request_url=request.path,
            target_url=base_url,
            request_headers=dict(request.headers),
            request_body=body,
        )
        if tracker is not None:
            tracker.emit_start()

        start_time = time.monotonic()
        try:
            chat_request = LiteLLMCompletionResponsesConfig.transform_responses_api_request_to_chat_completion_request(
                model=litellm_model,
                input=input_data,
                responses_api_request=resp_kwargs,
                stream=is_stream,
            )
            chat_request["api_key"] = real_token
            chat_request["api_base"] = base_url
            for _k in ("client_metadata", "max_output_tokens", "previous_response_id"):
                chat_request.pop(_k, None)
            if "tools" in chat_request:
                chat_request["tools"] = [
                    t for t in chat_request["tools"] if t.get("type") == "function"
                ]
                if not chat_request["tools"]:
                    chat_request.pop("tools", None)
                    chat_request.pop("tool_choice", None)
            logger.debug(
                "V1 RESPONSES chat_request: %s",
                {k: v for k, v in chat_request.items() if k not in ("api_key",)},
            )

            if is_stream:
                chat_request["extra_headers"] = _with_identity_encoding(
                    chat_request.get("extra_headers")
                )
                stream_response = await litellm.acompletion(**chat_request)
                iterator = LiteLLMCompletionStreamingIterator(
                    model=litellm_model,
                    litellm_custom_stream_wrapper=stream_response,
                    request_input=input_data,
                    responses_api_request=resp_kwargs,
                )
                resp = web.StreamResponse(
                    status=200,
                    headers={
                        "Content-Type": "text/event-stream",
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "X-Accel-Buffering": "no",
                    },
                )
                await resp.prepare(request)
                async for event in iterator:
                    if hasattr(event, "model_dump"):
                        data = f"data: {event.model_dump_json()}\n\n".encode("utf-8")
                    elif isinstance(event, dict):
                        data = f"data: {json.dumps(event)}\n\n".encode("utf-8")
                    else:
                        data = f"data: {json.dumps(str(event))}\n\n".encode("utf-8")
                    if tracker is not None:
                        tracker.feed_chunk(data, "text/event-stream")
                    await resp.write(data)
                await resp.write(b"data: [DONE]\n\n")
                await resp.write_eof()
                if tracker is not None:
                    tracker.emit_end_stream(200, dict(resp.headers))
                elapsed = time.monotonic() - start_time
                logger.debug(
                    "V1 RESPONSES STREAM %s -> [%s] %s %.2fs",
                    original_model,
                    target_provider.name,
                    200,
                    elapsed,
                )
                _record_v1_usage(
                    proxy_token,
                    target_provider.name,
                    stream_response,
                    is_stream=True,
                    client=client,
                    model=mapped_model,
                )
                return resp
            else:
                completion_response = await litellm.acompletion(**chat_request)
                result = LiteLLMCompletionResponsesConfig.transform_chat_completion_response_to_responses_api_response(
                    chat_completion_response=completion_response,
                    request_input=input_data,
                    responses_api_request=resp_kwargs,
                )
                if hasattr(result, "model_dump"):
                    resp_body = json.dumps(result.model_dump(), ensure_ascii=False)
                elif isinstance(result, dict):
                    resp_body = json.dumps(result, ensure_ascii=False)
                else:
                    resp_body = str(result)
                elapsed = time.monotonic() - start_time
                logger.debug(
                    "V1 RESPONSES %s -> [%s] %s %.2fs",
                    original_model,
                    target_provider.name,
                    200,
                    elapsed,
                )
                if tracker is not None:
                    tracker.feed_non_stream_response(resp_body.encode("utf-8"), 200, {})
                _record_v1_usage(
                    proxy_token,
                    target_provider.name,
                    completion_response,
                    is_stream=False,
                    client=client,
                    model=mapped_model,
                )
                return web.Response(
                    text=resp_body,
                    content_type="application/json",
                )
        except litellm.exceptions.AuthenticationError as e:
            logger.error(
                "V1 RESPONSES upstream auth failed: %s %s -> %s",
                original_model,
                target_provider.name,
                e,
            )
            return web.json_response(
                {"error": "upstream authentication failed"}, status=401
            )
        except litellm.exceptions.NotFoundError as e:
            return web.json_response({"error": str(e)}, status=404)
        except litellm.exceptions.RateLimitError:
            return web.json_response(
                {"error": "upstream rate limit exceeded"}, status=429
            )
        except litellm.exceptions.Timeout:
            return web.json_response(
                {"error": "upstream request timed out"}, status=504
            )
        except Exception as e:
            logger.error(
                "V1 RESPONSES error: %s %s -> %s",
                original_model,
                target_provider.name,
                e,
            )
            return web.json_response({"error": f"upstream error: {e}"}, status=502)

    async def v1_list_models(request: web.Request) -> web.StreamResponse:
        proxy_token, auth_err = _authenticate(request)
        if auth_err:
            return auth_err
        entry = token_pool.get(proxy_token)
        data = []
        for row in get_site_models(db_conn, enabled_only=True):
            if is_site_model_pattern(row["model"]):
                continue
            provider = _get_provider_by_name(cfg, row["provider"])
            if provider is None:
                continue
            if entry is None or not entry.has_permission(provider):
                continue
            if not entry.has_model_permission(row["model"]):
                continue
            data.append(
                {
                    "id": row["model"],
                    "object": "model",
                    "created": 0,
                    "owned_by": row["provider"],
                }
            )
        return web.json_response({"object": "list", "data": data})

    async def v1_get_model(request: web.Request) -> web.StreamResponse:
        proxy_token, auth_err = _authenticate(request)
        if auth_err:
            return auth_err
        name = request.match_info.get("model", "")
        row = (
            resolve_site_model_exact(db_conn, name)
            if not is_site_model_pattern(name)
            else None
        )
        provider = _get_provider_by_name(cfg, row["provider"]) if row else None
        entry = token_pool.get(proxy_token)
        visible = (
            row is not None
            and provider is not None
            and entry is not None
            and entry.has_permission(provider)
            and entry.has_model_permission(row["model"])
        )
        if not visible:
            return web.json_response({"error": f"model '{name}' not found"}, status=404)
        return web.json_response(
            {
                "id": row["model"],
                "object": "model",
                "created": 0,
                "owned_by": row["provider"],
            }
        )

    return v1_chat_completions, v1_responses, v1_list_models, v1_get_model
