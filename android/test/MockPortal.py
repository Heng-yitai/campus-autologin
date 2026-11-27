# -*- coding: utf-8 -*-
"""模拟 Dr.COM 门户，只在本机 127.0.0.1 上监听，用来测 Portal.java 的请求与解析。

按 user_password 的值决定返回哪种响应；每条请求的查询串都记到日志里，
方便断言"发出去的参数对不对"。
"""
import sys
import socketserver
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

PORT = int(sys.argv[1])
LOG = sys.argv[2]

RESP = {
    "ok": 'dr1234({"result":1,"msg":"Portal协议认证成功！"});',
    "online": 'dr1234({"result":0,"msg":"IP: 172.17.76.147 已经在线！","ret_code":2});',
    "bad": 'dr1234({"result":0,"msg":"账号或密码错误","ret_code":1});',
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(self.path + "\n")

        q = parse_qs(urlparse(self.path).query)
        pw = (q.get("user_password") or [""])[0]
        body = RESP.get(pw, "garbage-response").encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


socketserver.TCPServer.allow_reuse_address = True
with socketserver.TCPServer(("127.0.0.1", PORT), Handler) as srv:
    srv.serve_forever()
