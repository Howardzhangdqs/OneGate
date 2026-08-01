from __future__ import annotations

import json
import logging
import time

from aiohttp import web

from src.config import AppConfig
from src.db import (
    record_success,
    record_token_usage,
    record_usage,
    resolve_model,
)
from src.models import Provider, TokenEntry
from src.pool import resolve_token
from src.throughput import make_tracker

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
) -> tuple[str | None, Provider | None, str]:
    mapped_model, mapped_provider_name = resolve_model(db_conn, proxy_token, model)
    provider = None
    if mapped_provider_name:
        provider = _get_provider_by_name(cfg, mapped_provider_name)
    return mapped_model, provider, model


def create_v1_handlers(
    cfg: AppConfig,
    token_pool: dict[str, TokenEntry],
    db_conn,
    monitor=None,
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
    ) -> None:
        if not proxy_token:
            return
        record_usage(db_conn, proxy_token, provider_name)
        record_success(db_conn, proxy_token, provider_name)
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
        record_token_usage(db_conn, proxy_token, provider_name, tin, tout, tcr, tcw)

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

        mapped_model, target_provider, original_model = _resolve_v1_mapping(
            db_conn, cfg, proxy_token, model
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
        if entry is None or not entry.has_permission(target_provider):
            return web.json_response(
                {
                    "error": f"token not authorized for provider '{target_provider.name}'"
                },
                status=403,
            )

        real_token = cfg.real_tokens.get(target_provider.name, "")
        litellm_model = _get_litellm_model(target_provider, mapped_model)
        base_url = _get_litellm_base_url(target_provider)
        is_stream = _parse_request_stream(body)

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
                logger.info(
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

        mapped_model, target_provider, original_model = _resolve_v1_mapping(
            db_conn, cfg, proxy_token, model
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
        if entry is None or not entry.has_permission(target_provider):
            return web.json_response(
                {
                    "error": f"token not authorized for provider '{target_provider.name}'"
                },
                status=403,
            )

        real_token = cfg.real_tokens.get(target_provider.name, "")
        litellm_model = _get_litellm_model(target_provider, mapped_model)
        base_url = _get_litellm_base_url(target_provider)
        is_stream = req_obj.get("stream", False)
        input_data = req_obj.get("input", "")

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
            logger.info(
                "V1 RESPONSES chat_request: %s",
                {k: v for k, v in chat_request.items() if k not in ("api_key",)},
            )

            if is_stream:
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
                logger.info(
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
                logger.info(
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

    return v1_chat_completions, v1_responses
