from fastapi import FastAPI

from api.routes import install_error_handlers, router as api_router

app = FastAPI(title="OTA Fleet Rollout", version="0.1.0")
app.include_router(api_router)
install_error_handlers(app)
