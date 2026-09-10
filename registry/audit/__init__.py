"""
Audit and Compliance Logging Package.

This package provides audit logging capabilities for the MCP Gateway Registry,
capturing API access and MCP server access events for compliance and security review.

Components:
- models: Pydantic models for audit log records
- service: AuditLogger class for async writing and rotation
- middleware: FastAPI middleware for request/response capture
- mcp_logger: MCPLogger class for MCP protocol-level logging
- routes: API endpoints for querying and exporting audit logs
"""

from .context import set_audit_action, set_audit_authorization
from .mcp_logger import MCPLogger
from .middleware import AuditMiddleware, add_audit_middleware
from .models import (
    SENSITIVE_QUERY_PARAMS,
    Action,
    Authorization,
    Identity,
    MCPRequest,
    MCPResponse,
    MCPServer,
    MCPServerAccessRecord,
    RegistryApiAccessRecord,
    Request,
    Response,
    mask_credential,
)
from .service import AuditLogger, NonDurableAuditError, enforce_durable_audit_sink

__all__ = [
    # Models
    "RegistryApiAccessRecord",
    "MCPServerAccessRecord",
    "MCPServer",
    "MCPRequest",
    "MCPResponse",
    "Identity",
    "Request",
    "Response",
    "Action",
    "Authorization",
    "mask_credential",
    "SENSITIVE_QUERY_PARAMS",
    # Service
    "AuditLogger",
    "NonDurableAuditError",
    "enforce_durable_audit_sink",
    # MCP Logger
    "MCPLogger",
    # Middleware
    "AuditMiddleware",
    "add_audit_middleware",
    # Context utilities
    "set_audit_action",
    "set_audit_authorization",
]
