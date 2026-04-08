#!/usr/bin/env python3

"""CGI script to trigger fastplot.sh and serve cached results.

This script handles GET requests with a candidate_url parameter,
validates input, checks for cached results, enforces rate limits
and concurrency, and launches fastplot_wrapper.sh for new jobs.
"""

# Handle cgi module removal in Python 3.13+
import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)
try:
    import cgi
except ImportError:
    import sys
    sys.exit("Error: 'cgi' module not found. "
             "For Python 3.13+, install: pip install legacy-cgi")

import os
import sys
import re
import time
import glob
import fcntl
import subprocess


# --- Configuration ---

# Maximum non-cached requests per hour
MAX_REQUESTS_PER_HOUR = 5

# Auto-refresh interval in seconds
REFRESH_INTERVAL = 15


def _parse_config_value(config_path, var_name):
    """Read a variable value from a bash-style config file."""
    try:
        with open(config_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith('#') or '=' not in line:
                    continue
                if line.startswith('export '):
                    line = line[7:]
                key, _, value = line.partition('=')
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                # Strip inline comments
                comment_pos = value.find(' #')
                if comment_pos >= 0:
                    value = value[:comment_pos].strip()
                if key == var_name:
                    return value
    except (FileNotFoundError, PermissionError):
        pass
    return None


def get_config():
    """Load configuration from environment or local_config.sh."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, 'local_config.sh')

    config = {}
    for var_name in ('DATA_PROCESSING_ROOT', 'VAST_REFERENCE_COPY',
                     'URL_OF_DATA_PROCESSING_ROOT'):
        val = os.environ.get(var_name)
        if not val:
            val = _parse_config_value(config_path, var_name)
        config[var_name] = val

    return config


def get_fastplot_dir(config):
    """Return the fastplot output directory path, creating it if needed."""
    data_root = config.get('DATA_PROCESSING_ROOT')
    if data_root:
        fastplot_dir = os.path.join(data_root, 'fastplot')
    else:
        # Fallback: use uploads/fastplot relative to script directory
        script_dir = os.path.dirname(os.path.abspath(__file__))
        fastplot_dir = os.path.join(script_dir, 'uploads', 'fastplot')

    os.makedirs(fastplot_dir, mode=0o755, exist_ok=True)
    return fastplot_dir


# --- Input Validation ---

# Strict patterns for safety
# HTML filename: alphanumeric, underscore, hyphen, dot
SAFE_HTML_BASENAME_RE = re.compile(r'^[a-zA-Z0-9_.-]+\.html$')
# Candidate ID (URL fragment): alphanumeric, underscore, hyphen, dot
SAFE_CANDIDATE_ID_RE = re.compile(r'^[a-zA-Z0-9_.-]+$')


def validate_candidate_url(raw_url, config):
    """Validate and sanitize the candidate URL.

    Returns (safe_url, candidate_id) or raises ValueError.

    SSRF prevention: we discard the hostname/scheme from user input
    and reconstruct the URL using our server-side configuration.
    """
    if not raw_url:
        raise ValueError("Missing candidate_url parameter")

    # Must contain a fragment
    if '#' not in raw_url:
        raise ValueError("URL must contain a '#' fragment pointing to a candidate")

    # Split into base URL and fragment
    base_part, fragment = raw_url.rsplit('#', 1)

    # Validate candidate ID (fragment)
    candidate_id = fragment.strip()
    if not candidate_id:
        raise ValueError("Empty candidate ID in URL fragment")
    if not SAFE_CANDIDATE_ID_RE.match(candidate_id):
        raise ValueError("Invalid candidate ID: must contain only "
                         "alphanumeric characters, underscores, hyphens, and dots")
    if len(candidate_id) > 256:
        raise ValueError("Candidate ID too long")

    # Extract just the basename of the HTML file from the path
    # This prevents path traversal and SSRF
    try:
        # Remove query string if present
        path_part = base_part.split('?')[0]
        # Get just the filename
        basename = os.path.basename(path_part)
    except Exception:
        raise ValueError("Cannot parse URL path")

    if not basename:
        raise ValueError("Cannot extract HTML filename from URL")
    if not SAFE_HTML_BASENAME_RE.match(basename):
        raise ValueError("Invalid HTML filename: must match pattern "
                         "[a-zA-Z0-9_.-]+.html")
    if '..' in basename:
        raise ValueError("Path traversal detected")

    # Reconstruct safe URL using server-side base URL
    url_base = config.get('URL_OF_DATA_PROCESSING_ROOT', '')
    if not url_base:
        raise ValueError("Server configuration error: "
                         "URL_OF_DATA_PROCESSING_ROOT not set")

    # Remove trailing slash from base URL
    url_base = url_base.rstrip('/')

    safe_url = url_base + '/' + basename + '#' + candidate_id

    return safe_url, candidate_id


# --- Cache ---

def check_cache(fastplot_dir, candidate_id):
    """Check if a cached archive exists for this candidate.

    Returns the archive basename if found, None otherwise.
    """
    pattern = os.path.join(fastplot_dir, 'fastplot__*__' + candidate_id + '.tar.bz2')
    matches = glob.glob(pattern)
    if matches:
        return os.path.basename(matches[0])
    return None


# --- Locking (flock-based, crash/power-off safe) ---

def check_lock(fastplot_dir):
    """Test whether a fastplot job is currently running.

    Returns (is_locked, current_candidate_id).
    Uses fcntl.flock which is compatible with the wrapper's flock command.
    """
    lock_file = os.path.join(fastplot_dir, '.fastplot.lock')

    try:
        fd = os.open(lock_file, os.O_RDONLY | os.O_CREAT, 0o644)
    except OSError:
        return False, None

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Lock acquired - no job running. Release immediately.
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        return False, None
    except (IOError, OSError):
        # Lock is held - a job is running
        # Read the candidate ID from the lock file
        current_id = None
        try:
            with open(lock_file, 'r') as f:
                current_id = f.read().strip()
        except (IOError, OSError):
            pass
        os.close(fd)
        return True, current_id


# --- Rate Limiting ---

def check_rate_limit(fastplot_dir):
    """Check if we're within the rate limit.

    Returns True if the request is allowed, False if rate limited.
    """
    rate_file = os.path.join(fastplot_dir, '.fastplot_rate_limit')
    now = time.time()
    cutoff = now - 3600  # 1 hour window

    recent = []
    try:
        with open(rate_file, 'r') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ts = float(line)
                    if ts > cutoff:
                        recent.append(ts)
                except ValueError:
                    continue
    except FileNotFoundError:
        pass

    return len(recent) < MAX_REQUESTS_PER_HOUR


def record_request(fastplot_dir):
    """Record a new request timestamp for rate limiting."""
    rate_file = os.path.join(fastplot_dir, '.fastplot_rate_limit')
    lock_file = rate_file + '.lock'
    now = time.time()
    cutoff = now - 3600

    # Use a separate lock file for atomic rate file updates
    try:
        lf = open(lock_file, 'w')
        fcntl.flock(lf, fcntl.LOCK_EX)

        # Read existing, filter, append
        recent = []
        try:
            with open(rate_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ts = float(line)
                        if ts > cutoff:
                            recent.append(ts)
                    except ValueError:
                        continue
        except FileNotFoundError:
            pass

        recent.append(now)

        with open(rate_file, 'w') as f:
            for ts in recent:
                f.write(str(ts) + '\n')

        fcntl.flock(lf, fcntl.LOCK_UN)
        lf.close()
    except (IOError, OSError):
        pass


# --- HTML Response Helpers ---

def send_html(status_code, title, body):
    """Send an HTML response with the given status code."""
    status_messages = {
        200: 'OK',
        302: 'Found',
        400: 'Bad Request',
        429: 'Too Many Requests',
        500: 'Internal Server Error',
        503: 'Service Unavailable',
    }
    status_text = status_messages.get(status_code, 'Error')
    print("Status: %d %s" % (status_code, status_text))
    print("Content-Type: text/html")
    print("")
    print("<html>")
    print("<head><title>Fastplot - %s</title></head>" % title)
    print("<body>")
    print(body)
    print("</body></html>")


def send_redirect(url):
    """Send a 302 redirect."""
    print("Status: 302 Found")
    print("Location: %s" % url)
    print("Content-Type: text/html")
    print("")
    print("<html><head>")
    print('<meta http-equiv="Refresh" content="0; url=%s">' % url)
    print("</head><body>")
    print('<p>Redirecting to <a href="%s">%s</a>...</p>' % (url, url))
    print("</body></html>")


def send_processing_page(candidate_id, candidate_url):
    """Send an auto-refreshing page while the job is running."""
    # Re-request the same URL to poll
    escaped_url = candidate_url.replace('&', '&amp;').replace('"', '&quot;')
    refresh_url = "?candidate_url=" + escaped_url

    print("Status: 200 OK")
    print("Content-Type: text/html")
    print("")
    print("<html>")
    print("<head>")
    print('<meta http-equiv="refresh" content="%d">' % REFRESH_INTERVAL)
    print("<title>Fastplot - Processing</title>")
    print("</head>")
    print("<body>")
    print("<h2>Fastplot job is running</h2>")
    print("<p>Processing candidate: <b>%s</b></p>" % candidate_id)
    print("<p>This page will auto-refresh every %d seconds.</p>" % REFRESH_INTERVAL)
    print("<p>The job typically takes several minutes. "
          "The output archive can be hundreds of MB.</p>")
    print("<p>You will be redirected to the download when the job completes.</p>")
    print("</body></html>")


def send_error_with_log(candidate_id, fastplot_dir, config):
    """Send an error page with a link to the log file."""
    log_file = 'fastplot_%s.log' % candidate_id
    log_path = os.path.join(fastplot_dir, log_file)

    body = "<h2>Fastplot job failed</h2>"
    body += "<p>The fastplot job for candidate <b>%s</b> " % candidate_id
    body += "appears to have failed.</p>"

    if os.path.exists(log_path):
        body += "<h3>Log output:</h3><pre>"
        try:
            with open(log_path, 'r') as f:
                # Show last 100 lines
                lines = f.readlines()
                for line in lines[-100:]:
                    # Escape HTML
                    line = line.replace('&', '&amp;')
                    line = line.replace('<', '&lt;')
                    line = line.replace('>', '&gt;')
                    body += line
        except (IOError, OSError):
            body += "(Could not read log file)"
        body += "</pre>"
    else:
        body += "<p>No log file found.</p>"

    send_html(500, "Error", body)


# --- Main CGI Handler ---

def main():
    """Main CGI entry point."""
    # Load configuration
    config = get_config()
    fastplot_dir = get_fastplot_dir(config)
    script_dir = os.path.dirname(os.path.abspath(__file__))

    # Parse query string
    query_string = os.environ.get('QUERY_STRING', '')
    params = {}
    if query_string:
        for pair in query_string.split('&'):
            if '=' in pair:
                key, _, value = pair.partition('=')
                # URL-decode
                try:
                    import urllib.parse
                    params[key] = urllib.parse.unquote(value)
                except Exception:
                    params[key] = value

    raw_url = params.get('candidate_url', '')

    # Step 1: Validate input
    try:
        safe_url, candidate_id = validate_candidate_url(raw_url, config)
    except ValueError as e:
        send_html(400, "Bad Request",
                  "<h2>Bad Request</h2><p>%s</p>" % str(e))
        return

    # Step 2: Check cache
    cached = check_cache(fastplot_dir, candidate_id)
    if cached:
        url_base = config.get('URL_OF_DATA_PROCESSING_ROOT', '').rstrip('/')
        # fastplot dir is under DATA_PROCESSING_ROOT, which maps to URL_OF_DATA_PROCESSING_ROOT
        # URL: URL_OF_DATA_PROCESSING_ROOT/../fastplot/<archive>
        # Since URL_OF_DATA_PROCESSING_ROOT is typically .../uploads,
        # and fastplot_dir is DATA_PROCESSING_ROOT/fastplot,
        # the URL depends on the server layout.
        # Use a path relative to URL_OF_DATA_PROCESSING_ROOT's parent
        # URL_OF_DATA_PROCESSING_ROOT = http://host/unmw/uploads
        # fastplot archives at: http://host/unmw/uploads/../fastplot/archive
        # = http://host/unmw/fastplot/archive
        # Actually, DATA_PROCESSING_ROOT/fastplot is served alongside uploads
        # Let's construct it properly
        data_root = config.get('DATA_PROCESSING_ROOT', '')
        if data_root and url_base:
            # URL_OF_DATA_PROCESSING_ROOT points to DATA_PROCESSING_ROOT
            # fastplot_dir is DATA_PROCESSING_ROOT/fastplot
            # So the URL is URL_OF_DATA_PROCESSING_ROOT/fastplot/
            download_url = url_base + '/fastplot/' + cached
        else:
            download_url = '/uploads/fastplot/' + cached
        send_redirect(download_url)
        return

    # Step 3: Check lock (is a job running?)
    is_locked, current_job_id = check_lock(fastplot_dir)
    if is_locked:
        if current_job_id == candidate_id:
            # Our job is running - show polling page
            send_processing_page(candidate_id, raw_url)
        else:
            # Different job is running
            send_html(503, "Server Busy",
                      "<h2>Server Busy</h2>"
                      "<p>Another fastplot job is currently running "
                      "(candidate: %s).</p>"
                      "<p>Please try again in a few minutes.</p>"
                      % (current_job_id or "unknown"))
        return

    # No job running and no cache - check if a previous job failed
    # (lock is free but no cached result)
    log_file = os.path.join(fastplot_dir, 'fastplot_%s.log' % candidate_id)
    if os.path.exists(log_file):
        # A previous job ran but produced no archive - check if it was recent
        log_mtime = os.path.getmtime(log_file)
        if time.time() - log_mtime < 300:  # Within 5 minutes
            send_error_with_log(candidate_id, fastplot_dir, config)
            return

    # Step 4: Check rate limit
    if not check_rate_limit(fastplot_dir):
        send_html(429, "Rate Limited",
                  "<h2>Rate Limited</h2>"
                  "<p>Maximum %d fastplot requests per hour exceeded.</p>"
                  "<p>Please try again later. "
                  "Cached results are served without rate limits.</p>"
                  % MAX_REQUESTS_PER_HOUR)
        return

    # Step 5: Record request and launch wrapper
    record_request(fastplot_dir)

    wrapper_path = os.path.join(script_dir, 'fastplot_wrapper.sh')
    if not os.path.isfile(wrapper_path) or not os.access(wrapper_path, os.X_OK):
        send_html(500, "Server Error",
                  "<h2>Server Error</h2>"
                  "<p>fastplot_wrapper.sh not found or not executable.</p>")
        return

    # Launch wrapper in background
    log_path = os.path.join(fastplot_dir, 'fastplot_%s.log' % candidate_id)
    try:
        log_fd = open(log_path, 'w')
        subprocess.Popen(
            [wrapper_path, safe_url, candidate_id],
            stdout=log_fd,
            stderr=subprocess.STDOUT,
            cwd=script_dir,
            # Detach from CGI process
            start_new_session=True,
        )
        log_fd.close()
    except Exception as e:
        send_html(500, "Server Error",
                  "<h2>Server Error</h2>"
                  "<p>Failed to launch fastplot wrapper: %s</p>" % str(e))
        return

    # Step 6: Return processing page
    send_processing_page(candidate_id, raw_url)


if __name__ == '__main__':
    main()
