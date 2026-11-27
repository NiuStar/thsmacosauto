"""Bounded authenticated loopback HTTP; GUI/engine imports are runtime-only."""
import argparse
from contextlib import nullcontext
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
from socketserver import TCPServer
import stat
import sys
import threading
import time
from urllib.parse import parse_qsl, urlsplit

MAX_BODY = 32768
MAX_DEPTH = 32
GET_PATHS = frozenset({"/v1/health", "/v1/capabilities", "/v1/real/native-current-panel",
                       "/v1/real/positions", "/v1/real/orders", "/v1/real/trades",
                       "/v1/real/positions-scope-diagnostic", "/v1/real/positions-panel",
                       "/v1/real/account-observation", "/v1/real/orders/status",
                       "/v1/real/form-observation", "/v1/real/form-discovery"})
POST_KEYS = {
    "/v1/operator/trade-page": frozenset({"action"}),
    "/v1/shadow/evidence": frozenset({"signal_id"}),
    "/v1/real/orders/submit": frozenset({"client_order_id", "mode", "shareholder_scope", "order_type", "symbol", "side", "price", "quantity"}),
    "/v1/auto/buy": frozenset({"request_id", "signal_id"}),
    "/v1/control/stop": frozenset(),
    "/v1/control/resume": frozenset(),
}
# Only fixed public messages/statuses cross the exception boundary. No str(exc).
ERRORS = {
    "READ_BUDGET_EXCEEDED": (503, "Read budget exceeded."),
    "READ_BUDGET_UNSUPPORTED": (503, "Read budget is unsupported."),
    "UNAUTHORIZED": (401, "Authentication required."),
    "FORBIDDEN_ORIGIN": (403, "Origin is not permitted."),
    "BAD_REQUEST": (400, "Invalid request."),
    "INVALID_JSON": (400, "Invalid JSON object."),
    "NOT_FOUND": (404, "Route not found."),
    "METHOD_NOT_ALLOWED": (405, "Method is not permitted."),
    "LENGTH_REQUIRED": (411, "Content-Length is required."),
    "BODY_TOO_LARGE": (413, "Request body is too large."),
    "UNSUPPORTED_MEDIA_TYPE": (415, "JSON content type is required."),
    "REQUEST_TIMEOUT": (408, "Request read timed out."),
    "BUSY": (409, "Service is busy."),
    "HTTP_BUSY": (503, "HTTP capacity is exhausted."),
    "BLOCKED": (409, "Operation is blocked."),
    "INVALID_REQUEST": (400, "Invalid request."),
    "INTERNAL_ERROR": (500, "Request failed."),
}


# Frozen engine codes: metadata may select one of these literals, never message text.
for _code in ("INVALID_REQUEST_ID", "INVALID_STATUS_QUERY", "INVALID_CONTROL_PAYLOAD",
              "INVALID_AUTO_PAYLOAD", "INVALID_MANUAL_FIELDS", "EXPLICIT_MANUAL_LIMIT_REQUIRED",
              "INVALID_SHAREHOLDER_SCOPE", "INVALID_LIMIT_ORDER"):
    ERRORS[_code] = (400, "Invalid request.")
ERRORS["ORDER_NOT_FOUND"] = (404, "Order not found.")
ERRORS['SHADOW_EXECUTION_FORBIDDEN'] = (409, "Shadow observation cannot execute.")
ERRORS['TRANSACTION_MODE_REQUIRED'] = (409, "Transaction operation mode is required.")
ERRORS['TRANSACTION_STATE_UNAVAILABLE'] = (503, "Transaction state is unavailable.")
for _code in ("ENGINE_CLOSED", "BACKEND_UNAVAILABLE", "CONTROL_PERSISTENCE_FAILED"):
    ERRORS[_code] = (503, "Service is unavailable.")
