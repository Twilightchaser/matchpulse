"""Run from the project folder: python check_connection.py. Never prints credentials."""
from app.main import llm_configured
from app.llm import DeepSeekRuntime

if __name__ == "__main__":
    if not llm_configured():
        print("Missing LLM_API_KEY or LLM_MODEL in .env / matchpulse.env")
        raise SystemExit(1)
    runtime = None
    try:
        runtime = DeepSeekRuntime()
        print(f"Transport: {runtime.transport} (Microsoft Agent Framework expected)")
        reply = runtime.ping()
        print("DeepSeek connection OK" if reply else "Empty response")
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}")
        raise SystemExit(1)
    finally:
        if runtime is not None:
            runtime.close()
