import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .api import router as api_router
from .config import settings
from .errors import AppError
from .web import router as web_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(title=settings.app_name)

templates = Jinja2Templates(directory="app/templates")

app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(api_router)
app.include_router(web_router)


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    # 提取第一条校验错误并给出可读的中文提示
    first = exc.errors()[0] if exc.errors() else {}
    loc = ".".join(str(p) for p in first.get("loc", []) if p not in ("body", "query"))
    raw = first.get("msg", "请求参数不合法")
    messages = []
    for err in exc.errors():
        where = ".".join(str(p) for p in err.get("loc", []) if p not in ("body", "query"))
        messages.append(f"{where}：{err.get('msg')}" if where else err.get("msg", ""))
    detail = "；".join(m for m in messages if m) or raw
    return JSONResponse(
        status_code=422,
        content={"error": {"code": "validation_failed", "message": f"输入校验未通过：{detail}", "detail": exc.errors()}},
    )


@app.get("/healthz", tags=["meta"])
def healthz():
    return {"status": "ok"}
