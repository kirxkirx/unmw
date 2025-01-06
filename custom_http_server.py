#!/usr/bin/env python3

import os
import sys # for sys.exit()
from http.server import HTTPServer, CGIHTTPRequestHandler

# Exit if the script is run via a CGI request
if "REQUEST_METHOD" in os.environ:
    print("This script cannot be run via a web request.", file=sys.stderr)
    sys.exit(1)

class CustomCGIHTTPRequestHandler(CGIHTTPRequestHandler):
    def translate_path(self, path):
        # Get the initial translation (without resolving symlinks)
        untranslated_path = super().translate_path(path)
        
        # Resolve symlinks
        return os.path.realpath(untranslated_path)

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('--bind', '-b', default='', metavar='ADDRESS',
                        help='Specify alternate bind address [default: all interfaces]')
    parser.add_argument('port', action='store', default=8080, type=int, nargs='?',
                        help='Specify alternate port [default: 8080]')
    args = parser.parse_args()
    
    # Set the environment variable needed for period search scripts - lk
    os.environ['HTTP_HOST'] = 'kirx.net/ticaariel'
    
    server_address = (args.bind, args.port)
    httpd = HTTPServer(server_address, CustomCGIHTTPRequestHandler)
    print(f"Serving HTTP on {args.bind} port {args.port} (http://{args.bind}:{args.port}/) ...")
    httpd.serve_forever()
