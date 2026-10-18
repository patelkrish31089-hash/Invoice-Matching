from __future__ import annotations

from pathlib import Path
import re


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _render_nginx_template(port: str, upstream: str) -> str:
    template = (PROJECT_ROOT / "frontend" / "nginx.conf").read_text()
    variables = {"PORT": port, "API_UPSTREAM": upstream}
    return re.sub(r"\$\{(PORT|API_UPSTREAM)\}", lambda match: variables[match.group(1)], template)


def test_backend_docker_command_honors_render_port_with_compose_safe_default():
    dockerfile = (PROJECT_ROOT / "backend" / "Dockerfile").read_text()
    assert "PORT=8000" in dockerfile
    assert "--host 0.0.0.0 --port ${PORT:-8000}" in dockerfile
    assert "EXPOSE 8000" in dockerfile


def test_frontend_nginx_template_renders_compose_and_render_upstreams_without_eating_nginx_variables():
    dockerfile = (PROJECT_ROOT / "frontend" / "Dockerfile").read_text()
    assert "COPY frontend/nginx.conf /etc/nginx/templates/default.conf.template" in dockerfile
    assert "NGINX_ENVSUBST_FILTER=^(PORT|API_UPSTREAM)$" in dockerfile

    compose_config = _render_nginx_template("80", "api:8000")
    render_config = _render_nginx_template("10000", "invoiceguard-api:10000")
    assert "listen 80;" in compose_config
    assert "proxy_pass http://api:8000;" in compose_config
    assert "listen 10000;" in render_config
    assert "proxy_pass http://invoiceguard-api:10000;" in render_config
    assert "$host" in render_config
    assert "$http_x_request_id" in render_config
    assert "${PORT}" not in render_config
    assert "${API_UPSTREAM}" not in render_config
