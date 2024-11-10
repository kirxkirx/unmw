#!/usr/bin/env python3
import cgi, os
import cgitb; cgitb.enable()
import random
import string
import time
import sys
import socket
import pwd

# Output the content type header first
print("Content-Type: text/html\n")

# Start logging output for display within the HTML page
message = 'Starting program ' + sys.argv[0] + ' <br>'

# Check the system load
emergency_load = 50.0
if os.access('/proc/loadavg', os.R_OK):
    with open('/proc/loadavg', 'r') as procload:
        loadline = procload.readline()
    load = float(loadline.split()[1])
    if load > emergency_load:
        message += 'System load is extremely high'
        print(f"<html><body><p>{message}</p></body></html>")
        sys.exit(1)  # Exit with HTML message

form = cgi.FieldStorage()

# Get input parameters
fileupload = 'True'

if fileupload == "True":
    message += 'Uploading new file <br>'
    
    # Generator to buffer file chunks
    def fbuffer(f, chunk_size=10000000):
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            yield chunk
       
    # A nested FieldStorage instance holds the file
    fileitem = form['file']

pid = os.getpid()
message += 'Process ID:  ' + str(pid) + ' <br>'

if fileupload == "True":
    JobID = 'web_upload_' + str(pid)
    JobID += ''.join(random.choice(string.ascii_letters) for _ in range(8))

dirname = 'uploads/' + JobID

if fileupload == "True":
    try:
        # Attempt to create the directory
        os.mkdir(dirname)
    except PermissionError as e:
        # Get the current working directory
        current_working_directory = os.getcwd()
        
        # Get the user information
        user_id = os.getuid()
        user_info = pwd.getpwuid(user_id)

        # Produce a meaningful error message
        message = (
            f"Error: Could not create directory '{dirname}'.<br>"
            f"Current working directory: {current_working_directory}<br>"
            f"Script is running as user: {user_info.pw_name} (UID: {user_id})<br>"
            f"Error details: {e}<br>"
        )

        # Print the error message in the HTML response and exit
        print("Content-Type: text/html\n")
        print(f"<html><body><p>{message}</p></body></html>")
        sys.exit(1)  # Exit the script with an error

dirname = dirname + '/'

# Enable cgitb with logging to the specified directory
cgitb.enable(display=1, logdir=dirname)

if fileupload == "True":
    # Strip leading path from file name to avoid directory traversal attacks
    fn = os.path.basename(fileitem.filename)
    # Truncate filename at 256 characters just in case
    fn = fn[:256]
    # Sanitize the filename
    fn = fn.replace(' ', '_').replace('..', '_').replace('%', '_')

    # Open file for writing
    with open(dirname + fn, 'wb', 100000000) as f:
        # Read the file in chunks
        for chunk in fbuffer(fileitem.file):
            f.write(chunk)
    message += f'The file "{fn}" was uploaded successfully! <br>'

fullhostname = socket.getfqdn()
message += f'<br><br>The output will be written to <a href="http://{fullhostname}/nmw/proc/upload/{dirname}">http://{fullhostname}/nmw/proc/upload/{dirname}</a><br><br>'

if form.getvalue('workstartemail'):
    syscmd = f'touch {dirname}workstartemail'
    CmdReturnStatus = os.system(syscmd)
    message += f' {syscmd} '

if form.getvalue('workendemail'):
    syscmd = f'touch {dirname}workendemail'
    CmdReturnStatus = os.system(syscmd)
    message += f' {syscmd} '

if form.getvalue('nonexistingfield'):
    syscmd = f'touch {dirname}nonexistingfield'
    CmdReturnStatus = os.system(syscmd)
    message += f' {syscmd} '

syscmd = f'ls -lh {dirname}{fn} > {dirname}upload.log'
CmdReturnStatus = os.system(syscmd)

results_page_url = f'http://{fullhostname}/unmw/{dirname}'

# Run the wrapper script
syscmd = f'./wrapper.sh {dirname}{fn}'
CmdReturnStatus = os.system(syscmd)

time.sleep(10)

# Wait until results file is ready
if not os.path.isfile(dirname + "results_url.txt"):
    time.sleep(30)
if not os.path.isfile(dirname + "results_url.txt"):
    time.sleep(30)
if not os.path.isfile(dirname + "results_url.txt"):
    time.sleep(30)
if not os.path.isfile(dirname + "results_url.txt"):
    time.sleep(60)

# Give it a few more seconds to draft the actual HTML page at this URL
time.sleep(10)

# Open the file with the redirection link if it exists
if os.path.isfile(dirname + "results_url.txt"):
    with open(dirname + "results_url.txt", "r") as file1:
        results_page_url = file1.readline()

# Redirect to results page
print(f"""\
<html>
<head>
<meta http-equiv="Refresh" content="0; url={results_page_url}"> 
</head>
<body>
<p>{message}</p>   
</body></html>
""")

sys.exit(0)
