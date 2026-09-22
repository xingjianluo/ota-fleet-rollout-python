"""服务入口：uvicorn main:app --reload。"""

from ota.api import create_app

app = create_app()