for _code in ("ENGINE_FENCED", "AUTOMATIC_STOPPED", "INVALID_PRESS_PERMIT", "STALE_CAPTURE",
              "DURABLE_PRESS_STATE_REQUIRED", "BACKEND_PRESS_GUARD_NOT_CALLED", "REAL_IDENTITY_UNPROVEN",
              "INCOMPLETE_ORDER_SCOPE", "ORIGINAL_PANEL_UNPROVEN", "FORM_CONTROL_SCOPE_UNPROVEN",
              "FORM_SCOPE_MISMATCH", "STALE_FORM", "TRUSTED_SIGNAL_PROVIDER_UNAVAILABLE",
              "TRUSTED_SIGNAL_INVALID", "STALE_SIGNAL", "SHAREHOLDER_SCOPE_MISMATCH",
              "SIGNAL_DAY_MISMATCH", "INSUFFICIENT_AVAILABLE_CASH", "ACCOUNT_UNKNOWN", "REQUEST_ID_CONFLICT",
              "BLOCKED_PROFILE_UNQUALIFIED", "NATIVE_TRADE_DISABLED", "NAVIGATION_DISABLED",
              "MACOS_REQUIRED", "AX_NOT_TRUSTED", "FRAMEWORK_SOURCE_MISMATCH", "APP_IDENTITY_MISMATCH",
              "APP_COUNT_MISMATCH", "APP_EXECUTABLE_MISMATCH", "PROCESS_OWNER_UNVERIFIED",
              "REAL_BROKER_IDENTITY_UNPROVEN", "ACCOUNT_SCOPE_NOT_CONFIGURED", "SELL_RULE_NOT_CONFIGURED"):
    ERRORS[_code] = (409, "Operation is blocked.")


_GUI_OWNER_CODES = frozenset({
    'GUI_OWNER_NOT_HELD', 'GUI_OWNER_PATH_UNSAFE', 'GUI_OWNER_DIRECTORY_NOT_PRIVATE',
    'GUI_OWNER_LOCK_UNSAFE', 'GUI_OWNER_IDENTITY_CHANGED', 'GUI_INCUMBENT_MARKER_PRESENT',
    'GUI_OWNER_PROOF_FAILED', 'GUI_OWNER_BUSY', 'GUI_OWNER_ACQUIRE_FAILED',
    'GUI_CANDIDATE_DISABLED', 'GUI_WRITER_INVENTORY_REQUIRED', 'GUI_OWNER_CONFIG_INVALID',
})
for _code in _GUI_OWNER_CODES:
    ERRORS[_code] = (503, "GUI ownership is unavailable.")

for _code in ('FORM_ASSOCIATION_UNPROVEN', 'NATIVE_CONTINUITY_CHANGED', 'STALE_READ_CAPTURE'):
    ERRORS[_code] = (503, "Form observation is unavailable.")

for _code in ('FIXED_SCOPE_CHANGED','FIXED_SCOPE_INCOMPLETE_OBSERVATION','FIXED_POLICY_UNAVAILABLE'):
    ERRORS[_code] = (503, "Fixed account observation is unavailable.")


