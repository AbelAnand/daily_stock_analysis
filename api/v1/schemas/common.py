# -*- coding: utf-8 -*-
"""
===================================
通用响应模型
===================================

职责：
1. 定义通用的响应模型（HealthResponse, ErrorResponse 等）
2. 提供统一的响应格式
"""

from typing import Optional, Any

from pydantic import BaseModel, ConfigDict, Field


class RootResponse(BaseModel):
    """API 根路由响应"""
    
    message: str = Field(..., description="API status message", json_schema_extra={"example": "Daily Stock Analysis API is running"})
    version: Optional[str] = Field(None, description="API version", json_schema_extra={"example": "1.0.0"})
    
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "message": "Daily Stock Analysis API is running",
            "version": "1.0.0"
        }
    })


class HealthResponse(BaseModel):
    """健康检查响应"""
    
    status: str = Field(..., description="Service status", json_schema_extra={"example": "ok"})
    timestamp: Optional[str] = Field(None, description="Timestamp")
    
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "status": "ok",
            "timestamp": "2024-01-01T12:00:00"
        }
    })


class ErrorResponse(BaseModel):
    """错误响应"""
    
    error: str = Field(..., description="Error type", json_schema_extra={"example": "validation_error"})
    message: str = Field(..., description="Error message", json_schema_extra={"example": "Invalid request parameters"})
    detail: Optional[Any] = Field(None, description="Additional error details")
    
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "error": "not_found",
            "message": "Resource not found",
            "detail": None
        }
    })


class SuccessResponse(BaseModel):
    """通用成功响应"""
    
    success: bool = Field(True, description="Whether the operation succeeded")
    message: Optional[str] = Field(None, description="Success message")
    data: Optional[Any] = Field(None, description="Response data")
    
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "success": True,
            "message": "Operation succeeded",
            "data": None
        }
    })
