import asyncio
import logging
import os
import ssl
import time

import aiohttp
from aiohttp import web
from dotenv import load_dotenv

from src.admin import create_admin_handlers, create_mapping_handlers
from src.config import load_config
from src.db import init_db
from src.handlers import create_handlers
from src.network import fetch_public_ip
from src.pool import load_pool_from_db
from src.throughput import ThroughputMonitor, create_throughput_handlers
from src.v1_handler import create_v1_handlers

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")


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
    connector = aiohttp.TCPConnector(ssl=ssl.create_default_context())
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
        monitor, token_pool
    )

    (
        serve_usage_page,
        info_api_handler,
        proxy_handler,
        serve_status_page,
        status_api_handler,
    ) = create_handlers(
        cfg, token_pool, db_conn, public_ip, session, time.time(), monitor
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

    v1_chat_completions, v1_responses = create_v1_handlers(
        cfg, token_pool, db_conn, monitor
    )

    app = web.Application(
        client_max_size=100 * 1024 * 1024,
        handler_args={"keepalive_timeout": 75},
    )
    app.on_cleanup.append(on_cleanup)
    app.router.add_static("/static", STATIC_DIR)
    app.router.add_get("/usage", serve_usage_page)
    app.router.add_post("/usage/api", info_api_handler)
    app.router.add_get("/admin", serve_admin_page)
    app.router.add_get("/admin/api/tokens", api_list_tokens)
    app.router.add_post("/admin/api/tokens", api_create_token)
    app.router.add_put("/admin/api/tokens", api_update_token)
    app.router.add_delete("/admin/api/tokens", api_delete_token)
    app.router.add_get("/admin/api/tokens/usage", api_tokens_usage)
    app.router.add_get("/status", serve_status_page)
    app.router.add_get("/status/api", status_api_handler)
    app.router.add_get("/throughput", serve_tp_page)
    app.router.add_post("/throughput/api/session", create_tp_session)
    app.router.add_get("/throughput/events", tp_sse)
    app.router.add_get("/mapping", serve_mapping_page)
    app.router.add_get("/mapping/api", api_list_mappings)
    app.router.add_post("/mapping/api", api_set_mapping)
    app.router.add_delete("/mapping/api", api_delete_mapping)

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
    print(f"Admin panel: http://0.0.0.0:{cfg.bind_port}/admin")
    print(f"Model mapping: http://0.0.0.0:{cfg.bind_port}/mapping")
    print(f"Throughput: http://0.0.0.0:{cfg.bind_port}/throughput")
    print(f"Intercept: {cfg.intercept_port or 'disabled'}")
    print(f"Logging: {'enabled' if cfg.enable_log else 'disabled'}")
    print(
        f"Timeouts: total={cfg.timeout_total}s, connect={cfg.timeout_connect}s, sock_read={cfg.timeout_sock_read}s"
    )
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
