import subprocess
import sys
import time
import webbrowser
import os
import socket
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parent
VENV_PYTHON = str(ROOT / ".venv" / "Scripts" / "python.exe")
urlopen = build_opener(ProxyHandler({})).open

def check_port(port):
    try:
        with socket.socket() as listener:
            if os.name == "nt":
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind(("127.0.0.1", port))
    except OSError:
        raise RuntimeError(f"端口 {port} 已被占用，请先停止此前启动的工作台，再重新运行。") from None


def wait_ready(process, url, name, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"{name}启动失败，请查看上方错误；也请确认没有重复启动占用端口。")
        try:
            with urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except (URLError, TimeoutError, OSError):
            pass
        time.sleep(0.2)
    raise RuntimeError(f"{name}未在 {timeout} 秒内就绪，请查看上方错误。")


def stop_process(process):
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
    else:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def main():
    api = ui = None
    print("正在启动售后工作台…\n", flush=True)
    try:
        subprocess.run([VENV_PYTHON, "-m", "scripts.local_pg", "ensure"], cwd=str(ROOT), check=True)
        check_port(8000)
        check_port(8501)
        api = subprocess.Popen(
            [VENV_PYTHON, "-m", "uvicorn", "src.main:app", "--host", "127.0.0.1", "--port", "8000"],
            cwd=str(ROOT),
        )
        wait_ready(api, "http://127.0.0.1:8000/api/v1/ready", "后端服务")
        print(f"   后端服务：http://localhost:8000（进程号 {api.pid}）", flush=True)
        ui = subprocess.Popen(
            [VENV_PYTHON, "-m", "streamlit", "run", "src/ui/streamlit_app.py",
             "--server.port", "8501", "--server.address", "127.0.0.1", "--server.headless", "true"],
            cwd=str(ROOT), stdout=subprocess.DEVNULL,
        )
        wait_ready(ui, "http://127.0.0.1:8501/_stcore/health", "售后工作台")
        print(f"   售后工作台：http://localhost:8501（进程号 {ui.pid}）", flush=True)
        webbrowser.open("http://localhost:8501")
        print("\n服务已启动，按 Ctrl+C 停止。\n", flush=True)
        while True:
            if api.poll() is not None or ui.poll() is not None:
                raise RuntimeError("服务意外停止，请查看上方错误并重新启动。")
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n正在停止服务…", flush=True)
        return 0
    except (subprocess.CalledProcessError, OSError, RuntimeError) as exc:
        message = "数据库检查未通过，工作台未启动。" if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        print(f"\n启动失败：{message}", file=sys.stderr, flush=True)
        return 1
    finally:
        stop_process(ui)
        stop_process(api)


if __name__ == "__main__":
    sys.exit(main())
