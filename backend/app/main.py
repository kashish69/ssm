from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app import mqtt_client
from app.db import init_db
from app.rate_limit import limiter
from app.routes import auth, capture, devices, images, upload, wifi


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    mqtt_client.start()
    yield
    mqtt_client.stop()


app = FastAPI(lifespan=lifespan)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

app.include_router(auth.router)
app.include_router(devices.router)
app.include_router(capture.router)
app.include_router(upload.router)
app.include_router(images.router)
app.include_router(wifi.router)

app.mount("/", StaticFiles(directory="static", html=True), name="static")
