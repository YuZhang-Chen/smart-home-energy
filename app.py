"""Hugging Face Spaces (Gradio SDK Free Tier) all-in-one launcher.

When deployed to a free Hugging Face Gradio Space:
1. Hugging Face installs system dependencies from `packages.txt` (mosquitto, nodejs, npm)
   and Python dependencies from `requirements.txt`.
2. It runs `python app.py`, expecting an HTTP server on port 7860.
3. This script ensures `.env` and `web/dist` exist, starts Mosquitto, virtual devices
   (AC, Washer, Bulb), Controller, and Copilot Runtime in the background, and finally
   serves the FastAPI + React 3D living room on 0.0.0.0:7860.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

import uvicorn

try:
    import spaces

    @spaces.GPU
    def _hf_zerogpu_placeholder() -> None:
        """Satisfy Hugging Face ZeroGPU startup check when Space hardware is set to ZeroGPU."""
        return None
except ImportError:
    pass

ROOT = Path(__file__).resolve().parent
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def _ensure_env_file() -> None:
    env_path = ROOT / ".env"
    example_path = ROOT / ".env.example"
    if not env_path.exists() and example_path.exists():
        shutil.copyfile(example_path, env_path)


def _ensure_frontend_built() -> None:
    web_dir = ROOT / "web"
    dist_index = web_dir / "dist" / "index.html"
    tsx_cli = web_dir / "node_modules" / "tsx" / "dist" / "cli.mjs"

    npm_cmd = "npm.cmd" if os.name == "nt" else "npm"
    if not tsx_cli.exists():
        print("[HF-Deploy] Installing web & Copilot Runtime dependencies (npm ci)...", flush=True)
        subprocess.run([npm_cmd, "ci", "--prefix", str(web_dir)], check=True)

    if not dist_index.exists():
        print("[HF-Deploy] Building React 3D Living Room frontend (npm run build)...", flush=True)
        subprocess.run([npm_cmd, "run", "build", "--prefix", str(web_dir)], check=True)


def _start_background_services(api_port: int, runtime_port: int) -> list[subprocess.Popen]:
    runtime_dir = ROOT / "artifacts" / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("MQTT_HOST", "127.0.0.1")
    os.environ.setdefault("MQTT_PORT", "1883")
    os.environ.setdefault("MQTT_TOPIC", "home/energy/ac")
    os.environ.setdefault("HEMS_MQTT_ROOT", "home/hems")
    os.environ.setdefault("HEMS_TRANSPORT", "mqtt")
    os.environ["HEMS_PYTHON_API_URL"] = f"http://127.0.0.1:{api_port}"
    os.environ["HEMS_COPILOT_RUNTIME_PORT"] = str(runtime_port)
    os.environ["HEMS_COPILOT_RUNTIME_URL"] = f"http://127.0.0.1:{runtime_port}"
    existing_pythonpath = os.environ.get("PYTHONPATH", "")
    os.environ["PYTHONPATH"] = f"{SRC_DIR}{os.pathsep}{existing_pythonpath}" if existing_pythonpath else str(SRC_DIR)

    processes: list[subprocess.Popen] = []

    # 1. Start Mosquitto MQTT Broker if not already listening on 127.0.0.1:1883
    mqtt_host = os.environ["MQTT_HOST"]
    mqtt_port = int(os.environ["MQTT_PORT"])
    if not _port_open(mqtt_host, mqtt_port):
        mosquitto_bin = shutil.which("mosquitto")
        if mosquitto_bin:
            print(f"[HF-Deploy] Starting Mosquitto MQTT Broker on {mqtt_host}:{mqtt_port}...", flush=True)
            out_log = open(runtime_dir / "mosquitto.out.log", "a", encoding="utf-8")
            err_log = open(runtime_dir / "mosquitto.err.log", "a", encoding="utf-8")
            processes.append(
                subprocess.Popen(
                    [mosquitto_bin, "-c", str(ROOT / "mosquitto" / "mosquitto.conf")],
                    cwd=str(ROOT),
                    stdout=out_log,
                    stderr=err_log,
                )
            )
            for _ in range(20):
                if _port_open(mqtt_host, mqtt_port):
                    break
                time.sleep(0.25)
        else:
            print("[HF-Deploy] WARNING: 'mosquitto' binary not found in PATH.", flush=True)

    # 2. Start Python virtual devices & resident controller
    for name in ("ac", "washer", "bulb", "control"):
        print(f"[HF-Deploy] Starting Python background service: {name}...", flush=True)
        out_log = open(runtime_dir / f"{name}.out.log", "a", encoding="utf-8")
        err_log = open(runtime_dir / f"{name}.err.log", "a", encoding="utf-8")
        processes.append(
            subprocess.Popen(
                [sys.executable, "-m", f"smart_home_energy.{name}"],
                cwd=str(ROOT),
                stdout=out_log,
                stderr=err_log,
                env=os.environ.copy(),
            )
        )

    # 3. Start Node.js Copilot Runtime
    node_bin = shutil.which("node")
    if node_bin:
        print(f"[HF-Deploy] Starting Copilot Runtime on 127.0.0.1:{runtime_port}...", flush=True)
        out_log = open(runtime_dir / "copilot-runtime.out.log", "a", encoding="utf-8")
        err_log = open(runtime_dir / "copilot-runtime.err.log", "a", encoding="utf-8")
        processes.append(
            subprocess.Popen(
                [node_bin, "node_modules/tsx/dist/cli.mjs", "runtime/server.ts"],
                cwd=str(ROOT / "web"),
                stdout=out_log,
                stderr=err_log,
                env=os.environ.copy(),
            )
        )
    else:
        print("[HF-Deploy] WARNING: 'node' binary not found in PATH.", flush=True)

    return processes


def main() -> None:
    _ensure_env_file()
    _ensure_frontend_built()

    api_port = int(os.getenv("PORT", os.getenv("GRADIO_SERVER_PORT", "7860")))
    runtime_port = int(os.getenv("HEMS_COPILOT_RUNTIME_PORT", "8503"))
    host = os.getenv("HEMS_HOST", os.getenv("GRADIO_SERVER_NAME", "0.0.0.0"))

    _start_background_services(api_port=api_port, runtime_port=runtime_port)

    from smart_home_energy.api import create_app

    fastapi_app = create_app()

    # Register a hidden Gradio Blocks with a @spaces.GPU callback so Hugging Face
    # ZeroGPU startup healthcheck detects the GPU function and keeps Uvicorn alive,
    # while "/" and "/api/*" still serve our React 3D Living Room and FastAPI routes.
    try:
        import gradio as gr
        import spaces

        @spaces.GPU
        def _zerogpu_ping(text: str = "ok") -> str:
            return text

        with gr.Blocks() as demo:
            btn = gr.Button("ping")
            out = gr.Textbox()
            btn.click(fn=_zerogpu_ping, inputs=out, outputs=out)

        demo.queue()
        # Mount Gradio on a sub-path AND expose its config routes so ZeroGPU supervisor sees it
        gr.mount_gradio_app(fastapi_app, demo, path="/_gradio_zerogpu")
        for route in demo.app.routes:
            route_path = getattr(route, "path", "")
            if route_path in ("/config", "/info", "/gradio_api/info"):
                fastapi_app.routes.insert(0, route)
        print("[HF-Deploy] Mounted ZeroGPU-compatible Gradio healthcheck routes.", flush=True)
    except Exception as exc:
        print(f"[HF-Deploy] Gradio ZeroGPU mount skipped: {exc}", flush=True)

    print(f"[HF-Deploy] Launching FastAPI + 3D Living Room on http://{host}:{api_port} ...", flush=True)
    uvicorn.run(fastapi_app, host=host, port=api_port)


if __name__ == "__main__":
    main()
