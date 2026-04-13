import uvicorn

from net_grading.app import app
from net_grading.config import get_settings


def run() -> None:
    s = get_settings()
    uvicorn.run(
        "net_grading.app:app",
        host=s.app_host,
        port=s.app_port,
        log_level=s.log_level.lower(),
        reload=s.app_env == "development",
    )


if __name__ == "__main__":
    run()


__all__ = ["app", "run"]
