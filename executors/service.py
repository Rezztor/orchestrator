"""What every executor shares: POST /v1/jobs with a Bearer job token and a JSON body, answered with JSON.

A subclass of JobHandler implements handle(body). ValueError answers 400 (a bad job), RuntimeError and a timeout
502 (the tool behind the executor failed).
"""
import hmac
import json
import os
import ssl
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class JobHandler(BaseHTTPRequestHandler):
    token = None
    timeout = 60  # seconds a connection may sit idle before a job arrives; a running job is not cut short

    def handle_job(self, body):
        raise NotImplementedError

    def reply(self, code, body):
        data = json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path != "/v1/jobs":
            return self.reply(404, {"error": "not found"})
        if not hmac.compare_digest(self.headers.get("Authorization", "").encode(), f"Bearer {self.token}".encode()):
            return self.reply(401, {"error": "bad job token"})
        length = self.headers.get("Content-Length", "")
        length = int(length) if length.isascii() and length.isdigit() else 0
        if not 0 < length <= 65536:
            return self.reply(413, {"error": "body must be 1 byte to 64 KiB"})
        try:
            return self.reply(200, self.handle_job(json.loads(self.rfile.read(length))))
        except (ValueError, AttributeError) as exc:
            return self.reply(400, {"error": str(exc)})
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            return self.reply(502, {"error": str(exc)})


class Server(ThreadingHTTPServer):
    """TLS, when `tls` holds a context, with each handshake in the connection's own thread under the handler's idle
    timeout: a handshake inside accept() let one client that never spoke stall every other connection."""
    tls = None

    def finish_request(self, request, client_address):
        if not self.tls:
            return super().finish_request(request, client_address)
        request.settimeout(self.RequestHandlerClass.timeout)
        try:
            with self.tls.wrap_socket(request, server_side=True) as conn:
                super().finish_request(conn, client_address)
        except OSError:  # a handshake that failed or never came
            pass


def serve(handler, prefix, default_listen):
    """Serve `handler` with PREFIX_TOKEN (32+ characters), PREFIX_LISTEN and optional PREFIX_CERT / PREFIX_KEY."""
    handler.token = os.environ[f"{prefix}_TOKEN"]
    if len(handler.token) < 32:
        raise SystemExit(f"{prefix}_TOKEN must be at least 32 characters")
    host, port = os.environ.get(f"{prefix}_LISTEN", default_listen).rsplit(":", 1)
    server = Server((host, int(port)), handler)
    if os.environ.get(f"{prefix}_CERT"):
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(os.environ[f"{prefix}_CERT"], os.environ[f"{prefix}_KEY"])
        server.tls = ctx
    server.serve_forever()
