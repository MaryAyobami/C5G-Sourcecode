#!/usr/bin/env python3
# Tiny PUT/POST sink so curl -T measures upload throughput.
# Usage: sudo python3 upload_server.py <bind_ip> <port>
import sys
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

class H(BaseHTTPRequestHandler):
    def do_PUT(self):
        n = int(self.headers.get('Content-Length', 0))
        rem = n
        while rem > 0:
            chunk = self.rfile.read(min(65536, rem))
            if not chunk: break
            rem -= len(chunk)
        self.send_response(200)
        self.send_header('Content-Length', '2')
        self.end_headers()
        self.wfile.write(b'OK')
    do_POST = do_PUT
    def log_message(self, *a, **k): pass

if __name__ == '__main__':
    ThreadingHTTPServer((sys.argv[1], int(sys.argv[2])), H).serve_forever()
