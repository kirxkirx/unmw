#!/usr/bin/env python3

import os
import sys
import subprocess
from http.server import HTTPServer, CGIHTTPRequestHandler

# Exit if the script is run via a CGI request
if "REQUEST_METHOD" in os.environ:
    print("This script cannot be run via a web request.", file=sys.stderr)
    sys.exit(1)


def is_wsl():
    """Check if running under WSL."""
    try:
        with open('/proc/version', 'r') as f:
            version = f.read().lower()
            return 'microsoft' in version or 'wsl' in version
    except Exception:
        return False


# Check once at startup
IS_WSL = is_wsl()


class CustomCGIHTTPRequestHandler(CGIHTTPRequestHandler):
    cgi_directories = ["/cgi-bin"]  # Keep the default directories

    def is_cgi(self):
        # Allow specific files like /upload.py to be treated as CGI
        if self.path.startswith("/upload.py"):
            # Handle query strings
            script = self.path.split('?')[0][1:]  # Remove leading /
            self.cgi_info = "", script
            return True
        return super().is_cgi()

    def translate_path(self, path):
        # Get the initial translation (without resolving symlinks)
        untranslated_path = super().translate_path(path)

        # Resolve symlinks
        return os.path.realpath(untranslated_path)

    def run_cgi(self):
        """Execute CGI script.

        On WSL2, Python's default CGI execution via os.execve() fails with
        'Permission denied' even on files with 0777 permissions. This appears
        to be a WSL2-specific bug with execve() after fork(). We work around
        this by using subprocess instead of the default fork+execve approach.

        On native Linux/macOS, we use the standard CGI handler which is more
        robust and handles edge cases better.
        """
        if not IS_WSL:
            # Use the standard implementation on native systems
            return super().run_cgi()

        # WSL2-specific subprocess-based execution
        self._run_cgi_subprocess()

    def _run_cgi_subprocess(self):
        """WSL2 workaround: run CGI via subprocess instead of fork+execve."""
        dir, rest = self.cgi_info
        script = rest.split('?')[0]
        scriptfile = self.translate_path('/' + script)

        if not os.path.exists(scriptfile):
            self.send_error(404, "CGI script not found")
            return

        # Build environment
        env = os.environ.copy()
        env['SERVER_SOFTWARE'] = self.version_string()
        env['SERVER_NAME'] = self.server.server_name
        env['GATEWAY_INTERFACE'] = 'CGI/1.1'
        env['SERVER_PROTOCOL'] = self.protocol_version
        env['SERVER_PORT'] = str(self.server.server_port)
        env['REQUEST_METHOD'] = self.command
        env['SCRIPT_NAME'] = '/' + script
        env['PATH_INFO'] = ''
        env['PATH_TRANSLATED'] = scriptfile
        env['QUERY_STRING'] = rest.split('?')[1] if '?' in rest else ''
        env['REMOTE_ADDR'] = self.client_address[0]

        # Handle content type and length for POST
        if self.command == 'POST':
            content_type = self.headers.get('Content-Type', '')
            content_length = self.headers.get('Content-Length', '0')
            env['CONTENT_TYPE'] = content_type
            env['CONTENT_LENGTH'] = content_length

        # Copy HTTP headers to environment
        for key, value in self.headers.items():
            key = key.replace('-', '_').upper()
            if key not in ('CONTENT_TYPE', 'CONTENT_LENGTH'):
                env['HTTP_' + key] = value

        # Read POST data if present
        # For large files, we need to read in chunks to ensure we get all data
        stdin_data = None
        if self.command == 'POST':
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length > 0:
                # Read in chunks to handle large uploads reliably
                chunks = []
                bytes_remaining = content_length
                while bytes_remaining > 0:
                    chunk_size = min(65536, bytes_remaining)  # 64KB chunks
                    chunk = self.rfile.read(chunk_size)
                    if not chunk:
                        break  # EOF reached
                    chunks.append(chunk)
                    bytes_remaining -= len(chunk)
                stdin_data = b''.join(chunks)
                if len(stdin_data) != content_length:
                    print(f"Warning: Expected {content_length} bytes, got {len(stdin_data)}",
                          file=sys.stderr)

        try:
            # Run CGI script using subprocess
            # Use python3 explicitly since we know our CGI scripts are Python
            result = subprocess.run(
                ['python3', scriptfile],
                env=env,
                cwd=os.path.dirname(scriptfile),
                input=stdin_data,
                capture_output=True,
                timeout=600  # 10 minute timeout
            )

            # Parse CGI output (headers + body)
            output = result.stdout
            if not output:
                self.send_error(500, "CGI script produced no output")
                if result.stderr:
                    print(f"CGI stderr: {result.stderr.decode('utf-8', errors='replace')}",
                          file=sys.stderr)
                return

            # Split headers and body
            # CGI scripts output headers, then blank line, then body
            try:
                header_end = output.find(b'\r\n\r\n')
                if header_end == -1:
                    header_end = output.find(b'\n\n')
                    separator_len = 2
                else:
                    separator_len = 4

                if header_end == -1:
                    # No headers found, treat entire output as body
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html')
                    self.end_headers()
                    self.wfile.write(output)
                else:
                    headers = output[:header_end].decode('utf-8', errors='replace')
                    body = output[header_end + separator_len:]

                    # Parse and send headers
                    status_code = 200
                    headers_sent = False
                    for line in headers.split('\n'):
                        line = line.strip()
                        if not line:
                            continue
                        if ':' in line:
                            key, value = line.split(':', 1)
                            key = key.strip()
                            value = value.strip()
                            if key.lower() == 'status':
                                # Parse status code from "Status: 200 OK"
                                status_code = int(value.split()[0])
                            else:
                                if not headers_sent:
                                    self.send_response(status_code)
                                    headers_sent = True
                                self.send_header(key, value)

                    if not headers_sent:
                        self.send_response(status_code)
                    self.end_headers()
                    self.wfile.write(body)

            except Exception as e:
                self.send_error(500, f"Error processing CGI output: {e}")
                print(f"CGI processing error: {e}", file=sys.stderr)

        except subprocess.TimeoutExpired:
            self.send_error(504, "CGI script timed out")
        except Exception as e:
            self.send_error(500, f"CGI execution failed: {e}")
            print(f"CGI execution error: {e}", file=sys.stderr)


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

    if IS_WSL:
        print("WSL detected: using subprocess-based CGI execution")

    server_address = (args.bind, args.port)
    httpd = HTTPServer(server_address, CustomCGIHTTPRequestHandler)
    print(f"Serving HTTP on {args.bind} port {args.port} (http://{args.bind}:{args.port}/) ...")
    httpd.serve_forever()