# NativeError is always 503. Enumerate its reviewed native producer codes;
# do not accept an arbitrary exception's chosen code/status.
NATIVE_ERROR_CODES=frozenset('''READ_BUDGET_EXCEEDED FIXED_SCOPE_INCOMPLETE_OBSERVATION
GUI_OWNER_PATH_UNSAFE GUI_OWNER_DIRECTORY_NOT_PRIVATE GUI_OWNER_LOCK_UNSAFE GUI_OWNER_IDENTITY_CHANGED
GUI_INCUMBENT_MARKER_PRESENT GUI_OWNER_PROOF_FAILED ACCOUNT_SCOPE_NOT_CONFIGURED APP_COUNT_MISMATCH APP_EXECUTABLE_MISMATCH
APP_IDENTITY_MISMATCH AX_NOT_TRUSTED AX_TRAVERSAL_LIMIT BASELINE_OR_INTENT_MISMATCH
BLOCKED_PROFILE_UNQUALIFIED BROKER_DAY_CHANGED CASH_ASSOCIATION_UNPROVEN CONFIRMATION_MISMATCH
FIXED_POLICY_CONFIG_CONFLICT FIXED_POLICY_UNAVAILABLE FIXED_SCOPE_CHANGED FORM_ASSOCIATION_UNPROVEN
FORM_VALUE_MISMATCH FRAMEWORK_SOURCE_MISMATCH GUI_OWNER_NOT_HELD GUI_OWNER_THREAD_MISMATCH
INCOHERENT_OR_DUPLICATE_POSITION INCOMPLETE_ORDER_SCOPE INSUFFICIENT_CASH INSUFFICIENT_SELLABLE
INVALID_INDEPENDENT_TRADE_EVIDENCE INVALID_NATIVE_INTENT INVALID_NATIVE_POLICY LIMIT_FORM_LABELS_UNPROVEN
MACOS_REQUIRED NATIVE_ATTEMPT_ALREADY_OWNED NATIVE_ATTEMPT_ID_ALREADY_USED NATIVE_ATTEMPT_STATE_MISMATCH
NATIVE_CONTENT_SHA_MISMATCH NATIVE_CONTINUITY_CHANGED NATIVE_ROW_KEYS_MISMATCH NATIVE_SESSION_IDENTITY_CHANGED
NATIVE_SESSION_UNCERTAIN NATIVE_TABLE_UNSTABLE NATIVE_TRADE_DISABLED NAVIGATION_DISABLED OPPOSITE_FORM_VISIBLE
ORDER_FIELDS_AMBIGUOUS ORDER_FIELD_UNSAFE ORIGINAL_FORM_UNREADABLE PANEL_CHANGED PRESS_ACTION_UNAVAILABLE
POSITIONS_PANEL_REQUIRED PRESS_CONTROL_UNSAFE PRESS_GUARD_REQUIRED PRESS_PERMISSION_DENIED PROCESS_OWNER_UNVERIFIED
READ_BUDGET_UNSUPPORTED REAL_BROKER_IDENTITY_UNPROVEN REQUESTED_MARKET_SHAREHOLDER_UNPROVEN
REFRESH_DISABLED REFRESH_HOLDINGS_REQUIRED REFRESH_STRUCTURE_UNPROVEN REFRESH_CONTROL_UNQUALIFIED
REFRESH_CONTROL_CHANGED REFRESH_ALREADY_CLAIMED REFRESH_TOKEN_INVALID
RESTORE_CONFIRMATION_PENDING RESTORE_FORM_POLICY_DISABLED RESTORE_NAVIGATION_DISABLED RESTORE_UNVERIFIED
SELL_RULE_NOT_CONFIGURED SHAREHOLDER_SCOPE_MISMATCH SHAREHOLDER_SCOPE_UNPROVEN_EMPTY_POSITIONS
STALE_BASELINE STALE_READ_CAPTURE UNSUPPORTED_READ_KIND'''.split())
NATIVE_ERRORS={code:(503, 'Native observation is unavailable.') for code in NATIVE_ERROR_CODES}

class Rejected(Exception):
    def __init__(self, code):
        self.code = code


def _diagnostic_metadata(value):
    """Only the native generated ID and explicit admission label cross HTTP."""
    rid = value.get('diagnostic_request_id')
    status = value.get('diagnostic_status')
    if type(rid) is str and re.fullmatch(r'[0-9a-f]{32}', rid):
        return {'diagnostic_request_id':rid,
                'diagnostic_status':status if status == 'ACCEPTED' else 'NOT_RECORDED'}
    return {}


def _http_diagnostic(engine):
    hook = getattr(engine, 'record_http_rejection', None)
    if not callable(hook):
        return {}
    try:
        return _diagnostic_metadata(hook())
    except Exception:
        return {'diagnostic_status':'NOT_RECORDED'}


def _reject_json(_value):
    raise ValueError("Non-integer JSON number")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate key")
        result[key] = value
    return result


