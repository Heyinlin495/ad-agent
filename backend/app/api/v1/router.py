"""v1 路由聚合。"""
from fastapi import APIRouter

from app.api.v1 import ads, health, history, kb, products

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(products.router)
api_router.include_router(kb.router)
api_router.include_router(ads.router)
api_router.include_router(history.router)
