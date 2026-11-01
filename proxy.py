import asyncio
import logging
import mimetypes
import os
import ssl
import time

import aiohttp
from aiohttp import web
from dotenv import load_dotenv

from src.admin import (
    create_admin_handlers,
    create_mapping_handlers,
    create_pricing_handlers,
    create_site_model_handlers,
)
from src.config import load_config
from src.db import init_db
from src.handlers import create_handlers
from src.network import fetch_public_ip
from src.pool import load_pool_from_db
from src.quota import create_quota_handlers
from src.throughput import ThroughputMonitor, create_throughput_handlers
from src.usage_monitor import UsageMonitor
from src.v1_handler import create_v1_handlers

mimetypes.add_type("font/ttf", ".ttf")
mimetypes.add_type("font/otf", ".otf")
mimetypes.add_type("font/woff", ".woff")
mimetypes.add_type("font/woff2", ".woff2")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

load_dotenv()
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "WARNING").upper(),
    format="%(levelname)s %(name)s %(message)s",
)


async def main():
    cfg = load_config()

    print("Fetching public IP...")
    public_ip = await fetch_public_ip()
    print(f"Current public IP: {public_ip}")

    db_conn, generated_admins = init_db(
        cfg.usage_db, cfg.pool_file, cfg.admin_tokens, cfg.pool_count
    )
    token_pool = load_pool_from_db(db_conn)

    if generated_admins:
        for t in generated_admins:
            print(f"[ADMIN] Auto-generated admin token: {t}")
            print(f"[ADMIN] Use this to access: http://0.0.0.0:{cfg.bind_port}/admin")

    provider_names = [p.name for p in cfg.providers]
    print(f"Providers configured: {provider_names}")

    timeout = aiohttp.ClientTimeout(
        total=cfg.timeout_total,
        sock_connect=cfg.timeout_connect,
        sock_read=cfg.timeout_sock_read,
    )
    connector = aiohttp.TCPConnector(
        ssl=ssl.create_default_context(),
        enable_cleanup_closed=True,
        keepalive_timeout=10,
    )
    session = aiohttp.ClientSession(
        connector=connector,
        timeout=timeout,
        skip_auto_headers={"User-Agent"},
        auto_decompress=True,
    )

    async def on_cleanup(app: web.Application):
        await session.close()

    monitor = ThroughputMonitor(token_pool)
    serve_tp_page, create_tp_session, tp_sse = create_throughput_handlers(
        monitor, token_pool, db_conn
    )

    usage_monitor = UsageMonitor(token_pool)

    quota_api_handler = create_quota_handlers(cfg, session)

    (
        serve_usage_page,
        info_api_handler,
        proxy_handler,
        serve_status_page,
        status_api_handler,
        serve_clients_page,
        clients_api_handler,
        usage_stream_api,
        usage_sse_handler,
    ) = create_handlers(
        cfg,
        token_pool,
        db_conn,
        public_ip,
        session,
        time.time(),
        monitor,
        usage_monitor,
    )

    (
        serve_admin_page,
        api_list_tokens,
        api_create_token,
        api_update_token,
        api_delete_token,
        api_tokens_usage,
    ) = create_admin_handlers(cfg, db_conn, token_pool)

    (
        serve_mapping_page,
        api_list_mappings,
        api_set_mapping,
        api_delete_mapping,
    ) = create_mapping_handlers(cfg, db_conn, token_pool)

    (
        serve_pricing_page,
        api_list_pricing,
        api_set_pricing,
        api_delete_pricing,
    ) = create_pricing_handlers(cfg, db_conn)

    (
        serve_models_page,
        api_list_models,
        api_set_model,
        api_delete_model,
    ) = create_site_model_handlers(cfg, db_conn)

    (
        v1_chat_completions,
        v1_responses,
        v1_list_models,
        v1_get_model,
    ) = create_v1_handlers(cfg, token_pool, db_conn, monitor, usage_monitor)

    if not cfg.enable_v1:

        async def v1_disabled(_request: web.Request) -> web.Response:
            return web.json_response(
                {"error": "v1 routes are disabled (enable_v1 = false)"}, status=410
            )

        v1_chat_completions = v1_responses = v1_list_models = v1_get_model = v1_disabled

    app = web.Application(
        client_max_size=100 * 1024 * 1024,
        handler_args={"keepalive_timeout": 75},
    )
    app.on_cleanup.append(on_cleanup)
    app.router.add_static("/static", STATIC_DIR)
    app.router.add_get("/usage", serve_usage_page)
    app.router.add_get("/usage/api/quota", quota_api_handler)
    app.router.add_post("/usage/api", info_api_handler)
    app.router.add_post("/usage/api/stream", usage_stream_api)
    app.router.add_get("/usage/events", usage_sse_handler)
    app.router.add_get("/admin", serve_admin_page)
    app.router.add_get("/admin/api/tokens", api_list_tokens)
    app.router.add_post("/admin/api/tokens", api_create_token)
    app.router.add_put("/admin/api/tokens", api_update_token)
    app.router.add_delete("/admin/api/tokens", api_delete_token)
    app.router.add_get("/admin/api/tokens/usage", api_tokens_usage)
    app.router.add_get("/status", serve_status_page)
    app.router.add_get("/status/api", status_api_handler)
    app.router.add_get("/clients", serve_clients_page)
    app.router.add_get("/clients/api", clients_api_handler)
    app.router.add_get("/throughput", serve_tp_page)
    app.router.add_post("/throughput/api/session", create_tp_session)
    app.router.add_get("/throughput/events", tp_sse)
    app.router.add_get("/mapping", serve_mapping_page)
    app.router.add_get("/mapping/api", api_list_mappings)
    app.router.add_post("/mapping/api", api_set_mapping)
    app.router.add_delete("/mapping/api", api_delete_mapping)
    app.router.add_get("/pricing", serve_pricing_page)
    app.router.add_get("/pricing/api", api_list_pricing)
    app.router.add_post("/pricing/api", api_set_pricing)
    app.router.add_delete("/pricing/api", api_delete_pricing)
    app.router.add_get("/models", serve_models_page)
    app.router.add_get("/admin/api/models", api_list_models)
    app.router.add_post("/admin/api/models", api_set_model)
    app.router.add_delete("/admin/api/models", api_delete_model)
    app.router.add_get("/v1/models", v1_list_models)
    app.router.add_get("/v1/model", v1_list_models)
    app.router.add_get("/v1/models/{model}", v1_get_model)
    app.router.add_get("/litellm/v1/models", v1_list_models)

    async def serve_index(_request: web.Request) -> web.FileResponse:
        return web.FileResponse(os.path.join(STATIC_DIR, "index.html"))

    app.router.add_get("/", serve_index)
    app.router.add_post("/v1/chat/completions", v1_chat_completions)
    app.router.add_post("/v1/responses", v1_responses)
    app.router.add_post("/litellm/v1/chat/completions", v1_chat_completions)
    app.router.add_post("/litellm/v1/responses", v1_responses)
    app.router.add_route("*", "/{path:.*}", proxy_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", cfg.bind_port)
    await site.start()

    provider_list = ", ".join(f"{p.prefix} -> {p.upstream}" for p in cfg.providers)
    print(f"Reverse proxy running on http://0.0.0.0:{cfg.bind_port}")
    print(f"Providers: {provider_list}")
    print(f"Token pool: {len(token_pool)} tokens")
    print(f"Usage query: http://0.0.0.0:{cfg.bind_port}/usage")
    print(f"Status page: http://0.0.0.0:{cfg.bind_port}/status")
    print(f"Client stats: http://0.0.0.0:{cfg.bind_port}/clients")
    print(f"Admin panel: http://0.0.0.0:{cfg.bind_port}/admin")
    print(f"Model mapping: http://0.0.0.0:{cfg.bind_port}/mapping")
    print(f"Pricing: http://0.0.0.0:{cfg.bind_port}/pricing")
    print(f"Models: http://0.0.0.0:{cfg.bind_port}/models")
    print(f"Throughput: http://0.0.0.0:{cfg.bind_port}/throughput")
    print(f"Intercept: {cfg.intercept_port or 'disabled'}")
    print(f"Logging: {'enabled' if cfg.enable_log else 'disabled'}")
    print(
        f"Privacy strip: {'enabled' if cfg.privacy_strip_tracking else 'disabled'}"
        + (
            f" (UA: {cfg.privacy_ua_override})"
            if cfg.privacy_strip_tracking and cfg.privacy_ua_override
            else ""
        )
    )
    print(
        f"Timeouts: total={cfg.timeout_total}s, connect={cfg.timeout_connect}s, sock_read={cfg.timeout_sock_read}s"
    )
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