def strict_json(raw):
    """Bound bytes, reject excessive container nesting before recursive decoding."""
    if len(raw) > MAX_BODY:
        raise ValueError("Body limit")
    text = raw.decode("utf-8", errors="strict")
    depth = 0
    quoted = escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > MAX_DEPTH:
                raise ValueError("Depth limit")
        elif char in "]}":
            depth -= 1
    value = json.loads(text, object_pairs_hook=_pairs, parse_float=_reject_json,
                       parse_constant=_reject_json)
    if not isinstance(value, dict):
        raise ValueError("Object required")
    # Reject escaped unpaired surrogates even though Python's JSON decoder accepts them.
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, str):
            item.encode("utf-8", errors="strict")
    return value


def _load_token(filename):
    """Walk canonical absolute components with O_NOFOLLOW, including directories."""
    try:
        if not isinstance(filename, str) or not filename.startswith("/"):
            raise ValueError
        path = Path(filename)
        if str(path) != filename or any(part in (".", "..") for part in filename.split("/")):
            raise ValueError
        if str(path.resolve(strict=True)) != filename:
            raise ValueError
        parts = path.parts[1:]
        if not parts:
            raise ValueError
        directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in parts[:-1]:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = next_fd
            parent = os.fstat(directory)
            if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
                raise ValueError
            descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                info = os.fstat(descriptor)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                        or not 1 <= info.st_size <= 4096):
                    raise ValueError
                raw = os.read(descriptor, 4097)
            finally:
                os.close(descriptor)
        finally:
            os.close(directory)
        token = raw.removesuffix(b"\n")
        if not token or len(token) > 4096 or not all(33 <= char <= 126 for char in token):
            raise ValueError
        return b"Bearer " + token
    except (OSError, ValueError, TypeError):
        raise ValueError("Invalid private token_file") from None


def _config(config):
    host = config.get("host", "127.0.0.1")
    try:
        address = ipaddress.ip_address(host)
    except (ValueError, TypeError):
        raise ValueError("A literal loopback host is required") from None
    if not address.is_loopback:
        raise ValueError("Only loopback binding is permitted")
    port = config.get("port", 0)
    workers = config.get("max_http_workers", 8)
    timeout = config.get("read_timeout_seconds", 5)
    if type(port) is not int or not 0 <= port <= 65535 or port == 18859:
        raise ValueError("Invalid development port")
    if type(workers) is not int or not 1 <= workers <= 64:
        raise ValueError("Invalid HTTP worker limit")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 30:
        raise ValueError("Invalid read deadline")
    origins = config.get("allowed_origins", [])
    if not isinstance(origins, list) or any(not isinstance(value, str) for value in origins):
        raise ValueError("Invalid origins")
    for value in origins:
        try:
            parsed = urlsplit(value)
            valid = (value.isascii() and not any(char.isspace() for char in value)
                     and "*" not in value and parsed.scheme in ("https", "http")
                     and parsed.hostname and not parsed.username and not parsed.password
                     and parsed.path == "" and not parsed.query and not parsed.fragment
                     and value == parsed.scheme + "://" + parsed.netloc)
            parsed.port  # Validate port syntax.
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("Origins must be exact serialized origins")
    return host, port, workers, timeout, frozenset(origins)


class BoundedHTTPServer(ThreadingHTTPServer):
    """Reserve capacity before creating a thread; no per-client waiter threads."""
    daemon_threads = False
    block_on_close = True
    allow_reuse_address = False
    request_queue_size = 16

    def server_bind(self):
        """Keep numeric loopback metadata without HTTPServer's reverse DNS."""
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            # Pre-auth overload is deliberately content-free: no route/engine info.
            body = json.dumps({'code':'HTTP_BUSY', 'message':'HTTP capacity is exhausted.',
                               **_http_diagnostic(self.engine)}).encode('ascii')
            wire = (b"HTTP/1.0 503 Service Unavailable\r\nConnection: close\r\n"
                    b"Content-Type: application/json\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
            try:
                request.settimeout(0.05)
                request.sendall(wire)
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, *_args):
        pass  # socketserver default traceback could disclose engine exception data.


