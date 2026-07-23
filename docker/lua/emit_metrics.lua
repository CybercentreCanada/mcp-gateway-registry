-- emit_metrics.lua: Capture MCP request metrics in log_by_lua phase (no network I/O)
local ok, cjson = pcall(require, "cjson")
if not ok then return end

local metrics = ngx.shared.metrics_buffer
if not metrics then return end

-- Skip buffering when no collector is configured (avoids pointless writes that TTL-expire)
local metrics_url = os.getenv("METRICS_SERVICE_URL") or ""
if metrics_url == "" then return end

-- Server/agent name for attribution. A location block may set
-- $metrics_server_name explicitly (e.g. A2A agent blocks, whose URI first
-- segment is the shared "agent" prefix and would otherwise bucket every agent
-- together); otherwise derive it from the first URI path segment: /<server>/...
local server_name = ngx.var.metrics_server_name
if server_name == nil or server_name == "" then
    server_name = ngx.var.uri:match("^/([^/]+)/")
end
if not server_name or server_name == "" then return end

-- Parse JSON-RPC body from X-Body header (set by capture_body.lua in rewrite phase)
local method = "unknown"
local tool_name = ""
local body = ngx.req.get_headers()["X-Body"]
if body then
    local dok, parsed = pcall(cjson.decode, body)
    if dok and parsed.method then
        method = parsed.method
        if method == "tools/call" and parsed.params and parsed.params.name then
            tool_name = parsed.params.name
        end
    end
end

local entry = cjson.encode({
    m = method,
    s = server_name,
    t = tool_name,
    c = ngx.req.get_headers()["X-Client-Name"] or "unknown",
    ok = ngx.status < 400,
    d = (tonumber(ngx.var.upstream_header_time) or tonumber(ngx.var.request_time) or 0) * 1000,
})

local key = "m:" .. ngx.now() .. ":" .. ngx.worker.pid() .. ":" .. math.random(1, 999999)
local set_ok, set_err = metrics:set(key, entry, 300)
if not set_ok then
    ngx.log(ngx.ERR, "metrics emit: shared dict full, dropping metric: ", set_err)
end
