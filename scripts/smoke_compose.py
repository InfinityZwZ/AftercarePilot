"""Real Compose build/start/seed/HTTP/down/up acceptance; never removes volumes."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

from dotenv import dotenv_values
import httpx

from scripts.configure_compose import write_configuration
from scripts.smoke_http import exercise_http, verify_persistence

ROOT = Path(__file__).resolve().parents[1]


def main():
    config = ROOT / ".local" / "compose.verify.env"
    write_configuration(config, verification=True)
    values = dotenv_values(config)
    if values.get("COMPOSE_PROJECT_NAME") != "aftercarepilot-verify" or values.get("OPENAI_API_KEY"):
        raise RuntimeError("Acceptance requires the isolated verification project and no model key")
    docker = shutil.which("docker")
    standalone = ROOT / ".local" / "tooling" / "compose" / "docker-compose.exe"
    if docker:
        prefix = [docker, "compose"]
    elif standalone.is_file():
        prefix = [str(standalone)]
    else:
        raise RuntimeError("Docker Compose is not installed; start Docker Desktop with Linux containers")
    prefix += ["--env-file", str(config), "-f", str(ROOT / "compose.yaml"), "-f", str(ROOT / "compose.verify.yaml")]
    def run(*args):
        with (ROOT / ".local" / "compose-validation.log").open("a", encoding="utf-8") as log:
            result = subprocess.run([*prefix, *args], cwd=ROOT, stdout=log, stderr=log, timeout=1200,
                                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        if result.returncode:
            raise RuntimeError("Compose operation failed; inspect ignored .local/compose-validation.log")
    run("config", "--quiet")
    run("ls", "--format", "json")  # Needs an actual daemon; config validation alone is insufficient.
    print("Building and starting the isolated verification stack (FakeLLM, real PG/MCP)...", flush=True)
    run("up", "--build", "--wait", "--wait-timeout", "180")
    run("exec", "-T", "api", "python", "-m", "scripts.seed_demo", "--database-name", "aftercare_demo")
    run("exec", "-T", "api", "python", "-m", "scripts.seed_demo", "--database-name", "aftercare_demo")
    base_url = f"http://127.0.0.1:{values['API_PORT']}/api/v1"
    token = values["OPERATOR_TOKEN"]
    artifact = ROOT / ".local" / "compose-verification.json"
    if artifact.exists():
        result = verify_persistence(base_url, token, json.loads(artifact.read_text(encoding="utf-8")))
    else:
        result = exercise_http(base_url, token)
        artifact.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    with httpx.Client(timeout=10) as client:
        client.get(f"http://127.0.0.1:{values['UI_PORT']}/_stcore/health").raise_for_status()
    print("Stopping containers without deleting the PostgreSQL volume, then starting again...", flush=True)
    run("down")
    run("up", "--wait", "--wait-timeout", "180")
    result = verify_persistence(base_url, token, result)
    result.update(transport="real_compose_http_stdio", ui_health="passed", seed_repeat="passed")
    artifact.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # No environment values, HTTP body, driver traceback or tokens in the terminal.
        print("Compose acceptance incomplete: " + (str(error) if isinstance(error, RuntimeError) else "verification failed; inspect local logs"), file=sys.stderr)
        sys.exit(1)