class _HeaderReader:
    """Track the actual terminator; stdlib accepts EOF as a header terminator."""
    def __init__(self, stream):
        self.stream = stream
        self.complete = False
        self.invalid = False
        self.total = 0

    def readline(self, size=-1):
        line = self.stream.readline(min(size if size >= 0 else 65537, 65537))
        self.total += len(line)
        if self.total > MAX_BODY:
            raise ValueError("Header budget")
        if line == b"\r\n":
            self.complete = True
        elif (not line.endswith(b"\r\n") or line[:1] in (b" ", b"\t")
              or not re.fullmatch(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+", line.split(b":", 1)[0])
              or b":" not in line):
            self.invalid = True
        return line


class RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "THSLoopback"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(self.server.read_timeout)
        # Absolute read deadline stops slow trickles from retaining slots forever.
        # Engine dispatch has no socket deadline/cancellation: only shutdown(SHUT_RD).
        self._read_timer = threading.Timer(self.server.read_timeout, self._expire_read)
        self._read_timer.daemon = True
        self._read_timer.start()

    def parse_request(self):
        stream = self.rfile
        reader = _HeaderReader(stream)
        self.rfile = reader
        try:
            try:
                parsed = super().parse_request()
            except (ValueError, TimeoutError, OSError):
                self.send_error(400)
                return False
        finally:
            self.rfile = stream
        if not parsed:
            return False
        if not reader.complete or reader.invalid:
            self.send_error(400)
            return False
        return True

    def _expire_read(self):
        try:
            self.connection.shutdown(socket.SHUT_RD)
        except OSError:
            pass

    def finish(self):
        self._read_timer.cancel()
        self._read_timer.join()
        super().finish()

    def log_message(self, *_args):
        pass

    def _reply(self, status, value):
        data = json.dumps(value, ensure_ascii=True, allow_nan=False).encode("ascii")
        self.close_connection = True
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            if getattr(self, "command", None) != "HEAD":
                self.wfile.write(data)
        except OSError:
            pass

    def _error(self, code, diagnostic=None):
        status, message = ERRORS[code]
        metadata = _http_diagnostic(self.server.engine) if diagnostic is None else diagnostic
        self._reply(status, {"code": code, "message": message, **metadata})

    def send_error(self, _code, message=None, explain=None):
        # Parsing errors and unknown methods never use reflected HTML/error text.
        headers = getattr(self, "headers", None)
        self._error("BAD_REQUEST" if headers is not None and self._authenticated() else "UNAUTHORIZED")

    def _authenticated(self):
        values = self.headers.get_all("Authorization", [])
        if len(values) != 1:
            return False
        try:
            return hmac.compare_digest(values[0].encode("ascii"), self.server.authorization)
        except UnicodeError:
            return False

    def handle_expect_100(self):
        # No interim response before auth. Actual bodies only after all checks.
        return True

    def __getattr__(self, name):
        if name.startswith("do_"):
            return self._handle
        raise AttributeError(name)

    get_paths = GET_PATHS
    post_keys = POST_KEYS

    def _route(self):
        if self.command not in ("GET", "POST"):
            raise Rejected("METHOD_NOT_ALLOWED")
        # BaseHTTPRequestHandler normalizes a leading //; reject original alias.
        target = self.requestline.split()[1]
        if target != self.path or not target.startswith("/") or target.startswith("//"):
            raise Rejected("NOT_FOUND")
        parsed = urlsplit(target)
        path = parsed.path
        if parsed.fragment or "#" in target or "%" in path:
            raise Rejected("BAD_REQUEST")
        if path not in self.get_paths and path not in self.post_keys:
            raise Rejected("NOT_FOUND")
        if path == '/v1/shadow/evidence' and getattr(self.server, 'runtime_mode', None) != 'SHADOW':
            raise Rejected('NOT_FOUND')
        if (self.command == "GET") != (path in self.get_paths):
            raise Rejected("METHOD_NOT_ALLOWED")
        query_payload = None
        if path == "/v1/real/orders/status":
            try:
                if re.search(r"%(?![0-9A-Fa-f]{2})", parsed.query):
                    raise ValueError
                values = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True,
                                   encoding="utf-8", errors="strict", max_num_fields=1)
                if len(values) != 1 or values[0][0] != "client_order_id":
                    raise ValueError
                identifier = values[0][1]
                if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", identifier):
                    raise ValueError
                query_payload = {"client_order_id": identifier}
            except (ValueError, UnicodeError):
                raise Rejected("BAD_REQUEST") from None
        elif "?" in target:
            raise Rejected("BAD_REQUEST")
        return path, query_payload

    def _body(self, path):
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) > 1 or self.headers.get_all("Transfer-Encoding", []):
            raise Rejected("BAD_REQUEST")
        if self.headers.get_all("Expect", []):
            raise Rejected("BAD_REQUEST")
        if lengths and not re.fullmatch(r"[0-9]{1,10}", lengths[0]):
            raise Rejected("BAD_REQUEST")
        length = int(lengths[0]) if lengths else 0
        if length > MAX_BODY or (path == '/v1/operator/trade-page' and length > 64):
            raise Rejected("BODY_TOO_LARGE")
        if self.command == "GET":
            if length:
                raise Rejected("BAD_REQUEST")
            return None
        if not lengths:
            raise Rejected("LENGTH_REQUIRED")
        types = self.headers.get_all("Content-Type", [])
        if len(types) != 1 or types[0].lower() not in ("application/json", "application/json; charset=utf-8"):
            raise Rejected("UNSUPPORTED_MEDIA_TYPE")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise Rejected("REQUEST_TIMEOUT")
        try:
            payload = strict_json(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise Rejected("INVALID_JSON") from None
        if set(payload) - self.post_keys[path]:
            raise Rejected("BAD_REQUEST")
        if path == '/v1/operator/trade-page' and (set(payload) != {'action'}
                or type(payload['action']) is not str or payload['action'] not in ('inspect','switch','login-inspect','login-open','ensure-login','retry-login','fresh-login')):
            raise Rejected('BAD_REQUEST')
        return payload

    def _handle(self):
        diagnostic = {}
        if not self._authenticated():
            self._error("UNAUTHORIZED")
            return
        try:
            path, query = self._route()
            if self.command == "POST":
                origins = self.headers.get_all("Origin", [])
                if len(origins) > 1 or (origins and origins[0] not in self.server.allowed_origins):
                    raise Rejected("FORBIDDEN_ORIGIN")
            body = self._body(path)
            self._read_timer.cancel()
            self._read_timer.join()
            # Preserve the established Engine status-query seam after strict validation.
            dispatch_path = path
            if query is not None:
                dispatch_path += "?client_order_id=" + query["client_order_id"]
            payload = None if self.command == "GET" else body
            # Only the Engine-owned, call-scoped seam supplies correlation. Never
            # trust arbitrary exception attributes, including ServiceError IDs.
            context = getattr(self.server.engine, '_http_diagnostics', None)
            with context() if callable(context) else nullcontext({}) as diagnostic:
                result = self.server.engine.dispatch(self.command, dispatch_path, payload)
            if not isinstance(result, dict):
                raise TypeError
            self._reply(200, result)
        except Rejected as error:
            self._error(error.code)
        except (TimeoutError, socket.timeout):
            self._error("REQUEST_TIMEOUT", _diagnostic_metadata(diagnostic) or None)
        except Exception as error:
            # ServiceError is intentionally duck-typed; engine import stays lazy.
            code = getattr(error, "code", None)
            status = getattr(error, "http_status", None)
            metadata = _diagnostic_metadata(diagnostic)
            # Keep existing Engine/transaction status contracts intact; only the
            # actual native producer's typed errors use its fixed 503 registry.
            native_type=getattr(sys.modules.get(__package__+'.native_backend'),'NativeError',None)
            if native_type is not None and isinstance(error,native_type) and isinstance(code,str) and code in NATIVE_ERRORS and status==503:
                self._reply(503,{'code':code,'message':NATIVE_ERRORS[code][1],**metadata})
            elif isinstance(code, str) and code in ERRORS and status == ERRORS[code][0]:
                self._error(code, metadata or None)
            else:
                self._error("INTERNAL_ERROR", metadata or None)

    do_GET = _handle
    do_POST = _handle


def make_server(config, engine, *, handler=RequestHandler, token_loader=None):
    """Validate, bind a loopback socket, return unstarted server; caller owns engine."""
    host, port, workers, timeout, origins = _config(config)
    authorization = (token_loader or _load_token)(config.get("token_file"))
    cls = BoundedHTTPServer
    if ipaddress.ip_address(host).version == 6:
        class IPv6Server(BoundedHTTPServer):
            address_family = socket.AF_INET6
        cls = IPv6Server
    server = cls((host, port), handler, bind_and_activate=False)
    server.authorization = authorization
    server.engine = engine
    server.runtime_mode = config.get('runtime_mode')
    server.allowed_origins = origins
    server.read_timeout = timeout
    server._slots = threading.BoundedSemaphore(workers)
    try:
        server.server_bind()
        server.server_activate()
    except BaseException:
        server.server_close()
        raise
    return server


class Service:
    """One HTTP/GUI lifetime. Failed close is terminal, not reusable.

    Drain handlers and the GUI worker before releasing the lease. Administrators
    must exit this process after a cleanup failure, not restart it in-place.
    """
    def __init__(self, lease):
        self.lease = lease
        self.engine = self.server = self.control = None
        self.writer = None
        self.writer_shutdown_timeout = 1
        self.writer_shutdown = None
        self._closed = False
        self._close_failed = False
        self._close_lock = threading.Lock()

    def close(self):
        with self._close_lock:
            if self._closed:return
            if self._close_failed:raise RuntimeError('SERVICE_TEARDOWN_INCOMPLETE')
            self._close_failed = True
            if self.engine is not None:
                # Stop fresh admissions even when a later drain/close fails.
                with self.engine._lifecycle:self.engine._closed=True
            failure = None
            drained = True
            def attempt(stage, action):
                nonlocal failure
                try:action();return True
                except BaseException as error:
                    import logging
                    logging.getLogger(__name__).error('service %s: %s',stage,type(error).__name__)
                    if failure is None:failure=error
                    return False
            # HTTP handlers may still dispatch. If their drain fails, no lower
            # resource is safe to close and the lease stays with this Service.
            if self.server is not None and not attempt('http_close',self.server.server_close):
                drained=False
            if drained:
                control_closed = self.control is None or attempt('control_close',self.control.close)
                control_drained = control_closed or (getattr(self.control,'thread',None) is not None and not self.control.thread.is_alive())
                drained = control_drained
                if drained and self.engine is not None:
                    attempt('engine_close',self.engine.close)
                    # Explicit ordinary join completion, not a failed join's
                    # potentially misleading thread liveness observation.
                    drained = self.engine._worker_drained
                if self.writer is not None:
                    def close_writer():
                        self.writer_shutdown=self.writer.shutdown(self.writer_shutdown_timeout)
                        if not self.writer_shutdown.joined or self.writer_shutdown.status!='CLOSED':
                            raise RuntimeError('TELEMETRY_TEARDOWN_INCOMPLETE')
                    attempt('telemetry_close',close_writer)
                # A stopped control lane cannot issue another GUI request.
                if drained and control_drained and self.lease is not None:
                    attempt('lease_close',self.lease.close)
            if failure is not None:
                if not isinstance(failure,Exception):raise failure
                raise RuntimeError('SERVICE_TEARDOWN_INCOMPLETE') from None
            if not drained:raise RuntimeError('SERVICE_TEARDOWN_INCOMPLETE')
            self._closed = True
